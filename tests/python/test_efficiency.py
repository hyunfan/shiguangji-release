"""Retry policy regressions: real recovery must remain possible after empty lookups."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from shiguangji.collect.archive import ArchiveError, Collector, Store, make_zip, zip_is_current
from test_archive import FixtureClient, HTML, IMAGE, TS


class RetryPolicyTests(unittest.TestCase):
    def test_mixed_network_and_absent_candidates_do_not_create_negative_cache(self):
        calls=[]
        def mixed(url,*args):
            calls.append(url)
            if len(calls)==1:raise ArchiveError('temporary timeout')
            raise ArchiveError('not found',status=404)
        self.client.download=mixed
        self.run_collection()
        post=Store(self.folder,'alice')
        try:self.assertEqual(post.posts()[0]['media'][0]['error_kind'],'network')
        finally:post.close()
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.folder = self.base / 'alice'
        self.client = FixtureClient()
        self.calls = []
        self.lookup = self.client.cdx
        self.fetch = self.client.download
        def cdx(url, match='exact'):
            self.calls.append(('cdx', url))
            yield from self.lookup(url, match)
        def absent(url, *args):
            self.calls.append(('download', url))
            raise ArchiveError('HTTP Error 404: NOT FOUND', status=404)
        self.client.cdx = cdx
        self.client.download = absent

    def tearDown(self):
        self.temp.cleanup()

    def run_collection(self, **options):
        status = Collector('alice', options, base=self.base, client=self.client).run()
        manifest = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        return status, manifest

    def edit_post(self, edit):
        store = Store(self.folder, 'alice')
        p = store.posts()[0]
        edit(p)
        store.save_post(p)
        store.close()

    def test_fresh_empty_media_and_video_lookups_make_no_requests(self):
        self.client.text = lambda url: HTML.replace(IMAGE, 'https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/thumb.jpg')
        _, first = self.run_collection()
        self.assertEqual(first['counts']['not_indexed'], 2)
        self.calls.clear()
        status, second = self.run_collection(mode='repair')
        self.assertEqual(status, 'partial')
        self.assertEqual(self.calls, [])
        self.assertEqual(second['last_run']['cached_missing'], 2)
        self.assertEqual(second['posts'], first['posts'])

    def test_force_recheck_can_recover_newly_available_media(self):
        self.run_collection()
        self.client.download = self.fetch
        status, m = self.run_collection(mode='repair', force_recheck=True)
        self.assertEqual(status, 'complete')
        self.assertEqual(m['counts']['saved'], 1)
        self.assertEqual(m['last_run']['cached_missing'], 0)

    def test_expired_and_invalid_check_times_are_retried(self):
        self.run_collection()
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for checked in (stale, 'not a date', future, '2026-09-11T00:00:00'):
            with self.subTest(checked=checked):
                self.edit_post(lambda p: p['media'][0].update(checked_at=checked))
                self.calls.clear()
                self.run_collection(mode='repair')
                self.assertTrue(any(kind == 'download' for kind, _ in self.calls))

    def test_new_capture_invalidates_recent_empty_lookup(self):
        self.run_collection()
        self.edit_post(lambda p: p['captures'].append({**p['captures'][0], 'timestamp': '20260910000000'}))
        self.client.download = self.fetch
        self.assertEqual(self.run_collection(mode='repair')[0], 'complete')

    def test_network_failures_are_never_negative_cached(self):
        def failure(*args):
            raise ArchiveError('connection timed out')
        self.client.download = failure
        self.run_collection()
        self.client.download = self.fetch
        self.assertEqual(self.run_collection(mode='repair')[0], 'complete')

    def test_truncated_saved_file_is_downloaded_again(self):
        self.client.download = self.fetch
        _, first = self.run_collection()
        path = self.folder / first['posts'][0]['media'][0]['path']
        original = path.read_bytes()
        path.write_bytes(original[:4])
        self.assertEqual(self.run_collection(mode='repair')[0], 'complete')
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.client.downloads, 2)

    def test_hls_only_index_is_not_reported_as_an_empty_index(self):
        self.client.text = lambda url: HTML.replace(IMAGE, 'https://pbs.twimg.com/ext_tw_video_thumb/123/pu/img/thumb.jpg')
        self.client.download = self.fetch
        lookup = self.lookup
        def hls(url, match='exact'):
            if url == 'video.twimg.com/ext_tw_video/123/':
                yield [{'timestamp': TS, 'original': 'https://video.twimg.com/ext_tw_video/123/pu/pl/a.m3u8', 'digest': 'H', 'mimetype': 'application/x-mpegURL', 'statuscode': '200'}]
            else:
                yield from lookup(url, match)
        self.lookup = hls
        _, m = self.run_collection()
        missing = [v for v in m['posts'][0]['media'] if v['status'] != 'saved']
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]['error_kind'], 'hls_pending')
        self.assertEqual(m['counts']['not_indexed'], 0)

    def test_zip_invalidates_when_manifest_or_zip_changes(self):
        self.run_collection()
        zip_path = make_zip(self.folder)
        self.assertTrue(zip_is_current(self.folder))
        with (self.folder / 'manifest.json').open('a', encoding='utf-8') as f:
            f.write('\n')
        self.assertFalse(zip_is_current(self.folder))
        make_zip(self.folder)
        self.assertTrue(zip_is_current(self.folder))
        zip_path.write_bytes(b'broken')
        self.assertFalse(zip_is_current(self.folder))


if __name__ == '__main__':
    unittest.main()
