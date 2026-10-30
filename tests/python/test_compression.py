"""Storage-format regressions: interruption, legacy indexes and real round trips."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from shiguangji.storage import compressed_files as stored
from shiguangji.collect.archive import Collector
from shiguangji.storage.backup_library import backup_collection, restore_backup
from shiguangji.storage.cloud_backup import make_bundle
from shiguangji.storage.collection_export import make_zip
from shiguangji.storage.compact_library import compact_library
from shiguangji.storage.storage_policy import reproducible
from test_archive import FixtureClient


class CompressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_round_trip_preserves_logical_name_bytes_and_timestamp(self):
        path=self.root/'中文 空格.txt';data=('历史页面正文 '*10000).encode()
        path.write_bytes(data);before=path.stat()
        result=stored.compress_file(path,self.root)
        self.assertGreater(result['saved'],0);self.assertFalse(path.exists())
        self.assertEqual(stored.read_bytes(path),data)
        stat=stored.logical_stat(path)
        self.assertEqual((stat.st_size,stat.st_mtime_ns),(len(data),before.st_mtime_ns))
        self.assertEqual(list(stored.logical_files(self.root)),[path])
        self.assertEqual(stored.compress_file(path,self.root)['status'],'already')
        self.assertEqual(stored.expand_file(path,self.root)['status'],'expanded')
        self.assertEqual(path.read_bytes(),data)
        self.assertEqual(path.stat().st_mtime_ns,before.st_mtime_ns)
        self.assertFalse(stored.packed_path(path).exists())

    def test_failed_verification_never_removes_original(self):
        path=self.root/'a.txt';data=b'hello'*10000;path.write_bytes(data)
        with patch('shiguangji.storage.compressed_files._check_gzip',side_effect=ValueError('bad readback')):
            with self.assertRaises(ValueError):stored.compress_file(path,self.root)
        self.assertEqual(path.read_bytes(),data)
        self.assertFalse(stored.packed_path(path).exists())
        self.assertEqual(list(self.root.glob('*.tmp')),[])

    def test_interruption_after_publish_is_readable_and_retryable(self):
        path=self.root/'a.txt';data=b'hello'*10000;path.write_bytes(data)
        real_unlink=Path.unlink
        def blocked(candidate,*args,**kwargs):
            if candidate==path:raise PermissionError('reader holding file')
            return real_unlink(candidate,*args,**kwargs)
        with patch.object(Path,'unlink',blocked):
            with self.assertRaises(PermissionError):stored.compress_file(path,self.root)
        self.assertTrue(path.exists());self.assertTrue(stored.packed_path(path).exists())
        self.assertEqual(stored.read_bytes(path),data)
        self.assertEqual(list(stored.logical_files(self.root)),[path])
        stored.compress_file(path,self.root)
        self.assertFalse(path.exists());self.assertEqual(stored.read_bytes(path),data)

    def test_bad_expected_hash_and_outside_path_are_rejected(self):
        path=self.root/'a.txt';path.write_bytes(b'hello'*10000)
        with self.assertRaises(ValueError):stored.compress_file(path,self.root,expected='0'*64)
        with self.assertRaises(ValueError):stored.compress_file(path,self.root/'other')
        self.assertTrue(path.is_file())

    def test_compressed_corruption_is_detected_on_resume(self):
        path=self.root/'a.txt';stored.write_bytes(path,b'hello'*10000)
        packed=stored.packed_path(path);packed.write_bytes(packed.read_bytes()[:-5])
        with self.assertRaises((OSError,EOFError,ValueError)):stored.compress_file(path,self.root)

    def test_parallel_work_is_bounded_and_pause_drains_before_releasing_lock(self):
        from shiguangji.storage.collection_lock import collection_lock, CollectionBusy
        base=self.root/'collections';folder=base/'alice';raw=folder/'raw';raw.mkdir(parents=True)
        (folder/'manifest.json').write_text('{}')
        for n in range(50):(raw/(str(n)+'.txt')).write_bytes(b'text'*10000)
        stop=threading.Event();mutex=threading.Lock();active=peak=done=0
        original=stored.compress_file
        def work(path,root,expected=None):
            nonlocal active,peak,done
            with mutex:active+=1;peak=max(peak,active)
            try:
                with self.assertRaises(CollectionBusy):
                    with collection_lock(folder):pass
                time.sleep(.02)
                item=original(path,root,expected)
                with mutex:
                    done+=1
                    if done>=3:stop.set()
                return item
            finally:
                with mutex:active-=1
        with patch('shiguangji.storage.compact_library.compress_file',side_effect=work):result=compact_library(base,stop=stop)
        self.assertTrue(result['paused']);self.assertFalse(result['errors'])
        self.assertGreater(peak,1);self.assertLessEqual(peak,8);self.assertEqual(active,0)
        self.assertLess(result['checked'],50)
        with collection_lock(folder):pass
        resumed=compact_library(base)
        self.assertFalse(resumed['paused']);self.assertFalse(resumed['errors'])
        self.assertEqual(resumed['already']+resumed['compressed'],50)

    def fixture(self):
        base=self.root/'collections';client=FixtureClient()
        Collector('alice',base=base,client=client).run()
        return base,client

    def test_corrupt_compressed_page_can_be_refetched_in_repair_mode(self):
        base,client=self.fixture()
        page=next(stored.logical_files(base/'alice/raw'));packed=stored.packed_path(page)
        packed.write_bytes(b'broken gzip')
        client.cdx=lambda *args,**kwargs:(_ for _ in ()).throw(AssertionError('repair rescanned'))
        Collector('alice',{'mode':'repair'},base=base,client=client).run()
        self.assertTrue(stored.read_text(page));self.assertEqual(client.page_reads,2)

    def test_collect_resume_backup_export_and_cloud_restore(self):
        base,client=self.fixture();folder=base/'alice';reads=client.page_reads
        pages=list(stored.logical_files(folder/'raw'))
        self.assertTrue(pages);self.assertTrue(any(stored.packed_path(p).is_file() for p in pages))
        Collector('alice',base=base,client=client).run()
        self.assertEqual(client.page_reads,reads)
        backups=self.root/'collections-backups'
        index=Path(backup_collection(base,'alice',backups)['path'])
        self.assertEqual(backup_collection(base,'alice',backups)['copied'],0)
        snapshot=json.loads(index.read_bytes())
        for relative,record in snapshot['files'].items():
            self.assertFalse(relative.endswith('.gz'))
            self.assertEqual(stored.digest_stored(base/relative),record['sha256'])
        restored=restore_backup(index,self.root/'restored')
        for relative,record in snapshot['files'].items():
            self.assertEqual(hashlib.sha256((restored/relative).read_bytes()).hexdigest(),record['sha256'])
        make_zip(folder);package=base/'alice-合集.zip'
        self.assertTrue(reproducible(package,folder,verify_content=True))
        with zipfile.ZipFile(package) as zipped:
            for page in pages:
                self.assertEqual(zipped.read('alice/'+page.relative_to(folder).as_posix()),stored.read_bytes(page))
        _,_,bundle,_=make_bundle(index,lambda **kwargs:None)
        unpacked=self.root/'cloud-copy'
        with zipfile.ZipFile(bundle) as zipped:zipped.extractall(unpacked)
        restore_backup(unpacked/index.name,self.root/'cloud-restored')

    def test_migration_preserves_existing_backup_indexes_and_old_objects(self):
        base,_=self.fixture();backups=self.root/'collections-backups'
        # Make an old-format raw page and backup object without changing content.
        index=Path(backup_collection(base,'alice',backups)['path']);before=index.read_bytes()
        snapshot=json.loads(before);raw_path=next(p for p in snapshot['files'] if '/raw/' in p)
        sha=snapshot['files'][raw_path]['sha256']
        for path in (base/raw_path,backups/'objects'/sha[:2]/sha):
            data=stored.read_bytes(path);path.write_bytes(data);stored.packed_path(path).unlink(missing_ok=True)
        media={p:hashlib.sha256((base/p).read_bytes()).hexdigest() for p in snapshot['files'] if '/media/' in p}
        result=compact_library(base)
        self.assertFalse(result['errors']);self.assertEqual(result['compressed'],2)
        self.assertEqual(index.read_bytes(),before)
        restore_backup(index,self.root/'restored')
        for p,sha in media.items():self.assertEqual(hashlib.sha256((base/p).read_bytes()).hexdigest(),sha)
        again=compact_library(base)
        self.assertFalse(again['errors']);self.assertEqual(again['saved_bytes'],0)
        expanded=compact_library(base,expand=True)
        self.assertFalse(expanded['errors']);self.assertEqual(expanded['expanded'],2)
        self.assertTrue((base/raw_path).is_file())
        self.assertEqual(index.read_bytes(),before)


if __name__=='__main__':unittest.main()
