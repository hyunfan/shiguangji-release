import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timezone
from shiguangji.tasks.auto_batches import AutoBatches
from shiguangji.tasks.task_queue import TaskQueue
from shiguangji.tasks.task_status import summary
from shiguangji.tasks.task_service import TaskService


def outcome(at):
    return {'state':'finished','reason':'retry_cooldown','automatic_retry':True,'next_retry_at':
            datetime.fromtimestamp(at,timezone.utc).isoformat(),'counts':{}}


class DeferredTests(unittest.TestCase):
    def test_retry_limit_survives_export_history_and_stale_cooldown(self):
        import json
        from shiguangji.collect.archive import Collector
        from test_archive import FixtureClient
        with tempfile.TemporaryDirectory() as d, patch('shiguangji.tasks.task_service.threading.Thread'), patch('shiguangji.tasks.task_service.require_space'):
            base=Path(d)
            Collector('alice',base=base,client=FixtureClient()).run()
            initial=json.loads((base/'alice/manifest.json').read_text(encoding='utf-8'))['counts']
            due=outcome(time.time()+900)['next_retry_at']
            class WaitingCollector:
                def __init__(self,handle,options,report,stop,base):self.report=report
                def run(self):
                    self.report(counts={**initial,'media_retry_pending':1},
                                batch={'stop_code':'request_limit'},
                                retry_policy={'waiting':3,'next_retry_at':due})
                    return 'paused'
            service=TaskService(base,collector_for=lambda _:WaitingCollector,options_for=dict)
            queue=service.task_queue();queue.add(['alice'],{'auto_continue':True,'mode':'repair'})
            item=queue.next_item();item['retry_rounds']=3;item['retry_started']=time.time()-3600
            service.start('alice',item['options'],queue_id=item['id'])
            options={**item['options'],'_queue_id':item['id'],'_run_id':service.job['run_id']}
            status=service.collect_batches('alice',options)
            self.assertEqual(status,'partial')
            self.assertEqual(service.job['automatic']['stop_code'],'retry_limit')
            service.finalize_result('alice',status,options)
            service.progress(status=status,stage='done')
            service.finish_worker(options)
            final=json.loads((base/'alice/manifest.json').read_text(encoding='utf-8'))['task_summary']
            self.assertEqual(final['reason'],'retry_limit')
            self.assertFalse(final['automatic_retry']);self.assertEqual(final['next_retry_at'],'')
            self.assertEqual(summary(service.job)['reason'],'retry_limit')
            self.assertEqual(service.history_result({})['items'][0]['reason'],'retry_limit')
            self.assertEqual(queue.data['state'],'finished')
            self.assertTrue(queue.data['items'][0]['retry_exhausted'])

    def test_old_exhausted_status_is_corrected_only_for_exact_finished_run(self):
        import copy
        with tempfile.TemporaryDirectory() as d:
            q=TaskQueue(Path(d)/'q.json');q.add(['alice'],{})
            item=q.next_item();q.mark_running(item['id'],'alice','old')
            q.data['items'][0].update(state='finished',retry_exhausted=True)
            before=copy.deepcopy(q.data)
            old={**outcome(time.time()+900),'run_id':'old'}
            corrected=q.reconcile(old)
            self.assertEqual(corrected['reason'],'retry_limit')
            self.assertFalse(corrected['automatic_retry']);self.assertEqual(corrected['next_retry_at'],'')
            for other in ({**old,'run_id':'new'},{**old,'state':'running'},{**old,'reason':'error'}):
                self.assertEqual(q.reconcile(other),other)
            self.assertEqual(q.data,before)

    def test_stale_cooldown_without_pending_work_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as d:
            result=summary({'status':'partial','counts':{},'automatic':{'enabled':True,'stop_code':'pass_finished'},
                            'retry_policy':{'waiting':1,'next_retry_at':outcome(time.time()+900)['next_retry_at']}})
            q=TaskQueue(Path(d)/'q.json')
            self.assertFalse(q.defer_manual('alice',{'auto_continue':True},result))

    def test_expired_deadline_still_arms_scheduler(self):
        with tempfile.TemporaryDirectory() as d,patch('shiguangji.tasks.task_queue.time.time',return_value=1000) as clock:
            q=TaskQueue(Path(d)/'q.json');q.defer_manual('alice',{},outcome(1100))
            clock.return_value=1200
            self.assertTrue(q.next_retry());self.assertEqual(q.next_item()['username'],'alice')

    def test_service_timer_wakes_due_account_and_respects_pause(self):
        with tempfile.TemporaryDirectory() as d, patch('shiguangji.tasks.task_queue.time.time',return_value=1000) as clock, patch('shiguangji.tasks.task_service.threading.Timer') as timer, patch('shiguangji.tasks.task_service.threading.Thread'):
            service=TaskService(Path(d),collector_for=lambda *a:None,options_for=dict)
            q=service.task_queue();q.defer_manual('alice',{'mode':'repair'},outcome(1900))
            service.advance_queue()
            self.assertFalse(service.job['running']);timer.return_value.start.assert_called_once()
            callback=timer.call_args.args[1]
            q.pause();clock.return_value=2000;callback()
            self.assertFalse(service.job['running'])
            q.resume();service.advance_queue()
            self.assertTrue(service.job['running']);self.assertEqual(service.job['username'],'alice')
            self.assertEqual(q.data['items'][0]['state'],'running')

    def test_blocked_batch_is_not_counted_as_unproductive(self):
        session=AutoBatches(True)
        due=outcome(time.time()+900)['next_retry_at']
        self.assertFalse(session.after_batch('partial',{'requests':0},{'missing':3},pending=True,
                                            cooldown={'waiting':3,'next_retry_at':due}))
        self.assertEqual(session.empty_batches,0)
        result=summary({'status':'partial','automatic':session.snapshot(),'counts':{'missing':3}})
        self.assertEqual(result['reason'],'retry_cooldown')
        self.assertEqual(result['next_retry_at'],due)

    def test_deferred_account_yields_then_retries_and_stops_at_limit(self):
        with tempfile.TemporaryDirectory() as d, patch('shiguangji.tasks.task_queue.time.time',return_value=1000) as clock:
            q=TaskQueue(Path(d)/'q.json');q.add(['alice','bob'],{})
            alice=q.next_item();q.mark_running(alice['id'],'alice','a')
            q.finish(alice['id'],'a',outcome(1900))
            self.assertEqual(q.next_item()['username'],'bob')
            self.assertEqual(q.public()['counts']['pending'],2)
            q.remove(q.next_item()['id'])
            self.assertIsNone(q.next_item())
            self.assertEqual(TaskQueue(q.path).data['state'],'paused')
            for number in range(1,4):
                clock.return_value=1900+(number-1)*900
                self.assertEqual(q.next_item()['username'],'alice')
                q.mark_running(alice['id'],'alice',str(number))
                q.finish(alice['id'],str(number),outcome(clock.return_value+900))
            self.assertEqual(q.data['state'],'finished')
            self.assertTrue(q.data['items'][0]['retry_exhausted'])
            self.assertEqual(q.data['items'][0]['outcome']['reason'],'retry_limit')
            self.assertFalse(q.data['items'][0]['outcome']['automatic_retry'])

    def test_retry_age_limit_ends_instead_of_promising_a_future_run(self):
        with tempfile.TemporaryDirectory() as d,patch('shiguangji.tasks.task_queue.time.time',return_value=1000):
            q=TaskQueue(Path(d)/'q.json');q.add(['alice'],{})
            item=q.next_item();q.mark_running(item['id'],'alice','a')
            q.finish(item['id'],'a',outcome(1000+49*3600))
            self.assertEqual(q.data['state'],'finished')
            self.assertEqual(q.data['items'][0]['outcome']['reason'],'retry_limit')
            self.assertEqual(q.next_retry(),'')

    def test_manual_deferral_preserves_mode_and_paused_queue(self):
        with tempfile.TemporaryDirectory() as d:
            q=TaskQueue(Path(d)/'q.json');q.add(['bob'],{});q.pause()
            self.assertTrue(q.defer_manual('alice',{'mode':'repair','auto_continue':True,'_run_id':'private'},outcome(time.time()+900)))
            self.assertEqual(q.data['state'],'paused')
            self.assertEqual(q.data['items'][-1]['options'],{'mode':'repair','auto_continue':True})
            self.assertFalse(q.defer_manual('alice',{},outcome(time.time()+900)))

    def test_shutdown_and_user_pause_do_not_schedule_retries(self):
        with tempfile.TemporaryDirectory() as d:
            q=TaskQueue(Path(d)/'q.json');q.add(['alice'],{})
            item=q.next_item();q.mark_running(item['id'],'alice','a')
            q.finish(item['id'],'a',outcome(time.time()+900),stopping=True)
            self.assertNotIn('next_retry_at',q.data['items'][0])
            self.assertFalse(q.defer_manual('bob',{}, {'state':'paused',**{k:v for k,v in outcome(time.time()+900).items() if k!='state'}}))
