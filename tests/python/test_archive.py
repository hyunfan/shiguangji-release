"""Deterministic fixtures, not claims of recovered real accounts. python -m unittest -v"""
import io
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from email.message import Message
from urllib.parse import parse_qs, urlsplit
from shiguangji.collect.archive import Client, ArchiveError, Collector, Store, parse_cdx, media_extension, make_zip, replay, export_collection, validate_replay_target, archive_only
from shiguangji.platforms.extractor import extract, username, media_item, snowflake_date, blank_post, merge_post

PID = '1346889436626259968'
URL = f'https://twitter.com/alice/status/{PID}'
TS = '20210201000000'
IMAGE = 'https://pbs.twimg.com/media/fixture.jpg'
from test_media_fixtures import PNG
HTML = f'''<html><body><article data-tweet-id="{PID}" data-screen-name="Alice">
<a href="/alice/status/{PID}">date</a><p class="tweet-text">测试 &amp; 帖子<br>第二行</p>
<time datetime="2021-01-06T20:00:00Z"></time><img src="{IMAGE}">
<div class="QuoteTweet"><p class="tweet-text">wrong quote</p><img src="https://pbs.twimg.com/media/quoted.jpg"></div>
</article><article data-tweet-id="12345" data-screen-name="bob"><a href="/bob/status/12345">reply</a><p class="tweet-text">wrong author</p><img src="https://pbs.twimg.com/media/bob.jpg"></article></body></html>'''

class ExtractTests(unittest.TestCase):
    def test_handle_inputs(self):
        for value in ('@Alice','https://x.com/Alice','https://web.archive.org/web/20200101000000/https://twitter.com/Alice'):
            self.assertEqual(username(value),'alice')
        for value in ('../bad','https://evil.test/alice','name-with-dash',''):
            with self.assertRaises(ValueError):username(value)
    def test_legacy_container_and_owner(self):
        posts=extract(HTML,URL,TS,'alice');self.assertEqual(len(posts),1)
        p=posts[0];self.assertEqual(p['text'],'测试 & 帖子\n第二行');self.assertEqual(p['created_at'],'2021-01-06T20:00:00Z')
        self.assertEqual([m['key'] for m in p['media']],['pbs.twimg.com/media/fixture.jpg'])
    def test_profile_links_do_not_steal_media(self):
        src=f'<a href="{URL}">Post</a><img src="{IMAGE}"><meta property="og:description" content="bio">'
        p=extract(src,'https://twitter.com/alice',TS,'alice')[0]
        self.assertEqual(p['media'],[]);self.assertEqual(p['text'],'')
    def test_replies_with_target_quote_are_not_target_article(self):
        src=f'<article><a href="/bob/status/90">bob</a><div data-testid="tweetText">bob text</div><a href="{URL}">alice quote</a><img src="{IMAGE}"></article>'
        p=extract(src,'https://twitter.com/alice',TS,'alice')[0]
        self.assertEqual(p['media'],[]);self.assertEqual(p['text'],'')
    def test_old_id_unknown_date(self):
        self.assertEqual(snowflake_date('20'),'');self.assertEqual(blank_post('20','alice')['date_source'],'unknown')
    def test_metadata_for_single_post(self):
        p=extract(f'<meta property="og:description" content="hello &amp; world"><meta property="og:image" content="{IMAGE}">',URL,TS,'alice')[0]
        self.assertEqual(p['text'],'hello & world');self.assertEqual(len(p['media']),1)
    def test_graphql_json_handles_nulls_and_video(self):
        data={'items':[{'core':None,'legacy':None},{'rest_id':PID,'legacy':{'full_text':'movie','created_at':'Wed Jan 06 20:00:00 +0000 2021','extended_entities':{'media':[{'media_url_https':IMAGE,'video_info':{'variants':[{'url':'https://video.twimg.com/ext_tw_video/123/pu/vid/640x360/test.mp4','bitrate':1000},{'url':'https://video.twimg.com/ext_tw_video/123/pu/pl/test.m3u8'}]}}]}},'core':{'user_results':{'result':{'legacy':{'screen_name':'alice'}}}}}]}
        p=extract('<script>window.__INITIAL_STATE__ = '+json.dumps(data)+';</script>',URL,TS,'alice')[0]
        self.assertEqual(p['text'],'movie');self.assertEqual({m['kind'] for m in p['media']},{'image','video','hls'})
    def test_normalized_entity_user_lookup(self):
        data={'users':{'1':{'id_str':'1','screen_name':'Alice'}},'tweets':{'a':{'id_str':PID,'user_id_str':'1','text':'normalized'}}}
        self.assertEqual(extract(json.dumps(data),URL,TS,'alice')[0]['text'],'normalized')
    def test_json_ld(self):
        data={'@type':'SocialMediaPosting','url':URL,'author':{'url':'https://twitter.com/alice'},'articleBody':'linked data','datePublished':'2021-01-06T20:00:00Z','image':{'contentUrl':IMAGE}}
        p=extract('<script type="application/ld+json">'+json.dumps(data)+'</script>',URL,TS,'alice')[0]
        self.assertEqual(p['text'],'linked data');self.assertEqual(p['date_source'],'page')
    def test_media_identity_and_rejected_hosts(self):
        self.assertEqual(media_item('https://pbs.twimg.com/media/ABC?format=jpg&name=small')['key'],media_item('https://pbs.twimg.com/media/ABC.jpg:orig')['key'])
        for u in ('https://evil.test/media/abc.jpg','file:///media/foo','https://pbs.twimg.com/profile_images/1/a.jpg'):
            self.assertIsNone(media_item(u))
    def test_merge_versions_and_media_resume(self):
        a=blank_post(PID,'alice');a['text']='old';a['media']=[media_item(IMAGE)];a['media'][0].update(status='saved',path='media/a.jpg')
        b=blank_post(PID,'alice');b['text']='new longer';b['media']=[media_item(IMAGE)]
        result=merge_post(a,b);self.assertEqual(result['text'],'new longer');self.assertIn('old',result['text_versions']);self.assertEqual(result['media'][0]['status'],'saved')

class FakeResponse(io.BytesIO):
    def __init__(self, data, url, mime='image/png'):
        super().__init__(data);self.url=url;self.headers=Message();self.headers['Content-Type']=mime;self.headers['Content-Length']=str(len(data))

class NetworkTests(unittest.TestCase):
    def test_cdx_resume_decoding_and_pagination(self):
        client=Client(delay=0);bodies=[f'{TS} {URL} text/html 200 A\n\ncom%2Ctwitter%29%2Falice+20210201000000%21\n',f'20210301000000 {URL} text/html 200 B\n'];called=[]
        def fake(url):called.append(url);return bodies.pop(0)
        client.text=fake
        rows=list(client.cdx('twitter.com/alice','prefix'))
        self.assertEqual(len(rows),2)
        self.assertEqual(parse_qs(urlsplit(called[1]).query)['resumeKey'][0],'com,twitter)/alice 20210201000000!')
    def test_cdx_repeated_resume_fails(self):
        client=Client(delay=0);client.text=lambda u:f'{TS} {URL} text/html 200 A\n\ncom%2Ctwitter%29%2Falice+20210201000000%21\n'
        with self.assertRaises(ArchiveError):list(client.cdx('twitter.com/alice'))
    def test_error_html_rejected(self):
        with self.assertRaises(ArchiveError):parse_cdx('<html>Wayback error</html>')
        self.assertEqual(media_extension(b'<html>not image</html>','image/jpeg'),'')
    def test_redirect_safety(self):
        for u in ('https://pbs.twimg.com/media/a.jpg','http://127.0.0.1/','https://web.archive.org.evil.test/'):
            with self.assertRaises(ArchiveError):archive_only(u)
        with self.assertRaises(ArchiveError):validate_replay_target(replay(TS,URL),replay(TS,'https://twitter.com/login'))
        validate_replay_target(replay(TS,URL),replay('20210301000000',URL.replace('twitter.com','x.com')))
    def test_download_is_atomic_and_size_limited(self):
        client=Client(delay=0);client.open=lambda u:FakeResponse(PNG,u)
        with tempfile.TemporaryDirectory() as d:
            dest=Path(d)/'a.bin';r=client.download(replay(TS,IMAGE),dest,1000)
            self.assertEqual(r['file'].read_bytes(),PNG);self.assertEqual(r['file'].suffix,'.png')
            self.assertFalse(dest.with_suffix('.bin.part').exists())
            with self.assertRaises(ArchiveError):client.download(replay(TS,IMAGE),dest,2)
            self.assertFalse(dest.with_suffix('.bin.part').exists())
    def test_html_not_saved_as_media(self):
        client=Client(delay=0);client.open=lambda u:FakeResponse(b'<html>not found</html>',u,'text/html')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ArchiveError):client.download(replay(TS,IMAGE),Path(d)/'a.bin',1000)
            self.assertFalse(list(Path(d).iterdir()))

class FixtureClient:
    def __init__(self):self.page_reads=0;self.downloads=0
    def check(self):pass
    def cdx(self,url,match='exact'):
        if url=='twitter.com/alice':yield [{'timestamp':TS,'original':URL,'mimetype':'text/html','statuscode':'200','digest':'A'}]
        else:yield []
    def text(self,url):self.page_reads+=1;return HTML
    def download(self,url,dest,max_bytes):
        self.downloads+=1;dest=dest.with_suffix('.png');dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(PNG)
        return {'file':dest,'bytes':len(PNG),'sha256':__import__('hashlib').sha256(PNG).hexdigest(),'archive_url':url}

class IntegrationTests(unittest.TestCase):
    def test_full_pipeline_checkpoint_resume_offline_zip(self):
        with tempfile.TemporaryDirectory() as d:
            client=FixtureClient();runner=Collector('alice',base=Path(d),client=client)
            self.assertEqual(runner.run(),'complete');folder=Path(d)/'alice'
            manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['saved'],1);self.assertEqual(manifest['counts']['posts'],1)
            self.assertTrue((folder/manifest['posts'][0]['media'][0]['path']).is_file())
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'complete')
            self.assertEqual(client.page_reads,1);self.assertEqual(client.downloads,1)
            packed=make_zip(folder)
            with zipfile.ZipFile(packed) as z:
                self.assertIn('alice/index.html',z.namelist());self.assertIn('alice/data.js',z.namelist());self.assertIn('alice/posts/2021/01/'+PID+'.txt',z.namelist());self.assertFalse(any(n.endswith('.sqlite') for n in z.namelist()))
                self.assertEqual(z.testzip(),None)
    def test_network_failure_is_not_success_or_empty_account(self):
        client=FixtureClient()
        def failure(*a,**k):raise ArchiveError('test timeout')
        client.cdx=failure
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'failed')
            m=json.loads((Path(d)/'alice'/'manifest.json').read_text(encoding='utf-8'));self.assertEqual(len(m['errors']),6)
    def test_failed_media_can_retry(self):
        client=FixtureClient();real=client.download
        def failure(*a,**k):raise ArchiveError('not archived')
        client.download=failure
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'partial')
            client.download=real
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'complete')
            self.assertEqual(client.page_reads,1)
    def test_cancel_exports_readable_partial_collection(self):
        stop=threading.Event();stop.set()
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Collector('alice',stop=stop,base=Path(d)).run(),'paused')
            self.assertTrue((Path(d)/'alice'/'index.html').exists())
    def test_export_text_is_not_executed(self):
        with tempfile.TemporaryDirectory() as d:
            store=Store(d,'alice');p=blank_post(PID,'alice');p['text']='</script><script>alert(1)</script>';store.put_post(p);export_collection(store,'complete',[]);store.close()
            self.assertNotIn('</script>',(Path(d)/'data.js').read_text(encoding='utf-8'))
    def test_video_thumbnail_discovers_archived_mp4(self):
        with tempfile.TemporaryDirectory() as d:
            client=FixtureClient();original_cdx=client.cdx
            mp4='https://video.twimg.com/ext_tw_video/123/pu/vid/test.mp4'
            def cdx(url,match='exact'):
                if url=='video.twimg.com/ext_tw_video/123/':yield [{'timestamp':TS,'original':mp4,'mimetype':'video/mp4','statuscode':'200','digest':'V'}]
                else:yield from original_cdx(url,match)
            client.cdx=cdx
            client.text=lambda url:HTML.replace(IMAGE,'https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/thumb.jpg')
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'complete')
            m=json.loads((Path(d)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertTrue(any(v['kind']=='video' and v['status']=='saved' for v in m['posts'][0]['media']))
    def test_missing_video_is_reported_even_if_thumbnail_saved(self):
        with tempfile.TemporaryDirectory() as d:
            client=FixtureClient();client.text=lambda url:HTML.replace(IMAGE,'https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/thumb.jpg')
            self.assertEqual(Collector('alice',base=Path(d),client=client).run(),'partial')
            m=json.loads((Path(d)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(m['counts']['missing'],1)

if __name__=='__main__':unittest.main()
