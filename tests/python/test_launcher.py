"""Regression coverage for Windows launch failures; no network or browser launch."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import app
import launcher


class LauncherTests(unittest.TestCase):
    def test_windows_line_endings(self):
        for filename in ('启动工具.cmd', '停止后台.cmd'):
            content = (app.ROOT / filename).read_bytes()
            self.assertTrue(content.isascii())
            self.assertNotIn(b'\n', content.replace(b'\r\n', b''))
            self.assertIn(b'pause >nul', content)

    @unittest.skipUnless(os.name == 'nt', 'Windows command processor required')
    def test_cmd_handles_success_and_failure_in_unicode_path(self):
        launcher = (app.ROOT / '启动工具.cmd').read_bytes()
        for code in (0, 7):
            with self.subTest(exit_code=code), tempfile.TemporaryDirectory(prefix='时光集 启动 ') as folder:
                root = Path(folder)
                (root / '启动工具.cmd').write_bytes(launcher)
                (root / 'app.py').write_text('# The launcher starts this in a separate process.', encoding='utf-8')
                (root / 'launcher.py').write_text(f'import sys; print("LAUNCHER_REACHED_PYTHON"); sys.exit({code})', encoding='utf-8')
                result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', '启动工具.cmd'],
                                        cwd=root, input=b'', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertIn(b'LAUNCHER_REACHED_PYTHON', result.stdout)
                self.assertEqual(b'Press any key to close' in result.stdout, code != 0)
                self.assertEqual(b'Startup failed.' in result.stdout, code != 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows command processor required')
    def test_stop_cmd_forwards_stop_option_and_custom_port(self):
        with tempfile.TemporaryDirectory(prefix='后台 停止 ') as folder:
            root = Path(folder)
            (root / '停止后台.cmd').write_bytes((app.ROOT / '停止后台.cmd').read_bytes())
            (root / 'app.py').write_text('# Not run directly', encoding='utf-8')
            (root / 'launcher.py').write_text('import sys; print(repr(sys.argv[1:])); sys.exit(0)', encoding='utf-8')
            result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', '停止后台.cmd', '--port', '9876'],
                                    cwd=root, input=b'', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn(b"['--stop', '--port', '9876']", result.stdout)
            self.assertNotIn(b'Press any key to close', result.stdout)

    @unittest.skipUnless(os.name == 'nt', 'Windows command processor required')
    def test_cmd_reports_incomplete_extraction(self):
        with tempfile.TemporaryDirectory(prefix='launcher incomplete ') as folder:
            root = Path(folder)
            (root / '启动工具.cmd').write_bytes((app.ROOT / '启动工具.cmd').read_bytes())
            (root / 'app.py').write_text('# Missing launcher.py', encoding='utf-8')
            result = subprocess.run([os.environ.get('COMSPEC', 'cmd.exe'), '/d', '/c', '启动工具.cmd'],
                                    cwd=root, input=b'', stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
            self.assertEqual(result.returncode, 1)
            self.assertIn(b'Extract the entire ZIP', result.stdout)
            self.assertIn(b'Press any key to close', result.stdout)

    def test_existing_instance_is_reused(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app.TASKS, 'base', Path(folder)), \
             patch.object(sys, 'argv', ['app.py', '--open']), \
             patch.object(app, 'LocalServer', side_effect=OSError('occupied')), \
             patch.object(app, 'running_instance', return_value={'app': 'x-wayback-library'}), \
             patch.object(app, 'open_browser') as browser, patch('sys.stdout', new=io.StringIO()) as output:
            self.assertEqual(app.main(), 0)
            browser.assert_called_once_with('http://127.0.0.1:8765')
            self.assertIn('already running', output.getvalue())

    def test_unrelated_port_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(app.TASKS, 'base', Path(folder)), \
             patch.object(sys, 'argv', ['app.py', '--open']), \
             patch.object(app, 'LocalServer', side_effect=OSError('occupied')), \
             patch.object(app, 'running_instance', return_value=None), \
             patch.object(app, 'open_browser') as browser, patch('sys.stdout', new=io.StringIO()) as output:
            self.assertEqual(app.main(), 1)
            browser.assert_not_called()
            self.assertIn('Could not start', output.getvalue())

    def test_browser_failure_leaves_manual_url(self):
        with patch.object(app.webbrowser, 'open', side_effect=OSError('no browser')), patch('sys.stdout', new=io.StringIO()) as output:
            self.assertFalse(app.open_browser('http://127.0.0.1:8765'))
            self.assertIn('http://127.0.0.1:8765', output.getvalue())

    def test_default_bind_failure_falls_back_and_opens_actual_port(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            health = {'app': 'x-wayback-library', 'data_dir': str(root / 'collections')}
            with patch.object(launcher, 'ROOT', root), \
                 patch.object(launcher, 'start_background', side_effect=[launcher.BackgroundExitError('child exited'), health]) as start, \
                 patch.object(launcher, 'bind_error', return_value=PermissionError(10013, 'denied')) as bind, \
                 patch.object(launcher.webbrowser, 'open', return_value=True) as browser, patch('sys.stdout', new=io.StringIO()):
                self.assertEqual(launcher.main([]), 0)
                self.assertEqual([call.args for call in start.call_args_list], [(root, 8765), (root, 8766)])
                bind.assert_called_once_with(8765)
                browser.assert_called_once_with('http://127.0.0.1:8766')
            self.assertEqual(launcher.active_port(root), 8766)

    def test_repeated_default_launch_reuses_recorded_port_without_another_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            launcher.remember_port(root, 8766)
            with patch.object(launcher, 'ROOT', root), \
                 patch.object(launcher, 'start_background', return_value={'app': 'x-wayback-library', 'data_dir': str(root / 'collections')}) as start, \
                 patch.object(launcher, 'bind_error') as bind, patch.object(launcher.webbrowser, 'open', return_value=True) as browser:
                self.assertEqual(launcher.main([]), 0)
                start.assert_called_once_with(root, 8766)
                bind.assert_not_called()
                browser.assert_called_once_with('http://127.0.0.1:8766')

    def test_explicit_port_does_not_fallback_on_failure(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'ROOT', Path(folder)), \
             patch.object(launcher, 'start_background', side_effect=launcher.BackgroundExitError('blocked')) as start, \
             patch.object(launcher, 'bind_error') as bind, patch.object(launcher.webbrowser, 'open') as browser, \
             patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(launcher.main(['--port', '8765']), 1)
            start.assert_called_once_with(Path(folder), 8765)
            bind.assert_not_called()
            browser.assert_not_called()

    def test_slow_start_does_not_start_second_background_server(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'ROOT', Path(folder)), \
             patch.object(launcher, 'start_background', side_effect=RuntimeError('still initializing')) as start, \
             patch.object(launcher, 'bind_error') as bind, patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(launcher.main(['--no-open']), 1)
            start.assert_called_once()
            bind.assert_not_called()

    def test_application_error_with_free_port_is_not_misreported_as_a_port_conflict(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'ROOT', Path(folder)), \
             patch.object(launcher, 'start_background', side_effect=launcher.BackgroundExitError('missing module')) as start, \
             patch.object(launcher, 'bind_error', return_value=None), patch('sys.stdout', new=io.StringIO()):
            self.assertEqual(launcher.main(['--no-open']), 1)
            start.assert_called_once_with(Path(folder), 8765)

    def test_default_stop_uses_actual_port_and_explicit_stop_keeps_requested_port(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher.remember_port(root, 8766)
            with patch.object(launcher, 'ROOT', root), patch.object(launcher, 'stop_background', return_value=True) as stop, \
                 patch.object(launcher, 'start_background') as start:
                self.assertEqual(launcher.main(['--stop']), 0)
                self.assertEqual(launcher.main(['--stop', '--port', '9876']), 0)
                self.assertEqual([call.args for call in stop.call_args_list], [(root, 8766), (root, 9876)])
                start.assert_not_called()

    def test_invalid_or_other_installation_port_record_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = launcher.runtime(root) / 'active-port.json'
            for record in ({'root': str(root), 'port': True}, {'root': str(root), 'port': 65536},
                           {'root': str(root / 'other'), 'port': 8766}, ['not an object']):
                path.write_text(json.dumps(record), encoding='utf-8')
                self.assertEqual(launcher.active_port(root), 8765)

    def test_default_launch_does_not_silently_use_another_library_folder(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'ROOT', Path(folder)), \
             patch.object(launcher, 'start_background', return_value={'app': 'x-wayback-library', 'data_dir': str(Path(folder) / 'different' / 'collections')}), \
             patch.object(launcher.webbrowser, 'open') as browser, patch('sys.stdout', new=io.StringIO()) as output:
            self.assertEqual(launcher.main([]), 1)
            self.assertIn('another library folder', output.getvalue())
            browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()
