"""Bounded batches must stop, preserve results, and resume fairly across sources."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

import app
from archive import Client, Collector, Store, replay
from extractor import blank_post, media_item
from media_recovery import RecoveryEngine
from run_budget import RunBudget, RunLimit
from source_http import SourceClient
from test_source_http import Response, IMAGE
from test_archive import PNG, TS


class BudgetTests(unittest.TestCase):
    def test_deadline_is_sticky_and_limits_request_timeout(self):
        current = [0.0]
        budget = RunBudget(minutes=1, clock=lambda: current[0])
        current[0] = 59
        self.assertEqual(budget.timeout(35), 1)
        current[0] = 60
        with self.assertRaises(RunLimit):
            budget.before_request()
        self.assertEqual(budget.requests, 0)
        current[0] = 0
        with self.assertRaises(RunLimit):
            budget.check()

    def test_inactivity_requires_attempts_and_does_not_reset_between_sources(self):
        current = [0.0]
        budget = RunBudget(clock=lambda: current[0])
        budget.media_phase()
        for _ in range(12):
            budget.before_request()
        current[0] = 170
        budget.saved('media/one.png')
        current[0] = 200
        budget.media_phase()
        budget.check()
        budget.saved('media/one.png')  # Reusing a path is not another recovery.
        current[0] = 350
        with self.assertRaises(RunLimit):
            budget.check()
        self.assertEqual(budget.snapshot()['new_files'], 1)
        self.assertEqual(budget.code, 'no_new_media')

    def test_both_transports_and_retries_share_one_request_budget(self):
        budget = RunBudget(max_requests=2)
        wayback = Client(delay=0, budget=budget)
        source = SourceClient(delay=0, budget=budget)
        url = replay(TS, IMAGE)
        wayback.opener = Mock()
        wayback.opener.open.return_value = Response(b'ok', url)
        wayback.open(url).close()
        source.opener = Mock()
        source.opener.open.side_effect = URLError('temporary failure')
        source.wait = lambda seconds: source.check()
        with self.assertRaises(RunLimit):
            source.open(IMAGE)
        self.assertEqual(budget.requests, 2)
        self.assertEqual(source.opener.open.call_count, 1)
        with self.assertRaises(RunLimit):
            wayback.open(url)
        self.assertEqual(wayback.opener.open.call_count, 1)

    def test_slow_page_stream_is_interrupted_and_closed(self):
        current = [0.0]
        budget = RunBudget(minutes=1, clock=lambda: current[0])
        response = Response(b'x' * 900_000, replay(TS, IMAGE), after_read=lambda: current.__setitem__(0, 61))
        client = Client(budget=budget)
        client.open = lambda url: response
        with self.assertRaises(RunLimit):
            client.text(response.url)
        self.assertTrue(response.closed)
        self.assertEqual(len(response.read_sizes), 1)

    def test_trickling_media_header_observes_deadline_and_leaves_no_file(self):
        current = [0.0]
        budget = RunBudget(minutes=1, clock=lambda: current[0])
        class SlowResponse(Response):
            def read1(self, size=-1):
                current[0] += 31
                return self.read(1)
        response = SlowResponse(PNG, replay(TS, IMAGE))
        client = Client(budget=budget)
        client.open = lambda url: response
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RunLimit):
                client.download(response.url, Path(directory)/'pending.bin', 1024)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertTrue(response.closed)


class BatchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        store = Store(self.base/'alice', 'alice')
        self.ids = [str(1500000000000000000+i) for i in range(3)]
        for index, pid in enumerate(self.ids):
            post = blank_post(pid, 'alice')
            post['text'] = f'Post {index}'
            post['captures'] = [{'timestamp': TS, 'url': post['url']}]
            post['media'] = [media_item(f'https://pbs.twimg.com/media/item{index}.png')]
            store.save_post(post)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def collector(self, **options):
        return Collector('alice', {'mode': 'repair', 'max_requests': 1, **options}, base=self.base)

    def test_limit_preserves_completed_download_and_unattempted_items_then_resumes(self):
        first = self.collector()
        first.client.delay = 0
        first.client.opener = Mock()
        first.client.opener.open.side_effect = lambda request, **kw: Response(PNG, request.full_url)
        self.assertEqual(first.run(), 'paused')
        manifest = json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['counts']['saved'], 1)
        self.assertEqual(manifest['last_run']['batch']['stop_code'], 'request_limit')
        self.assertEqual(manifest['last_run']['batch']['new_files'], 1)
        self.assertEqual(manifest['errors'], [])
        self.assertNotEqual(manifest['posts'][1]['media'][0]['status'], 'failed')
        self.assertNotEqual(manifest['posts'][2]['media'][0]['status'], 'failed')
        original = self.base/'alice'/manifest['posts'][0]['media'][0]['path']
        original_bytes = original.read_bytes()
        second = self.collector()
        self.assertEqual(second.ordered_posts('media')[0]['id'], self.ids[1])
        second.client.delay = 0
        second.client.opener = Mock()
        second.client.opener.open.side_effect = lambda request, **kw: Response(PNG, request.full_url)
        self.assertEqual(second.run(), 'paused')
        manifest = json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['counts']['saved'], 2)
        self.assertEqual(manifest['posts'][1]['media'][0]['status'], 'saved')
        self.assertEqual(original.read_bytes(), original_bytes)

    def test_next_batch_reaches_alternate_source_without_repeating_wayback(self):
        first = self.collector(multi_source=True)
        with patch.object(first, 'download_media', side_effect=lambda: first.budget.finish('time_limit', 'fixture limit')), \
             patch.object(RecoveryEngine, 'run') as rescue:
            self.assertEqual(first.run(), 'paused')
            rescue.assert_not_called()
        second = self.collector(multi_source=True)
        with patch.object(second, 'download_media') as wayback, \
             patch.object(RecoveryEngine, 'run', side_effect=lambda: second.budget.finish('time_limit', 'fixture limit')) as rescue:
            self.assertEqual(second.run(), 'paused')
            rescue.assert_called_once()
            wayback.assert_not_called()

    def test_multisource_budget_exits_whole_engine_without_caching_false_absence(self):
        collector = self.collector(mode='rescue')
        http = SourceClient(budget=collector.budget, delay=0, max_requests=0)
        engine = RecoveryEngine(collector, http=http, discover=lambda *a, **k: [], common_crawl=Mock())
        with self.assertRaises(RunLimit):
            engine.run()
        posts = collector.store.posts()
        self.assertTrue(all(not m.get('recovery_checked_at') for p in posts for m in p['media']))
        self.assertTrue(all(not m.get('error_kind') for p in posts for m in p['media']))
        self.assertEqual(engine.stats['checked'], 1)
        self.assertEqual(http.requests, 0)
        collector.store.close()

    def test_worker_keeps_partial_counter_and_stop_reason_through_restart(self):
        with patch.object(app.TASKS, 'base', self.base), patch.dict(app.TASKS.job, {
                'username': 'alice', 'running': True, 'pause_requested': False,
                'status': 'running', 'stage': 'media', 'completed': 2, 'total': 100,
                'logs': [], 'counts': {}, 'batch': {'new_files': 1, 'stop_reason': '达到本批请求上限'}
        }, clear=True), patch.object(app, 'Collector') as constructor:
            constructor.return_value.run.return_value = 'paused'
            app.TASKS.worker('alice', {})
            self.assertFalse(app.TASKS.job['running'])
            self.assertEqual(app.TASKS.job['completed'], 2)
            self.assertIn('本批已结束', app.TASKS.job['logs'][-1]['text'])
            app.TASKS.restore_job()
            self.assertEqual(app.TASKS.job['batch']['new_files'], 1)
            self.assertEqual(app.TASKS.job['completed'], 2)


if __name__ == '__main__':
    unittest.main()
