"""Cloud mounts are simulated; no test accesses the user's cloud drive."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import zipfile
from unittest.mock import Mock, patch

from shiguangji.collect.archive import Collector
from shiguangji.storage.backup_library import backup_collection, digest, restore_backup
from shiguangji.storage import cloud_backup as cloud
from test_archive import FixtureClient


class CloudBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = self.root / 'collections'
        self.base.mkdir()
        self.destination = self.root / 'cloud' / 'backup'
        self.destination.parent.mkdir()
        self.control = cloud.backup_root(self.base) / '.cloud'
        self.settings = cloud.save_config(self.base, {'enabled': True, 'destination': str(self.destination)})
        self.mount = patch.object(cloud, 'check_mount')
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def backup(self):
        Collector('alice', base=self.base, client=FixtureClient()).run()
        return Path(backup_collection(self.base, 'alice', cloud.backup_root(self.base))['path'])

    def run_copy(self):
        return cloud.copy_round(self.base, self.destination, self.settings['library_id'], self.control)

    def unpack(self, name='downloaded'):
        receipt = cloud.read_json(self.control/'receipts.json')['alice']
        target = self.root/name
        with zipfile.ZipFile(self.destination/receipt['bundle']) as archive:
            self.assertIsNone(archive.testzip())
            archive.extractall(target)
        return target

    def test_copy_restore_and_restart_skips_unchanged_snapshots(self):
        index = self.backup()
        state = self.run_copy()
        self.assertEqual(state['state'], 'submitted')
        self.assertEqual(state['pending'], 0)
        downloaded = self.unpack()
        restored = restore_backup(downloaded / index.name, self.root / 'restored')
        with closing(sqlite3.connect(restored / 'alice/archive.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM posts').fetchone()[0], 1)
        for path in (restored / 'alice/media').rglob('*'):
            if path.is_file():
                self.assertEqual(digest(path), digest(self.base / path.relative_to(restored)))
        with patch.object(cloud, 'copy_snapshot') as copy, patch.object(cloud, 'check_mount') as mount:
            self.run_copy()
            copy.assert_not_called(); mount.assert_not_called()
        self.assertIn('实际上传', state['message'])

    def test_failed_mount_preserves_pending_snapshot_and_retries_later(self):
        self.backup()
        with patch.object(cloud, 'check_mount', side_effect=OSError('offline')):
            with self.assertRaises(OSError): self.run_copy()
        self.assertFalse(self.destination.exists())
        self.assertEqual(cloud.read_json(self.control / 'receipts.json'), {})
        self.assertEqual(cloud.read_json(self.control / 'progress.json')['pending'], 1)
        self.assertEqual(self.run_copy()['submitted'], 1)

    def test_failed_new_snapshot_does_not_overwrite_previous_cloud_index(self):
        index = self.backup(); self.run_copy()
        pointer = self.destination/'alice.bundle.json'
        previous = pointer.read_bytes()
        index = Path(backup_collection(self.base, 'alice', cloud.backup_root(self.base))['path'])
        snapshot = json.loads(index.read_bytes())
        (index.parent / snapshot['metadata']).write_bytes(b'broken metadata')
        self.assertEqual(self.run_copy()['state'], 'retrying')
        self.assertEqual(pointer.read_bytes(), previous)
        self.assertEqual(len(list((self.destination/'恢复包').glob('alice-*.zip'))), 1)

    def test_partial_bundle_repair_and_prior_snapshot_preservation(self):
        self.backup(); self.run_copy()
        receipt = cloud.read_json(self.control/'receipts.json')['alice']
        damaged = self.destination/receipt['bundle']; damaged.write_bytes(b'incomplete')
        cloud.write_json(self.control/'receipts.json', {})
        self.run_copy(); self.assertEqual(digest(damaged), receipt['sha256'])
        Path(backup_collection(self.base, 'alice', cloud.backup_root(self.base))['path'])
        self.run_copy()
        self.assertEqual(cloud.read_json(self.control / 'receipts.json')['alice']['copied'], 1)
        self.assertEqual(len(list((self.destination/'恢复包').glob('alice-*.zip'))), 2)
        self.assertTrue(damaged.exists())

    def test_different_target_requeues_even_after_receipt(self):
        self.backup(); self.run_copy()
        self.destination = self.root / 'second-cloud'
        self.assertEqual(self.run_copy()['submitted'], 1)
        self.assertTrue((self.destination / 'alice.bundle.json').is_file())

    def test_unrelated_nonempty_folder_is_not_modified(self):
        self.backup(); self.destination.mkdir(parents=True)
        other = self.destination / 'family.txt'; other.write_text('keep')
        with self.assertRaises(ValueError): self.run_copy()
        self.assertEqual(list(self.destination.iterdir()), [other])

    def test_bad_index_traversal_never_touches_cloud(self):
        index = self.backup(); data = json.loads(index.read_bytes())
        data['metadata'] = '../outside.zip'; index.write_text(json.dumps(data))
        self.assertEqual(self.run_copy()['state'], 'retrying')
        self.assertFalse(self.destination.exists())

    def test_corrupt_index_does_not_block_other_collections(self):
        self.backup()
        (cloud.backup_root(self.base) / 'broken.json').write_text('{broken')
        state = self.run_copy()
        self.assertEqual(state['submitted'], 1); self.assertEqual(state['pending'], 1)
        self.assertEqual(state['state'], 'retrying')
        self.assertTrue((self.destination / 'alice.bundle.json').exists())

    def test_save_configuration_and_status_never_probe_network(self):
        service = cloud.CloudBackup(self.base)
        with patch.object(cloud, 'check_mount', side_effect=AssertionError('network access')):
            result = service.configure({'enabled': True, 'destination': str(self.destination)})
            self.assertTrue(result['enabled']); self.assertEqual(service.snapshot()['state'], 'waiting')
        self.assertFalse(self.destination.exists())
        for value in ('relative/path', str(self.base), str(cloud.backup_root(self.base)), self.root.anchor):
            with self.assertRaises(ValueError):
                service.configure({'enabled': True, 'destination': value})

    def test_disabled_service_keeps_cloud_files_and_receipts(self):
        self.backup(); self.run_copy()
        service = cloud.CloudBackup(self.base)
        receipt = (self.control / 'receipts.json').read_bytes()
        state = service.configure({'enabled': False, 'destination': str(self.destination)})
        self.assertFalse(state['enabled']); self.assertTrue((self.destination / 'alice.bundle.json').exists())
        self.assertEqual((self.control / 'receipts.json').read_bytes(), receipt)

    def test_legacy_receipts_are_preserved_without_reupload_or_deletion(self):
        index = self.backup(); snapshot, raw, revision = cloud.checked_snapshot(index)
        cloud.write_json(self.control/'receipts.json', {'alice':{'revision':revision,'destination':str(self.destination)}})
        with patch.object(cloud, 'copy_snapshot') as copy:
            self.assertEqual(self.run_copy()['submitted'],1)
            copy.assert_not_called()

    def test_failed_transfer_reuses_verified_local_bundle_and_cleans_after_success(self):
        self.backup()
        with patch.object(cloud, 'copy_verified', side_effect=OSError('offline')):
            self.assertEqual(self.run_copy()['state'],'retrying')
        cache = cloud.backup_root(self.base)/'.cloud/packages'
        package = next(cache.glob('*.zip')); stamp = package.stat().st_mtime_ns
        with patch.object(cloud, 'copy_verified', side_effect=OSError('offline')):
            self.run_copy()
        self.assertEqual(package.stat().st_mtime_ns,stamp)
        self.run_copy(); self.assertFalse(package.exists())

    def test_stalled_child_is_killed_without_touching_archive_process(self):
        service = cloud.CloudBackup(self.base)
        child = Mock(); child.poll.return_value = None
        with patch.object(cloud.subprocess, 'Popen', return_value=child), patch.object(cloud, 'STALL_SECONDS', -1):
            with self.assertRaises(TimeoutError): service.execute(self.settings, 0)
        child.terminate.assert_called_once(); child.wait.assert_called_once_with(timeout=3)

    def test_shutdown_cancels_only_owned_copy_child(self):
        service = cloud.CloudBackup(self.base); service.stopped.set()
        child = Mock(); child.poll.return_value = None
        with patch.object(cloud.subprocess, 'Popen', return_value=child):
            self.assertFalse(service.execute(self.settings, 0))
        child.terminate.assert_called_once()

    def test_empty_backup_library_waits_without_creating_remote_folder(self):
        state = self.run_copy()
        self.assertEqual(state['state'], 'waiting'); self.assertFalse(self.destination.exists())

    def test_local_sync_folder_copy_and_restore_needs_no_provider_label(self):
        self.mount.stop()
        self.backup()
        self.assertEqual(self.run_copy()['submitted'], 1)
        restored = self.root / 'restore'
        restore_backup(self.unpack() / 'alice.json', restored)
        self.assertTrue((restored / 'alice/archive.sqlite').is_file())

    def test_missing_parent_does_not_create_an_unmounted_sync_root(self):
        self.mount.stop()
        self.backup()
        self.destination.parent.rmdir()
        with self.assertRaisesRegex(OSError, '上级目录'):
            self.run_copy()
        self.assertFalse(self.destination.parent.exists())

    def test_replaced_destination_is_not_recreated_or_written(self):
        self.backup(); self.run_copy()
        self.destination.rename(self.root / 'disconnected-target')
        self.backup()
        with self.assertRaisesRegex(ValueError, '标识'):
            self.run_copy()
        self.assertFalse(self.destination.exists())

    def test_foreign_marker_on_reused_path_stops_before_copy(self):
        self.backup(); self.run_copy()
        marker = self.destination / '.shiguangji-backup.json'
        data = cloud.read_json(marker); data['destination_id'] = 'f' * 32
        cloud.write_json(marker, data)
        self.backup()
        with patch.object(cloud, 'copy_snapshot') as copy:
            with self.assertRaisesRegex(ValueError, '标识'):
                self.run_copy()
            copy.assert_not_called()

    def test_legacy_marker_can_be_pinned_without_reuploading_old_receipts(self):
        self.destination.mkdir()
        marker = self.destination / '.shiguangji-backup.json'
        cloud.write_json(marker, {'schema':1, 'kind':'shiguangji-backup', 'library_id':self.settings['library_id']})
        self.backup(); self.run_copy()
        self.assertEqual(cloud.read_json(marker)['schema'], 2)
        receipt = (self.control / 'receipts.json').read_bytes()
        self.run_copy()
        self.assertEqual((self.control / 'receipts.json').read_bytes(), receipt)

    def test_corrupt_local_binding_does_not_reset_destination_protection(self):
        self.backup(); self.run_copy(); self.backup()
        (self.control / 'destination-bindings.json').write_text('{bad')
        with self.assertRaisesRegex(ValueError, '核对记录'):
            self.run_copy()

    def test_old_receipt_with_missing_marker_does_not_adopt_new_target(self):
        index = self.backup()
        revision = cloud.checked_snapshot(index)[2]
        cloud.write_json(self.control / 'receipts.json', {'old': {'destination':str(self.destination), 'revision':revision}})
        with self.assertRaisesRegex(ValueError, '标识'):
            self.run_copy()
        self.assertFalse(self.destination.exists())

    def test_concurrent_accounts_can_backup_identical_content_without_staging_collision(self):
        import shutil
        from shiguangji.storage import backup_library
        Collector('alice',base=self.base,client=FixtureClient()).run()
        shutil.copytree(self.base/'alice',self.base/'bob')
        barrier=threading.Barrier(2)
        original=shutil.copyfile
        staged={}; guard=threading.Lock()
        def copying(source,target,*args,**kwargs):
            if Path(target).suffix=='.part' and Path(target).parent.parent.name=='objects':
                owner=threading.get_ident()
                with guard:
                    first=owner not in staged
                    if first:staged[owner]=str(target)
                if first:barrier.wait(timeout=10)
            return original(source,target,*args,**kwargs)
        with patch.object(backup_library.shutil,'copyfile',side_effect=copying):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda handle:backup_collection(self.base,handle,cloud.backup_root(self.base)),('alice','bob')))
        self.assertEqual(len(set(staged.values())),2)
        self.assertTrue(all(result['verified'] for result in results))


if __name__ == '__main__':
    unittest.main()
