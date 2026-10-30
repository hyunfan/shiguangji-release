"""Identity discovery fixtures; no real network or user collections."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from shiguangji.platforms.account_identity import search_handles
from shiguangji.collect.archive import ArchiveError, Cancelled, Client, Collector
from shiguangji.collect.identity_discovery import discover_aliases, endpoints, profile_pairs, same_endpoint
from test_archive import FixtureClient

TS = '20200102030405'


def card(handle='oldalice', uid='123'):
    return f'<div data-screen-name="{handle}" data-user-id="{uid}"></div>'


class IdentityClient(FixtureClient):
    def __init__(self):
        super().__init__()
        self.calls, self.rows, self.raw = [], {}, {}
        self.scope = None

    def identity_client(self, budget):
        self.scope = budget
        return self

    def request(self, kind, url):
        self.check()
        self.scope.before_request()
        self.calls.append((kind, url))

    def identity_index(self, url, latest=False):
        self.request('index', url)
        value = self.rows.get(url, [])
        if isinstance(value, Exception):
            raise value
        return value

    def text_with_meta(self, url, limit):
        self.request('replay', url)
        value = self.raw.get(url, '')
        if isinstance(value, Exception):
            raise value
        return value, {'url': url}


class Public:
    def __init__(self, value, client):
        self.value, self.client, self.calls = value, client, []

    def fetch(self, url, limit):
        self.client.request('public', url)
        self.calls.append(url)
        if isinstance(self.value, Exception):
            raise self.value
        return json.dumps(self.value).encode(), {'url': url}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def runner(self, client=None, public=False):
        runner = Collector('alice', {'public_mirrors': public}, base=self.base, client=client or IdentityClient())
        self.addCleanup(runner.store.close)
        self.addCleanup(runner.identities.close)
        return runner

    def anchor(self, runner):
        runner.identities.record('alice', 'anchor', card('alice'), TS, 'https://twitter.com/alice')

    def capture(self, runner, raw):
        endpoint = endpoints('alice', '123')[0]
        runner.client.rows[endpoint] = [{'original': endpoint, 'timestamp': TS, 'statuscode': '200'}]
        runner.client.raw[f'https://web.archive.org/web/{TS}id_/{endpoint}'] = raw

    def finish(self, runner, public=None):
        # A real next batch has a fresh smaller allowance, but the same persistent checks.
        for _ in range(20):
            discover_aliases(runner, public)
            if not runner.alias_search_pending:
                return
            del runner._alias_search_budget
        self.fail('finite identity lookup did not finish')

    def test_automatically_links_old_name_without_manual_candidate(self):
        runner = self.runner(); self.anchor(runner)
        self.capture(runner, card() + card('unrelated', '999'))
        self.finish(runner)
        info = runner.identities.snapshot('alice')
        self.assertEqual(search_handles(info), ['alice', 'oldalice'])
        self.assertEqual(info['aliases'][0]['state'], 'verified')
        self.assertEqual(info['search']['status'], 'checked')
        self.assertEqual(runner.store.posts(), [])
        self.assertEqual(runner.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 0)
        self.assertTrue(list((runner.folder/'identity-evidence').glob('*.txt')))

    def test_verified_names_do_not_hide_failed_source_checks(self):
        runner = self.runner(); self.anchor(runner)
        self.capture(runner, card())
        runner.client.rows[endpoints('alice', '123')[-1]] = ArchiveError('timeout')
        self.finish(runner)
        report = runner.identities.search_report('alice')
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['verified_aliases'], ['oldalice'])
        self.assertEqual(report['failed_items'], 2)
        self.assertEqual(report['pending_items'], 0)
        self.assertEqual(report['checked_items'], 15, 'duplicate oldest/latest replay counted only once')
        self.assertTrue(report['next_retry_at'])
        calls = len(runner.client.calls)
        discover_aliases(runner)
        self.assertEqual(len(runner.client.calls), calls, 'cached errors cannot keep the loop spinning')

    def test_report_separates_finite_pending_work_and_no_numeric_identity(self):
        runner = self.runner(); self.anchor(runner)
        discover_aliases(runner)
        report = runner.identities.search_report('alice')
        self.assertEqual(report['state'], 'pending')
        self.assertEqual(report['checked_items'], 8)
        self.assertEqual(report['pending_items'], 6)
        del runner._alias_search_budget
        self.finish(runner)
        self.assertEqual(runner.identities.search_report('alice')['state'], 'completed')

    def test_missing_identity_is_blocked_but_does_not_fail_post_collection(self):
        runner = self.runner()
        self.finish(runner)
        report = runner.identities.search_report('alice')
        self.assertEqual((report['state'], report['reason']), ('blocked', 'uid_missing'))
        self.assertEqual(report['failed_items'], 0)
        self.assertEqual(report['pending_items'], 0)
        self.assertEqual(runner.errors, [])

    def test_unknown_handle_bootstraps_numeric_id_from_exact_public_profile(self):
        runner = self.runner(public=True)
        public = Public({'code': 200, 'user': {'type': 'profile', 'id': '123', 'screen_name': 'Alice'}}, runner.client)
        self.capture(runner, card())
        self.finish(runner, public)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice', 'oldalice'])
        self.assertEqual(public.calls[0], 'https://api.fxtwitter.com/2/profile/alice')
        self.assertIn('https://api.fxtwitter.com/2/profile/id:123', public.calls)

    def test_recycled_handle_is_linked_but_still_requires_post_author_checks(self):
        runner = self.runner(); self.anchor(runner)
        runner.identities.record('other', 'different-owner', card('oldalice', '999'), TS, 'https://twitter.com/oldalice')
        self.capture(runner, card())
        self.finish(runner)
        info = runner.identities.snapshot('alice')
        self.assertEqual(info['aliases'][0]['state'], 'reused')
        wrong = card('oldalice', '999').replace('</div>', '<div data-tweet-id="100">wrong</div></div>')
        self.assertEqual(runner.parse_capture(wrong, 'https://twitter.com/oldalice/status/100', TS, 'wrong'), [])

    def test_excluded_name_is_not_reenabled_by_network_evidence(self):
        runner = self.runner(); self.anchor(runner)
        runner.identities.choice('alice', 'oldalice', 'excluded')
        self.capture(runner, card())
        self.finish(runner)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice'])

    def test_conflicting_primary_does_not_make_requests(self):
        runner = self.runner(); self.anchor(runner)
        runner.identities.record('other', 'other', card('alice', '999'), TS, 'https://twitter.com/alice')
        self.finish(runner)
        self.assertEqual(runner.client.calls, [])
        self.assertEqual(runner.identities.search_report('alice')['status'], 'conflict')

    def test_empty_and_failed_sources_are_cached_and_do_not_loop(self):
        runner = self.runner(); self.anchor(runner)
        runner.client.rows[endpoints('alice', '123')[0]] = ArchiveError('HTTP 503')
        self.finish(runner)
        count = len(runner.client.calls)
        del runner._alias_search_budget
        self.finish(runner)
        self.assertEqual(len(runner.client.calls), count)
        self.assertEqual(runner.identities.search_report('alice')['errors'], 2)

    def test_work_is_bounded_per_batch_and_persistent_between_batches(self):
        runner = self.runner(); self.anchor(runner)
        discover_aliases(runner)
        self.assertEqual(len(runner.client.calls), 8)
        self.assertTrue(runner.alias_search_pending)
        later = self.runner()
        self.finish(later)
        self.assertEqual(len(later.client.calls), 6)
        self.assertEqual(later.identities.search_report('alice')['checked'], 14)

    def test_new_id_learned_after_empty_bootstrap_schedules_numeric_sources(self):
        runner = self.runner()
        self.finish(runner)
        self.assertEqual(runner.identities.search_report('alice')['status'], 'no_id')
        self.anchor(runner)
        self.capture(runner, card())
        del runner._alias_search_budget
        self.finish(runner)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice', 'oldalice'])

    def test_public_disabled_never_constructs_mirror_transport(self):
        runner = self.runner(); self.anchor(runner)
        with patch('shiguangji.sources.source_http.SourceClient') as source:
            self.finish(runner)
        source.assert_not_called()

    def test_cancel_preserves_unfinished_queue_without_error_caching(self):
        runner = self.runner(); self.anchor(runner)
        runner.client.rows[endpoints('alice', '123')[0]] = Cancelled()
        with self.assertRaises(Cancelled):
            discover_aliases(runner)
        self.assertEqual(runner.identities.search_report('alice')['checked'], 0)
        self.assertEqual(runner.identities.search_report('alice')['status'], 'searching')
        runner.client.rows.clear()
        self.finish(runner)

    def test_cdx_false_endpoint_match_and_redirect_only_names_are_rejected(self):
        runner = self.runner(); self.anchor(runner)
        self.capture(runner, '<a href="https://twitter.com/oldalice">@oldalice</a>')
        url = endpoints('alice', '123')[0]
        runner.client.rows[url].append({'original': url+'4', 'timestamp': TS, 'statuscode': '200'})
        self.finish(runner)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice'])
        self.assertEqual(sum(k == 'replay' for k, _ in runner.client.calls), 1)

    def test_profile_does_not_accept_wrong_user_or_username_changes_count_as_names(self):
        raw = json.dumps({'code': 200, 'user': {'type': 'profile', 'id': '999', 'screen_name': 'other',
                           'about_account': {'username_changes': {'count': 3}}}})
        self.assertEqual(profile_pairs(raw, handle='alice'), [])
        self.assertEqual(profile_pairs(raw, uid='123'), [])
        self.assertFalse(same_endpoint('https://twitter.com/i/user/123', 'https://evil.test/i/user/123'))

    def test_full_pipeline_indexes_and_downloads_newly_discovered_old_name(self):
        from test_account_identity import card as tweet_card
        runner = self.runner(); self.anchor(runner)
        runner.options['max_pages'] = 1
        self.capture(runner, card())
        queried = []
        def cdx(url, match='exact'):
            queried.append(url)
            if url == 'twitter.com/oldalice':
                yield [{'timestamp': TS, 'original':'https://twitter.com/oldalice/status/1346889436626259968',
                        'mimetype':'text/html', 'statuscode':'200', 'digest':'OLD'}]
            else:
                yield []
        runner.client.cdx = cdx
        runner.client.text = lambda url: tweet_card('oldalice', '123')
        self.assertIn(runner.run(), {'partial', 'complete'})
        manifest = json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertIn('twitter.com/oldalice', queried)
        self.assertEqual(manifest['counts']['posts'], 1)
        self.assertEqual(manifest['counts']['saved'], 1)
        self.assertEqual(manifest['posts'][0]['username'], 'oldalice')

    def test_deleting_collection_clears_network_checks_for_fresh_readdition(self):
        from shiguangji.storage.collection_delete import delete_collection
        runner = self.runner()
        self.finish(runner)
        runner.store.close(); runner.identities.close()
        delete_collection(self.base, 'alice')
        later = self.runner()
        self.assertEqual(later.identities.search_report('alice'), {})

    def test_expired_error_can_retry_without_erasing_accepted_evidence(self):
        runner = self.runner(); self.anchor(runner)
        url = endpoints('alice', '123')[0]
        runner.client.rows[url] = ArchiveError('HTTP 503')
        self.finish(runner)
        row = runner.identities.db.execute("SELECT value FROM settings WHERE key='alias_search:alice'").fetchone()
        state = json.loads(row[0])
        for _name, entry in state['entries'].items():
            if entry['status'] == 'unavailable':
                entry['at'] = time.time() - 86401
        runner.identities.db.execute("UPDATE settings SET value=? WHERE key='alias_search:alice'", (json.dumps(state),))
        runner.identities.db.commit()
        self.capture(runner, card())
        del runner._alias_search_budget
        self.finish(runner)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice', 'oldalice'])

    def test_archived_redirect_uses_actual_date_and_only_supported_profile_sources(self):
        runner = self.runner(); self.anchor(runner)
        self.capture(runner, card())
        def replay(url, limit):
            runner.client.request('replay', url)
            return card(), {'url':'https://web.archive.org/web/20210203040506id_/https://twitter.com/oldalice'}
        runner.client.text_with_meta = replay
        self.finish(runner)
        proof = runner.identities.snapshot('alice')['aliases'][0]['evidence'][0]
        self.assertEqual(proof['timestamp'], '20210203040506')
        self.assertEqual(proof['source'], 'https://twitter.com/oldalice')

    def test_archived_redirect_to_unrelated_host_cannot_supply_alias_evidence(self):
        runner = self.runner(); self.anchor(runner)
        self.capture(runner, card())
        def replay(url, limit):
            runner.client.request('replay', url)
            return card(), {'url':f'https://web.archive.org/web/{TS}id_/https://evil.test/oldalice'}
        runner.client.text_with_meta = replay
        self.finish(runner)
        self.assertEqual(search_handles(runner.identities.snapshot('alice')), ['alice'])


class CDXTests(unittest.TestCase):
    def test_index_samples_both_history_ends_with_small_exact_queries(self):
        client = Client(delay=0)
        calls = []
        def text(url, limit):
            calls.append((parse_qs(urlsplit(url).query), limit))
            return ''
        client.text = text
        self.assertEqual(client.identity_index('twitter.com/intent/user?user_id=123'), [])
        self.assertEqual(client.identity_index('twitter.com/intent/user?user_id=123', latest=True), [])
        self.assertEqual([c[0]['limit'] for c in calls], [['3'], ['-3']])
        self.assertTrue(all(c[0]['matchType'] == ['exact'] and c[1] == 128*1024 for c in calls))


if __name__ == '__main__':
    unittest.main()
