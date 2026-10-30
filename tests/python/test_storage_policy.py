"""Space management never uses real collections or cloud mounts."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from shiguangji.collect.archive import Collector, make_zip
from test_archive import FixtureClient
from shiguangji.storage.backup_library import backup_collection, restore_backup
from shiguangji.storage import cloud_backup
from shiguangji.storage import cloud_media
from shiguangji.storage.storage_policy import settings, save_settings, plan_cleanup, execute_cleanup
from shiguangji.storage.storage_service import StorageService
from shiguangji.tasks.task_service import TaskService


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.base=self.root/'collections';self.base.mkdir()
        self.folder=self.base/'alice'
        Collector('alice',base=self.base,client=FixtureClient()).run()
        self.package=make_zip(self.folder)

    def test_zip_on_demand_is_default_and_rejects_invalid_options(self):
        self.assertTrue(settings(self.base)['zip_on_demand'])
        for values in ({'cache_gb':True},{'cache_gb':-1},{'backup_versions':0},{'zip_on_demand':1},{'unknown':1}):
            with self.assertRaises(ValueError):save_settings(self.base,values)
        save_settings(self.base,{'zip_on_demand':False,'backup_versions':1})
        self.assertFalse(settings(self.base)['zip_on_demand'])

    def test_preview_does_not_delete_and_confirmation_removes_only_generated_zip(self):
        files={str(p.relative_to(self.folder)):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()}
        plan=plan_cleanup(self.base,all_exports=True)
        self.assertEqual(len(plan['items']),1);self.assertTrue(self.package.exists())
        self.assertEqual(execute_cleanup(self.base,plan['id'])['removed'],1)
        self.assertFalse(self.package.exists())
        self.assertEqual(files,{str(p.relative_to(self.folder)):p.read_bytes() for p in self.folder.rglob('*') if p.is_file()})
        with self.assertRaises(ValueError):execute_cleanup(self.base,plan['id'])
        make_zip(self.folder);self.assertTrue(self.package.is_file())

    def test_changed_package_and_missing_source_are_never_deleted(self):
        plan=plan_cleanup(self.base,all_exports=True);self.package.write_bytes(b'changed')
        self.assertEqual(execute_cleanup(self.base,plan['id'])['removed'],0)
        self.assertEqual(self.package.read_bytes(),b'changed')
        make_zip(self.folder)
        plan=plan_cleanup(self.base,all_exports=True)
        source=next((self.folder/'media').rglob('*.*'));source.unlink()
        self.assertEqual(execute_cleanup(self.base,plan['id'])['removed'],0)
        self.assertEqual(plan_cleanup(self.base,all_exports=True)['items'],[])

    def test_unknown_zip_and_live_collection_are_skipped(self):
        other=self.base/'bob-合集.zip';other.write_bytes(b'user-file')
        manifest=self.folder/'manifest.json';data=json.loads(manifest.read_bytes());data['status']='running';manifest.write_text(json.dumps(data))
        self.assertEqual(plan_cleanup(self.base,all_exports=True)['items'],[])
        self.assertTrue(other.exists());self.assertTrue(self.package.exists())

    def test_same_size_source_change_prevents_zip_release(self):
        plan=plan_cleanup(self.base,all_exports=True)
        source=next(p for p in (self.folder/'media').rglob('*') if p.is_file())
        original=source.read_bytes()
        source.write_bytes(bytes([original[0]^1])+original[1:])
        self.assertEqual(execute_cleanup(self.base,plan['id'])['removed'],0)
        self.assertTrue(self.package.is_file())

    def test_expiry_and_metadata_retention_preserve_current_restorable_backup(self):
        self.assertEqual(plan_cleanup(self.base)['items'],[])
        save_settings(self.base,{'backup_versions':1})
        root=cloud_backup.backup_root(self.base)
        for _ in range(3):result=backup_collection(self.base,'alice',root)
        self.assertEqual(len(list(root.glob('alice-*.zip'))),1)
        restore_backup(Path(result['path']),self.root/'restored')
        self.assertTrue((self.root/'restored/alice/archive.sqlite').is_file())

    def test_regular_task_skips_zip_but_explicit_export_still_builds_it(self):
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        service.job.update(username='alice',running=True,run_id='test')
        with patch.object(service,'collect_batches',return_value='partial'),patch('shiguangji.tasks.task_service.make_zip') as build:
            service._worker_body('alice',{})
            build.assert_not_called()
            service._worker_body('alice',{},zip_only=True)
            build.assert_called_once()

    def cloud(self):
        target=self.root/'cloud';config=cloud_backup.save_config(self.base,{'enabled':True,'destination':str(target)})
        backup_collection(self.base,'alice',cloud_backup.backup_root(self.base))
        with patch.object(cloud_backup,'check_mount'):
            cloud_backup.copy_round(self.base,target,config['library_id'],cloud_backup.backup_root(self.base)/'.cloud')
        manifest=json.loads((self.folder/'manifest.json').read_bytes())
        item=next(m for p in manifest['posts'] for m in p['media'] if m.get('status')=='saved')
        return target,item

    def extract(self,command,**kwargs):
        request=json.loads(Path(command[command.index('--request')+1]).read_bytes())
        with patch.object(cloud_backup,'check_mount'):
            cloud_media.extract_worker(request,Path(command[command.index('--target')+1]))
        return Mock(returncode=0)

    def test_cloud_reader_verifies_content_then_works_offline_from_bounded_cache(self):
        target,item=self.cloud()
        with patch.object(cloud_media.subprocess,'run',side_effect=self.extract) as run:
            with cloud_media.open_media(self.base,'alice',item['path']) as (path,record,hit):
                self.assertFalse(hit);self.assertEqual(path.read_bytes(),(self.folder/item['path']).read_bytes())
                self.assertEqual(record['sha256'],item['sha256'])
                self.assertEqual(cloud_media.trim_cache(self.base,limit=0),record['bytes'])
            with cloud_media.open_media(self.base,'alice',item['path']) as (_,_,hit):self.assertTrue(hit)
            self.assertEqual(run.call_count,1)
        self.assertEqual(cloud_media.trim_cache(self.base,limit=0),0)
        self.assertTrue((self.folder/item['path']).is_file())

    def test_corrupt_package_and_timeout_do_not_publish_or_remove_original(self):
        target,item=self.cloud()
        with patch.object(cloud_media.subprocess,'run',side_effect=subprocess.TimeoutExpired('reader',1)):
            with self.assertRaises(TimeoutError):
                with cloud_media.open_media(self.base,'alice',item['path']):pass
        self.assertEqual(list(cloud_media.cache_root(self.base).iterdir()),[])
        self.assertEqual(cloud_media._reserved,0);self.assertEqual(cloud_media._pinned,{})
        request=cloud_media.descriptor(self.base,'alice',item['path'])
        (target/request['bundle']).write_bytes(b'corrupt')
        with patch.object(cloud_media.subprocess,'run',side_effect=self.extract):
            with self.assertRaises(ValueError):
                with cloud_media.open_media(self.base,'alice',item['path']):pass
        self.assertTrue((self.folder/item['path']).is_file())
        self.assertFalse(list(cloud_media.cache_root(self.base).glob('*.blob')))

    def test_stale_snapshot_and_path_escape_fail_before_mount_access(self):
        _,item=self.cloud()
        for path in ('../other','media/../../other','C:/other','media\\other'):
            with self.assertRaises(ValueError):cloud_media.descriptor(self.base,'alice',path)
        index=cloud_backup.backup_root(self.base)/'alice.json';index.write_bytes(index.read_bytes()+b' ')
        with self.assertRaises(ValueError):cloud_media.descriptor(self.base,'alice',item['path'])

    def test_storage_snapshot_does_not_probe_mount(self):
        service=StorageService(self.base)
        with patch.object(cloud_backup,'check_mount',side_effect=AssertionError('no network')):
            self.assertFalse(service.snapshot()['running'])


if __name__=='__main__':unittest.main()
