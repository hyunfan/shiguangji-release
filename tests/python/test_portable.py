"""Portable package boundaries; no real user shortcuts or registry writes."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import build_portable as portable
import build_release
import windows_integration as entry

ROOT = build_release.ROOT


def runtime_fixture(extra=None, missing=None):
    names = ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll', 'LICENSE.txt',
             '_ssl.pyd', '_sqlite3.pyd', '_ctypes.pyd', 'python314.zip', 'python314._pth')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name in names:
            if name != missing:
                archive.writestr(name, b'fixture')
        if extra:
            archive.writestr(*extra)
    data = buffer.getvalue()
    pin = json.loads((ROOT/'python-runtime.json').read_text())
    pin['sha256'] = hashlib.sha256(data).hexdigest()
    return data, pin


class PortableTests(unittest.TestCase):
    def test_wrong_runtime_digest_is_rejected_before_unpacking(self):
        data, pin = runtime_fixture()
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            portable.read_runtime(data + b'changed', pin)

    def test_runtime_has_relative_isolated_paths_and_license(self):
        data, pin = runtime_fixture()
        result = portable.read_runtime(data, pin)
        self.assertEqual(result['python/python314._pth'].splitlines(), [b'python314.zip', b'.', b'..'])
        self.assertIn('python/LICENSE.txt', result)

    def test_incomplete_runtime_is_rejected(self):
        data, pin = runtime_fixture(missing='pythonw.exe')
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            portable.read_runtime(data, pin)

    def test_runtime_rejects_paths_duplicates_and_links(self):
        linked = zipfile.ZipInfo('link.pyd')
        linked.create_system = 3
        linked.external_attr = 0o120777 << 16
        for name in ('../app.py', '/app.py', 'C:app.py', 'dir\\file.py', 'PYTHON.EXE', linked):
            with self.subTest(name=str(name)):
                data, pin = runtime_fixture(extra=(name, b'target'))
                with self.assertRaises(ValueError):
                    portable.read_runtime(data, pin)

    def test_source_whitelist_includes_entries_but_no_private_data(self):
        files = build_release.application_files()
        for name in ('启动工具.cmd', '停止后台.cmd', '创建桌面快捷方式.cmd', '开始使用.txt'):
            self.assertIn(name, files)
        for name in files:
            self.assertFalse(name.startswith(('collections/', 'runtime/', 'diagnostics/', 'python/', 'tests/')))

    def test_consecutive_builds_keep_the_same_outer_sha256(self):
        def payload():
            return {'b.txt': b'beta', 'a.txt': '甲'.encode(), '启动.txt': b'cmd'}

        def clock(hour):
            def localtime(_seconds=None):
                return time.struct_time((2024, 6, 15, hour, 30, 0, 5, 167, 0))
            return localtime

        with tempfile.TemporaryDirectory() as folder:
            with patch('time.localtime', clock(1)):
                left = build_release.write_package(folder, 'left.zip', payload(), edition='source')
            with patch('time.localtime', clock(9)):
                right = build_release.write_package(folder, 'right.zip', payload(), edition='source')
            left_sha = hashlib.sha256(left.read_bytes()).hexdigest()
            right_sha = hashlib.sha256(right.read_bytes()).hexdigest()
            self.assertEqual(left_sha, right_sha)
            with zipfile.ZipFile(left) as archive:
                self.assertEqual(archive.namelist(), sorted([*payload(), 'release.json']))
                self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))
                self.assertTrue(all(item.compress_type == zipfile.ZIP_DEFLATED for item in archive.infolist()))
                self.assertEqual(archive.read('启动.txt'), b'cmd')
                manifest = json.loads(archive.read('release.json'))
                self.assertEqual(manifest['edition'], 'source')
                self.assertEqual(manifest['files'], {name: hashlib.sha256(data).hexdigest() for name, data in payload().items()})

    def test_all_files_and_runtime_provenance_are_in_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            result = {'app.py': b'app', 'python/LICENSE.txt': b'license'}
            target = build_release.write_package(folder, 'fixture.zip', result, edition='windows-x64-portable')
            with zipfile.ZipFile(target) as archive:
                manifest = json.loads(archive.read('release.json'))
                self.assertEqual(set(archive.namelist()), {*result, 'release.json'})
                self.assertEqual(manifest['edition'], 'windows-x64-portable')
                self.assertEqual(manifest['files'], {name: hashlib.sha256(data).hexdigest() for name, data in result.items()})

    def test_shortcut_prefers_embedded_python_even_when_run_by_system_python(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'python').mkdir()
            executable = root/'python/pythonw.exe'
            executable.touch()
            self.assertEqual(entry._shortcut_values(root)['target'], str(executable.resolve()))
            self.assertIn(str(executable.resolve()), entry.protocol_command(root))

    def test_shortcut_does_not_use_system_python_for_incomplete_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'portable.json').write_text('{}')
            with self.assertRaisesRegex(RuntimeError, '便携版'):
                entry.pythonw_path(root)

    @unittest.skipUnless(os.name == 'nt', 'Windows cmd required')
    def test_missing_bundle_keeps_error_visible_and_never_falls_back(self):
        for name in ('启动工具.cmd', '停止后台.cmd', '创建桌面快捷方式.cmd'):
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix='便携 缺失 ') as folder:
                root = Path(folder)
                (root/name).write_bytes((ROOT/name).read_bytes())
                (root/'portable.json').write_text('{}')
                for script in ('app.py', 'launcher.py', 'windows_integration.py'):
                    (root/script).write_text('raise AssertionError("SYSTEM_FALLBACK_REACHED")')
                result = subprocess.run([os.environ['COMSPEC'], '/d', '/c', name], cwd=root,
                                        input=b'', capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(b'The bundled Python runtime is missing', result.stdout)
                self.assertIn(b'Press any key to close', result.stdout)
                self.assertNotIn(b'SYSTEM_FALLBACK_REACHED', result.stderr)


if __name__ == '__main__':
    unittest.main()
