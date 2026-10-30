"""Background lifecycle tests. Real subprocess tests use isolated, synthetic libraries."""
import io
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import app
import launcher


# No archive requests or real collections are used by this minimal HTTP fixture.
SERVER_FIXTURE = r'''
import argparse, json, os, threading, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from launcher import background_ready, background_stopped, monitor_stop
root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument('--port', type=int)
parser.add_argument('--background-token')
args = parser.parse_args()
lock = threading.RLock()
stop = threading.Event()
stopping = threading.Event()
job = {'running': False}
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        data = json.dumps({'app': 'x-wayback-library', 'pid': os.getpid(),
                           'data_dir': str(root / 'collections'), 'job': job}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
with (root / 'starts.txt').open('a') as f: f.write(str(os.getpid()) + '\n')
background_ready(root, args.port, args.background_token)
threading.Thread(target=monitor_stop, args=(root, args.port, args.background_token,
                 server, stop, job, lock, lambda **kw: None, stopping), daemon=True).start()
try:
    server.serve_forever(poll_interval=0.05)
finally:
    server.server_close()
    background_stopped(root, args.port, args.background_token)
'''


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class BackgroundUnitTests(unittest.TestCase):
    def test_reuses_existing_server_without_spawning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'app.py').write_text('# existing server')
            existing = {'app': 'x-wayback-library', 'data_dir': str(root / 'collections')}
            with patch.object(launcher, 'ping', return_value=existing), \
                 patch.object(launcher, 'spawn_server') as spawn, patch('sys.stdout', new=io.StringIO()):
                self.assertIs(launcher.start_background(root), existing)
                spawn.assert_not_called()

    def test_crashed_child_reports_log_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'app.py').write_text('# test')
            child = Mock(pid=54321)
            child.poll.return_value = 7
            with patch.object(launcher, 'ping', return_value=None), \
                 patch.object(launcher, 'spawn_server', return_value=child):
                with self.assertRaisesRegex(RuntimeError, r'code 7.*server-8765\.log'):
                    launcher.start_background(root, timeout=1)
                child.terminate.assert_not_called()

    def test_timeout_does_not_kill_initializing_server(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'app.py').write_text('# test')
            child = Mock(pid=54321)
            child.poll.return_value = None
            with patch.object(launcher, 'ping', return_value=None), \
                 patch.object(launcher, 'spawn_server', return_value=child):
                with self.assertRaisesRegex(RuntimeError, 'longer than expected'):
                    launcher.start_background(root, timeout=0)
                child.terminate.assert_not_called()
                child.kill.assert_not_called()

    def test_invalid_or_other_library_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher.write_state(root, 8765, {'root': folder, 'port': 8765, 'session': '../escape'})
            self.assertIsNone(launcher.read_state(root, 8765))
            launcher.write_state(root, 8765, {'root': folder + '/other', 'port': 8765, 'session': 'a' * 32})
            self.assertIsNone(launcher.read_state(root, 8765))

    def test_stop_refuses_port_now_owned_by_another_library(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher.background_ready(root, 8765, 'a' * 32)
            with patch.object(launcher, 'ping', return_value={'app': 'x-wayback-library', 'data_dir': str(root / 'other')}):
                with self.assertRaisesRegex(RuntimeError, 'another library'):
                    launcher.stop_background(root)
            self.assertFalse(list(launcher.runtime(root).glob('stop-*')))

    def test_monitor_waits_for_job_before_shutdown(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session = 'b' * 32
            (launcher.runtime(root) / f'stop-8765-{session}').touch()
            server = Mock()
            stop, stopping = threading.Event(), threading.Event()
            lock = threading.RLock()
            job = {'running': True}
            progress = Mock()
            monitor = threading.Thread(target=launcher.monitor_stop, args=(root, 8765, session,
                server, stop, job, lock, progress, stopping), daemon=True)
            monitor.start()
            self.assertTrue(stop.wait(2))
            self.assertTrue(stopping.is_set())
            server.shutdown.assert_not_called()
            progress.assert_called_once()
            with lock:
                job['running'] = False
            monitor.join(2)
            self.assertFalse(monitor.is_alive())
            server.shutdown.assert_called_once()

    def test_shutdown_blocks_new_jobs_without_clearing_stop(self):
        with patch.object(app.TASKS, 'stopping', threading.Event()) as stopping, \
             patch.object(app.TASKS, 'stop', threading.Event()) as stop, \
             patch.object(app.threading, 'Thread') as worker:
            stopping.set()
            stop.set()
            with self.assertRaisesRegex(ValueError, '正在保存'):
                app.TASKS.start('sample', {})
            worker.assert_not_called()
            self.assertTrue(stop.is_set())

    def test_spawn_detaches_and_redirects_console_streams(self):
        with tempfile.TemporaryDirectory(prefix='后台 空格 ') as folder, \
             patch.object(launcher.subprocess, 'Popen') as spawn:
            launcher.spawn_server(Path(folder), 8765, 'a' * 32)
            args, kwargs = spawn.call_args
            self.assertEqual(args[0][-4:], ['--port', '8765', '--background-token', 'a' * 32])
            self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
            self.assertTrue(kwargs['close_fds'])
            self.assertIs(kwargs['stdout'], kwargs['stderr'])
            self.assertEqual(kwargs['env']['PYTHONUTF8'], '1')
            if os.name == 'nt':
                self.assertTrue(kwargs['creationflags'] & subprocess.DETACHED_PROCESS)
                self.assertTrue(kwargs['creationflags'] & subprocess.CREATE_NEW_PROCESS_GROUP)
                self.assertEqual(kwargs['startupinfo'].wShowWindow, subprocess.SW_HIDE)
            else:
                self.assertTrue(kwargs['start_new_session'])

    def test_browser_error_does_not_report_background_start_failure(self):
        with patch.object(launcher, 'start_background'), \
             patch.object(launcher.webbrowser, 'open', side_effect=OSError('no browser')), \
             patch('sys.stdout', new=io.StringIO()) as output:
            self.assertEqual(launcher.main([]), 0)
            self.assertIn('http://127.0.0.1:8765', output.getvalue())


class BackgroundProcessTests(unittest.TestCase):
    def test_server_survives_launcher_exit_and_double_launch(self):
        with tempfile.TemporaryDirectory(prefix='后台 启动 ') as folder:
            root = Path(folder)
            shutil.copyfile(launcher.ROOT / 'launcher.py', root / 'launcher.py')
            shutil.copytree(launcher.ROOT / 'shiguangji', root / 'shiguangji',
                            ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'app.py').write_text(SERVER_FIXTURE, encoding='utf-8')
            port = free_port()
            command = [sys.executable, '-X', 'utf8', str(root / 'launcher.py'), '--no-open', '--port', str(port)]
            try:
                # Both caller processes exit; only their detached child should remain.
                first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                first_output = first.communicate(timeout=45)[0]
                second_output = second.communicate(timeout=45)[0]
                self.assertEqual(first.returncode, 0, first_output)
                self.assertEqual(second.returncode, 0, second_output)
                health = launcher.ping(port)
                self.assertIsNotNone(health)
                state = launcher.read_state(root, port)
                self.assertEqual(health['pid'], state['pid'])
                self.assertNotIn(state['pid'], {first.pid, second.pid})
                self.assertEqual(len((root / 'starts.txt').read_text().splitlines()), 1)
                # Reopening does not create an additional background service.
                with patch('sys.stdout', new=io.StringIO()):
                    launcher.start_background(root, port)
                self.assertEqual(launcher.ping(port)['pid'], state['pid'])
                self.assertTrue((root / 'runtime' / f'server-{port}.log').is_file())
            finally:
                with patch('sys.stdout', new=io.StringIO()):
                    launcher.stop_background(root, port, timeout=10)
                # Let the process release the log handle after its stopped handshake.
                deadline = time.monotonic() + 5
                while launcher.ping(port) and time.monotonic() < deadline:
                    time.sleep(0.1)
                time.sleep(0.2)
            self.assertIsNone(launcher.ping(port))
            self.assertEqual(launcher.read_state(root, port)['state'], 'stopped')


if __name__ == '__main__':
    unittest.main()
