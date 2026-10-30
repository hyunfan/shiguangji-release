"""Characterization tests for Collector.download_media with a scripted archive.

Every download and index query is answered from a table, so the candidate
order, de-duplication, failure classification, and per-run failure reuse are
pinned down without any network access.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

from shiguangji.collect.archive import Collector
from shiguangji.core.archive_support import replay
from shiguangji.core.core_errors import ArchiveError
from shiguangji.platforms.extractor import blank_post, media_item
from test_media_fixtures import PNG

T1, T2, T3 = '20210301000000', '20210201000000', '20200101000000'


def image(name):
    return f'https://pbs.twimg.com/media/{name}.jpg'


def candidates(name, stamps=(T1, T2)):
    """Exact replays tried first: the first two URL forms, newest captures first."""
    return [replay(ts, url) for url in (image(name), image(name) + ':orig') for ts in stamps]


class ScriptedArchive:
    def __init__(self, downloads, indexes):
        self.downloads, self.indexes, self.calls = downloads, indexes, []

    def check(self):
        pass

    def cdx(self, url, match='exact'):
        self.calls.append(('cdx', url, match))
        rows = self.indexes.get(url, [])
        if isinstance(rows, Exception):
            raise rows
        yield rows

    def download(self, url, destination, max_bytes):
        self.calls.append(('download', url, max_bytes))
        outcome = self.downloads.get(url, ArchiveError('missing', status=404))
        if isinstance(outcome, Exception):
            raise outcome
        target = destination.with_suffix('.png')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(PNG)
        return {'file': target, 'bytes': len(PNG), 'sha256': hashlib.sha256(PNG).hexdigest(), 'archive_url': url}


class DownloadMediaFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def collect(self, posts, downloads, indexes, options=None):
        archive = ScriptedArchive(downloads, indexes)
        runner = Collector('alice', options or {}, base=self.base, client=archive)
        self.addCleanup(runner.store.close)
        self.addCleanup(runner.identities.close)
        for post in posts:
            runner.store.save_post(post)
        runner.download_media()
        saved = {p['id']: p for p in runner.store.posts()}
        return archive.calls, saved, runner

    def post(self, pid, *media, captures=(T2, T1)):
        p = blank_post(pid, 'alice')
        p['captures'] = [{'timestamp': ts, 'url': p['url']} for ts in captures]
        p['media'] = list(media)
        return p

    @staticmethod
    def state(m):
        return {k: m.get(k) for k in ('status', 'error_kind', 'error', 'archive_url', 'attempts', 'path')}

    def test_candidate_order_reuse_and_index_variant(self):
        shared = media_item(image('shared'))
        variant_only = media_item(image('later'))
        later_variant = 'https://pbs.twimg.com/media/later?format=jpg&name=large'
        posts = [self.post('1000', dict(shared)),
                 self.post('2000', dict(shared), dict(media_item(image('ref')), reference_only=True)),
                 self.post('3000', dict(variant_only))]
        downloads = {replay(T2, image('shared')): {}, replay(T3, later_variant): {}}
        indexes = {'pbs.twimg.com/media/later': [{'timestamp': T3, 'original': 'https://pbs.twimg.com/media/other.jpg'},
                                                 {'timestamp': T3, 'original': later_variant}]}
        calls, saved, runner = self.collect(posts, downloads, indexes, {'max_media_mb': 3})
        limit = 3 * 1024 * 1024
        self.assertEqual(calls, [('download', url, limit) for url in candidates('shared')[:2] + candidates('later')]
                         + [('cdx', 'pbs.twimg.com/media/later', 'prefix'), ('download', replay(T3, later_variant), limit)])
        first, second, third = saved['1000']['media'][0], saved['2000']['media'], saved['3000']['media'][0]
        self.assertEqual((first['status'], first['attempts'], first['archive_url']), ('saved', 2, replay(T2, image('shared'))))
        self.assertEqual(second[0]['path'], first['path'])
        self.assertEqual(second[0]['status'], 'saved')
        self.assertEqual(second[1], dict(media_item(image('ref')), reference_only=True))
        self.assertEqual((third['status'], third['attempts'], third['archive_url']), ('saved', 5, replay(T3, later_variant)))
        self.assertTrue(first['path'].startswith('media/unknown-date/1000_'), first['path'])
        self.assertEqual(runner.run_info['cached_missing'], 0)

    def test_failures_are_classified_and_reused_within_the_run(self):
        missing = media_item(image('missing'))
        flaky = media_item(image('flaky'))
        broken = media_item(image('broken'))
        hls = media_item('https://video.twimg.com/ext_tw_video/123/pu/pl/video.m3u8')
        posts = [self.post('1000', dict(missing), dict(flaky)),
                 self.post('2000', dict(missing), dict(broken), captures=(T1,)),
                 self.post('3000', dict(hls), dict(flaky))]
        downloads = {replay(T3, image('flaky')): ArchiveError('reset by peer'),
                     replay(T1, image('flaky')): ArchiveError('forbidden', status=403)}
        indexes = {'pbs.twimg.com/media/flaky': [{'timestamp': T1, 'original': image('flaky')},
                                                 {'timestamp': T3, 'original': image('flaky')}],
                   'pbs.twimg.com/media/broken': ArchiveError('index timeout')}
        calls, saved, _ = self.collect(posts, downloads, indexes)
        self.assertEqual([c[:2] for c in calls],
                         [('download', u) for u in candidates('missing')] + [('cdx', 'pbs.twimg.com/media/missing')]
                         + [('download', u) for u in candidates('flaky')] + [('cdx', 'pbs.twimg.com/media/flaky')]
                         + [('download', replay(T3, image('flaky')))]
                         + [('download', u) for u in candidates('broken', (T1,))] + [('cdx', 'pbs.twimg.com/media/broken')])
        missing1, flaky1 = saved['1000']['media']
        self.assertEqual(self.state(missing1), {
            'status': 'failed', 'error_kind': 'not_indexed', 'path': '', 'attempts': 4,
            'error': '原链接返回 404，当前索引也未找到同一媒体的其他尺寸存档。',
            'archive_url': replay(T1, image('missing'), raw=False)})
        self.assertEqual((flaky1['status'], flaky1['error_kind'], flaky1['error'], flaky1['attempts']),
                         ('failed', 'network', 'reset by peer', 5))
        missing2, broken2 = saved['2000']['media']
        self.assertEqual(self.state(missing2)['error_kind'], 'not_indexed')
        self.assertNotIn('attempts', missing2)
        self.assertTrue(missing2['checked_context'])
        self.assertEqual((broken2['status'], broken2['error_kind'], broken2['error'], broken2['archive_url']),
                         ('failed', 'network', 'index timeout', replay(T1, image('broken'), raw=False)))
        hls3, flaky3 = saved['3000']['media']
        self.assertEqual((hls3['status'], hls3['error_kind']), ('link_only', 'hls_pending'))
        self.assertEqual(hls3['archive_url'], replay(T2, hls['urls'][0], raw=False))
        self.assertEqual((flaky3['status'], flaky3['error'], flaky3['checked_at']), ('failed', 'reset by peer', flaky1['checked_at']))

    def test_saved_and_recently_checked_media_are_not_requested(self):
        saved_media = media_item(image('done'))
        missing = media_item(image('gone'))
        posts = [self.post('1000', dict(saved_media)), self.post('2000', dict(missing))]
        calls, _, _ = self.collect(posts, {replay(T1, image('done')): {}}, {})
        self.assertEqual([c[1] for c in calls], candidates('done')[:1] + candidates('gone') + ['pbs.twimg.com/media/gone'])
        again, _, runner = self.collect([], {}, {})
        self.assertEqual(again, [])
        self.assertEqual(runner.run_info['cached_missing'], 1)
        forced, _, _ = self.collect([], {}, {}, {'force_recheck': True})
        self.assertEqual([c[1] for c in forced], candidates('gone') + ['pbs.twimg.com/media/gone'])


if __name__ == '__main__':
    unittest.main()
