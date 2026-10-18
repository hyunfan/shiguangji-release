"""Regressions for the Windows sharing violation that interrupted real collections."""
import ctypes
import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import archive
import atomic_io
from test_archive import FixtureClient


def locked(code=5):
    error = PermissionError(errno.EACCES, 'fixture sharing lock')
    error.winerror = code
    return error


class AtomicStorageTests(unittest.TestCase):
    def test_transient_lock_retries_without_removing_previous_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            path.write_text('{"old":true}', encoding='utf-8')
            real_replace = os.replace
            attempts = []
            def replace(source, target):
                attempts.append(source)
                if len(attempts) <= 3:
                    self.assertEqual(json.loads(path.read_text()), {'old': True})
                    raise locked((5, 32, 33)[len(attempts)-1])
                real_replace(source, target)
            with patch('atomic_io.os.replace', side_effect=replace), patch('atomic_io.time.sleep'):
                archive.write_json(path, {'text': '图片与视频'})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'text': '图片与视频'})
            self.assertEqual(len(attempts), 4)
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_persistent_lock_preserves_old_file_and_is_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.js'
            path.write_bytes(b'old')
            with patch('atomic_io.os.replace', side_effect=locked()) as replace, patch('atomic_io.time.sleep'):
                with self.assertRaises(PermissionError):
                    atomic_io.atomic_write_bytes(path, b'new')
            self.assertEqual(replace.call_count, 11)
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(Path(folder).glob('*.tmp')), [])

    def test_disk_full_fails_without_retry_or_destroying_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'data.js'
            path.write_bytes(b'old')
            with patch('atomic_io.os.replace', side_effect=OSError(errno.ENOSPC, 'disk full')) as replace, patch('atomic_io.time.sleep') as sleep:
                with self.assertRaises(OSError):
                    atomic_io.atomic_write_bytes(path, b'new')
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(path.read_bytes(), b'old')

    @unittest.skipUnless(os.name == 'nt', 'Windows file-sharing regression')
    def test_real_windows_reader_can_release_lock_during_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'manifest.json'
            path.write_bytes(b'old')
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            kernel.CreateFileW.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.restype = ctypes.c_int
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0, None)
            self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
            released = threading.Event()
            def release():
                kernel.CloseHandle(handle)
                released.set()
            timer = threading.Timer(0.2, release)
            timer.start()
            try:
                atomic_io.atomic_write_bytes(path, b'new')
                self.assertTrue(released.is_set())
                self.assertEqual(path.read_bytes(), b'new')
            finally:
                timer.join()

    def test_running_snapshot_lock_does_not_abort_committed_downloads(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            original = archive.export_collection
            def export(store, status, *args, **kwargs):
                if status == 'running':
                    raise locked()
                return original(store, status, *args, **kwargs)
            with patch('archive.export_collection', side_effect=export):
                result = archive.Collector('alice', base=base, client=FixtureClient()).run()
            manifest = json.loads((base/'alice'/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(result, 'complete')
            self.assertEqual(manifest['counts']['saved'], 1)
            self.assertGreater(manifest['last_run']['deferred_checkpoints'], 0)
            self.assertTrue((base/'alice'/manifest['posts'][0]['media'][0]['path']).is_file())

    def test_final_export_failure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as folder:
            collector = archive.Collector('alice', base=Path(folder), client=FixtureClient())
            with patch('archive.export_collection', side_effect=locked()):
                with self.assertRaises(PermissionError):
                    collector.run()

    def test_unchanged_offline_assets_are_not_republished(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'gallery.js'
            atomic_io.publish_if_changed(path, b'one')
            with patch('atomic_io.replace_file') as replace:
                atomic_io.publish_if_changed(path, b'one')
                replace.assert_not_called()


if __name__ == '__main__':
    unittest.main()
