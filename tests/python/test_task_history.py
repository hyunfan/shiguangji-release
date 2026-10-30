"""Persistent task search, legacy import and safe restart regression tests."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import app
from shiguangji.storage.collection_export import export_collection
from shiguangji.storage.collection_store import Store
from shiguangji.platforms.extractor import blank_post
from shiguangji.tasks.job_state import write_job
from shiguangji.tasks.task_history import TaskHistory
from shiguangji.tasks.task_queue import TaskQueue
from shiguangji.tasks.task_service import TaskService


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.service = TaskService(self.base, collector_for=lambda _:None, options_for=app.collect_options)
        self.history = self.service.task_history()

    def add(self, handle='alice', run='first', status='paused', mode='full', **options):
        job = dict(username=handle, run_id=run, status='running', running=True,
                   started_at='2026-01-01T00:00:00Z', counts={}, mode=mode)
        self.history.begin(job, app.collect_options({'mode':mode, **options}))
        job.update(running=False, status=status, finished_at='2026-01-01T01:00:00Z')
        if status == 'paused': job['batch'] = {'stop_code':'user_pause'}
        self.history.finish(job, {'user_id':'12345', 'aliases':[{'username':'oldalice','state':'verified'}, {'username':'notproven','state':'candidate'}]})
        return job

    def test_failed_before_any_manifest_is_persistent_and_searchable(self):
        self.add(status='failed', proxy='http://private:secret@127.0.0.1:7890')
        reopened = TaskHistory(self.base)
        item = reopened.search({'q':'@ALICE', 'state':'unfinished'})['items'][0]
        self.assertEqual(item['state'], 'failed')
        self.assertFalse(item['has_collection'])
        self.assertNotIn('options', item)
        self.assertNotIn('secret', self.history.path.read_bytes().decode('latin1'))
        self.assertNotIn('proxy', reopened.get('first')['options'])

    def test_verified_alias_numeric_id_platform_and_literal_search(self):
        self.add();self.add('instagram--alice','ig')
        self.assertEqual(self.history.search({'state':'paused'})['total'], 2)
        self.assertEqual(self.history.search({'q':'12345','platform':'x'})['total'], 1)
        self.assertEqual(self.history.search({'q':'oldalice','platform':'x'})['total'], 1)
        self.assertEqual(self.history.search({'q':'notproven'})['total'], 0)
        for query in ('%', '_', "' OR 1=1 --"):
            self.assertEqual(self.history.search({'q':query})['total'], 0)

    def test_history_survives_queue_clear_and_pages_have_no_duplicates(self):
        for n in range(25): self.add(run=str(n))
        self.service.task_queue().clear_finished()
        pages = [self.history.search({'limit':10,'offset':n}) for n in (0,10,20)]
        self.assertEqual(len({i['id'] for p in pages for i in p['items']}), 25)
        self.assertFalse(pages[-1]['has_more'])

    def test_restarts_mark_running_history_interrupted_without_starting_network(self):
        self.history.begin(dict(username='alice',run_id='live',running=True,status='running'), {})
        self.history.bootstrap(self.service.task_queue(), 'live')
        self.assertEqual(self.history.get('live')['state'], 'running')
        self.history.bootstrap(self.service.task_queue())
        self.assertEqual(self.history.get('live')['state'], 'interrupted')

    def test_bootstrap_imports_available_evidence_once_and_preserves_final_failure(self):
        root = self.base/'legacy';store=Store(root/'alice','alice')
        try:
            post=blank_post('1234567890','alice');post['text']='saved'
            store.save_post(post);export_collection(store,'running',[],run_info={'run_id':'oldrun','options':{'mode':'discover'}})
        finally:store.close()
        write_job(root/'.job-state.json',dict(username='alice',run_id='oldrun',mode='discover',running=False,status='failed',stage='done',counts={'posts':1},logs=[]))
        history=TaskHistory(root);queue=TaskQueue(root/'.task-queue.json')
        history.bootstrap(queue);history.bootstrap(queue)
        result=history.search({});self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['state'],'failed')
        self.assertEqual(result['items'][0]['mode'],'discover')
        self.assertEqual(result['items'][0]['origin'],'checkpoint')

    def test_retry_preserves_task_mode_and_options_but_uses_current_proxy(self):
        self.add(mode='discover', multi_source=False, max_pages=77, public_mirrors=False)
        with patch('shiguangji.tasks.task_service.threading.Thread'), patch.object(self.service,'start',wraps=self.service.start) as start:
            self.service.retry_history('first', app.collect_options({'proxy':'http://127.0.0.1:4567','max_pages':9}))
        handle, options = start.call_args.args
        self.assertEqual(handle,'alice');self.assertEqual(options['mode'],'discover')
        self.assertEqual(options['max_pages'],77);self.assertFalse(options['public_mirrors'])
        self.assertEqual(options['proxy'],'http://127.0.0.1:4567')

    def test_retry_paused_account_does_not_resume_unrelated_queue_items(self):
        self.add()
        queue=self.service.task_queue();queue.add(['alice','bob'],{});queue.pause()
        with patch('shiguangji.tasks.task_service.threading.Thread'):
            self.service.retry_history('first',app.collect_options({}))
        self.assertEqual(queue.data['state'],'paused')
        self.assertEqual([i['state'] for i in queue.data['items']],['running','pending'])
        self.assertEqual(self.service.job['username'],'alice')
        self.service.job.update(running=True,status='complete',stage='done')
        self.service.finish_worker({'_run_id':self.service.job['run_id'],'_queue_id':queue.data['items'][0]['id']})
        self.assertEqual(queue.data['state'],'paused')
        self.assertEqual(queue.data['items'][1]['state'],'pending')

    def test_retry_busy_or_stopping_does_not_create_another_run(self):
        self.add()
        with patch.object(self.service,'start') as start:
            self.service.job['running']=True
            with self.assertRaises(ValueError):self.service.retry_history('first',{})
            self.service.job['running']=False;self.service.stopping.set()
            with self.assertRaises(ValueError):self.service.retry_history('first',{})
            start.assert_not_called()

    def test_narrow_retry_does_not_consume_a_queued_full_archive(self):
        self.add(mode='discover')
        queue=self.service.task_queue();queue.add(['alice'],{});queue.pause()
        with patch('shiguangji.tasks.task_service.threading.Thread'):
            self.service.retry_history('first',app.collect_options({}))
        self.assertEqual(queue.data['items'][0]['state'],'pending')
        self.assertEqual(queue.data['state'],'paused')

    def test_legacy_queue_preserves_the_recorded_terminal_reason(self):
        root=self.base/'oldqueue';queue=TaskQueue(root/'.task-queue.json')
        queue.add(['alice'],{});item=queue.data['items'][0]
        queue.mark_running(item['id'],'alice','old-run')
        queue.finish(item['id'],'old-run',{'state':'finished','reason':'no_progress','counts':{},'mode':'full'})
        history=TaskHistory(root);history.bootstrap(queue)
        imported=history.get('old-run')
        self.assertEqual(imported['state'],'finished')
        self.assertEqual(imported['reason'],'no_progress')

    def test_zip_failure_retries_only_export_without_erasing_saved_post(self):
        self.add(status='failed')
        store=Store(self.base/'alice','alice')
        try: export_collection(store,'partial',[])
        finally: store.close()
        self.history.finish(dict(username='alice',run_id='first',status='failed',running=False,task_summary={'schema_version':1,'state':'failed','reason':'zip_failed'}))
        with patch('shiguangji.tasks.task_service.threading.Thread'), patch.object(self.service,'start',wraps=self.service.start) as start:
            self.service.retry_history('first',{})
        self.assertTrue(start.call_args.kwargs['zip_only'])

    def test_forget_removes_only_the_selected_platform_and_does_not_reimport(self):
        self.add();self.add('instagram--alice','ig')
        self.history.forget('alice');self.history.bootstrap(self.service.task_queue())
        self.assertEqual([i['username'] for i in self.history.search({})['items']],['instagram--alice'])

    def test_corrupted_history_is_preserved_and_does_not_start_work(self):
        self.history.path.write_bytes(b'corrupted history')
        with patch('shiguangji.tasks.task_service.threading.Thread') as thread:
            with self.assertRaises(OSError):self.service.start('alice',{})
            thread.assert_not_called()
        self.assertFalse(self.service.job['running'])
        self.assertEqual(self.history.path.read_bytes(),b'corrupted history')

    def test_queue_prepare_failure_is_recorded_even_after_history_was_opened(self):
        queue=self.service.task_queue();queue.add(['alice'],{})
        with patch('shiguangji.tasks.queue_coordinator.canonical_handle',side_effect=ValueError('conflict')):
            self.service.advance_queue()
        self.assertEqual(self.history.search({'state':'failed'})['total'],1)

    def test_identity_read_failure_does_not_lose_outcome_or_known_aliases(self):
        job = self.add()
        self.service.job.update(job, status='failed', running=False)
        with patch('shiguangji.tasks.queue_coordinator.platform_for') as platform:
            platform.return_value.identity.side_effect = OSError('temporarily unavailable')
            self.service.save_history_result()
        item = self.history.search({'q':'oldalice'})['items'][0]
        self.assertEqual(item['state'], 'failed')
        self.assertEqual(item['user_id'], '12345')


class HistoryApiTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.service=TaskService(Path(temporary.name),collector_for=lambda _:None,options_for=app.collect_options)
        patched=patch.object(app,'TASKS',self.service);patched.start();self.addCleanup(patched.stop)
        self.server=app.LocalServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)
        history=self.service.task_history()
        history.begin(dict(username='alice',run_id='retry-me',running=True,status='running'),{})
        history.finish(dict(username='alice',run_id='retry-me',running=False,status='failed'))

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)

    def request(self,path,body=None,token=True):
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=10)
        connection.request('GET' if body is None else 'POST',path,None if body is None else json.dumps(body),{'X-Local-Token':app.TOKEN if token else ''})
        result=connection.getresponse();data=result.read();connection.close()
        return result.status,json.loads(data)

    def test_query_and_authenticated_retry_have_distinct_boundaries(self):
        status,data=self.request('/api/history?q=alice&state=failed')
        self.assertEqual(status,200);self.assertEqual(data['total'],1)
        self.assertEqual(self.request('/api/history?state=invalid')[0],400)
        with patch.object(self.service,'worker'), patch.object(self.service,'start',wraps=self.service.start) as start:
            self.assertEqual(self.request('/api/history/retry',{'id':'retry-me'},token=False)[0],403)
            start.assert_not_called()
            self.assertEqual(self.request('/api/history/retry',{'id':'missing'})[0],400)
            self.assertEqual(self.request('/api/history/retry',{'id':'retry-me'})[0],200)
            start.assert_called_once()


if __name__=='__main__':unittest.main()
