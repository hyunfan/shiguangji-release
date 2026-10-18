"""Real local reuse behavior with isolated fixtures; no network or user files."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from account_identity import Catalog
from archive import Collector, Store, Cancelled, export_collection, make_zip
from collection_lock import collection_lock
from extractor import extract
from local_aliases import reuse_local_aliases
from test_account_identity import card
from test_archive import TS, PNG, FixtureClient


class LocalAliasTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(self.base)
        self.addCleanup(self.catalog.close)
        self.catalog.record('alice','primary',card('alice','123'),TS,'https://twitter.com/alice')
        self.catalog.record('oldalice','alias',card('oldalice','123'),TS,'https://twitter.com/oldalice')

    def source(self, uid='123', pid='1346889436626259968', data=PNG, saved=True, raw=None):
        store = Store(self.base/'oldalice','oldalice')
        try:
            url='https://twitter.com/oldalice/status/'+pid
            raw=raw if raw is not None else card('oldalice',uid,pid=pid)
            name=hashlib.sha256((TS+url).encode()).hexdigest()[:24]+'.txt'
            path=store.folder/'raw'/name
            path.parent.mkdir(exist_ok=True)
            path.write_text(raw,encoding='utf-8')
            store.db.execute('INSERT OR REPLACE INTO captures VALUES(?,?,?,?,?,?)',(TS,url,'text/html','-','done',''))
            store.db.commit()
            posts=extract(raw,url,TS,'oldalice')
            if saved:
                for p in posts:
                    for item in p['media']:
                        dest=store.folder/'media'/(pid+'.png');dest.parent.mkdir(exist_ok=True)
                        dest.write_bytes(data)
                        item.update(status='saved',path=dest.relative_to(store.folder).as_posix(),bytes=len(data),
                                    sha256=hashlib.sha256(data).hexdigest(),archive_url='https://web.archive.org/web/'+TS+'/https://pbs.twimg.com/media/rename.jpg')
            store.put_posts(posts)
            export_collection(store,'complete',[])
            return path
        finally:
            store.close()

    def runner(self, **options):
        runner=Collector('alice',options,base=self.base,client=FixtureClient())
        self.addCleanup(runner.store.close)
        return runner

    def test_imports_verified_posts_raw_and_media_without_network_and_keeps_source(self):
        self.source()
        source=self.base/'oldalice'
        before={p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob('*') if p.is_file() and p.suffix!='.lock'}
        runner=self.runner()
        runner.client.text=lambda *_: self.fail('Unexpected network read')
        runner.client.download=lambda *_: self.fail('Unexpected network download')
        report=reuse_local_aliases(runner)
        self.assertEqual((report['new_posts'],report['reused_files']),(1,1))
        post=runner.store.posts()[0]
        self.assertEqual(post['username'],'oldalice')
        self.assertEqual(post['author_ids'],['123'])
        self.assertEqual(post['media'][0]['reused_from_collection'],'oldalice')
        self.assertEqual((runner.folder/post['media'][0]['path']).read_bytes(),PNG)
        self.assertEqual(runner.store.db.execute("SELECT COUNT(*) FROM captures WHERE status='done'").fetchone()[0],1)
        self.assertEqual(runner.budget.snapshot()['reused_files'],1)
        self.assertEqual(runner.budget.requests,0)
        for name,digest in before.items():
            self.assertEqual(hashlib.sha256((source/name).read_bytes()).hexdigest(),digest)
        with patch('local_aliases.extract',side_effect=AssertionError('unchanged raw parsed again')):
            again=reuse_local_aliases(runner)
        self.assertEqual((again['checked'],again['cached'],again['reused_files']),(0,1,0))
        self.assertFalse(again['pending'])

    def test_compressed_alias_page_reuses_media_and_retains_parser_cache(self):
        from compressed_files import compress_file, read_text
        path=self.source();original=path.read_text(encoding='utf-8')+'\n'*4096
        path.write_text(original,encoding='utf-8')
        compress_file(path,path.parent)
        self.assertFalse(path.exists());self.assertEqual(read_text(path),original)
        runner=self.runner()
        runner.client.text=lambda *_:self.fail('Unexpected network read')
        report=reuse_local_aliases(runner)
        self.assertEqual((report['new_posts'],report['reused_files']),(1,1))
        with patch('local_aliases.extract',side_effect=AssertionError('compressed page parsed again')):
            again=reuse_local_aliases(runner)
        self.assertEqual(again['cached'],1)

    def test_recycled_alias_rejects_other_owner_and_metadata_without_numeric_author(self):
        self.source();self.source(uid='999',pid='1346889436626259969')
        self.source(pid='1346889436626259970',raw='<meta property="og:description" content="no author proof">')
        self.catalog.record('oldalice','recycled',card('oldalice','999'),TS,'https://twitter.com/oldalice')
        runner=self.runner()
        report=reuse_local_aliases(runner)
        self.assertEqual(report['new_posts'],1)
        self.assertEqual([p['id'] for p in runner.store.posts()],['1346889436626259968'])

    def stamp_source_cache(self,path,version=None):
        from archive import PARSER_CACHE_VERSION
        from account_identity import revision
        source=Store(self.base/'oldalice','oldalice')
        stat=path.stat()
        source.db.execute('INSERT OR REPLACE INTO raw_checks VALUES(?,?,?,?)',
                          (path.name,stat.st_size,stat.st_mtime_ns,PARSER_CACHE_VERSION if version is None else version))
        source.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',
                          ('parsed_identity',revision(self.catalog.snapshot('oldalice'))))
        source.db.commit();source.close()

    def test_current_source_parser_cache_reuses_verified_models_and_still_validates_media(self):
        path=self.source();self.stamp_source_cache(path)
        runner=self.runner()
        with patch('local_aliases.extract',side_effect=AssertionError('Unchanged, already parsed source')):
            result=reuse_local_aliases(runner)
        self.assertEqual((result['new_posts'],result['reused_files']),(1,1))
        item=runner.store.posts()[0]['media'][0]
        self.assertNotEqual(item['path'],'media/1346889436626259968.png')
        self.assertEqual((runner.folder/item['path']).read_bytes(),PNG)

    def test_changed_source_snapshot_or_parser_never_trusts_old_cached_model(self):
        for changed in ('bytes','parser','identity'):
            with self.subTest(changed=changed):
                path=self.source();self.stamp_source_cache(path,version=0 if changed=='parser' else None)
                if changed=='bytes':path.write_text('changed',encoding='utf-8')
                if changed=='identity':
                    source=Store(self.base/'oldalice','oldalice')
                    source.db.execute("UPDATE settings SET value='obsolete' WHERE key='parsed_identity'")
                    source.db.commit();source.close()
                runner=self.runner()
                with patch('local_aliases.extract',return_value=[]) as parse:
                    result=reuse_local_aliases(runner)
                self.assertEqual(result['new_posts'],0)
                parse.assert_called_once()
                runner.store.close()

    def test_current_cache_never_imports_a_source_model_without_matching_author(self):
        path=self.source(uid='999');self.stamp_source_cache(path)
        runner=self.runner()
        with patch('local_aliases.extract',side_effect=AssertionError('Unchanged source cache')):
            result=reuse_local_aliases(runner)
        self.assertEqual(result['new_posts'],0)

    def test_unknown_primary_and_excluded_alias_never_import(self):
        self.source()
        self.catalog.choice('alice','oldalice','excluded')
        runner=self.runner()
        self.assertEqual(reuse_local_aliases(runner)['new_posts'],0)
        self.catalog.choice('alice','oldalice','auto')
        self.catalog.record('alice','conflict',card('alice','456'),TS,'https://twitter.com/alice')
        self.assertEqual(reuse_local_aliases(runner)['new_posts'],0)

    def test_bounded_batches_resume_without_reparsing_completed_files(self):
        self.source();self.source(pid='1346889436626259969')
        runner=self.runner()
        first=reuse_local_aliases(runner,max_files=1)
        self.assertTrue(first['pending'])
        self.assertEqual(first['checked'],1)
        second=reuse_local_aliases(runner,max_files=1)
        self.assertEqual(second['checked'],1)
        self.assertFalse(second['pending'])
        self.assertEqual(len(runner.store.posts()),2)
        self.assertEqual(runner.setting('local_alias_checked'),'2')

    def test_same_media_url_across_posts_uses_one_file_and_one_gain(self):
        self.source();self.source(pid='1346889436626259969')
        runner=self.runner()
        result=reuse_local_aliases(runner)
        self.assertEqual(result['new_posts'],2)
        self.assertEqual(result['reused_files'],1)
        self.assertEqual(runner.budget.snapshot()['new_files'],1)
        self.assertEqual(len({p['media'][0]['path'] for p in runner.store.posts()}),1)

    def test_discovery_does_not_copy_media_but_later_full_run_upgrades_existing_items(self):
        self.source()
        discover=self.runner(mode='discover')
        report=reuse_local_aliases(discover)
        self.assertEqual((report['new_posts'],report['reused_files']),(1,0))
        self.assertFalse((discover.folder/'media').exists())
        discover.store.close()
        full=self.runner()
        report=reuse_local_aliases(full)
        self.assertEqual((report['new_posts'],report['reused_files']),(0,1))
        self.assertEqual(full.store.posts()[0]['media'][0]['status'],'saved')

    def test_repair_reuses_only_known_posts_and_full_run_can_import_the_rest(self):
        self.source();self.source(pid='1346889436626259969')
        runner=self.runner(mode='repair')
        runner.store.put_posts(extract(card('oldalice','123'),'https://twitter.com/oldalice',TS,'oldalice'))
        result=reuse_local_aliases(runner)
        self.assertEqual((result['new_posts'],result['reused_files']),(0,1))
        self.assertEqual(len(runner.store.posts()),1)
        runner.options['mode']='full'
        result=reuse_local_aliases(runner)
        self.assertEqual(result['new_posts'],1)
        self.assertEqual(len(runner.store.posts()),2)

    def test_corrupt_source_bytes_are_not_counted_or_published_and_repair_invalidates_cache(self):
        self.source()
        path=next((self.base/'oldalice/media').glob('*'))
        path.write_bytes(PNG[:-1]+b'!')
        runner=self.runner()
        report=reuse_local_aliases(runner)
        self.assertEqual(report['reused_files'],0)
        self.assertEqual(report['skipped_media'],1)
        self.assertEqual(list((runner.folder/'media').rglob('*.part')),[])
        self.assertEqual(runner.budget.snapshot()['new_files'],0)
        path.write_bytes(PNG)
        self.assertEqual(reuse_local_aliases(runner)['reused_files'],1)

    def test_repair_checks_a_newly_known_post_even_if_source_did_not_change(self):
        self.source(pid='1346889436626259969')
        runner=self.runner(mode='repair')
        self.assertEqual(reuse_local_aliases(runner)['new_posts'],0)
        runner.store.put_posts(extract(card('oldalice','123',pid='1346889436626259969'),
                                      'https://twitter.com/oldalice',TS,'oldalice'))
        result=reuse_local_aliases(runner)
        self.assertEqual((result['checked'],result['reused_files']),(1,1))

    def test_missing_destination_is_copied_again_on_a_later_run(self):
        self.source()
        runner=self.runner();reuse_local_aliases(runner)
        item=runner.store.posts()[0]['media'][0]
        (runner.folder/item['path']).unlink()
        runner.store.close()
        resumed=self.runner()
        self.assertEqual(reuse_local_aliases(resumed)['reused_files'],1)
        self.assertEqual(resumed.budget.snapshot()['new_files'],1)

    def test_source_path_escape_is_never_read_or_copied(self):
        self.source()
        source=Store(self.base/'oldalice','oldalice')
        post=source.posts()[0]
        post['media'][0]['path']='../../outside.png'
        source.save_post(post);source.close()
        runner=self.runner()
        report=reuse_local_aliases(runner)
        self.assertEqual(report['reused_files'],0)
        self.assertEqual(report['skipped_media'],1)
        self.assertFalse((runner.folder/'media').exists())

    def test_pause_during_streaming_copy_cleans_partial_and_resumes(self):
        self.source(data=__import__('test_media_fixtures').png(2_000_000))
        runner=self.runner()
        normal_check=runner.identity_check
        def stop_in_copy():
            if list(runner.folder.rglob('*.part')):
                raise Cancelled()
            normal_check()
        with patch.object(runner,'identity_check',stop_in_copy):
            with self.assertRaises(Cancelled):reuse_local_aliases(runner)
        self.assertEqual(list(runner.folder.rglob('*.part')),[])
        self.assertEqual(runner.budget.snapshot()['new_files'],0)
        self.assertEqual(reuse_local_aliases(runner)['reused_files'],1)

    def test_size_limit_change_reconsiders_prior_skip(self):
        self.source(data=__import__('test_media_fixtures').png(1_100_000))
        runner=self.runner(max_media_mb=1)
        self.assertEqual(reuse_local_aliases(runner)['reused_files'],0)
        runner.options['max_media_mb']=2
        self.assertEqual(reuse_local_aliases(runner)['reused_files'],1)

    def test_busy_source_is_deferred_without_waiting_or_losing_data(self):
        self.source()
        ready,release=threading.Event(),threading.Event()
        def hold():
            with collection_lock(self.base/'oldalice'):
                ready.set();release.wait(10)
        thread=threading.Thread(target=hold);thread.start();ready.wait(5)
        runner=self.runner()
        try:
            report=reuse_local_aliases(runner)
            self.assertTrue(report['pending']);self.assertEqual(report['checked'],0)
        finally:
            release.set();thread.join()
        self.assertEqual(reuse_local_aliases(runner)['new_posts'],1)

    def test_normal_collection_pipeline_uses_local_content_and_exports_media(self):
        self.source()
        runner=self.runner()
        runner.client.cdx=lambda *args,**kwargs: iter([[]])
        runner.client.text=lambda *_: self.fail('Local capture should be reused')
        runner.client.download=lambda *_: self.fail('Local media should be reused')
        self.assertEqual(runner.run(),'complete')
        manifest=json.loads((runner.folder/'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['counts']['saved'],1)
        self.assertEqual(manifest['last_run']['local_aliases']['reused_files'],1)
        import zipfile
        with zipfile.ZipFile(make_zip(runner.folder)) as z:
            self.assertIsNone(z.testzip())
            self.assertEqual(z.read('alice/'+manifest['posts'][0]['media'][0]['path']),PNG)


if __name__=='__main__':
    unittest.main()
