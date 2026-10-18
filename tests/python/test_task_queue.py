"""Serial worker, persistence and API tests; synthetic archives, no remote requests."""
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import app
import task_service
from archive import Store, export_collection, make_zip, zip_is_current
from extractor import blank_post
from task_queue import TaskQueue, parse_handles
from task_status import summary


class QueueStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / '.task-queue.json'
        self.queue = TaskQueue(self.path)

    def test_parse_normalizes_handles_links_and_separators(self):
        self.assertEqual(parse_handles('@Alice\nbob，https://x.com/CHARLIE; alice'), ['alice','bob','charlie','alice'])
        for invalid in ('alice\n../evil', '', '\n'.join('alice' for _ in range(101))):
            with self.assertRaises(ValueError): parse_handles(invalid)

    def test_duplicate_submission_is_idempotent_even_after_completion(self):
        result = self.queue.add(['alice','alice','bob'], {'proxy':'http://private-proxy'}, 'request-one')
        self.assertEqual(result, {'added':2,'duplicates':1})
        item = self.queue.next_item(); self.queue.mark_running(item['id'],'alice','run-one')
        self.queue.finish(item['id'],'run-one',summary({'status':'complete'}))
        self.assertTrue(self.queue.add(['alice','bob'],{},'request-one')['replayed'])
        self.assertEqual(len(self.queue.data['items']),2)
        self.assertNotIn('private-proxy',json.dumps(self.queue.public()))

    def test_restart_preserves_order_settings_and_requires_resume(self):
        self.queue.add(['alice','bob'], {'max_media_mb':64})
        item = self.queue.next_item(); self.queue.mark_running(item['id'],'alice','run-one')
        restored = TaskQueue(self.path)
        self.assertEqual(restored.data['state'],'paused')
        self.assertIsNone(restored.next_item())
        self.assertEqual([i['username'] for i in restored.data['items']],['alice','bob'])
        self.assertEqual(restored.data['items'][0]['options']['max_media_mb'],64)
        restored.resume(); self.assertEqual(restored.next_item()['username'],'alice')

    def test_append_never_unpauses_the_queue(self):
        self.queue.add(['alice'],{}); self.queue.pause()
        self.queue.add(['bob'],{})
        self.assertEqual(self.queue.data['state'],'paused')
        self.assertIsNone(self.queue.next_item())

    def test_write_failure_does_not_report_items_as_saved(self):
        with patch('task_queue.atomic_write_text',side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.queue.add(['alice'],{})
        self.assertEqual(self.queue.data['items'],[])

    def test_corrupt_queue_is_preserved_and_cannot_be_silently_overwritten(self):
        self.path.write_text('{broken',encoding='utf-8')
        restored=TaskQueue(self.path)
        self.assertTrue(restored.error)
        with self.assertRaises(ValueError): restored.add(['alice'],{})
        self.assertEqual(self.path.read_text(encoding='utf-8'),'{broken')

    def test_stale_worker_cannot_finish_another_run(self):
        self.queue.add(['alice'],{}); item=self.queue.next_item()
        self.queue.mark_running(item['id'],'alice','new-run')
        self.queue.finish(item['id'],'old-run',summary({'status':'complete'}))
        self.assertEqual(self.queue.data['items'][0]['state'],'running')
        with self.assertRaises(ValueError): self.queue.remove(item['id'])

    def test_resumed_item_does_not_still_display_previous_paused_outcome(self):
        self.queue.add(['alice'],{}); item=self.queue.next_item()
        self.queue.mark_running(item['id'],'alice','old-run')
        self.queue.finish(item['id'],'old-run',summary({'status':'paused','batch':{'stop_code':'user_pause'}}))
        self.queue.resume();self.queue.mark_running(item['id'],'alice','new-run')
        current=self.queue.public()['items'][0]
        self.assertNotIn('outcome',current)
        self.assertNotIn('finished_at',current)
        self.assertEqual(current['state'],'running')


class QueueIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name)
        from storage_policy import save_settings
        save_settings(self.base,{'zip_on_demand':False})  # Existing ZIP-before-next tests cover this optional mode.
        self.patches=[patch.object(app.TASKS, 'base',self.base),patch.object(app.TASKS, 'queue',None),
                      patch.object(app.TASKS, 'stop',threading.Event()),patch.object(app.TASKS, 'stopping',threading.Event()),
                      patch.dict(app.TASKS.job,{'running':False,'username':'','status':'idle','stage':'idle','counts':{},'logs':[]},clear=True)]
        for p in self.patches:p.start()
        self.started=[]; self.finished=[]; self.entered=threading.Event(); self.release=threading.Event(); self.release.set()
        self.guards=0; self.peak_guards=0
        @contextmanager
        def guard(callback):
            self.guards+=1; self.peak_guards=max(self.guards,self.peak_guards)
            try: yield
            finally:self.guards-=1
        self.patches += [patch.object(app.TASKS, 'collect_batches',side_effect=self.collect),patch.object(task_service, 'prevent_idle_sleep',guard)]
        self.patches[-2].start(); self.patches[-1].start()
        self.server=app.LocalServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        app.TASKS.stopping.set();app.TASKS.stop.set();self.release.set()
        self.wait(lambda:not app.TASKS.job.get('running'))
        self.server.shutdown();self.server.server_close();self.thread.join(3)
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()

    def wait(self,predicate):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            with app.TASKS.lock:
                if predicate():return
            time.sleep(.01)
        self.fail('Queue did not settle in time')

    def collect(self,handle,options):
        self.started.append((handle,dict(options)));self.entered.set()
        if not self.release.wait(5):raise TimeoutError('fixture stalled')
        paused=app.TASKS.stop.is_set()
        status='paused' if paused else 'partial'
        automatic={'enabled':True,'stop_code':'user_pause' if paused else 'no_progress'}
        store=Store(self.base/handle,handle)
        try:
            post=blank_post('1346889436626259968',handle);post['text']='Synthetic queue test'
            store.put_post(post)
            m=export_collection(store,status,[],run_info={'run_id':options['_run_id'],'automatic':automatic})
        finally:store.close()
        app.TASKS.progress(counts=m['counts'],automatic=automatic)
        return status

    def request(self,path,body=None,token=True):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        headers={'Content-Type':'application/json'}
        if token:headers['X-Local-Token']=app.TOKEN
        c.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,headers)
        r=c.getresponse();result=(r.status,json.loads(r.read()));c.close();return result

    def add(self,names='alice\nbob',**options):
        status,result=self.request('/api/queue/add',{'usernames':names,**options})
        self.assertEqual(status,200,result);return result

    def test_serial_next_begins_after_zip_and_sleep_guard_are_released(self):
        def zip_checked(folder):
            if folder.name=='bob':self.assertTrue(zip_is_current(self.base/'alice'))
            value=make_zip(folder);self.finished.append(folder.name);return value
        with patch.object(task_service, 'make_zip',side_effect=zip_checked):
            self.add(max_media_mb=64,public_mirrors=False,auto_continue=False)
            self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['alice','bob'])
        self.assertEqual(self.finished,['alice','bob']);self.assertEqual(self.peak_guards,1)
        self.assertTrue(all(o['auto_continue'] and o['mode']=='full' and o['max_media_mb']==64 and not o['public_mirrors'] for _,o in self.started))
        self.assertTrue(all(i['state']=='finished' for i in app.TASKS.task_queue().data['items']))

    def test_on_demand_queue_finishes_without_zip_and_keeps_offline_pages(self):
        from storage_policy import save_settings
        save_settings(self.base,{'zip_on_demand':True})
        with patch.object(task_service,'make_zip') as build:
            self.add();self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
            build.assert_not_called()
        self.assertEqual([i['state'] for i in app.TASKS.task_queue().data['items']],['finished','finished'])
        self.assertTrue((self.base/'alice/index.html').is_file())
        self.assertTrue((self.base/'bob/index.html').is_file())

    def test_pause_stops_whole_queue_and_resume_uses_saved_progress(self):
        self.release.clear();self.add();self.assertTrue(self.entered.wait(2))
        self.assertEqual(self.request('/api/queue/pause',{})[0],200)
        self.assertEqual(self.request('/api/queue/resume',{})[0],400)
        self.release.set();self.wait(lambda:not app.TASKS.job['running'])
        self.assertEqual([h for h,_ in self.started],['alice'])
        self.assertEqual(app.TASKS.task_queue().data['state'],'paused')
        self.assertTrue(zip_is_current(self.base/'alice'))
        self.assertEqual(self.request('/api/start',{'username':'alice'})[0],200)
        self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['alice','alice','bob'])

    def test_one_zip_failure_is_recorded_and_next_account_still_runs(self):
        def generate(folder):
            if folder.name=='alice':raise OSError('fixture ZIP failure')
            return make_zip(folder)
        with patch.object(task_service, 'make_zip',side_effect=generate):
            self.add();self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        items=app.TASKS.task_queue().data['items']
        self.assertEqual([i['state'] for i in items],['failed','finished'])
        self.assertEqual(items[0]['outcome']['reason'],'zip_failed')
        self.assertTrue(zip_is_current(self.base/'bob'))

    def test_stop_request_prevents_launching_next_account(self):
        self.release.clear();self.add();self.assertTrue(self.entered.wait(2))
        app.TASKS.stopping.set();app.TASKS.stop.set();self.release.set()
        self.wait(lambda:not app.TASKS.job['running'])
        self.assertEqual([h for h,_ in self.started],['alice'])
        self.assertEqual(app.TASKS.task_queue().data['state'],'paused')
        self.assertEqual(TaskQueue(self.base/'.task-queue.json').data['state'],'paused')

    def test_remove_pending_does_not_delete_collections_or_skip_the_next_one(self):
        self.release.clear();self.add('alice bob charlie');self.assertTrue(self.entered.wait(2))
        items=app.TASKS.task_queue().data['items']
        self.assertEqual(self.request('/api/queue/remove',{'id':items[0]['id']})[0],400)
        self.assertEqual(self.request('/api/queue/remove',{'id':items[1]['id']})[0],200)
        self.release.set();self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['alice','charlie'])

    def test_invalid_input_is_atomic_and_queue_api_requires_token(self):
        self.assertEqual(self.request('/api/queue/add',{'usernames':'alice ../evil'})[0],400)
        self.assertEqual(self.request('/api/queue/add',{'usernames':'alice'},token=False)[0],403)
        self.assertEqual(app.TASKS.task_queue().data['items'],[])
        self.assertFalse((self.base/'alice').exists())

    def test_enqueue_while_manual_task_runs_waits_and_both_pause_controls_stop_queue(self):
        app.TASKS.job.update(running=True,username='manual',stage='pages',run_id='manual',automatic={'enabled':True})
        self.add();self.assertEqual(self.started,[])
        self.assertEqual(self.request('/api/pause',{})[0],200)
        self.assertEqual(app.TASKS.task_queue().data['state'],'paused')
        app.TASKS.job.update(running=False,pause_requested=False)
        self.assertEqual(self.request('/api/queue/resume',{})[0],200)
        self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['alice','bob'])

    def test_thread_start_failure_does_not_leave_running_queue_entry(self):
        with app.TASKS.lock:
            app.TASKS.task_queue().add(['alice','bob'],{})
            with patch.object(app.threading,'Thread',side_effect=RuntimeError('cannot spawn')):
                app.TASKS.advance_queue()
        self.assertFalse(app.TASKS.job['running'])
        self.assertEqual([i['state'] for i in app.TASKS.task_queue().data['items']],['failed','failed'])

    def test_delete_is_blocked_until_pending_account_is_removed(self):
        app.TASKS.task_queue().add(['alice'],{}); app.TASKS.task_queue().pause()
        with patch.object(app,'delete_collection') as delete:
            status,_=self.request('/api/delete',{'username':'alice','confirm_username':'alice'})
        self.assertEqual(status,409);delete.assert_not_called()

    def test_append_and_deduplicate_while_running_never_starts_a_second_worker(self):
        self.release.clear();self.add();self.assertTrue(self.entered.wait(2))
        result=self.add('alice bob charlie')
        self.assertEqual((result['added'],result['duplicates']),(1,2))
        self.assertEqual([h for h,_ in self.started],['alice'])
        self.release.set();self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['alice','bob','charlie'])

    def test_storage_failure_returns_error_without_starting_network_work(self):
        with patch('task_queue.atomic_write_text',side_effect=OSError('disk full')):
            status,result=self.request('/api/queue/add',{'usernames':'alice bob'})
        self.assertEqual(status,503);self.assertIn('无法保存',result['error'])
        self.assertEqual(self.started,[]);self.assertEqual(app.TASKS.task_queue().data['items'],[])

    def test_manual_job_completion_starts_waiting_queue(self):
        app.TASKS.job.update(running=True,username='manual',stage='pages',run_id='manual')
        self.add('alice bob');self.assertEqual(self.started,[])
        app.TASKS.worker('manual',{'_run_id':'manual'})
        self.wait(lambda:app.TASKS.task_queue().data['state']=='finished')
        self.assertEqual([h for h,_ in self.started],['manual','alice','bob'])


if __name__=='__main__':unittest.main()
