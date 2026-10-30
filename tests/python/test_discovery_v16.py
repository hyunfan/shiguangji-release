"""Regressions for actual WARC resource omissions, using synthetic posts."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from shiguangji.collect.archive import Client, Collector, Store, INDEX_POLICY_VERSION
from test_archive import FixtureClient, TS, URL


class DiscoveryTests(unittest.TestCase):
    def test_cdx_includes_resource_records_and_keeps_resume(self):
        client, urls = Client(delay=0), []
        bodies = [f'{TS} {URL} application/json - D\n\nnext-key\n', '']
        def read(url):
            urls.append(url)
            return bodies.pop(0)
        client.text = read
        pages = list(client.cdx_pages('twitter.com/alice', 'prefix'))
        self.assertEqual(pages[0][0][0]['statuscode'], '-')
        for url in urls:
            self.assertEqual(parse_qs(urlsplit(url).query)['filter'], ['statuscode:(200|-)'])
        self.assertEqual(parse_qs(urlsplit(urls[1]).query)['resumeKey'], ['next-key'])

    def test_old_filtered_cursor_invalidated_without_losing_posts(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder, 'alice')
            row = dict(timestamp=TS, original=URL, mimetype='text/html', digest='D')
            store.add_captures([row], index_url='twitter.com/alice', next_key='old-key')
            store.db.execute("DELETE FROM settings WHERE key='index_policy'")
            store.db.commit(); store.close()
            store = Store(folder, 'alice')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM index_progress').fetchone()[0], 0)
            self.assertEqual(len(store.posts()), 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
            store.add_captures([], index_url='twitter.com/alice', next_key='new-key')
            store.close()
            store = Store(folder, 'alice')
            self.assertEqual(store.db.execute('SELECT resume_key FROM index_progress').fetchone()[0], 'new-key')
            self.assertEqual(store.db.execute("SELECT value FROM settings WHERE key='index_policy'").fetchone()[0], INDEX_POLICY_VERSION)
            store.close()

    def test_discover_recovers_body_without_media_or_neighbor_accounts(self):
        client = FixtureClient()
        def cdx(url, match='exact'):
            if url != 'twitter.com/alice':
                yield []; return
            yield [dict(timestamp=TS, original=URL, mimetype='application/json', statuscode='-', digest='D'),
                   dict(timestamp=TS, original=URL.replace('/alice/', '/alicea/'), mimetype='application/json', statuscode='-', digest='N')]
        client.cdx = cdx
        with tempfile.TemporaryDirectory() as folder, patch('shiguangji.media.media_recovery.RecoveryEngine.run') as rescue:
            runner = Collector('alice', {'mode': 'discover', 'multi_source': True}, base=Path(folder), client=client)
            self.assertEqual(runner.run(), 'partial')
            manifest = json.loads((Path(folder)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['posts'], 1)
            self.assertEqual(manifest['counts']['text'], 1)
            self.assertEqual(manifest['counts']['captures'], 1)
            self.assertEqual(client.downloads, 0)
            rescue.assert_not_called()
            stats = manifest['last_run']['discovery']
            self.assertEqual((stats['new_posts'], stats['new_text'], stats['resource_captures']), (1, 1, 1))
            self.assertEqual((stats['queried_hosts'], stats['failed_hosts']), (6, 0))

    def test_empty_rescue_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = Collector('alice', {'mode': 'rescue'}, base=Path(folder), client=FixtureClient())
            self.assertEqual(runner.run(), 'failed')
            manifest = json.loads((Path(folder)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertIn('扩展查找帖子', manifest['errors'][0]['error'])

    def test_prefix_neighbor_suggested_without_importing_its_posts(self):
        client = FixtureClient()
        client.cdx = lambda url, match='exact': iter([[dict(timestamp=TS, original=URL.replace('/alice/', '/alicea/'),
                                                          mimetype='application/json', statuscode='-', digest='N')]
                                                     if url == 'twitter.com/alice' else []])
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(Collector('alice', {'mode':'discover'}, base=Path(folder), client=client).run(), 'complete')
            manifest = json.loads((Path(folder)/'alice/manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['counts']['posts'], 0)
            self.assertEqual(manifest['last_run']['discovery']['nearby_handles'], ['alicea'])


if __name__ == '__main__':
    unittest.main()
