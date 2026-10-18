"""Auto continuation runs in one server worker and always has a finite ending."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
import task_service
from auto_batches import AutoBatches


LIMIT = {'stop_code': 'request_limit', 'new_files': 0}


class AutoPolicyTests(unittest.TestCase):
    def test_default_keeps_going_with_real_progress_without_requiring_time_limit_clicks(self):
        current = [0]
        session = AutoBatches(True, clock=lambda: current[0])
        for number in range(100):
            current[0] = number * 600
            self.assertTrue(session.after_batch('paused', {**LIMIT, 'new_files': 1}, {}))
        self.assertEqual(session.new_files, 100)
        self.assertEqual(session.code, '')

    def test_parsing_and_discovery_count_as_progress_before_media_downloads(self):
        session = AutoBatches(True, {'parsed': 0})
        for count in range(1, 6):
            self.assertTrue(session.after_batch('paused', LIMIT, {'parsed': count}))
        self.assertEqual(session.empty_batches, 0)
        self.assertEqual(session.new_files, 0)

    def test_three_empty_batches_stop_but_gains_reset_streak(self):
        session = AutoBatches(True)
        self.assertTrue(session.after_batch('paused', LIMIT, {}))
        self.assertTrue(session.after_batch('paused', {**LIMIT, 'new_files': 1}, {}))
        self.assertEqual(session.empty_batches, 0)
        self.assertTrue(session.after_batch('paused', LIMIT, {}))
        self.assertTrue(session.after_batch('paused', LIMIT, {}))
        self.assertFalse(session.after_batch('paused', LIMIT, {}))
        self.assertEqual(session.code, 'no_progress')

    def test_local_checks_are_finite_progress_and_reused_files_are_a_subset(self):
        session=AutoBatches(True, {'local_alias_checked': 0})
        for count in range(1,5):
            self.assertTrue(session.after_batch('partial',{'new_files':2,'reused_files':1},
                                               {'local_alias_checked':count},pending=True))
        self.assertEqual(session.snapshot()['new_files'],8)
        self.assertEqual(session.snapshot()['reused_files'],4)
        self.assertEqual(session.empty_batches,0)
        for expected in [True,True,False]:
            self.assertEqual(session.after_batch('partial',{}, {'local_alias_checked':4},pending=True),expected)
        self.assertEqual(session.code,'no_progress')

    def test_manual_pause_error_and_completed_pass_never_repeat(self):
        for status, stopped, batch in [('paused', True, LIMIT), ('paused', False, {}),
                                        ('failed', False, LIMIT), ('partial', False, {}), ('complete', False, {})]:
            with self.subTest(status=status, stopped=stopped):
                self.assertFalse(AutoBatches(True).after_batch(status, batch, {}, stopped))
        self.assertTrue(AutoBatches(True).after_batch('partial', {}, {}, pending=True))

    def test_whole_session_and_batch_count_are_bounded_even_with_gains(self):
        current = [0]
        session = AutoBatches(True, clock=lambda: current[0], max_minutes=120)
        current[0] = 7200
        self.assertFalse(session.after_batch('paused', {**LIMIT, 'new_files': 1}, {}))
        self.assertEqual(session.code, 'session_limit')
        session = AutoBatches(True, max_batches=24)
        for _ in range(23):
            self.assertTrue(session.after_batch('paused', {**LIMIT, 'new_files': 1}, {}))
        self.assertFalse(session.after_batch('paused', {**LIMIT, 'new_files': 1}, {}))
        self.assertEqual(session.code, 'batch_limit')


class AutoWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data = patch.object(app.TASKS, 'base', self.base)
        self.state = patch.dict(app.TASKS.job, {'running': True, 'username': 'alice', 'status': 'running', 'stage': 'repair', 'logs': [], 'counts': {}}, clear=True)
        self.stop = patch.object(app.TASKS, 'stop', threading.Event())
        self.data.start(); self.state.start(); self.stop.start()

    def tearDown(self):
        self.stop.stop(); self.state.stop(); self.data.stop(); self.temp.cleanup()

    def factory(self, results, calls):
        class FixtureCollector:
            index_ok = len(task_service.HOSTS)
            def __init__(self, handle, options, report, stop, base):
                self.options, self.report = dict(options), report
                calls.append(self.options)
            def run(self):
                value = results.pop(0)
                self.report(stage=value.get('stage', 'media'), completed=1, total=10)
                self.report(counts=value.get('counts', {}), batch=value.get('batch', LIMIT))
                return value.get('status', 'paused')
        return FixtureCollector

    def test_one_worker_continues_without_browser_and_stops_after_three_empty_batches(self):
        calls = []
        with patch.object(app, 'Collector', self.factory([{}, {}, {}], calls)), patch.object(app.TASKS.stop, 'wait', return_value=False) as wait:
            status = app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'repair'})
        self.assertEqual(status, 'complete')
        self.assertEqual(app.task_summary({**app.TASKS.job, 'status': status}, running=False)['state'], 'finished')
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(options['_defer_offline_export'] for options in calls))
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(app.TASKS.job['automatic']['stop_code'], 'no_progress')

    def test_explicit_media_modes_do_not_repeat_for_unqueried_old_handles(self):
        for mode in ('repair', 'rescue'):
            with self.subTest(mode=mode):
                calls = []
                results = [{'status': 'complete', 'batch': {}, 'counts': {'identity_pending': 1, 'alias_search_pending': 1}}]
                with patch.object(app, 'Collector', self.factory(results, calls)):
                    self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': mode}), 'complete')
                self.assertEqual(len(calls), 1)

    def test_full_mode_automatically_finishes_finite_alias_search_queue(self):
        calls = []
        results = [{'status': 'partial', 'batch': {}, 'counts': {'alias_search_pending': 1, 'alias_search_checked': 8}},
                   {'status': 'complete', 'batch': {}, 'counts': {'alias_search_pending': 0, 'alias_search_checked': 14}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'full'}), 'complete')
        self.assertEqual(len(calls), 2)

    def test_media_mode_continues_unfinished_local_reuse_then_finishes(self):
        calls = []
        results = [{'status': 'partial', 'batch': {}, 'counts': {
                       'identity_pending': 1, 'local_alias_pending': 1, 'local_alias_checked': 1}},
                   {'status': 'complete', 'batch': {}, 'counts': {
                       'identity_pending': 1, 'local_alias_pending': 0, 'local_alias_checked': 2}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'repair'}), 'complete')
        self.assertEqual(len(calls), 2)

    def test_live_new_files_survives_log_only_updates_without_counting_previous_batch_twice(self):
        seen, batches = [], [2, 3]
        class Collector:
            index_ok = len(task_service.HOSTS)
            def __init__(self, handle, options, report, stop, base):
                self.report = report
            def run(self):
                count = batches.pop(0)
                self.report(stage='media', message='Checking next file')
                seen.append(app.TASKS.job['automatic']['new_files'])
                self.report(batch={'new_files': count, 'stop_code': 'request_limit' if batches else ''},
                            counts={'saved': count})
                seen.append(app.TASKS.job['automatic']['new_files'])
                self.report(message='Saving progress')
                seen.append(app.TASKS.job['automatic']['new_files'])
                return 'paused' if batches else 'complete'
        with patch.object(app, 'Collector', Collector), patch.object(app.TASKS.stop, 'wait', return_value=False):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'full'}), 'complete')
        self.assertEqual(seen, [0, 2, 2, 2, 5, 5])
        self.assertEqual(app.TASKS.job['automatic']['new_files'], 5)

    def test_slow_pages_yield_to_media_and_completed_index_is_not_requeried(self):
        calls = []
        results = [{'stage': 'pages', 'counts': {'captures': 30, 'parsed': 10}},
                   {'stage': 'sources', 'status': 'partial', 'batch': {}, 'counts': {'captures': 30, 'parsed': 10}},
                   {'stage': 'pages', 'status': 'partial', 'batch': {}, 'counts': {'captures': 30, 'parsed': 30}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'full'}), 'partial')
        self.assertEqual(len(calls), 3)
        self.assertTrue(calls[1]['_skip_pages'])
        self.assertTrue(calls[1]['_skip_index'])
        self.assertFalse(calls[2]['_skip_pages'])
        self.assertEqual(app.TASKS.job['automatic']['stop_code'], 'pass_finished')

    def test_pause_during_batch_rest_prevents_next_collector(self):
        calls = []
        def pause(seconds):
            app.TASKS.stop.set()
            return True
        with patch.object(app, 'Collector', self.factory([{}], calls)), patch.object(app.TASKS.stop, 'wait', side_effect=pause):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True}), 'paused')
        self.assertEqual(len(calls), 1)
        self.assertTrue(app.TASKS.stop.is_set())
        self.assertEqual(app.TASKS.job['automatic']['stop_code'], 'user_pause')

    def test_unfinished_sources_and_network_errors_retry_but_stop_after_three_empty_batches(self):
        for counts, options in [({'cc_pending': 2}, {'multi_source': True}),
                                ({'source_retry_pending': 1}, {'multi_source': True}),
                                ({'media_retry_pending': 1}, {})]:
            with self.subTest(counts=counts):
                calls = []
                results = [{'status': 'partial', 'batch': {}, 'counts': counts} for _ in range(3)]
                with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
                    app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'repair', **options})
                self.assertEqual(len(calls), 3)
                self.assertEqual(app.TASKS.job['automatic']['stop_code'], 'no_progress')

    def test_disabled_sources_discovery_and_plain_missing_do_not_trigger_retries(self):
        for counts, options in [({'cc_pending': 2}, {'multi_source': True, 'common_crawl': False}),
                                ({'source_retry_pending': 2}, {'multi_source': False}),
                                ({'media_retry_pending': 1, 'cc_pending': 2}, {'mode': 'discover', 'multi_source': True}),
                                ({'missing': 4, 'retryable': 2}, {'multi_source': True})]:
            with self.subTest(counts=counts, options=options):
                calls = []
                with patch.object(app, 'Collector', self.factory([{'status': 'partial', 'batch': {}, 'counts': counts}], calls)):
                    app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'repair', **options})
                self.assertEqual(len(calls), 1)
                self.assertEqual(app.TASKS.job['automatic']['stop_code'], 'pass_finished')

    def test_existing_media_downloads_before_unread_snapshots_then_resumes_pages(self):
        folder = self.base / 'alice'
        folder.mkdir()
        counts = {'media': 5, 'saved': 0, 'captures': 30, 'parsed': 10}
        (folder / 'manifest.json').write_text(json.dumps({'counts': counts}), encoding='utf-8')
        calls = []
        results = [{'stage': 'media', 'status': 'partial', 'batch': {'new_files': 5},
                    'counts': {**counts, 'saved': 5}},
                   {'stage': 'pages', 'status': 'complete', 'batch': {},
                    'counts': {**counts, 'saved': 5, 'parsed': 30}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            self.assertEqual(app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'repair'}), 'complete')
        self.assertTrue(calls[0]['_skip_pages'])
        self.assertFalse(calls[1]['_skip_pages'])
        self.assertEqual(app.TASKS.job['automatic']['new_files'], 5)

    def test_media_first_full_run_still_performs_deferred_discovery(self):
        folder = self.base / 'alice'
        folder.mkdir()
        counts = {'media': 1, 'saved': 0, 'captures': 1, 'parsed': 1}
        (folder / 'manifest.json').write_text(json.dumps({'counts': counts}), encoding='utf-8')
        calls = []
        results = [{'status': 'complete', 'batch': {'new_files': 1}, 'counts': {**counts, 'saved': 1}},
                   {'status': 'complete', 'batch': {}, 'counts': {**counts, 'saved': 1}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'full', 'multi_source': True})
        self.assertEqual([c['mode'] for c in calls], ['repair', 'full'])
        self.assertTrue(calls[0]['multi_source'])
        self.assertFalse(calls[1].get('_skip_index', False))

    def test_explicit_discovery_keeps_text_priority_even_with_missing_media(self):
        folder = self.base / 'alice'
        folder.mkdir()
        (folder / 'manifest.json').write_text(json.dumps({'counts': {'media': 1, 'saved': 0}}), encoding='utf-8')
        calls = []
        with patch.object(app, 'Collector', self.factory([{'status': 'partial', 'batch': {}}], calls)):
            app.TASKS.collect_batches('alice', {'auto_continue': True, 'mode': 'discover'})
        self.assertEqual(calls[0]['mode'], 'discover')
        self.assertFalse(calls[0]['_skip_pages'])

    def test_current_batch_metrics_do_not_double_count_completed_files(self):
        calls = []
        results = [{'batch': {**LIMIT, 'new_files': 2}}, {'status': 'partial', 'batch': {'new_files': 3}}]
        with patch.object(app, 'Collector', self.factory(results, calls)), patch.object(app.TASKS.stop, 'wait', return_value=False):
            app.TASKS.collect_batches('alice', {'auto_continue': True})
        self.assertEqual(app.TASKS.job['automatic']['new_files'], 5)
        app.TASKS.persist_job(force=True)
        saved = task_service.read_job(self.base / '.job-state.json')
        self.assertEqual(saved['automatic']['new_files'], 5)
