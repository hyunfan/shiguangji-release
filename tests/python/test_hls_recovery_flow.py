"""Characterization tests for hls_recovery.recover with fake transport and tools.

No ffmpeg, network, or real collection is used: the transport serves fixture
bytes, and run_tool/binary/file_valid are replaced so every branch of the
selection, download, and validation flow runs on any platform.
"""
import hashlib
import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shiguangji.core.core_errors import ArchiveError
from shiguangji.media import hls_recovery

BASE = 'https://video.twimg.com/ext_tw_video/123/pu/pl/'
MASTER = BASE + 'master.m3u8'
TS = b'\x47' + b'ts-segment' * 10
FMP4 = b'\x00\x00\x00\x18ftypiso5' + b'm4s' * 10
MEDIA = ('#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"\n'
         '#EXT-X-STREAM-INF:BANDWIDTH=100,AUDIO="a"\nlow.m3u8\n'
         '#EXT-X-STREAM-INF:BANDWIDTH=200,AUDIO="a"\nhigh.m3u8\n')


def media(*names, duration=2.0, initial=None):
    lines = ['#EXTM3U']
    if initial:
        lines.append(f'#EXT-X-MAP:URI="{initial}"')
    for name in names:
        lines += [f'#EXTINF:{duration},', name]
    return '\n'.join(lines + ['#EXT-X-ENDLIST']) + '\n'


class Transport:
    """Serve fixture bodies by exact URL; record every request in order."""

    def __init__(self, bodies, cdx=()):
        self.bodies, self.rows, self.calls = bodies, list(cdx), []
        self.retry_policy = Mock()

    def check(self):
        pass

    def cdx(self, url):
        self.calls.append(('cdx', url))
        return [self.rows]

    def open(self, url):
        self.calls.append(('open', url))
        body = self.bodies.get(url)
        if body is None or isinstance(body, Exception):
            raise body or OSError('missing')
        data = body.encode() if isinstance(body, str) else body
        response = io.BytesIO(data)
        response.url = url
        response.headers = Message()
        response.headers['Content-Length'] = str(len(data))
        return response


class Tools:
    """Stand-in for ffmpeg/ffprobe that writes a small MP4 and reports metadata."""

    def __init__(self, duration, video=True):
        self.duration, self.video, self.calls = duration, video, []

    def __call__(self, args, check, timeout=180, cwd=None):
        self.calls.append(list(args))
        check()
        if args[0] == 'ffprobe':
            streams = [{'codec_type': 'video'}] if self.video else [{'codec_type': 'audio'}]
            return json.dumps({'format': {'duration': str(self.duration)}, 'streams': streams}).encode()
        if args[-1] != '-':
            Path(args[-1]).write_bytes(b'\x00\x00\x00\x18ftypmp42merged')
        return b''


class RecoverFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = {}
        self.logs = []

    def collector(self, archive=None, budget=False):
        def setting(key, value=None):
            if value is not None:
                self.settings[key] = value
            return self.settings.get(key)
        c = SimpleNamespace(folder=self.root, client=archive or Transport({}), setting=setting, log=self.logs.append)
        if budget:
            c.budget = SimpleNamespace(last_saved=None)
        return c

    def run_recover(self, http, archive=None, *, duration=4.0, video=True, valid=True, urls=(MASTER,),
                    max_bytes=10 * 1024 ** 2, budget=False):
        tools = Tools(duration, video)
        c = self.collector(archive, budget)
        destination = self.root / 'media' / 'clip.bin'
        with patch.object(hls_recovery, 'binary', side_effect=lambda name: name), \
                patch.object(hls_recovery, 'run_tool', side_effect=tools), \
                patch.object(hls_recovery, 'file_valid', return_value=valid):
            try:
                result = hls_recovery.recover(c, http, {'id': '1'}, {'urls': list(urls)}, destination, max_bytes)
            finally:
                self.leftover = sorted(p.name for p in (self.root / 'media').glob('*')) if (self.root / 'media').exists() else []
        return result, tools, c

    def cache(self, anchor=MASTER):
        return self.root / '.hls-cache' / hashlib.sha256(anchor.encode()).hexdigest()

    def test_master_with_audio_merges_video_and_audio_tracks_from_current_public(self):
        http = Transport({MASTER: MEDIA, BASE + 'high.m3u8': media('v0.ts', 'v1.ts'),
                          BASE + 'audio.m3u8': media('a0.ts', 'a1.ts'),
                          BASE + 'v0.ts': TS, BASE + 'v1.ts': TS, BASE + 'a0.ts': TS, BASE + 'a1.ts': TS})
        result, tools, c = self.run_recover(http, budget=True)
        self.assertEqual([url for _, url in http.calls],
                         [MASTER, BASE + 'high.m3u8', BASE + 'v0.ts', BASE + 'v1.ts',
                          BASE + 'audio.m3u8', BASE + 'a0.ts', BASE + 'a1.ts'])
        hls = result['hls']
        self.assertEqual((hls['playlist'], hls['scope'], hls['segments'], hls['duration'], hls['decoded']),
                         (MASTER, 'current_public', 4, 4.0, True))
        self.assertEqual(hls['sources'], [BASE + n for n in ('v0.ts', 'v1.ts', 'a0.ts', 'a1.ts')])
        self.assertEqual(self.settings['hls_segments'], '7')
        self.assertIsNotNone(c.budget.last_saved)
        merge = tools.calls[0]
        self.assertEqual(merge.count('-i'), 2)
        self.assertEqual(merge[merge.index('-map') + 1:merge.index('-map') + 4], ['0:v:0', '-map', '1:a:0'])
        self.assertEqual([call[0] for call in tools.calls], ['ffmpeg', 'ffprobe', 'ffmpeg'])
        self.assertIn('-xerror', tools.calls[2])
        track = (self.cache() / 'track-0.m3u8').read_text(encoding='utf-8')
        self.assertEqual(track, '#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:600\n#EXT-X-MEDIA-SEQUENCE:0\n'
                                '#EXTINF:2.0,\npart-0-0.ts\n#EXTINF:2.0,\npart-0-1.ts\n#EXT-X-ENDLIST\n')
        digest = hashlib.sha256(b'\x00\x00\x00\x18ftypmp42merged').hexdigest()
        self.assertEqual(result['sha256'], digest)
        self.assertEqual(result['file'].name, 'clip_' + digest[:12] + '.mp4')
        self.assertEqual(self.leftover, [result['file'].name])
        self.assertTrue(any('2/2' in line for line in self.logs))

    def test_single_media_playlist_with_initial_map_uses_m4s_parts_and_optional_audio(self):
        url = BASE + 'video.m3u8'
        http = Transport({url: media('s0.m4s', initial='init.mp4'), BASE + 'init.mp4': FMP4, BASE + 's0.m4s': FMP4})
        result, tools, _ = self.run_recover(http, urls=(url,), duration=2.0)
        track = (self.cache(url) / 'track-0.m3u8').read_text(encoding='utf-8')
        self.assertIn('#EXT-X-MAP:URI="init-0.mp4"\n', track)
        self.assertIn('part-0-0.m4s', track)
        self.assertEqual((self.cache(url) / 'init-0.mp4').read_bytes(), FMP4)
        self.assertEqual(result['hls']['segments'], 2)
        self.assertIn('0:a?', tools.calls[0])
        self.assertEqual([u for _, u in http.calls], [url, BASE + 'init.mp4', BASE + 's0.m4s'])

    def test_cached_segments_are_reused_on_a_second_run(self):
        url = BASE + 'video.m3u8'
        http = Transport({url: media('s0.ts'), BASE + 's0.ts': TS})
        first, _, _ = self.run_recover(http, urls=(url,), duration=2.0)
        count = len(http.calls)
        second, _, _ = self.run_recover(http, urls=(url,), duration=2.0)
        self.assertEqual(len(http.calls), count)
        self.assertEqual(second['sha256'], first['sha256'])
        self.assertEqual(self.settings['hls_segments'], '2')

    def test_wayback_playlist_is_used_when_current_public_copies_fail(self):
        stamp = '20200102030405'
        replay = lambda u: f'https://web.archive.org/web/{stamp}id_/{u}'
        url = BASE + 'video.m3u8'
        http = Transport({})
        archive = Transport({replay(url): media('s0.ts', 's1.ts'), replay(BASE + 's0.ts'): TS, replay(BASE + 's1.ts'): TS},
                            cdx=[{'timestamp': stamp}])
        result, _, _ = self.run_recover(http, archive, urls=(url, url, BASE + 'other.m3u8', BASE + 'x.mp4'))
        self.assertEqual([u for _, u in http.calls], [url, BASE + 'other.m3u8'])
        self.assertEqual(http.retry_policy.failure.call_count, 2)
        self.assertEqual(archive.calls, [('cdx', url), ('open', replay(url)), ('open', replay(BASE + 's0.ts')),
                                         ('open', replay(BASE + 's1.ts'))])
        self.assertEqual((result['hls']['scope'], result['hls']['playlist']), ('wayback', url))
        self.assertEqual(result['hls']['sources'], [replay(BASE + 's0.ts'), replay(BASE + 's1.ts')])

    def test_last_error_is_raised_when_no_copy_is_readable(self):
        url = BASE + 'video.m3u8'
        archive = Transport({}, cdx=[{'timestamp': '20200102030405'}])
        with self.assertRaises(ArchiveError) as raised:
            self.run_recover(Transport({}), archive, urls=(url,))
        self.assertEqual(raised.exception.reason, 'network')
        with self.assertRaises(ArchiveError) as raised:
            self.run_recover(Transport({url: '#EXTM3U\n#EXTINF:2,\ns.ts\n'}), Transport({}), urls=(url,))
        self.assertEqual(raised.exception.reason, 'hls_unsupported')

    def test_rejections_keep_no_published_file(self):
        url = BASE + 'video.m3u8'
        cases = [
            ('hls_unsupported', dict(urls=(BASE + 'clip.mp4',)), {}),
            ('invalid_media', {}, {BASE + 's0.ts': b'<html>not video</html>'}),
            ('invalid_media', {}, {BASE + 's0.ts': b''}),
            ('too_large', dict(max_bytes=len(TS) + 1), {}),
            ('hls_validation', dict(duration=9.0), {}),
            ('hls_validation', dict(video=False), {}),
            ('hls_validation', dict(valid=False), {}),
            ('hls_unsupported', {}, {BASE + 'video.m3u8': MEDIA.replace('high.m3u8', 'video.m3u8').replace('low.m3u8', 'video.m3u8')}),
        ]
        for reason, options, overrides in cases:
            with self.subTest(reason=reason, options=options, overrides=list(overrides)):
                self.setUp()
                bodies = {url: media('s0.ts', 's1.ts'), BASE + 's0.ts': TS, BASE + 's1.ts': TS, **overrides}
                with self.assertRaises(ArchiveError) as raised:
                    self.run_recover(Transport(bodies), urls=options.pop('urls', (url,)), **options)
                self.assertEqual(raised.exception.reason, reason)
                self.assertFalse([name for name in self.leftover if name.endswith('.mp4')])


if __name__ == '__main__':
    unittest.main()
