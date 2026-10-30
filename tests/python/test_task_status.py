"""Lifecycle and recovery are independent; persistence and publication must agree."""
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app
from shiguangji.tasks import result_finalizer, task_service
from shiguangji.collect.archive import Cancelled, Store, export_collection, zip_is_current
from shiguangji.platforms.extractor import blank_post
from shiguangji.tasks.job_state import read_job, write_job
from shiguangji.tasks.task_status import summary, collection_summary, same_run


COUNTS = {'posts': 2, 'text': 2, 'media': 5, 'saved': 3, 'missing': 2, 'captures': 4, 'parsed': 4}


class ClassificationTests(unittest.TestCase):
    def test_scope_checked_can_have_missing_media(self):
        s = summary({'status': 'partial', 'counts': COUNTS})
        self.assertEqual((s['state'], s['scope_status'], s['result_status']), ('finished', 'complete', 'partial'))
        self.assertEqual(s['label'], '本轮查找完成')

    def test_automatic_no_progress_is_finished_and_manual_pause_is_paused(self):
        for code, state in [('no_progress', 'finished'), ('user_pause', 'paused'), ('session_limit', 'paused')]:
            with self.subTest(code=code):
                s = summary({'status': 'paused', 'counts': COUNTS, 'automatic': {'enabled': True, 'stop_code': code}})
                self.assertEqual(s['state'], state)
                self.assertEqual(s['scope_status'], 'partial')

    def test_active_pause_and_publication_are_not_terminal(self):
        for extra, expected in [({}, 'running'), ({'pause_requested': True}, 'pausing'), ({'stage': 'zip'}, 'finalizing')]:
            s = summary({'running': True, 'status': 'running', 'stage': 'media',
                         'task_summary': {'finished_at': '2026-10-01T01:00:00Z'}, **extra})
            self.assertEqual(s['state'], expected)
            self.assertEqual(s['finished_at'], '')

    def test_failures_pending_and_disabled_modes_do_not_claim_scope_checked(self):
        cases = [({'errors': [{'error': 'timeout'}]}, 'full'),
                 ({'counts': {**COUNTS, 'parsed': 3}}, 'full'),
                 ({'alias_discovery': {'state': 'partial', 'failed_items': 1}}, 'full'),
                 ({'counts': {**COUNTS, 'local_alias_pending': 1}}, 'repair')]
        for extra, mode in cases:
            s = summary({'status': 'partial', 'counts': COUNTS, 'mode': mode, **extra})
            self.assertEqual(s['reason'], 'sources_unavailable')
        s = summary({'status': 'partial', 'mode': 'repair', 'counts': COUNTS,
                     'alias_discovery': {'state': 'pending', 'failed_items': 2}})
        self.assertEqual(s['scope_status'], 'complete')

    def test_no_posts_network_failure_is_not_no_archives(self):
        s = summary({'status': 'failed', 'counts': {'posts': 0}, 'errors': [{'error': 'timeout'}]})
        self.assertEqual(s['state'], 'failed')
        self.assertNotEqual(s['result_label'], '本轮未发现帖子')
        self.assertEqual(summary({'status': 'complete', 'counts': {'posts': 0}})['result_label'], '本轮未发现帖子')

    def test_legacy_unknown_and_orphan_interruption_are_honest(self):
        self.assertEqual(summary({'status': 'paused'})['state'], 'unknown')
        self.assertEqual(summary({'status': 'running'})['state'], 'interrupted')
        self.assertEqual(summary({'status': 'paused', 'interrupted': True})['state'], 'interrupted')

    def test_missing_or_conflicting_identity_is_not_a_finished_old_name_search(self):
        for reason in ('uid_missing', 'uid_conflict'):
            s = summary({'status': 'complete', 'counts': COUNTS,
                         'alias_discovery': {'state': 'blocked', 'reason': reason}})
            self.assertEqual((s['scope_status'], s['reason']), ('partial', 'identity_unverified'))

    def test_end_time_is_shared_with_saved_summary_after_zip_publication(self):
        stored = summary({'status': 'complete', 'finished_at': '2026-10-01T01:00:00Z'})
        s = summary({'status': 'complete', 'finished_at': '2026-10-01T02:00:00Z', 'task_summary': stored})
        self.assertEqual(s['finished_at'], stored['finished_at'])

    def test_finished_labels_explain_a_limited_mode(self):
        for mode, text in [('identity', '仅核对用户名'), ('discover', '仅查找帖子'), ('repair', '仅补全已有媒体')]:
            self.assertIn(text, summary({'status': 'complete', 'mode': mode})['label'])

    def test_restored_and_zip_jobs_preserve_saved_collection_scope(self):
        saved = summary({'status': 'complete', 'mode': 'discover', 'counts': COUNTS})
        for mode in ('', 'zip'):
            s = summary({'status': 'complete', 'mode': mode, 'task_summary': saved, 'counts': COUNTS})
            self.assertEqual(s['mode'], 'discover')
            self.assertIn('仅查找帖子', s['label'])

    def test_same_run_failure_overrides_checkpoint_but_not_another_run(self):
        m = {'username': 'alice', 'run_id': 'one', 'status': 'complete', 'counts': COUNTS}
        m['task_summary'] = summary(m)
        job = {'username': 'alice', 'run_id': 'one', 'running': False, 'status': 'failed'}
        self.assertEqual(collection_summary(m, job)['state'], 'failed')
        self.assertEqual(collection_summary(m, {**job, 'run_id': 'two'})['state'], 'finished')
        self.assertEqual(collection_summary(m, {**job, 'status': 'paused', 'interrupted': True})['state'], 'interrupted')

    def test_same_username_alone_never_reconciles_a_legacy_run(self):
        manifest = {'username': 'alice', 'updated_at': '2026-10-01T02:00:00Z', 'counts': COUNTS, 'status': 'paused'}
        job = {'username': 'alice', 'running': False, 'counts': COUNTS, 'status': 'paused',
               'started_at': '2026-10-01T01:00:00Z', 'finished_at': '2026-10-01T03:00:00Z',
               'automatic': {'enabled': True, 'stop_code': 'no_progress'}}
        self.assertTrue(same_run(manifest, job))
        self.assertEqual(collection_summary(manifest, job)['state'], 'finished')
        for change in [{'counts': {**COUNTS, 'saved': 4}}, {'started_at': '2026-10-02T01:00:00Z'}, {'mode': 'zip'}]:
            self.assertFalse(same_run(manifest, {**job, **change}))
            self.assertEqual(collection_summary(manifest, {**job, **change})['state'], 'unknown')

    def test_run_ids_and_other_collections_are_isolated(self):
        a = {'username': 'alice', 'run_id': 'one', 'status': 'paused', 'counts': COUNTS}
        j = {'username': 'alice', 'run_id': 'two', 'running': False,
             'automatic': {'enabled': True, 'stop_code': 'no_progress'}}
        self.assertFalse(same_run(a, j))
        self.assertEqual(collection_summary(a, j)['state'], 'unknown')
        self.assertEqual(collection_summary(a, {**j, 'username': 'bob', 'running': True})['state'], 'unknown')


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        from shiguangji.storage.storage_policy import save_settings
        save_settings(self.base,{'zip_on_demand':False})  # Exercise opted-in automatic ZIP publication.
        self.data = patch.object(app.TASKS, 'base', self.base)
        self.job = patch.dict(app.TASKS.job, {'username': 'alice', 'run_id': 'run-one', 'running': True,
            'status': 'running', 'stage': 'media', 'logs': [], 'counts': {},
            'automatic': {'enabled': True, 'stop_code': 'no_progress'}}, clear=True)
        self.data.start(); self.job.start()
        store = Store(self.base / 'alice', 'alice')
        post = blank_post('1500000000000000000', 'alice'); post['text'] = 'Saved post'
        store.put_post(post)
        export_collection(store, 'paused', [], run_info={'run_id': 'run-one'})
        store.close()

    def tearDown(self):
        self.job.stop(); self.data.stop(); self.temp.cleanup()

    def manifest(self):
        return json.loads((self.base / 'alice/manifest.json').read_text(encoding='utf-8'))

    def test_final_summary_is_in_manifest_offline_zip_and_restart_state(self):
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'):
            app.TASKS.worker('alice', {'auto_continue': True})
        s = self.manifest()['task_summary']
        self.assertEqual((s['state'], s['reason'], s['run_id']), ('finished', 'no_progress', 'run-one'))
        self.assertTrue(zip_is_current(self.base / 'alice'))
        with zipfile.ZipFile(self.base / 'alice-合集.zip') as z:
            self.assertEqual(json.loads(z.read('alice/manifest.json'))['task_summary'], s)
            self.assertIn(b'no_progress', z.read('alice/data.js'))
        saved = read_job(self.base / '.job-state.json')
        self.assertEqual(saved['task_summary'], s)
        app.TASKS.restore_job()
        self.assertEqual(summary(app.TASKS.job)['state'], 'finished')
        self.assertFalse(app.TASKS.job['running'])

    def test_zip_failure_preserves_posts_and_rebuild_restores_capture_outcome(self):
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'), patch.object(task_service, 'make_zip', side_effect=OSError('disk full')):
            app.TASKS.worker('alice', {'auto_continue': True})
        m = self.manifest()
        self.assertEqual(m['task_summary']['reason'], 'zip_failed')
        self.assertEqual(m['posts'][0]['text'], 'Saved post')
        self.assertFalse(zip_is_current(self.base / 'alice'))
        app.TASKS.job.update(running=True, stage='export', status='running', mode='zip')
        app.TASKS.worker('alice', {}, zip_only=True)
        self.assertTrue(zip_is_current(self.base / 'alice'))
        self.assertEqual(self.manifest()['task_summary']['reason'], 'no_progress')

    def test_state_list_and_detail_use_the_same_classification(self):
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'):
            app.TASKS.worker('alice', {})
        server = app.LocalServer(('127.0.0.1', 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def get(path):
            c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            c.request('GET', path); r = c.getresponse(); data = json.loads(r.read()); c.close(); return data
        try:
            summaries = [get('/api/state')['job']['task_summary'], get('/api/collections')[0]['task_summary'],
                         get('/api/collection/alice')['task_summary']]
            self.assertEqual({s['state'] for s in summaries}, {'finished'})
            self.assertEqual({s['reason'] for s in summaries}, {'no_progress'})
            self.assertEqual({s['run_id'] for s in summaries}, {'run-one'})
        finally:
            server.shutdown(); server.server_close(); thread.join(5)

    def test_new_run_clears_old_outcome_and_assigns_unique_id(self):
        app.TASKS.job.update(running=False, task_summary={'state': 'finished'}, work_pending=True)
        with patch.object(app.threading, 'Thread'):
            app.TASKS.start('alice', {})
        first = app.TASKS.job['run_id']
        self.assertEqual(app.TASKS.job['task_summary'], {})
        self.assertNotIn('work_pending', app.TASKS.job)
        app.TASKS.job['running'] = False
        with patch.object(app.threading, 'Thread'):
            app.TASKS.start('alice', {})
        self.assertNotEqual(app.TASKS.job['run_id'], first)

    def test_zip_of_a_saved_manifest_without_database_never_erases_posts(self):
        before = self.manifest()
        (self.base / 'alice/archive.sqlite').unlink()
        app.TASKS.worker('alice', {}, zip_only=True)
        self.assertEqual(self.manifest()['posts'], before['posts'])
        self.assertEqual(self.manifest()['counts'], before['counts'])
        self.assertFalse((self.base / 'alice/archive.sqlite').exists())
        self.assertTrue(zip_is_current(self.base / 'alice'))

    def test_repeated_zip_failure_without_database_can_recover(self):
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'):
            app.TASKS.worker('alice', {})
        before = self.manifest()
        (self.base / 'alice/archive.sqlite').unlink()
        for _ in range(2):
            app.TASKS.job.update(running=True, status='running', stage='export', mode='zip', run_id='zip-run')
            with patch.object(task_service, 'make_zip', side_effect=OSError('disk full')):
                app.TASKS.worker('alice', {}, zip_only=True)
            self.assertEqual(self.manifest()['task_summary']['reason'], 'zip_failed')
            self.assertEqual(self.manifest()['last_run']['capture_summary']['reason'], 'no_progress')
        app.TASKS.job.update(running=True, status='running', stage='export', mode='zip')
        app.TASKS.worker('alice', {}, zip_only=True)
        self.assertEqual(self.manifest()['task_summary']['reason'], 'no_progress')
        self.assertEqual(self.manifest()['posts'], before['posts'])
        self.assertEqual(self.manifest()['counts'], before['counts'])
        self.assertFalse((self.base / 'alice/archive.sqlite').exists())
        self.assertTrue(zip_is_current(self.base / 'alice'))

    def test_final_export_failure_is_saved_and_survives_restart(self):
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'), patch.object(result_finalizer, 'export_collection', side_effect=OSError('export blocked')):
            app.TASKS.worker('alice', {})
        m = self.manifest()
        self.assertEqual(m['task_summary']['state'], 'failed')
        self.assertEqual(m['task_summary']['reason'], 'error')
        self.assertEqual(m['posts'][0]['text'], 'Saved post')
        app.TASKS.restore_job()
        self.assertEqual(collection_summary(m, app.TASKS.job)['state'], 'failed')

    def test_local_identity_pause_does_not_inherit_previous_batch_reason(self):
        for response, reason in [({'remaining': 1}, 'time_limit'), (Cancelled(), 'user_pause')]:
            with self.subTest(reason=reason):
                with patch.object(app.TASKS, 'collect_batches', return_value='partial'):
                    app.TASKS.worker('alice', {})
                app.TASKS.job.update(running=True, status='running', mode='identity', run_id='identity-run',
                               batch={}, automatic={}, task_summary={})
                scan = patch('shiguangji.platforms.account_identity.Catalog.scan', side_effect=response) if isinstance(response, Exception) else patch('shiguangji.platforms.account_identity.Catalog.scan', return_value=response)
                with scan:
                    app.TASKS.worker('alice', {'mode': 'identity', '_run_id': 'identity-run'})
                m = self.manifest()
                self.assertEqual((m['task_summary']['state'], m['task_summary']['reason']), ('paused', reason))
                self.assertEqual(m['task_summary']['mode'], 'identity')
                self.assertEqual(m['posts'][0]['text'], 'Saved post')

    def test_unwritable_collection_still_reports_same_run_failure_through_job(self):
        before = self.manifest()
        with patch.object(app.TASKS, 'collect_batches', return_value='partial'), patch.object(result_finalizer, 'export_collection', side_effect=OSError('export blocked')), patch.object(result_finalizer, 'publish_manifest', side_effect=OSError('read only')):
            app.TASKS.worker('alice', {})
        self.assertEqual(self.manifest(), before)
        app.TASKS.restore_job()
        self.assertEqual(collection_summary(before, app.TASKS.job)['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
