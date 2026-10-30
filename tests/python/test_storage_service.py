"""StorageService behaviour seen through snapshot(); fixture libraries only, no real mounts."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from shiguangji.collect.archive import Collector, make_zip
from shiguangji.storage import cloud_backup, cloud_media, storage_service
from shiguangji.storage.backup_library import backup_collection
from shiguangji.storage.storage_service import StorageService
from test_archive import FixtureClient

KIB = 1024
MIB = 1024 * 1024


def wait_idle(service, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = service.snapshot()
        if not state['running']:
            return state
        time.sleep(0.02)
    raise AssertionError('storage operation did not finish')


class StorageServiceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.base = self.root / 'collections'
        self.base.mkdir()

    def library(self):
        Collector('alice', base=self.base, client=FixtureClient()).run()
        return make_zip(self.base / 'alice')

    def manifest(self, handle, media):
        folder = self.base / handle
        folder.mkdir()
        posts = [{'media': media}]
        (folder / 'manifest.json').write_text(json.dumps({'posts': posts}), encoding='utf-8')

    def test_fresh_service_is_idle_and_reports_settings_and_cache(self):
        state = StorageService(self.base).snapshot()
        self.assertFalse(state['running'])
        self.assertEqual(state['operation'], '')
        self.assertIn('ZIP 按需生成', state['message'])
        self.assertTrue(state['settings']['zip_on_demand'])
        self.assertIn('cache', state)

    def test_restart_reports_previous_compression_result(self):
        path = self.base / '.compression-state.json'
        path.write_text(json.dumps({'running': True, 'finished_at': 5}), encoding='utf-8')
        state = StorageService(self.base).snapshot()
        self.assertEqual((state['operation'], state['finished_at']), ('compress', 5))
        self.assertIn('上次压缩中断', state['message'])
        self.assertFalse(state['running'])
        path.write_text(json.dumps({'running': False, 'message': '压缩完成'}), encoding='utf-8')
        state = StorageService(self.base).snapshot()
        self.assertEqual(state['message'], '压缩完成')
        self.assertEqual(state['result'], {'running': False, 'message': '压缩完成'})

    def test_configure_saves_settings_and_rejects_invalid_values(self):
        service = StorageService(self.base)
        self.assertFalse(service.configure({'zip_on_demand': False})['settings']['zip_on_demand'])
        self.assertFalse(StorageService(self.base).snapshot()['settings']['zip_on_demand'])
        with self.assertRaises(ValueError):
            service.configure({'cache_gb': -1})

    def test_invalid_requests_are_rejected_before_any_work(self):
        service = StorageService(self.base)
        with self.assertRaisesRegex(ValueError, '未知空间操作'):
            service.submit('format_disk')
        with self.assertRaisesRegex(ValueError, '请先预览'):
            service.submit('cleanup')
        self.assertFalse(service.snapshot()['running'])

    def test_preview_then_cleanup_releases_only_the_generated_zip(self):
        package = self.library()
        service = StorageService(self.base)
        started = service.submit('preview', all_exports=True)
        self.assertEqual(started['operation'], 'preview')
        state = wait_idle(service)
        self.assertEqual(len(state['result']['items']), 1)
        self.assertIn('可释放 1 个生成的 ZIP', state['message'])
        self.assertTrue(package.exists())
        self.assertIn('finished_at', state)
        service.submit('cleanup', plan_id=state['result']['id'])
        state = wait_idle(service)
        self.assertEqual(state['result']['removed'], 1)
        self.assertIn('已释放 1 个 ZIP', state['message'])
        self.assertFalse(package.exists())
        self.assertTrue((self.base / 'alice' / 'manifest.json').is_file())

    def test_failed_operation_reports_error_and_becomes_idle(self):
        service = StorageService(self.base)
        service.submit('cleanup', plan_id='missing-plan')
        state = wait_idle(service)
        self.assertTrue(state['error'])
        self.assertTrue(state['message'].startswith('本次操作未完成：'))
        self.assertNotIn('result', state)

    def test_running_operation_blocks_another_and_shows_progress(self):
        release = threading.Event()
        reported = threading.Event()

        def slow_plan(base, all_exports, progress):
            progress('正在扫描 ZIP')
            reported.set()
            release.wait(10)
            return {'id': 'p', 'items': []}

        service = StorageService(self.base)
        with patch.object(storage_service, 'plan_cleanup', side_effect=slow_plan):
            service.submit('preview')
            self.assertTrue(reported.wait(10))
            state = service.snapshot()
            self.assertTrue(state['running'])
            self.assertEqual(state['message'], '正在扫描 ZIP')
            with self.assertRaisesRegex(ValueError, '空间任务正在处理'):
                service.submit('clear_cache')
            release.set()
            state = wait_idle(service)
        self.assertIn('可释放 0 个', state['message'])

    def test_pause_only_affects_a_running_compression(self):
        service = StorageService(self.base)
        idle = service.submit('pause_compression')
        self.assertFalse(idle['running'])
        self.assertNotIn('暂停', idle['message'])
        entered = threading.Event()

        def compact(base, progress, stop):
            entered.set()
            paused = stop.wait(10)
            return {'message': '已暂停' if paused else '未收到暂停'}

        with patch('shiguangji.storage.compact_library.compact_library', side_effect=compact):
            service.submit('compress')
            self.assertTrue(entered.wait(10))
            self.assertIn('正在暂停压缩', service.submit('pause_compression')['message'])
            state = wait_idle(service)
        self.assertEqual(state['message'], '已暂停')

    def test_clear_cache_reports_remaining_bytes(self):
        service = StorageService(self.base)
        service.submit('clear_cache')
        state = wait_idle(service)
        self.assertEqual(state['result'], {'remaining_bytes': 0})
        self.assertIn('已清理未使用的媒体缓存', state['message'])

    def test_thread_start_failure_leaves_service_idle(self):
        service = StorageService(self.base)
        thread = Mock()
        thread.return_value.start.side_effect = RuntimeError('no threads')
        with patch.object(storage_service.threading, 'Thread', thread):
            with self.assertRaises(RuntimeError):
                service.submit('clear_cache')
        state = service.snapshot()
        self.assertFalse(state['running'])
        self.assertIn('未能启动', state['message'])

    def test_probe_without_suitable_media_explains_why(self):
        self.manifest('bob', [{'status': 'saved', 'path': 'media/big.mp4', 'kind': 'video', 'bytes': 300 * MIB},
                              {'status': 'failed', 'path': 'media/a.jpg', 'kind': 'image', 'bytes': 80 * KIB}])
        service = StorageService(self.base)
        with self.assertRaisesRegex(ValueError, '没有适合试读的媒体'):
            service.probe('bob')
        service.submit('probe', handle='bob')
        state = wait_idle(service)
        self.assertIn('没有适合试读的媒体', state['message'])

    def test_probe_picks_representative_files_and_reports_each_failure(self):
        self.manifest('bob', [
            {'status': 'saved', 'path': 'media/thumb.jpg', 'kind': 'image', 'bytes': 10 * KIB},
            {'status': 'saved', 'path': 'media/large.jpg', 'kind': 'image', 'bytes': 900 * KIB},
            {'status': 'saved', 'path': 'media/photo.jpg', 'kind': 'image', 'bytes': 60 * KIB},
            {'status': 'saved', 'path': 'media/clip-b.mp4', 'kind': 'video', 'bytes': 700 * KIB},
            {'status': 'saved', 'path': 'media/clip-a.mp4', 'kind': 'video', 'bytes': 500 * KIB},
            {'status': 'saved', 'path': '', 'kind': 'video', 'bytes': 2 * MIB},
        ])
        service = StorageService(self.base)
        with patch.object(storage_service, 'descriptor', side_effect=ValueError('恢复包缺失')):
            service.submit('probe', handle='bob')
            state = wait_idle(service)
        result = state['result']
        self.assertEqual([f['path'] for f in result['files']], ['media/photo.jpg', 'media/clip-a.mp4'])
        self.assertTrue(all(not f['verified'] and f['error'] == '恢复包缺失' for f in result['files']))
        self.assertEqual((result['username'], result['source']), ('bob', 'mount_readback'))
        self.assertFalse(result['provider_upload_confirmed'])
        self.assertIn('0/2 项校验通过', state['message'])
        self.assertIn('未能验证', state['message'])

    def test_probe_reads_back_from_cloud_recovery_package(self):
        self.library()
        target = self.root / 'cloud'
        config = cloud_backup.save_config(self.base, {'enabled': True, 'destination': str(target)})
        backup_collection(self.base, 'alice', cloud_backup.backup_root(self.base))
        with patch.object(cloud_backup, 'check_mount'):
            cloud_backup.copy_round(self.base, target, config['library_id'],
                                    cloud_backup.backup_root(self.base) / '.cloud')

        def extract(command, **kwargs):
            request = json.loads(Path(command[command.index('--request') + 1]).read_bytes())
            with patch.object(cloud_backup, 'check_mount'):
                cloud_media.extract_worker(request, Path(command[command.index('--target') + 1]))
            return Mock(returncode=0)

        service = StorageService(self.base)
        with patch.object(cloud_media.subprocess, 'run', side_effect=extract):
            service.submit('probe', handle='alice')
            state = wait_idle(service)
        files = state['result']['files']
        self.assertTrue(files)
        self.assertTrue(all(f['verified'] and f['bytes'] > 0 and f['cache_hit'] is False for f in files))
        self.assertIn(f'{len(files)}/{len(files)} 项校验通过', state['message'])
        self.assertIn('挂载盘读回不代表服务端上传完成', state['message'])
        self.assertTrue((self.base / 'alice' / files[0]['path']).is_file())


if __name__ == '__main__':
    unittest.main()
