"""Launcher boundaries and shortcut round-trip; never register real user settings."""
from contextlib import nullcontext
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import launcher
import windows_integration as entry


class DesktopEntryTests(unittest.TestCase):
    def test_protocol_accepts_only_fixed_open_action(self):
        for uri in (entry.OPEN_URI, entry.OPEN_URI + '/'):
            self.assertEqual(entry._launch_action(['--protocol', uri]), 'open')
        for args in (['--protocol'], ['--protocol', entry.OPEN_URI, '--stop'],
                     ['--protocol', entry.OPEN_URI + '?cmd=calc'],
                     ['--protocol', entry.OPEN_URI + '#stop'], ['--protocol', 'file:///tmp'],
                     ['--protocol', 'x-wayback-library://stop'],
                     ['--protocol', 'x-wayback-library://open/../../app.py'],
                     ['--protocol', entry.OPEN_URI + '" --stop'], ['--autostart', 'enable']):
            with self.subTest(args=args), self.assertRaises(ValueError):
                entry._launch_action(args)

    def test_success_uses_saved_port_and_checks_same_installation_before_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with patch.object(launcher, 'start_automatic', return_value=8766) as start, \
                 patch.object(launcher, 'ping', return_value={'data_dir': str(root/'collections')}), \
                 patch.object(entry.webbrowser, 'open', return_value=True) as browser:
                self.assertEqual(entry.desktop_main([], root), 0)
                start.assert_called_once_with(root)
                browser.assert_called_once_with('http://127.0.0.1:8766/')

    def test_protocol_failure_does_not_launch_or_execute_anything(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'start_automatic') as start, \
             patch.object(entry, '_message_error') as message:
            root = Path(folder)
            self.assertEqual(entry.desktop_main(['--protocol', entry.OPEN_URI, '--stop'], root), 1)
            start.assert_not_called()
            message.assert_called_once()
            self.assertIn('ValueError', (root/'runtime/desktop-launcher.log').read_text(encoding='utf-8'))

    def test_wrong_installation_and_missing_health_never_open_browser(self):
        with tempfile.TemporaryDirectory() as folder:
            for health in (None, {}, {'data_dir': str(Path(folder)/'other/collections')}):
                with self.subTest(health=health), \
                     patch.object(launcher, 'start_automatic', return_value=8766), \
                     patch.object(launcher, 'ping', return_value=health), \
                     patch.object(entry.webbrowser, 'open') as browser, \
                     patch.object(entry, '_message_error') as message:
                    self.assertEqual(entry.desktop_main([], folder), 1)
                    browser.assert_not_called()
                    message.assert_called_once()

    def test_stop_entry_uses_graceful_control_and_does_not_open_browser(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(launcher, 'launch_lock', return_value=nullcontext()) as lock, \
             patch.object(launcher, 'active_port', return_value=8766), \
             patch.object(launcher, 'stop_background', return_value=True) as stop, \
             patch.object(entry.webbrowser, 'open') as browser:
            root = Path(folder).resolve()
            self.assertEqual(entry.desktop_main(['--stop'], root), 0)
            lock.assert_called_once_with(root, 'auto')
            stop.assert_called_once_with(root, 8766)
            browser.assert_not_called()

    def test_no_open_and_browser_failure_have_distinct_results(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(launcher, 'start_automatic', return_value=8766), \
             patch.object(launcher, 'ping', return_value={'data_dir': str(Path(folder)/'collections')}), \
             patch.object(entry.webbrowser, 'open', return_value=False) as browser, \
             patch.object(entry, '_message_error') as message:
            self.assertEqual(entry.desktop_main(['--no-open'], folder), 0)
            browser.assert_not_called()
            self.assertEqual(entry.desktop_main([], folder), 1)
            self.assertIn('http://127.0.0.1:8766/', message.call_args.args[0])

    @unittest.skipUnless(os.name == 'nt', 'Windows shortcut COM required')
    def test_native_shortcut_round_trip_in_chinese_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='时光集 快捷方式 ') as folder:
            root = Path(folder)
            for stop in (False, True):
                values = entry._shortcut_values(root, stop=stop)
                path = root/('停止.lnk' if stop else '启动.lnk')
                actual = entry._shortcut('save', path, values)
                self.assertTrue(path.is_file())
                self.assertTrue(entry._matching_shortcut(actual, values), actual)
                self.assertTrue(entry._matching_shortcut(entry._shortcut('read', path), values))

    @unittest.skipUnless(os.name == 'nt', 'Windows shell link interface required')
    def test_shortcut_metacharacters_and_unicode_are_literal_data(self):
        with tempfile.TemporaryDirectory(prefix='快捷方式 数据 ') as folder:
            root = Path(folder).resolve()
            values = entry._shortcut_values(root)
            values.update(arguments='x "$(whoami)" 中文 & echo example', description='中文说明')
            path = root / '启动.lnk'
            with patch.object(entry.subprocess, 'run', side_effect=AssertionError('No shell process may run')):
                actual = entry._shortcut('save', path, values)
                self.assertTrue(entry._matching_shortcut(actual, values), actual)
                self.assertEqual(entry._shortcut('read', path)['arguments'], values['arguments'])

    @unittest.skipUnless(os.name == 'nt', 'Windows shell link interface required')
    def test_reading_missing_shortcut_does_not_create_a_file(self):
        with tempfile.TemporaryDirectory(prefix='快捷方式 只读 ') as folder:
            path = Path(folder) / '不存在.lnk'
            with self.assertRaises(OSError):
                entry._shortcut('read', path)
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows paths required')
    def test_install_is_idempotent_and_only_changes_desktop_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('desktop_launcher.pyw', 'launcher.py', 'app.py', 'windows_integration.py'):
                (root/name).touch()
            links, registry = {}, [None]
            def shortcut(operation, path, values=None):
                if operation == 'save':
                    path.touch()
                    links[str(path)] = values
                return links[str(path)]
            with patch.object(entry, 'special_folder', return_value=root) as special, \
                 patch.object(entry, '_read_protocol', side_effect=lambda: registry[0]), \
                 patch.object(entry, '_write_protocol', side_effect=lambda value: registry.__setitem__(0, value)), \
                 patch.object(entry, '_shortcut', side_effect=shortcut):
                for _ in range(2):
                    result = entry.install(root)
                    self.assertEqual(result['autostart'], 'unchanged')
                    self.assertTrue(entry.protocol_available(root))
                self.assertEqual({call.args for call in special.call_args_list}, {('desktop',)})
                self.assertEqual(len(links), 2)
                self.assertIn('--stop', links[str(root/entry.STOP_SHORTCUT_NAME)]['arguments'])

    @unittest.skipUnless(os.name == 'nt', 'Windows paths required')
    def test_conflicting_registration_or_shortcut_prevents_all_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('desktop_launcher.pyw', 'launcher.py', 'app.py', 'windows_integration.py'):
                (root/name).touch()
            with patch.object(entry, '_read_protocol', return_value={'owner': 'elsewhere'}), \
                 patch.object(entry, '_write_protocol') as write, patch.object(entry, '_shortcut') as shortcut:
                with self.assertRaises(RuntimeError): entry.install(root)
                write.assert_not_called()
                shortcut.assert_not_called()
            (root/entry.STOP_SHORTCUT_NAME).touch()
            with patch.object(entry, '_read_protocol', return_value=None), \
                 patch.object(entry, 'special_folder', return_value=root), \
                 patch.object(entry, '_write_protocol') as write, \
                 patch.object(entry, '_shortcut', return_value={}) as shortcut:
                with self.assertRaises(RuntimeError): entry.install(root)
                write.assert_not_called()
                self.assertEqual([call.args[0] for call in shortcut.call_args_list], ['read'])

    @unittest.skipUnless(os.name == 'nt', 'Windows paths required')
    def test_protocol_capability_rejects_changed_command_and_errors(self):
        expected = entry._expected_protocol(entry.ROOT)
        for registration in (None, dict(expected, owner='elsewhere'), dict(expected, command='other.exe'),
                             dict(expected, delegate='unexpected')):
            with patch.object(entry, '_read_protocol', return_value=registration):
                self.assertFalse(entry.protocol_available(entry.ROOT))
        with patch.object(entry, '_read_protocol', side_effect=PermissionError('denied')):
            self.assertFalse(entry.protocol_available(entry.ROOT))


if __name__ == '__main__':
    unittest.main()
