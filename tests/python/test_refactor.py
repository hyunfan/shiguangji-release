"""Regression boundaries discovered by the full-project audit. Offline only."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from shiguangji.sources.archive_http import Client
from shiguangji.sources.source_http import SourceClient
from shiguangji.core.core_errors import ArchiveError, failure_kind
from shiguangji.media.media_files import media_valid, saved_file_valid
from test_archive import FakeResponse, IMAGE
from test_media_fixtures import PNG, VIDEO, FRAGMENTED, WEBM, png, box
from shiguangji.tasks.task_queue import TaskQueue
from shiguangji.tasks.job_dispatch import dispatch
from shiguangji.tasks.task_status import summary
from shiguangji.storage.collection_reader import read_summary, read_document, post_page, _summaries, _documents
from shiguangji.storage.collection_export import publish_manifest
from shiguangji.sources.search_plan import indexes_plan, update_progress
from shiguangji.platforms.platform_registry import Account, platform_for


class RefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)

    def test_all_transports_reject_head_only_mp4_and_unterminated_jpeg(self):
        for transport in (Client,SourceClient):
            for content,url in [(box(b'ftyp',b'isom'+b'0'*12),'https://video.twimg.com/tweet_video/a.mp4'),(b'\xff\xd8\xff'+b'x'*29,IMAGE)]:
                with self.subTest(transport=transport.__name__,url=url):
                    client=transport();client.open=Mock(return_value=FakeResponse(content,url))
                    with self.assertRaises(ArchiveError) as caught:client.download(url,self.folder/'x.bin',1024)
                    self.assertEqual(failure_kind(caught.exception),'invalid_media')
                    self.assertEqual(list(self.folder.iterdir()),[])

    def test_container_formats_and_truncation(self):
        for content,kind in [(PNG,'image'),(VIDEO,'video'),(FRAGMENTED,'video'),(WEBM,'video')]:
            with self.subTest(kind=kind,head=content[:12]):
                self.assertTrue(media_valid(content,kind))
                self.assertFalse(media_valid(content[:-2],kind))
        self.assertFalse(media_valid(VIDEO,'image'))
        self.assertFalse(media_valid(PNG,'video'))
        self.assertFalse(media_valid(PNG[:-1]+b'x','image'))

    def test_changed_same_size_bytes_and_wrong_digest_are_not_cached_success(self):
        path=self.folder/'media.png';path.write_bytes(PNG)
        item={'status':'saved','path':path.name,'bytes':len(PNG),'kind':'image','sha256':hashlib.sha256(PNG).hexdigest()}
        self.assertTrue(saved_file_valid(self.folder,item));self.assertTrue(saved_file_valid(self.folder,item))
        path.write_bytes(b'x'*len(PNG));self.assertFalse(saved_file_valid(self.folder,item))
        path.write_bytes(PNG);item['sha256']='0'*64;self.assertFalse(saved_file_valid(self.folder,item))

    def test_valid_container_with_changed_payload_fails_expected_hash(self):
        path=self.folder/'video.mp4';path.write_bytes(VIDEO)
        item={'status':'saved','path':path.name,'bytes':len(VIDEO),'kind':'video','sha256':hashlib.sha256(VIDEO).hexdigest()}
        self.assertTrue(saved_file_valid(self.folder,item))
        path.write_bytes(VIDEO[:-1]+b'!')
        self.assertTrue(media_valid(path.read_bytes(),'video'))
        self.assertFalse(saved_file_valid(self.folder,item))

    def test_prepare_error_advances_once_then_starts_next(self):
        for error in (sqlite3.OperationalError('database is locked'),ValueError('旧名歧义')):
            with self.subTest(error=error):
                q=TaskQueue(self.folder/(type(error).__name__+'.json'));q.add(['alice','bob'],{})
                prepare=Mock(side_effect=[error,('bob',{})]);launch=Mock()
                dispatch(q,prepare,launch,lambda:True)
                self.assertEqual(prepare.call_count,2);launch.assert_called_once()
                self.assertEqual(q.data['items'][0]['state'],'failed')
                self.assertIn(str(error),q.public()['items'][0]['message'])

    def test_dispatch_storage_failure_returns_without_loop(self):
        q=TaskQueue(self.folder/'q.json');q.add(['alice','bob'],{})
        prepare=Mock(side_effect=RuntimeError('prepare'))
        with patch.object(q,'commit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):dispatch(q,prepare,Mock(),lambda:True)
        self.assertEqual(prepare.call_count,1);self.assertEqual(q.next_item()['username'],'alice')

    def test_late_duplicate_finish_cannot_overwrite_terminal_outcome(self):
        q=TaskQueue(self.folder/'q.json');q.add(['alice'],{});item=q.next_item()
        q.mark_running(item['id'],'alice','run');q.finish(item['id'],'run',summary({'status':'complete'}))
        before=json.dumps(q.data);q.finish(item['id'],'run',summary({'status':'failed'}))
        self.assertEqual(json.dumps(q.data),before)

    def manifest(self,count=105):
        return {'username':'alice','status':'partial','updated_at':'2026-10-08T00:00:00Z','counts':{'posts':count},'posts':[
            {'id':str(1000+i),'username':'alice','created_at':'2021-01-01T00:00:00Z' if i%2 else '',
             'text':'needle' if i==55 else 'body','media':[],'captures':[]} for i in range(count)]}

    def test_summary_does_not_read_manifest_body_on_warm_or_persisted_hit(self):
        publish_manifest(self.folder,self.manifest())
        _summaries.clear();_documents.clear()
        original=Path.read_bytes
        def guard(path):
            if path.name=='manifest.json':raise AssertionError('unnecessary full read')
            return original(path)
        with patch.object(Path,'read_bytes',guard):
            self.assertEqual(read_summary(self.folder)['counts']['posts'],105)
            self.assertEqual(read_summary(self.folder)['posts'],[])

    def test_legacy_summary_fallback_is_cached_and_read_only(self):
        target=self.folder/'manifest.json';target.write_text(json.dumps(self.manifest()),encoding='utf-8')
        before=target.read_bytes();self.assertEqual(read_summary(self.folder)['counts']['posts'],105)
        with patch.object(Path,'read_bytes',side_effect=AssertionError('should be cached')):
            self.assertEqual(read_summary(self.folder)['posts'],[])
        self.assertEqual(target.read_bytes(),before);self.assertFalse((self.folder/'summary.json').exists())

    def test_summary_replacement_invalidates_even_when_updated_at_unchanged(self):
        publish_manifest(self.folder,self.manifest())
        first=read_summary(self.folder)
        publish_manifest(self.folder,self.manifest(3))
        second=read_summary(self.folder)
        self.assertNotEqual(first['content_revision'],second['content_revision'])
        self.assertEqual(second['counts']['posts'],3)

    def test_pagination_has_no_gaps_searches_whole_collection_and_keeps_unknown_last(self):
        publish_manifest(self.folder,self.manifest())
        pages=[post_page(self.folder,{'offset':str(n),'limit':'40'}) for n in (0,40,80)]
        ids=[p['id'] for page in pages for p in page['posts']]
        self.assertEqual(len(set(ids)),105)
        self.assertEqual(len(pages[0]['posts']),40);self.assertFalse(pages[-1]['has_more'])
        self.assertTrue(pages[0]['posts'][0]['created_at']);self.assertFalse(pages[-1]['posts'][-1]['created_at'])
        self.assertEqual(post_page(self.folder,{'search':'needle'})['posts'][0]['id'],'1055')
        self.assertEqual(post_page(self.folder,{'month':'unknown'})['total'],53)
        self.assertTrue(post_page(self.folder,{'sort':'asc'})['posts'][0]['created_at'])
        self.assertTrue(post_page(self.folder,{'revision':'old'})['stale'])

    def test_light_checkpoint_preserves_previous_offline_generation(self):
        publish_manifest(self.folder,self.manifest(2));before=(self.folder/'data.js').read_bytes()
        publish_manifest(self.folder,self.manifest(3),offline=False)
        self.assertEqual((self.folder/'data.js').read_bytes(),before)
        self.assertEqual(read_summary(self.folder)['counts']['posts'],3)

    def test_zip_generation_hashes_agree_with_packaged_files(self):
        from shiguangji.storage.collection_export import make_zip
        from test_archive import FixtureClient
        from shiguangji.collect.archive import Collector
        import zipfile
        Collector('alice',base=self.folder,client=FixtureClient()).run()
        with zipfile.ZipFile(make_zip(self.folder/'alice')) as package:
            generation=json.loads(package.read('alice/generation.json'))
            for name,digest in generation['files'].items():
                self.assertEqual(hashlib.sha256(package.read('alice/'+name)).hexdigest(),digest)
            self.assertNotIn('alice/summary.json',package.namelist())

    def test_structured_http_errors_do_not_parse_numbers_in_messages(self):
        self.assertEqual(failure_kind(ArchiveError('timeout after HTTP 404 candidate')),'network')
        self.assertEqual(failure_kind(ArchiveError('gone',status=410)),'not_found')
        self.assertEqual(failure_kind(ArchiveError('denied',status=403)),'access_denied')

    def test_plan_retains_order_and_explains_deep_scope(self):
        a,b,c='CC-MAIN-2025-01','CC-MAIN-2024-01','CC-MAIN-2013-01'
        plan=indexes_plan([a,b,c],[a])
        newer=indexes_plan([b,a,c],[a],plan)
        self.assertEqual(newer['indexes'],[a,b,c])
        update_progress(newer,[a],[],{});self.assertEqual(newer['phase'],'deep')
        update_progress(newer,[a,b,c],[],{});self.assertEqual(newer['reason'],'scope_checked')

    def test_platform_key_preserves_native_name_and_directory(self):
        self.assertEqual(Account.from_key('instagram--bob.name').username,'bob.name')
        self.assertEqual(Account.from_key('alice','123').key,'alice')
        self.assertEqual(platform_for('instagram--bob').name,'instagram')


if __name__=='__main__':unittest.main()
