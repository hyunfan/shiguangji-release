"""Background start/stop decisions on a temporary installation; children and browsers are faked."""
import io
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import launcher

PORT = 9876


class FakeChild:
    def __init__(self, pid=4242, code=None):
        self.pid, self.code = pid, code

    def poll(self):
        return self.code


class LauncherBackgroundTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / 'app.py').write_text('# fixture', encoding='utf-8')
        output = patch('sys.stdout', new=io.StringIO())
        self.output = output.start()
        self.addCleanup(output.stop)

    def ready(self, session='a' * 32, pid=4242, state='ready'):
        launcher.write_state(self.root, PORT, dict(root=str(self.root), port=PORT, session=session, pid=pid, state=state))

    def health(self, data_dir=None):
        return {'app': 'x-wayback-library', 'data_dir': str(data_dir or self.root / 'collections')}

    def test_bind_error_reports_only_an_occupied_port(self):
        with socket.socket() as busy:
            busy.bind(('127.0.0.1', 0))
            busy.listen()
            port = busy.getsockname()[1]
            self.assertIsInstance(launcher.bind_error(port), OSError)
        self.assertIsNone(launcher.bind_error(port))

    @unittest.skipIf(os.name == 'nt', 'flock semantics')
    def test_second_launcher_waits_then_gives_up_while_lock_is_held(self):
        with launcher.launch_lock(self.root, PORT):
            with self.assertRaisesRegex(RuntimeError, 'Another launcher'):
                with launcher.launch_lock(self.root, PORT, timeout=0.3):
                    pass
        with launcher.launch_lock(self.root, PORT, timeout=0.3):
            pass

    def test_state_file_from_another_installation_or_bad_token_is_ignored(self):
        self.ready()
        self.assertEqual(launcher.read_state(self.root, PORT)['pid'], 4242)
        self.ready(session='not-a-token')
        self.assertIsNone(launcher.read_state(self.root, PORT))
        launcher.write_state(self.root, PORT, dict(root=str(self.root / 'other'), port=PORT, session='a' * 32))
        self.assertIsNone(launcher.read_state(self.root, PORT))
        with self.assertRaises(ValueError):
            launcher.background_ready(self.root, PORT, 'bad')
        launcher.background_ready(self.root, PORT, 'c' * 32)
        state = launcher.read_state(self.root, PORT)
        self.assertEqual((state['state'], state['pid']), ('ready', os.getpid()))

    def test_incomplete_extraction_is_reported_before_spawning(self):
        (self.root / 'app.py').unlink()
        with patch.object(launcher, 'spawn_server') as spawn:
            with self.assertRaisesRegex(RuntimeError, 'Extract the entire ZIP'):
                launcher.start_background(self.root, PORT)
        spawn.assert_not_called()

    def test_existing_server_is_reused_and_guarded(self):
        with patch.object(launcher, 'ping', return_value=self.health()), \
             patch.object(launcher, 'spawn_server') as spawn, patch.object(launcher, 'ensure_guard') as guard:
            self.assertEqual(launcher.start_background(self.root, PORT), self.health())
        spawn.assert_not_called()
        guard.assert_called_once_with(self.root, PORT)
        self.assertIn('already running', self.output.getvalue())

    def test_new_server_is_returned_once_its_session_is_ready(self):
        def spawn(root, port, session, recovery_run=''):
            self.spawned = (session, recovery_run)
            self.ready(session=session)
            return FakeChild()

        with patch.object(launcher, 'ping', side_effect=[None, self.health()]), \
             patch.object(launcher, 'spawn_server', side_effect=spawn), patch.object(launcher, 'ensure_guard') as guard:
            result = launcher.start_background(self.root, PORT, timeout=5, recovery_run='run-3')
        self.assertEqual(result, self.health())
        self.assertEqual(self.spawned[1], 'run-3')
        guard.assert_called_once_with(self.root, PORT)
        self.assertIn('Running in background', self.output.getvalue())
        with patch.object(launcher, 'ping', side_effect=[None, self.health()]), \
             patch.object(launcher, 'spawn_server', side_effect=spawn), patch.object(launcher, 'ensure_guard') as guard:
            launcher.start_background(self.root, PORT, timeout=5, supervised=False)
        guard.assert_not_called()

    def test_child_exit_reuses_a_winning_copy_or_reports_the_exit(self):
        with patch.object(launcher, 'ping', side_effect=[None, self.health()]), \
             patch.object(launcher, 'spawn_server', return_value=FakeChild(code=0)):
            self.assertEqual(launcher.start_background(self.root, PORT, timeout=5), self.health())
        for code in (0, 3):
            with self.subTest(code=code), patch.object(launcher, 'ping', return_value=None), \
                 patch.object(launcher, 'spawn_server', return_value=FakeChild(code=code)):
                with self.assertRaisesRegex(launcher.BackgroundExitError, f'code {code}'):
                    launcher.start_background(self.root, PORT, timeout=5)

    def test_slow_child_is_left_running_after_timeout(self):
        child = FakeChild()
        child.kill = Mock()
        with patch.object(launcher, 'ping', return_value=None), patch.object(launcher, 'spawn_server', return_value=child):
            with self.assertRaisesRegex(RuntimeError, 'taking longer than expected'):
                launcher.start_background(self.root, PORT, timeout=0.2)
        child.kill.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX detach options')
    def test_guard_is_started_once_for_a_ready_session(self):
        (self.root / 'supervisor.py').write_text('# fixture', encoding='utf-8')
        with patch.object(launcher.subprocess, 'Popen') as popen:
            launcher.ensure_guard(self.root, PORT)
            popen.assert_not_called()
            self.ready(session='d' * 32, pid=77)
            with patch('supervisor.guard_status', return_value={'state': 'watching'}):
                launcher.ensure_guard(self.root, PORT)
            popen.assert_not_called()
            launcher.ensure_guard(self.root, PORT)
        command = popen.call_args.args[0]
        self.assertEqual(command[-6:], ['--port', str(PORT), '--session', 'd' * 32, '--pid', '77'])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])

    @unittest.skipIf(os.name == 'nt', 'POSIX detach options')
    def test_spawned_server_is_detached_and_logs_to_runtime(self):
        with patch.object(launcher.subprocess, 'Popen') as popen:
            launcher.spawn_server(self.root, PORT, 'e' * 32, 'run-9')
        command = popen.call_args.args[0]
        self.assertEqual(command[-6:], ['--port', str(PORT), '--background-token', 'e' * 32, '--recover-queue-run', 'run-9'])
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.kwargs['env']['PYTHONUTF8'], '1')
        self.assertIn('--- Launch', (launcher.runtime(self.root) / f'server-{PORT}.log').read_text(encoding='utf-8'))

    def test_stop_without_a_session_or_response_sends_nothing(self):
        self.assertTrue(launcher.stop_background(self.root, PORT))
        self.assertIn('No background session', self.output.getvalue())
        self.ready()
        with patch.object(launcher, 'ping', return_value=None):
            self.assertFalse(launcher.stop_background(self.root, PORT))
        with patch.object(launcher, 'ping', return_value=self.health(self.root / 'other')):
            with self.assertRaisesRegex(RuntimeError, 'another library folder'):
                launcher.stop_background(self.root, PORT)
        self.assertEqual(list(launcher.runtime(self.root).glob('stop-*')), [])

    def test_stop_request_waits_for_the_session_to_stop(self):
        self.ready()
        marker = launcher.runtime(self.root) / f'stop-{PORT}-{"a" * 32}'
        with patch.object(launcher, 'ping', return_value=self.health()):
            self.assertTrue(launcher.stop_background(self.root, PORT, timeout=0))
        self.assertTrue(marker.exists())
        self.assertIn('remains pending', self.output.getvalue())
        def server_saves_and_stops(_):
            launcher.background_stopped(self.root, PORT, 'a' * 32)

        with patch.object(launcher, 'ping', return_value=self.health()), \
             patch.object(launcher.time, 'sleep', side_effect=server_saves_and_stops) as sleep:
            self.assertTrue(launcher.stop_background(self.root, PORT, timeout=5))
        sleep.assert_called_once_with(0.2)
        self.assertIn('Background server stopped', self.output.getvalue())
        self.assertFalse(marker.exists())

    def test_stop_marker_pauses_running_job_before_server_shutdown(self):
        session = 'f' * 32
        job, lock = {'running': True}, threading.Lock()
        stop_event, stopping, server, progress = threading.Event(), threading.Event(), Mock(), Mock()
        worker = threading.Thread(target=launcher.monitor_stop,
                                  args=(self.root, PORT, session, server, stop_event, job, lock, progress, stopping))
        worker.start()
        self.assertFalse(stop_event.wait(0.1))
        (launcher.runtime(self.root) / f'stop-{PORT}-{session}').touch()
        self.assertTrue(stop_event.wait(5))
        self.assertTrue(stopping.is_set())
        progress.assert_called_once()
        self.assertTrue(progress.call_args.kwargs['pause_requested'])
        server.shutdown.assert_not_called()
        with lock:
            job['running'] = False
        worker.join(5)
        self.assertFalse(worker.is_alive())
        server.shutdown.assert_called_once()

    def test_main_rejects_invalid_port(self):
        with patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit):
            launcher.main(['--port', '0'])

    def test_explicit_port_is_remembered_only_for_this_library(self):
        with patch.object(launcher, 'ROOT', self.root), \
             patch.object(launcher, 'start_background', return_value=self.health(self.root / 'other')):
            self.assertEqual(launcher.main(['--port', '9001', '--no-open']), 0)
        self.assertEqual(launcher.active_port(self.root), 8765)
        with patch.object(launcher, 'ROOT', self.root), patch.object(launcher, 'start_background', return_value=self.health()):
            self.assertEqual(launcher.main(['--port', '9001', '--no-open']), 0)
        self.assertEqual(launcher.active_port(self.root), 9001)

    def test_failure_is_reported_even_when_log_cannot_be_written(self):
        (launcher.runtime(self.root) / 'launcher.log').mkdir()
        with patch.object(launcher, 'ROOT', self.root), \
             patch.object(launcher, 'start_background', side_effect=RuntimeError('boom')):
            self.assertEqual(launcher.main(['--port', '9001', '--no-open']), 1)
        self.assertIn('Startup/control failed: boom', self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
