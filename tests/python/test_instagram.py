"""Instagram ownership, archive boundaries and platform-isolation regression tests."""
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
import app
from archive import Store, export_collection, make_zip, ArchiveError, validate_replay_target, replay
from instagram_archive import InstagramCollector
from instagram_extract import extract, blank_post, media_item, post_links
from platform_accounts import account_input, collection_key
from task_queue import TaskQueue, parse_handles
from collection_delete import delete_collection

TS='20200101000000'
CDN='https://scontent.cdninstagram.com/v/t51.2885-15/photo.jpg?sig=one'


def post(owner='alice',code='AbC_123',caption='Archived caption',**extra):
    return {'shortcode':code,'owner':{'username':owner},'taken_at_timestamp':1577836800,
            'edge_media_to_caption':{'edges':[{'node':{'text':caption}}]},'display_url':CDN,**extra}


def page(obj):return '<script>window._sharedData = '+json.dumps(obj)+';</script>'


class InstagramExtractionTests(unittest.TestCase):
    def test_platform_names_are_disjoint_and_strict(self):
        self.assertEqual(account_input('Alice','x'),'alice')
        self.assertEqual(account_input('https://www.instagram.com/Alice.Name/','instagram'),'instagram--alice.name')
        self.assertEqual(collection_key('instagram--alice.name'),'instagram--alice.name')
        self.assertEqual(parse_handles('@Alice，bob.name\nALICE','instagram'),['instagram--alice','instagram--bob.name','instagram--alice'])
        for value in ('../bob','.bob','bob.','bob..x','accounts','https://evil.test/alice','https://www.instagram.com/p/code/'):
            with self.assertRaises(ValueError):account_input(value,'instagram')
        with self.assertRaises(ValueError):account_input('https://instagram.com/alice','x')
        with self.assertRaises(ValueError):account_input('https://x.com/alice','instagram')

    def test_shared_data_owner_and_caption_and_real_date(self):
        result,_=extract(page({'graphql':{'shortcode_media':post()}}),'https://www.instagram.com/p/AbC_123/',TS,'alice')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['text'],'Archived caption')
        self.assertEqual(result[0]['created_at'],'2020-01-01T00:00:00Z')
        self.assertEqual(result[0]['media'][0]['urls'],[CDN])
        self.assertTrue(result[0]['url'].startswith('https://www.instagram.com/p/'))

    def test_other_author_mentions_and_recommendations_are_excluded(self):
        source=page({'items':[post('bob',caption='hello @alice'),post('alice','Other')]})
        self.assertEqual(extract(source,'https://www.instagram.com/p/AbC_123/',TS,'alice')[0],[])
        self.assertEqual(extract('<meta property="og:description" content="@alice: hello"><meta property="og:image" content="'+CDN+'">','https://instagram.com/p/AbC_123/',TS,'alice')[0],[])

    def test_matching_profile_owner_timeline_inherits_only_when_no_contradiction(self):
        owned=post();owned.pop('owner')
        result,_=extract(page({'graphql':{'user':{'username':'alice','edge_owner_to_timeline_media':{'edges':[{'node':owned},{'node':post('bob','Other')}]}}}}),'https://www.instagram.com/alice/',TS,'alice')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['shortcode'],'AbC_123')

    def test_carousel_images_video_and_signed_variants(self):
        obj=post(edge_sidecar_to_children={'edges':[{'node':{'display_url':CDN}},{'node':{'video_url':'https://video.xx.fbcdn.net/movie.mp4?one=1','display_url':CDN.replace('photo','cover')}}]})
        result,_=extract(page(obj),'https://instagram.com/p/AbC_123/',TS,'alice')
        self.assertEqual([m['kind'] for m in result[0]['media']],['image','image','video'])
        self.assertIsNone(media_item('https://cdninstagram.com.evil.test/a.jpg'))
        self.assertIsNone(media_item('http://127.0.0.1/a.jpg'))

    def test_json_ld_and_owner_metadata_recover_media(self):
        obj={'@type':'ImageObject','url':'https://www.instagram.com/p/AbC_123/','author':{'url':'https://instagram.com/alice/'},'image':CDN,'description':'Deleted photo','datePublished':'2019-03-04T12:00:00Z'}
        result,_=extract('<script type="application/ld+json">'+json.dumps(obj)+'</script>','https://www.instagram.com/p/AbC_123/',TS,'alice')
        self.assertEqual(result[0]['text'],'Deleted photo')
        source='<meta property="og:title" content="Alice (@alice) on Instagram: hello"><meta property="og:image" content="'+CDN+'">'
        result,_=extract(source,'https://www.instagram.com/p/AbC_123/',TS,'alice')
        self.assertEqual(len(result[0]['media']),1)
        self.assertEqual(result[0]['created_at'],'')  # never infer a Twitter snowflake date

    def test_post_links_validate_host_and_limit_and_normalize_reels(self):
        self.assertEqual(post_links('https://instagram.com/reel/AbC_123/\nhttps://www.instagram.com/p/AbC_123/'),['https://www.instagram.com/p/AbC_123/'])
        for value in ('https://evil.test/p/abc/','https://instagram.com@evil.test/p/abc/', '\n'.join('https://instagram.com/p/a/' for _ in range(21))):
            with self.assertRaises(ValueError):post_links(value)

    def test_archive_redirect_cannot_substitute_other_post_or_media(self):
        for a,b in [('https://instagram.com/p/A/','https://instagram.com/p/B/'),(CDN,CDN.replace('photo','other'))]:
            with self.assertRaises(ArchiveError):validate_replay_target(replay(TS,a),replay(TS,b))
        validate_replay_target(replay(TS,CDN),replay(TS,CDN.replace('one','two')))


class FakeArchive:
    def __init__(self,fail=False,stop=None):self.calls=[];self.fail=fail;self.stop=stop
    def check(self):
        if self.stop and self.stop.is_set():
            from archive import Cancelled
            raise Cancelled()
    def cdx_pages(self,url,match='exact',resume=''):
        self.check();self.calls.append(url)
        if self.fail:raise ArchiveError('Network unavailable')
        if '/alice' in url:
            yield [{'timestamp':TS,'original':'https://www.instagram.com/alice/','mimetype':'text/html','digest':'profile'}],''
        elif '/p/AbC_123/' in url:
            yield [{'timestamp':TS,'original':'https://www.instagram.com/p/AbC_123/','mimetype':'text/html','digest':'post'}],''
        elif 'cdninstagram.com' in url:
            yield [{'timestamp':TS,'original':CDN,'mimetype':'image/jpeg','digest':'media'}],''
        else:yield [],''
    def text(self,url):
        return page({'graphql':{'shortcode_media':post()}})
    def download(self,url,path,max_bytes):
        self.check();path=path.with_suffix('.jpg');path.parent.mkdir(parents=True,exist_ok=True)
        content=__import__('test_media_fixtures').PNG;path.write_bytes(content)
        return {'file':path,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest(),'archive_url':url}


class InstagramArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.key='instagram--alice'
    def tearDown(self):self.temp.cleanup()
    def run_collection(self,client=None,**options):
        c=InstagramCollector(self.key,{'_run_id':'test-run',**options},base=self.base,client=client or FakeArchive())
        status=c.run();manifest=json.loads((self.base/self.key/'manifest.json').read_text(encoding='utf-8'))
        return status,manifest
    def test_complete_pipeline_and_offline_zip_preserve_original_urls(self):
        status,m=self.run_collection()
        self.assertEqual(status,'complete');self.assertEqual(m['counts']['saved'],1)
        self.assertEqual(m['platform'],'instagram');self.assertEqual(m['display_username'],'alice')
        self.assertEqual(m['posts'][0]['url'],'https://www.instagram.com/p/AbC_123/')
        self.assertEqual(m['posts'][0]['platform'],'instagram')
        with zipfile.ZipFile(make_zip(self.base/self.key)) as z:
            self.assertIsNone(z.testzip());self.assertIn(self.key+'/index.html',z.namelist())
            self.assertEqual(json.loads(z.read(self.key+'/manifest.json'))['counts']['saved'],1)
    def test_new_pages_compress_and_resume_from_cache_without_network(self):
        import sqlite3
        import compressed_files as raw_files
        from backup_library import backup_collection,restore_backup,digest
        client=FakeArchive()
        content=page({'graphql':{'shortcode_media':post(caption='Archived caption '*200)}})
        client.text=lambda url:content
        status,m=self.run_collection(client)
        self.assertEqual(status,'complete')
        pages=list(raw_files.logical_files(self.base/self.key/'raw'))
        self.assertTrue(pages)
        for raw in pages:
            self.assertFalse(raw.exists());self.assertTrue(raw_files.packed_path(raw).is_file())
            self.assertEqual(raw_files.read_text(raw),content)
        db=sqlite3.connect(self.base/self.key/'archive.sqlite')
        try:
            db.execute("UPDATE captures SET status='pending'");db.commit()
        finally:db.close()
        client.text=lambda url:self.fail('Compressed pages must be reused without network')
        status,m=self.run_collection(client)
        self.assertEqual(status,'complete');self.assertEqual(m['counts']['saved'],1)
        with tempfile.TemporaryDirectory() as other:
            backup=Path(other)/'backups'
            index=Path(backup_collection(self.base,self.key,backup)['path'])
            snapshot=json.loads(index.read_bytes())
            restored=restore_backup(index,Path(other)/'restored')
            for relative,record in snapshot['files'].items():
                self.assertEqual(digest(restored/relative),record['sha256'])
    def test_corrupt_compressed_page_is_refetched_and_replaced(self):
        import sqlite3
        import compressed_files as raw_files
        client=FakeArchive();self.run_collection(client)
        raw=next(raw_files.logical_files(self.base/self.key/'raw'))
        raw_files.packed_path(raw).write_bytes(b'bad gzip');raw.unlink(missing_ok=True)
        db=sqlite3.connect(self.base/self.key/'archive.sqlite')
        try:
            db.execute("UPDATE captures SET status='pending'");db.commit()
        finally:db.close()
        calls=[];text=client.text
        client.text=lambda url:(calls.append(url),text(url))[1]
        status,m=self.run_collection(client)
        self.assertEqual(status,'complete');self.assertEqual(len(calls),1)
        self.assertTrue(raw_files.read_text(raw))
    def test_missing_caption_never_receives_snowflake_date(self):
        store=Store(self.base/self.key,self.key)
        try:
            p=blank_post('TestCode','alice');store.put_post(p)
            self.assertEqual(store.posts()[0]['created_at'],'');self.assertEqual(store.posts()[0]['url'],p['url'])
        finally:store.close()
    def test_source_failure_is_not_reported_as_empty_success(self):
        status,m=self.run_collection(FakeArchive(fail=True))
        self.assertEqual(status,'partial');self.assertEqual(m['counts']['posts'],0)
        self.assertTrue(m['errors']);self.assertEqual(m['task_summary']['reason'],'sources_unavailable')
    def test_login_only_capture_remains_unreadable_across_batches(self):
        client=FakeArchive();client.text=lambda url:'<html>Log in to Instagram</html>'
        status,m=self.run_collection(client)
        self.assertEqual(status,'partial');self.assertTrue(m['errors']);self.assertEqual(m['counts']['posts'],0)
        status,m=self.run_collection(client)
        self.assertTrue(m['errors']);self.assertEqual(status,'partial')
    def test_repair_does_not_query_profile_and_reuses_saved_media(self):
        self.run_collection();client=FakeArchive();status,m=self.run_collection(client,mode='repair')
        self.assertEqual(client.calls,[]);self.assertEqual(m['counts']['saved'],1)
    def test_media_first_batch_does_not_skip_new_run_profile_refresh(self):
        self.run_collection()
        self.run_collection(mode='repair',_run_id='new-run')
        client=FakeArchive();self.run_collection(client,_run_id='new-run')
        self.assertTrue(any('/alice' in url for url in client.calls))
    def test_pause_is_checkpointed_before_exit(self):
        event=threading.Event();event.set()
        status,m=self.run_collection(FakeArchive(stop=event))
        self.assertEqual(status,'paused');self.assertEqual(m['last_run']['batch']['stop_code'],'user_pause')
    def test_same_handle_two_platforms_queue_restart_and_delete_isolation(self):
        (self.base/'alice').mkdir();(self.base/'alice'/'keep.txt').write_text('X account')
        self.run_collection()
        q=TaskQueue(self.base/'.queue.json');q.add(['alice',self.key],{})
        loaded=TaskQueue(q.path);self.assertEqual([i['username'] for i in loaded.public()['items']],['alice',self.key])
        self.assertEqual(loaded.public()['items'][1]['platform'],'instagram')
        delete_collection(self.base,self.key)
        self.assertEqual((self.base/'alice'/'keep.txt').read_text(),'X account')
    def test_dispatch_only_instagram_to_new_collector(self):
        self.assertIs(app.collector_for(self.key),InstagramCollector)
        self.assertIs(app.collector_for('alice'),app.Collector)
        self.assertEqual(app.canonical_handle(self.base,self.key),self.key)
        self.assertEqual(app.identity_snapshot(self.base,self.key)['status'],'unsupported')


class InstagramApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.patches=[patch.object(app.TASKS, 'base',self.base),patch.object(app.TASKS, 'queue',None),patch.object(app.TASKS, 'stopping',threading.Event()),
                      patch.dict(app.TASKS.job,{'running':False,'username':'','status':'idle','stage':'idle','counts':{},'logs':[]},clear=True)]
        for p in self.patches:p.start()
        self.server=app.LocalServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def request(self,path,body=None):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        c.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),{'X-Local-Token':app.TOKEN,'Content-Type':'application/json'})
        r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result
    def test_start_routes_platform_and_rejects_mismatched_url(self):
        with patch.object(app.TASKS, 'start') as start:
            self.assertEqual(self.request('/api/start',{'platform':'instagram','username':'alice.name','instagram_links':'https://instagram.com/reel/ABC/'})[0],200)
            self.assertEqual(start.call_args.args[0],'instagram--alice.name')
            self.assertEqual(start.call_args.args[1]['instagram_links'],['https://www.instagram.com/p/ABC/'])
            start.reset_mock()
            self.assertEqual(self.request('/api/start',{'platform':'instagram','username':'https://x.com/alice'})[0],400)
            start.assert_not_called()
    def test_queue_deduplicates_per_platform_and_retains_both(self):
        app.TASKS.job.update(running=True,username='manual',stage='pages')
        for platform in ('x','instagram'):
            code,result=self.request('/api/queue/add',{'platform':platform,'usernames':'alice alice'})
            self.assertEqual(code,200);self.assertEqual((result['added'],result['duplicates']),(1,1))
        q=self.request('/api/state')[1]['queue']
        self.assertEqual([x['username'] for x in q['items']],['alice','instagram--alice'])
        app.TASKS.job['running']=False
    def test_collection_download_and_restore_use_platform_key(self):
        key='instagram--alice'
        c=InstagramCollector(key,base=self.base,client=FakeArchive());c.run();make_zip(self.base/key)
        code,m=self.request('/api/collection/'+key)
        self.assertEqual(code,200);self.assertTrue(m['zip_ready']);self.assertEqual(m['platform'],'instagram')
        self.assertEqual(self.request('/api/collections')[1][0]['display_username'],'alice')
        app.TASKS.job.update(username=key,status='partial',stage='done',running=False)
        app.TASKS.persist_job(force=True);app.TASKS.restore_job();self.assertEqual(app.TASKS.job['username'],key)
        with patch.object(app.TASKS, 'start') as start:
            self.assertEqual(self.request('/api/zip',{'username':key})[0],200)
            self.assertEqual(start.call_args.args[0],key)
    def test_unsupported_identity_does_not_write_x_catalog(self):
        self.assertEqual(self.request('/api/start',{'platform':'instagram','username':'alice','mode':'identity'})[0],400)
        self.assertEqual(self.request('/api/identity',{'username':'instagram--alice','alias':'bob'})[0],400)
        self.assertFalse((self.base/'.account-identities.sqlite').exists())


if __name__=='__main__':unittest.main()
