"""Characterization tests for discover_aliases state handling and its full report."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from shiguangji.collect.archive import ArchiveError, Collector
from shiguangji.collect import identity_discovery
from shiguangji.collect.identity_discovery import POLICY, discover_aliases, endpoints
from test_identity_discovery import TS, IdentityClient, card

PROFILE = {'code': 200, 'user': {'type': 'profile', 'id': '123', 'screen_name': 'alice'}}


class FakeSourceClient:
    created = []

    def __init__(self, stop, log, proxy, max_requests, budget):
        self.args = (stop, log, proxy, max_requests, budget)
        self.retry_policy = 'unset'
        self.urls = []
        FakeSourceClient.created.append(self)

    def fetch(self, url, limit):
        self.urls.append((url, limit))
        return json.dumps(PROFILE).encode(), {'url': url}


class AliasSearchFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        FakeSourceClient.created = []

    def runner(self, options=None, base=None):
        runner = Collector('alice', {'public_mirrors': False, **(options or {})}, base=base or self.base, client=IdentityClient())
        self.addCleanup(runner.store.close)
        self.addCleanup(runner.identities.close)
        self.events = []
        runner.progress = lambda **update: self.events.append(update)
        return runner

    def stored(self, runner):
        row = runner.identities.db.execute("SELECT value FROM settings WHERE key='alias_search:alice'").fetchone()
        return json.loads(row[0])

    def put(self, runner, raw):
        with runner.identities.db:
            runner.identities.db.execute("INSERT OR REPLACE INTO settings VALUES('alias_search:alice',?)", (raw,))

    def test_unreadable_or_outdated_state_restarts_the_search(self):
        for index, raw in enumerate(('{broken', json.dumps({'policy': POLICY - 1, 'entries': {'x': {'at': 9e12}}, 'checked': 99}))):
            with self.subTest(raw=raw):
                base = self.base / str(index)
                base.mkdir()
                runner = self.runner(base=base)
                runner.identities.record('alice', 'anchor', card('alice'), TS, 'https://twitter.com/alice')
                self.put(runner, raw)
                discover_aliases(runner)
                state = self.stored(runner)
                self.assertEqual((state['policy'], state['checked']), (POLICY, 8))
                self.assertNotIn('x', state['entries'])

    def test_default_public_client_is_built_once_with_search_budget(self):
        runner = self.runner({'public_mirrors': True, 'proxy': 'http://127.0.0.1:9'})
        runner.retry_policy = 'policy'
        with patch('shiguangji.sources.source_http.SourceClient', FakeSourceClient):
            discover_aliases(runner)
        self.assertEqual(len(FakeSourceClient.created), 1)
        client = FakeSourceClient.created[0]
        self.assertEqual(client.args, (runner.stop, runner.log, 'http://127.0.0.1:9', identity_discovery.MAX_UNITS,
                                       runner._alias_search_budget))
        self.assertEqual(client.retry_policy, 'policy')
        self.assertEqual(client.urls, [('https://api.fxtwitter.com/2/profile/alice', 1024 * 1024),
                                       ('https://api.fxtwitter.com/2/profile/id:123', 1024 * 1024)])
        self.assertEqual(runner.identities.snapshot('alice')['user_id'], '123')

    def test_one_batch_report_request_order_and_progress(self):
        runner = self.runner()
        runner.identities.record('alice', 'anchor', card('alice'), TS, 'https://twitter.com/alice')
        first = endpoints('alice', '123')[0]
        runner.client.rows[first] = [{'original': first, 'timestamp': TS, 'statuscode': '200'},
                                     {'original': first, 'timestamp': 'bad', 'statuscode': '200'},
                                     {'original': first + '&x=1', 'timestamp': TS, 'statuscode': '200'}]
        runner.client.raw[f'https://web.archive.org/web/{TS}id_/{first}'] = card('oldalice')
        runner.client.rows[endpoints('alice', '123')[1]] = ArchiveError('index down')
        report = discover_aliases(runner)
        self.assertEqual(runner.client.calls[:5], [
            ('index', first), ('replay', f'https://web.archive.org/web/{TS}id_/{first}'), ('index', first),
            ('index', endpoints('alice', '123')[1]), ('index', endpoints('alice', '123')[1])])
        self.assertEqual(len(runner.client.calls), 8)
        state = self.stored(runner)
        self.assertEqual(state['entries']['index|' + first + '|first']['records'], [{'original': first, 'timestamp': TS}])
        self.assertEqual(state['entries']['replay|' + first + '|' + TS]['matches'], 1)
        failed = state['entries']['index|' + endpoints('alice', '123')[1] + '|first']
        self.assertEqual((failed['status'], failed['error']), ('unavailable', 'index down'))
        expected = {'status': 'searching', 'state': 'pending', 'reason': 'sources_unavailable', 'new_aliases': ['oldalice'],
                    'verified_aliases': ['oldalice'], 'checked_items': 8, 'pending_items': 7, 'failed_items': 2,
                    'errors': 2, 'checked_at': ''}
        self.assertEqual({k: state[k] for k in expected}, expected)
        self.assertTrue(state['next_retry_at'].endswith('Z'))
        self.assertEqual(report, runner.run_info['alias_search'])
        self.assertTrue(runner.alias_search_pending)
        messages = [e['message'] for e in self.events if 'message' in e]
        events = [e for e in self.events if 'message' not in e]
        self.assertEqual(events[0]['stage'], 'identity_search')
        self.assertEqual(events[0]['alias_discovery']['state'], 'running')
        self.assertEqual([e['completed'] for e in events[1:-1]], list(range(1, 9)))
        self.assertEqual({k: v for k, v in events[-1].items() if k != 'batch'}, {'alias_discovery': report})
        self.assertEqual(sum('index down' in m for m in messages), 2)
        self.assertTrue(messages[-1].startswith('自动核实同一账号的其他用户名：@oldalice'))


if __name__ == '__main__':
    unittest.main()
