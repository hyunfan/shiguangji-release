import io
import hashlib
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from urllib.parse import urlsplit
from shiguangji.media.hls_recovery import parse_playlist, recover, run_tool, binary
from shiguangji.core.core_errors import ArchiveError

URL='https://video.twimg.com/ext_tw_video/123/pu/pl/video.m3u8'


class HlsTests(unittest.TestCase):
    def test_exact_video_mp4_fallback_precedes_hls_merge(self):
        from shiguangji.media.media_recovery import RecoveryEngine
        from shiguangji.platforms.extractor import blank_post,media_item
        with tempfile.TemporaryDirectory() as d:
            c=SimpleNamespace(folder=Path(d),stop=None,options={},log=lambda _:None,saved_file_valid=lambda _:False)
            good=media_item('https://video.twimg.com/ext_tw_video/123/pu/vid/720x720/good.mp4')
            wrong=media_item('https://video.twimg.com/ext_tw_video/999/pu/vid/720x720/wrong.mp4')
            engine=RecoveryEngine(c,http=Mock(),discover=lambda *a,**k:[wrong,good])
            p=blank_post('1','alice');m=media_item(URL);p['media']=[m]
            def direct(post,target,candidates):
                self.assertEqual([u for u,_ in candidates],good['urls'])
                target.update(status='saved',path='media/good.mp4');return True
            engine.direct=direct
            with patch('shiguangji.media.hls_recovery.recover',side_effect=AssertionError('MP4 already recovered')):
                engine.hls(p,m)
            self.assertEqual((m['kind'],m['status']),('video','saved'))

    def test_rejects_live_encrypted_foreign_and_truncated_playlists(self):
        for body in ['#EXTM3U\n#EXTINF:2,\na.ts',
                     '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n',
                     '#EXTM3U\n#EXTINF:2,\nhttps://127.0.0.1/private\n#EXT-X-ENDLIST',
                     '#EXTM3U\n#EXTINF:2,\n/ext_tw_video/999/part.ts\n#EXT-X-ENDLIST',
                     '#EXTM3U\n#EXTINF:2,\na.ts\n#EXTINF:3,\n#EXT-X-ENDLIST']:
            with self.subTest(body=body),self.assertRaises(ArchiveError):parse_playlist(body.encode(),URL)

    def test_master_selects_video_and_its_audio(self):
        body=b'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="a"\nlow.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="a"\nhigh.m3u8\n'
        self.assertEqual(parse_playlist(body,URL)['variants'],[URL.rsplit('/',1)[0]+'/high.m3u8',URL.rsplit('/',1)[0]+'/audio.m3u8'])

    def test_real_ts_and_fmp4_segments_merge_decode_and_reuse(self):
        try:ffmpeg=binary('ffmpeg');binary('ffprobe')
        except ArchiveError:self.skipTest('Optional video tools not installed')
        for segment_type in ('mpegts','fmp4'):
            with self.subTest(segment_type=segment_type),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);fixture=root/'fixture';fixture.mkdir()
                run_tool([ffmpeg,'-nostdin','-v','error','-f','lavfi','-i','testsrc2=size=64x64:rate=10','-f','lavfi','-i','sine=frequency=440:sample_rate=44100','-t','3','-c:v','libx264','-g','10','-c:a','aac','-hls_time','1','-hls_list_size','0','-hls_segment_type',segment_type,str(fixture/'video.m3u8')],lambda:None,cwd=fixture)
                calls=[];settings={};unavailable=set()
                class Transport:
                    def check(self):pass
                    def cdx(self,url):return []
                    def open(self,url):
                        calls.append(url)
                        if url in unavailable:raise OSError('segment missing')
                        data=(fixture/Path(urlsplit(url).path).name).read_bytes()
                        response=io.BytesIO(data);response.url=url;response.headers=Message();response.headers['Content-Length']=str(len(data));return response
                client=Transport()
                def setting(key,value=None):
                    if value is not None:settings[key]=value
                    return settings.get(key)
                c=SimpleNamespace(folder=root,client=client,setting=setting,log=lambda _:None)
                item={'urls':[URL]};dest=root/'media'/'test.bin'
                result=recover(c,client,{'id':'1'},item,dest,10*1024**2)
                self.assertTrue(result['hls']['decoded']);self.assertAlmostEqual(result['hls']['duration'],3,delta=.2)
                self.assertTrue(result['file'].is_file());count=len(calls)
                again=recover(c,client,{'id':'1'},item,dest,10*1024**2)
                self.assertEqual(len(calls),count);self.assertEqual(again['sha256'],result['sha256'])
                cache=root/'.hls-cache'/hashlib.sha256(URL.encode()).hexdigest()
                ledger_path=cache/'segments.json'
                for invalid in ([],None,{hashlib.sha256(URL.encode()).hexdigest():[]}):
                    with self.subTest(cache=invalid):
                        ledger_path.write_text(json.dumps(invalid),encoding='utf-8')
                        repaired=recover(c,client,{'id':'1'},item,dest,10*1024**2)
                        self.assertEqual(repaired['sha256'],result['sha256'])
                        self.assertTrue(repaired['hls']['decoded'])
                segment_url=next(u for u in calls if u.endswith(('.ts','.m4s')))
                cache=root/'.hls-cache'/hashlib.sha256(URL.encode()).hexdigest()
                stored=cache/(hashlib.sha256(segment_url.encode()).hexdigest()+'.bin')
                stored.write_bytes(b'corrupt cache')
                repaired=recover(c,client,{'id':'1'},item,dest,10*1024**2)
                self.assertEqual(repaired['sha256'],result['sha256']);self.assertGreater(len(calls),count)
                stored.write_bytes(b'corrupt cache');unavailable.add(segment_url)
                with self.assertRaises(ArchiveError):recover(c,client,{'id':'1'},item,root/'media'/'failed.bin',10*1024**2)
                self.assertEqual(list((root/'media').glob('failed*.mp4')),[])
