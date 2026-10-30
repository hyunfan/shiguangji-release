"""Regressions discovered in the user's actual 2026-09-11 archive run."""
import gzip
import hashlib
import json
import tempfile
import unittest
import zlib
from email.message import Message
from pathlib import Path
from shiguangji.collect.archive import ArchiveError, Client, Collector, decode_page, corrupt_page, media_search_prefix
from shiguangji.platforms.extractor import extract, media_item
from test_archive import FixtureClient, FakeResponse, URL, TS, HTML, IMAGE, PNG

class RecoveryTests(unittest.TestCase):
    def headers(self, encoding='gzip'):
        h=Message();h['Content-Type']='text/html; charset=utf-8';h['Content-Encoding']=encoding;return h
    def test_gzip_plain_and_concatenated_members(self):
        data='归档正文 <article>hello</article>'.encode()
        self.assertEqual(decode_page(gzip.compress(data),self.headers()),data.decode())
        self.assertEqual(decode_page(gzip.compress(b'one')+gzip.compress(b'two'),self.headers()),'onetwo')
        self.assertEqual(decode_page(data,self.headers()),data.decode())
        self.assertEqual(decode_page(zlib.compress(data),self.headers('deflate')),data.decode())
    def test_corrupt_or_oversized_compressed_response_is_not_marked_read(self):
        with self.assertRaises(ArchiveError):decode_page(gzip.compress(b'a'*1000),self.headers(),100)
        with self.assertRaises(ArchiveError):decode_page(gzip.compress(b'hello')[:-5],self.headers())
        self.assertTrue(corrupt_page('\ufffd'*1000))
    def test_gzip_media_is_verified_after_decompression(self):
        client=Client(delay=0)
        def response(url):
            r=FakeResponse(gzip.compress(PNG),url);r.headers['Content-Encoding']='gzip';return r
        client.open=response
        with tempfile.TemporaryDirectory() as directory:
            result=client.download('https://web.archive.org/web/'+TS+'id_/'+IMAGE,Path(directory)/'a.bin',1000)
            self.assertEqual(result['file'].read_bytes(),PNG)
    def test_thumbnail_query_style_identity_and_prefix(self):
        legacy=media_item('https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/ABC.jpg:large')
        modern=media_item('https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/ABC?format=jpg&name=small')
        self.assertEqual(legacy['key'],modern['key'])
        self.assertEqual(media_search_prefix(legacy),'pbs.twimg.com/ext_tw_video_thumb/123/pu/img/ABC')
        self.assertTrue(any('?format=jpg&name=orig' in u for u in legacy['urls']))
    def test_repair_mode_refetches_damaged_cache_without_reindex(self):
        with tempfile.TemporaryDirectory() as directory:
            client=FixtureClient();collector=Collector('alice',base=Path(directory),client=client)
            collector.run()
            folder=Path(directory)/'alice'
            raw=folder/'raw'/(hashlib.sha256((TS+URL).encode()).hexdigest()[:24]+'.txt')
            raw.write_text('\ufffd'*2000,encoding='utf-8')
            client.cdx=lambda *a,**k: (_ for _ in ()).throw(AssertionError('Repair must not rescan account index'))
            before_downloads=client.downloads
            self.assertEqual(Collector('alice',{'mode':'repair'},base=Path(directory),client=client).run(),'complete')
            self.assertEqual(client.page_reads,2)
            self.assertEqual(client.downloads,before_downloads)
            from shiguangji.storage.compressed_files import read_text
            self.assertEqual(read_text(raw),HTML)
    def test_successful_page_retry_clears_old_failure(self):
        client=FixtureClient();read=client.text;calls=[]
        def temporary_failure(url):
            calls.append(url)
            if len(calls)==1:raise ArchiveError('HTTP Error 404: NOT FOUND', status=404)
            return read(url)
        client.text=temporary_failure
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(Collector('alice',base=Path(directory),client=client).run(),'complete')
            m=json.loads((Path(directory)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(m['errors'],[])
    def test_access_notice_is_reported_without_false_author_content(self):
        src='<html><body>Age-restricted adult content. You’ll need to log in.</body></html>'
        p=extract(src,URL,TS,'alice')[0]
        self.assertEqual(p['text'],'');self.assertEqual(p['media'],[])
        self.assertEqual(p['content_reason'],'restricted')
    def test_media_fallback_finds_query_style_capture(self):
        client=FixtureClient();lookup=client.cdx;download=client.download
        modern='https://pbs.twimg.com/media/fixture?format=jpg&name=large'
        searched=[]
        def cdx(url,match='exact'):
            searched.append(url)
            if url=='pbs.twimg.com/media/fixture':
                yield [{'timestamp':TS,'original':modern,'mimetype':'image/jpeg','statuscode':'200','digest':'IMAGE'}]
            else:
                yield from lookup(url,match)
        def fetch(url,dest,max_bytes):
            if modern not in url:
                raise ArchiveError('HTTP Error 404: NOT FOUND', status=404)
            return download(url,dest,max_bytes)
        client.cdx=cdx;client.download=fetch
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(Collector('alice',base=Path(directory),client=client).run(),'complete')
            self.assertIn('pbs.twimg.com/media/fixture',searched)
            manifest=json.loads((Path(directory)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['saved'],1)
            self.assertIn(modern,manifest['posts'][0]['media'][0]['archive_url'])

if __name__=='__main__':unittest.main()
