"""Renames must broaden discovery without admitting a different numeric author."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from account_identity import Catalog, observations, search_handles
from archive import Collector, Store, export_collection, make_zip
from extractor import extract, blank_post
from test_archive import FixtureClient, TS, PNG


def card(name, uid, pid='1346889436626259968', text='old post'):
    return f'<article data-screen-name="{name}" data-user-id="{uid}" data-tweet-id="{pid}"><p class="tweet-text">{text}</p><img src="https://pbs.twimg.com/media/rename.jpg"></article>'


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.catalog = Catalog(self.base)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.catalog.close)

    def record(self, name, uid, key=None):
        self.catalog.record('alice', key or name, card(name, uid), TS, 'https://twitter.com/'+name)

    def test_same_id_forms_alias_and_recycled_name_requires_per_post_verification(self):
        self.record('alice', '123')
        self.record('oldalice', '123')
        info = self.catalog.snapshot('alice')
        self.assertEqual(info['user_id'], '123')
        self.assertEqual(info['aliases'][0]['state'], 'verified')
        self.assertEqual(search_handles(info), ['alice', 'oldalice'])
        self.record('oldalice', '999', 'recycled')
        self.assertEqual(self.catalog.snapshot('alice')['aliases'][0]['state'], 'reused')
        self.assertEqual(search_handles(self.catalog.snapshot('alice')), ['alice', 'oldalice'])

    def test_unrelated_candidate_with_another_id_is_not_searched(self):
        self.record('alice','123');self.record('oldalice','999')
        self.catalog.choice('alice','oldalice','candidate')
        self.assertEqual(self.catalog.snapshot('alice')['aliases'][0]['state'],'conflict')
        self.assertEqual(search_handles(self.catalog.snapshot('alice')),['alice'])

    def test_recycled_name_never_redirects_typed_input_to_a_different_owner(self):
        from account_identity import canonical_handle
        self.record('alice','123');self.record('oldalice','123');self.record('oldalice','999','new-owner')
        (self.base/'alice').mkdir()
        (self.base/'alice/manifest.json').write_text('{}')
        self.assertEqual(canonical_handle(self.base,'oldalice'),'oldalice')

    def test_recycled_alias_imports_only_posts_with_the_primary_numeric_author(self):
        self.record('alice','123');self.record('oldalice','123');self.record('oldalice','999','new-owner')
        runner=Collector('alice',base=self.base,client=FixtureClient())
        self.addCleanup(runner.store.close)
        posts=runner.parse_capture(card('oldalice','123')+card('oldalice','999',pid='1346889436626259969'),
                                   'https://twitter.com/oldalice',TS,'mixed')
        self.assertEqual([p['id'] for p in posts],['1346889436626259968'])
        self.assertEqual(posts[0]['author_ids'],['123'])
        self.assertTrue(runner.identity_pending())
        self.assertEqual(runner.parse_capture('<meta property="og:description" content="unknown owner">',
                                             'https://twitter.com/oldalice/status/12345',TS,'unscoped'),[])

    def test_ambiguous_primary_id_does_not_auto_choose_owner(self):
        self.record('alice', '123')
        self.record('alice', '456', 'second')
        self.record('oldalice', '123')
        self.assertEqual(self.catalog.snapshot('alice')['status'], 'conflict')
        self.assertEqual(search_handles(self.catalog.snapshot('alice')), ['alice'])

    def test_candidate_is_not_proof_and_exclusion_survives_new_evidence(self):
        self.catalog.choice('alice', 'oldalice', 'candidate')
        self.assertEqual(self.catalog.snapshot('alice')['aliases'][0]['state'], 'candidate')
        self.catalog.choice('alice', 'oldalice', 'excluded')
        self.record('alice', '123')
        self.record('oldalice', '123')
        self.assertEqual(search_handles(self.catalog.snapshot('alice')), ['alice'])
        self.catalog.choice('alice', 'oldalice', 'auto')
        self.assertEqual(search_handles(self.catalog.snapshot('alice')), ['alice', 'oldalice'])

    def test_names_avatars_and_unscoped_user_ids_are_not_identity(self):
        raw = '<p>@oldalice</p><img alt="alice"><div data-screen-name="alice"></div><i data-user-id="123"></i>'
        self.assertEqual(observations(raw), [])
        self.assertEqual(observations(json.dumps({'id_str':'88','user_id_str':'123','text':'@oldalice'})), [])

    def test_html_and_structured_user_formats_preserve_large_ids(self):
        uid = '1234567890123456789'
        fixtures = [card('alice', uid),
            json.dumps({'rest_id':uid,'legacy':{'screen_name':'alice'}}),
            '<script>window.__INITIAL_STATE__ = '+json.dumps({'users':{'u':{'id_str':uid,'screen_name':'alice'}}})+';</script>',
            json.dumps({'includes':{'users':[{'id':uid,'username':'alice'}]}}),
            json.dumps({'rest_id':uid,'core':{'screen_name':'alice'}})]
        for raw in fixtures:
            with self.subTest(raw=raw):
                self.assertIn((uid, 'alice'), {(uid, name) for uid, name, kind in observations(raw)})

    def test_structured_posts_keep_numeric_author_evidence(self):
        fixtures=[{'id_str':'100','text':'legacy','user':{'id_str':'123','screen_name':'alice'}},
          {'rest_id':'100','legacy':{'full_text':'graphql','user_id_str':'123'},'core':{'user_results':{'result':{'rest_id':'123','legacy':{'screen_name':'alice'}}}}},
          {'data':[{'id':'100','author_id':'123','text':'v2'}],'includes':{'users':[{'id':'123','username':'alice'}]}}]
        for payload in fixtures:
            with self.subTest(payload=payload):
                posts=extract(json.dumps(payload),'https://twitter.com/alice','20210101','alice')
                self.assertEqual(posts[0]['author_ids'],['123'])

    def test_post_ids_never_become_user_ids(self):
        raw=json.dumps({'rest_id':'999','legacy':{'full_text':'post','user_id_str':'123'},
                        'core':{'user_results':{'result':{'rest_id':'123','legacy':{'screen_name':'alice'}}}}})
        self.assertEqual(observations(raw), [('123','alice','json')])

    def test_controls_validate_paths_limits_and_primary_name(self):
        for name in ('../outside', 'https://evil.test/alice', 'alice'):
            with self.assertRaises(ValueError):
                self.catalog.choice('alice', name, 'candidate')
        for n in range(12):
            self.catalog.choice('alice', f'alias{n}', 'candidate')
        with self.assertRaises(ValueError):
            self.catalog.choice('alice', 'too_many', 'candidate')

    def test_incremental_scan_resumes_and_reuses_unchanged_cache(self):
        store = Store(self.base/'bob', 'bob')
        self.addCleanup(store.close)
        for n, name in enumerate(['alice', 'oldalice']):
            url = 'https://twitter.com/bob/status/'+str(n+10)
            key = hashlib.sha256((TS+url).encode()).hexdigest()[:24]+'.txt'
            store.db.execute('INSERT INTO captures VALUES(?,?,?,?,?,?)', (TS,url,'text/html','-','done',''))
            raw = store.folder/'raw'/key
            raw.parent.mkdir(exist_ok=True)
            raw.write_text(card(name, '123'), encoding='utf-8')
        store.db.commit()
        first = self.catalog.scan(max_files=1)
        self.assertEqual(first, {'processed':1,'remaining':1})
        second = self.catalog.scan(max_files=1)
        self.assertEqual(second, {'processed':1,'remaining':0})
        self.assertEqual(self.catalog.scan()['processed'],0)
        self.assertEqual(self.catalog.snapshot('alice')['aliases'][0]['username'], 'oldalice')

    def test_cancel_keeps_scan_cursor(self):
        def stop():
            raise RuntimeError('cancelled')
        store = Store(self.base/'bob', 'bob')
        self.addCleanup(store.close)
        store.db.execute('INSERT INTO captures VALUES(?,?,?,?,?,?)', (TS,'https://twitter.com/bob','text/html','-','done',''))
        store.db.commit()
        with self.assertRaisesRegex(RuntimeError, 'cancelled'):
            self.catalog.scan(check=stop)

    def test_cross_collection_evidence_is_used_without_copying_posts(self):
        self.catalog.record('bob', 'one', card('alice','123')+card('oldalice','123'), TS, 'https://twitter.com/bob')
        self.assertEqual(self.catalog.snapshot('alice')['aliases'][0]['state'],'verified')
        self.assertFalse((self.base/'alice').exists())

    def test_typing_known_old_name_reuses_existing_collection(self):
        from account_identity import canonical_handle
        self.record('alice','123');self.record('oldalice','123')
        (self.base/'alice').mkdir()
        (self.base/'alice/manifest.json').write_text('{}')
        self.assertEqual(canonical_handle(self.base,'oldalice'),'alice')
        self.catalog.choice('alice','oldalice','excluded')
        self.assertEqual(canonical_handle(self.base,'oldalice'),'oldalice')
        self.catalog.choice('alice','oldalice','auto')
        (self.base/'oldalice').mkdir()
        (self.base/'oldalice/manifest.json').write_text('{}')
        self.assertEqual(canonical_handle(self.base,'oldalice'),'oldalice')

    def test_verified_alias_posts_dedupe_and_preserve_real_author(self):
        self.record('alice','123');self.record('oldalice','123')
        runner = Collector('alice', base=self.base, client=FixtureClient())
        self.addCleanup(runner.store.close)
        posts = runner.parse_capture(card('oldalice','123'), 'https://twitter.com/oldalice', TS, 'old')
        runner.store.put_posts(posts)
        same = runner.parse_capture(card('alice','123',text='new version'), 'https://twitter.com/alice', TS, 'new')
        runner.store.put_posts(same)
        self.assertEqual(len(runner.store.posts()),1)
        post = runner.store.posts()[0]
        self.assertEqual(post['username'],'oldalice')
        self.assertEqual(post['observed_usernames'],['alice','oldalice'])
        self.assertEqual(post['author_ids'],['123'])
        self.assertIn('old post',post['text_versions'])

    def test_alias_link_and_metadata_without_author_id_are_not_imported(self):
        self.record('alice','123');self.record('oldalice','123')
        runner = Collector('alice', base=self.base, client=FixtureClient())
        self.addCleanup(runner.store.close)
        raw='<meta property="og:description" content="unverified"><meta property="og:image" content="https://pbs.twimg.com/media/notours.jpg">'
        self.assertEqual(runner.parse_capture(raw,'https://twitter.com/oldalice/status/12345',TS,'unscoped'), [])

    def test_different_numeric_author_never_imported_under_alias(self):
        self.record('alice','123');self.record('oldalice','123')
        runner = Collector('alice', base=self.base, client=FixtureClient())
        self.addCleanup(runner.store.close)
        self.assertEqual(runner.parse_capture(card('oldalice','999'), 'https://twitter.com/oldalice', TS, 'different'), [])

    def test_alias_cdx_does_not_create_unverified_blank_posts(self):
        store = Store(self.base/'alice','alice');self.addCleanup(store.close)
        store.add_captures([{'timestamp':TS,'original':'https://twitter.com/oldalice/status/10','mimetype':'text/html','digest':'-'}],handles=['alice','oldalice'])
        self.assertEqual(store.posts(),[])
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0],1)

    def test_alias_full_pipeline_downloads_into_primary_offline_collection(self):
        self.record('alice','123');self.record('oldalice','123')
        client=FixtureClient()
        queried=[]
        def cdx(url, match='exact'):
            queried.append(url)
            if url=='twitter.com/oldalice':
                yield [{'timestamp':TS,'original':'https://twitter.com/oldalice/status/1346889436626259968','mimetype':'text/html','digest':'OLD'}]
            else:
                yield []
        client.cdx=cdx
        client.text=lambda url:card('oldalice','123')
        result=Collector('alice',base=self.base,client=client).run()
        self.assertEqual(result,'complete')
        manifest=json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertIn('twitter.com/oldalice',queried)
        self.assertEqual(manifest['counts']['saved'],1)
        self.assertEqual(manifest['posts'][0]['username'],'oldalice')
        self.assertEqual(manifest['identity']['aliases'][0]['state'],'verified')
        self.assertFalse((self.base/'oldalice').exists())
        import zipfile
        with zipfile.ZipFile(make_zip(self.base/'alice')) as archive:
            self.assertEqual(archive.testzip(),None)
            self.assertIn(b'oldalice',archive.read('alice/posts.jsonl'))

    def test_identity_only_run_preserves_collection_status_errors_and_counts(self):
        store=Store(self.base/'alice','alice')
        store.put_post(blank_post('12345','alice'))
        export_collection(store,'partial',[{'stage':'missing','error':'prior error'}],run_info={'prior':1})
        store.close()
        runner=Collector('alice',{'mode':'identity'},base=self.base,client=FixtureClient())
        self.assertEqual(runner.run(),'complete')
        result=json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['errors'][0]['error'],'prior error')
        self.assertEqual(result['last_run']['prior'],1)
        self.assertEqual(result['counts']['posts'],1)

    def test_recycled_alias_pipeline_searches_old_name_but_never_downloads_other_owner(self):
        self.record('alice','123');self.record('oldalice','123');self.record('oldalice','999','new-owner')
        client=FixtureClient()
        accepted='1346889436626259968'; rejected='1346889436626259969'
        queried=[]
        def cdx(url,match='exact'):
            queried.append(url)
            yield ([{'timestamp':TS,'original':'https://twitter.com/oldalice/status/'+pid,
                     'mimetype':'text/html','digest':pid} for pid in [accepted,rejected]]
                   if url=='twitter.com/oldalice' else [])
        client.cdx=cdx
        client.text=lambda url: card('oldalice','999' if rejected in url else '123',
                                     pid=rejected if rejected in url else accepted)
        self.assertEqual(Collector('alice',base=self.base,client=client).run(),'complete')
        manifest=json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertIn('twitter.com/oldalice',queried)
        self.assertEqual([p['id'] for p in manifest['posts']],[accepted])
        self.assertEqual(manifest['counts']['saved'],1)
        self.assertEqual(manifest['identity']['aliases'][0]['state'],'reused')

    def test_identity_scan_responds_to_pause(self):
        stop=threading.Event();stop.set()
        runner=Collector('alice',{'mode':'identity'},base=self.base,stop=stop)
        # Include one indexed capture so the scan invokes its cancellation check.
        runner.store.db.execute('INSERT INTO captures VALUES(?,?,?,?,?,?)',(TS,'https://twitter.com/alice','text/html','-','done',''))
        runner.store.db.commit()
        self.assertEqual(runner.run(),'paused')

    def test_delete_removes_catalog_evidence_and_choices(self):
        from collection_delete import delete_collection
        (self.base/'alice').mkdir()
        self.record('alice','123')
        self.catalog.choice('alice','oldalice','candidate')
        delete_collection(self.base,'alice')
        info=self.catalog.snapshot('alice')
        self.assertEqual(info['status'],'unknown')
        self.assertEqual(info['aliases'],[])


if __name__=='__main__':
    unittest.main()
