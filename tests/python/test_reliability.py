"""Fault injection with isolated fixture libraries; no external archive claims."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from shiguangji.collect.archive import Client, Collector
from shiguangji.core.core_errors import ArchiveError
from shiguangji.sources.retry_policy import RetryPolicy, DeferredRequest, TrackedResponse
from shiguangji.storage.storage_guard import StorageLow, rotate_log
from shiguangji.storage.backup_library import backup_collection, restore_backup, digest, sqlite_copy
from shiguangji.system.reliability import settings, save_settings, worker_health
from shiguangji.tasks.task_service import TaskService
from shiguangji.tasks.task_status import summary
from test_archive import FixtureClient, FakeResponse, PNG
import supervisor
import launcher


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name);self.base=self.root/'collections';self.base.mkdir()
        self.path=self.base/'retry.json';self.clock=1000

    def policy(self, **kwargs):return RetryPolicy(self.path,clock=lambda:self.clock,**kwargs)

    def test_http_failure_is_deferred_in_a_new_client_without_sending_request(self):
        url='https://web.archive.org/web/20200101000000id_/https://twitter.com/alice'
        client=Client(delay=0,attempts=1);client.retry_policy=self.policy()
        client.opener=Mock();client.opener.open.side_effect=HTTPError(url,404,'missing',{},None)
        with self.assertRaises(ArchiveError):client.open(url)
        other=Client(delay=0,attempts=1);other.retry_policy=self.policy();other.opener=Mock()
        with self.assertRaises(DeferredRequest):other.open(url)
        other.opener.open.assert_not_called();self.assertEqual(other.requests,0)
        self.clock+=86401
        self.assertTrue(self.policy().allowed(url))

    def test_host_breaker_persists_and_force_is_once_per_run(self):
        policy=self.policy()
        for n in range(3):policy.failure(f'https://web.archive.org/{n}',503)
        self.assertFalse(self.policy().allowed('https://web.archive.org/other'))
        self.assertTrue(self.policy().allowed('https://pbs.twimg.com/media/a'))
        forced=self.policy(force_run='first');self.assertFalse(forced.allowed('https://web.archive.org/other'))
        forced.failure('https://pbs.twimg.com/media/a',404)
        self.assertFalse(self.policy(force_run='first').allowed('https://pbs.twimg.com/media/a'))
        self.assertTrue(self.policy(force_run='next').allowed('https://pbs.twimg.com/media/a'))

    def test_retry_snapshot_uses_effective_host_deadline_after_restart(self):
        from shiguangji.sources.retry_policy import utc
        policy=self.policy()
        for n in range(3):policy.failure(f'https://web.archive.org/{n}',503)
        self.assertEqual(self.policy().snapshot()['next_retry_at'],utc(self.clock+900))
        # An unrelated host may become available earlier.
        policy.failure('https://pbs.twimg.com/media/a',503)
        self.assertEqual(self.policy().snapshot()['next_retry_at'],utc(self.clock+60))
        self.clock+=61
        self.assertEqual(self.policy().snapshot()['next_retry_at'],utc(1900))
        self.assertNotIn('https://',self.path.read_text(encoding='utf-8'))

    def test_old_retry_records_learn_host_when_checked(self):
        from shiguangji.sources.retry_policy import utc
        url='https://web.archive.org/old'
        policy=self.policy();policy.failure(url,503)
        policy.data['urls'][policy.key(url)].pop('host',None)
        policy.data['hosts']['web.archive.org']={'count':3,'until':1900}
        policy.save();policy=self.policy()
        with self.assertRaises(DeferredRequest):policy.check(url)
        self.assertEqual(policy.snapshot()['next_retry_at'],utc(1900))

    def test_body_failure_does_not_reset_history_or_redirect_metadata(self):
        policy=self.policy();url='https://web.archive.org/a';policy.failure(url)
        response=FakeResponse(b'content','https://web.archive.org/redirected','text/plain')
        with self.assertRaises(TimeoutError):
            with TrackedResponse(response,policy,url) as tracked:
                self.assertEqual(tracked.url,response.url)
                raise TimeoutError('body interrupted')
        self.assertEqual(policy.data['urls'][policy.key(url)]['count'],1)
        with TrackedResponse(FakeResponse(b'ok',url),policy,url):pass
        self.assertTrue(policy.allowed(url))

    def test_bad_retry_file_is_safe_and_cooldown_never_means_complete_scope(self):
        self.path.write_text('[1,2]',encoding='utf-8');self.assertEqual(self.policy().snapshot()['waiting'],0)
        status=summary({'status':'complete','retry_policy':{'waiting':3},'counts':{}})
        self.assertEqual(status['reason'],'retry_cooldown');self.assertEqual(status['scope_status'],'partial')

    def test_low_space_stops_before_network_and_preserves_old_file(self):
        client=Client(delay=0);client.open=Mock()
        old=self.base/'old.png';old.write_bytes(PNG)
        with patch('shiguangji.storage.storage_guard.shutil.disk_usage',return_value=Mock(free=1)):
            with self.assertRaises(StorageLow):client.download('https://pbs.twimg.com/media/a.png',old,0)
        client.open.assert_not_called();self.assertEqual(old.read_bytes(),PNG)
        self.assertFalse(old.with_suffix('.png.part').exists())

    def test_low_space_pauses_queue_without_failing_pending_accounts(self):
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        service.task_queue().add(['alice','bob'],{})
        with patch('shiguangji.tasks.queue_coordinator.require_space',side_effect=StorageLow('full')),patch.object(service,'start') as start:
            service.advance_queue()
        start.assert_not_called();self.assertEqual(service.task_queue().data['reason'],'storage_low')
        self.assertEqual([i['state'] for i in service.task_queue().data['items']],['pending','pending'])

    def test_health_read_does_not_turn_polling_or_logs_into_progress(self):
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        service.job.update(running=True,stage='pages');service.activity_clock=service.progress_clock=0
        with patch('shiguangji.tasks.task_service.time.monotonic',return_value=700):
            service.progress(message='waiting again')
            health=service.health_snapshot();self.assertEqual(health['state'],'slow')
            self.assertEqual(service.health_snapshot()['progress_seconds_ago'],700)
            service.progress(counts={'parsed':1})
            self.assertEqual(service.health_snapshot()['state'],'active')
        self.assertEqual(worker_health({'running':True,'stage':'zip'},700,0,0)['state'],'saving')

    def test_backup_restore_database_media_incremental_and_corruption(self):
        Collector('alice',base=self.base,client=FixtureClient()).run()
        destination=self.root/'backups'
        first=backup_collection(self.base,'alice',destination)
        second=backup_collection(self.base,'alice',destination)
        self.assertGreater(first['copied'],0);self.assertEqual(second['copied'],0)
        restored=restore_backup(second['path'],self.root/'restored')
        with closing(sqlite3.connect(restored/'alice/archive.sqlite')) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],1)
        source=next(p for p in (self.base/'alice/media').rglob('*') if p.is_file())
        self.assertEqual(digest(source),digest(restored/source.relative_to(self.base)))
        with self.assertRaises(ValueError):restore_backup(second['path'],restored)
        object_path=next(p for p in (destination/'objects').rglob('*') if p.is_file());object_path.write_bytes(b'broken')
        with self.assertRaises(ValueError):restore_backup(second['path'],self.root/'invalid')
        self.assertFalse((self.root/'invalid').exists())
        self.assertEqual(backup_collection(self.base,'alice',destination)['copied'],1)

    def test_same_size_and_timestamp_changes_are_not_missed_by_backup(self):
        import os
        Collector('alice',base=self.base,client=FixtureClient()).run()
        destination=self.root/'backups';backup_collection(self.base,'alice',destination)
        from shiguangji.storage.compressed_files import logical_files, logical_stat, read_bytes
        raw=next(logical_files(self.base/'alice/raw'));before=logical_stat(raw);data=read_bytes(raw)
        raw.write_bytes(b'X'+data[1:]);os.utime(raw,ns=(before.st_mtime_ns,before.st_mtime_ns))
        self.assertEqual(backup_collection(self.base,'alice',destination)['copied'],1)

    def test_backup_path_traversal_is_rejected_before_restore(self):
        Collector('alice',base=self.base,client=FixtureClient()).run()
        result=backup_collection(self.base,'alice',self.root/'backups');state=Path(result['path'])
        data=json.loads(state.read_text());record=next(iter(data['files'].values()));data['files']['../escape']=record
        state.write_text(json.dumps(data))
        with self.assertRaises(ValueError):restore_backup(state,self.root/'restore')
        self.assertFalse((self.root/'restore').exists())

    def test_backup_sqlite_busy_wait_is_bounded(self):
        source=self.base/'busy.sqlite'
        with closing(sqlite3.connect(source)) as db:db.execute('CREATE TABLE test (id INTEGER)');db.commit()
        connection=sqlite3.connect(source)
        try:
            connection.execute('BEGIN EXCLUSIVE')
            with self.assertRaises(TimeoutError):sqlite_copy(source,self.root/'copy.sqlite',timeout=0.1)
        finally:connection.rollback();connection.close()

    def test_interrupted_backup_does_not_leave_large_partial_media(self):
        Collector('alice',base=self.base,client=FixtureClient()).run()
        destination=self.root/'backups'
        def interrupted(source,target):
            Path(target).write_bytes(b'partial');raise OSError('copy interrupted')
        with patch('shiguangji.storage.backup_library.shutil.copyfile',side_effect=interrupted):
            with self.assertRaises(OSError):backup_collection(self.base,'alice',destination)
        self.assertFalse(list(destination.rglob('*.part')))
        self.assertTrue((self.base/'alice/archive.sqlite').is_file())

    def test_settings_are_strict_and_default_to_no_auto_resume(self):
        self.assertFalse(settings(self.base)['resume_queue_after_crash'])
        save_settings(self.base,{'resume_queue_after_crash':True})
        self.assertTrue(settings(self.base)['resume_queue_after_crash'])
        with self.assertRaises(ValueError):save_settings(self.base,{'resume_queue_after_crash':'false'})

    def test_backup_failure_keeps_collection_outcome_and_pauses_following_queue(self):
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        service.task_queue().add(['alice','bob'],{})
        service.job.update(status='partial',task_summary={'state':'finished','reason':'sources_unavailable'})
        with patch('shiguangji.storage.backup_library.backup_collection',side_effect=OSError('copy failed')):
            service.backup_result('alice')
        self.assertEqual(service.job['status'],'partial')
        self.assertEqual(service.job['backup']['state'],'failed')
        self.assertEqual(service.task_queue().data['reason'],'backup_failed')

    def test_storage_failure_during_final_export_preserves_checkpoint(self):
        Collector('alice',base=self.base,client=FixtureClient()).run()
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        service.job.update(running=True,username='alice',run_id='fixture',status='running')
        with patch.object(service,'collect_batches',return_value='complete'),patch.object(service,'finalize_result',side_effect=StorageLow('full')):
            service._worker_body('alice',{})
        manifest=json.loads((self.base/'alice/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['task_summary']['reason'],'storage_low')
        self.assertEqual(service.job['status'],'paused')
        self.assertEqual(manifest['counts']['saved'],1)

    def test_recovery_requires_matching_interrupted_queue_and_opt_in(self):
        service=TaskService(self.base,collector_for=Mock(),options_for=dict)
        queue=service.task_queue();queue.add(['alice','bob'],{})
        queue.mark_running(queue.data['items'][0]['id'],'alice','run')
        service.job.update(username='alice',run_id='run',interrupted=True,running=False)
        with patch.object(service,'advance_queue') as advance:
            self.assertFalse(service.recover_queue('run'));advance.assert_not_called()
            save_settings(self.base,{'resume_queue_after_crash':True})
            self.assertFalse(service.recover_queue('other'));advance.assert_not_called()
            service.recover_queue('run');advance.assert_called_once()
        queue.pause()
        with patch.object(service,'advance_queue') as advance:
            service.recover_queue('run');advance.assert_not_called()

    def test_rotation_bounds_only_application_log(self):
        path=self.base/'run-log.jsonl'
        for n in range(6):path.write_text(str(n)*10);rotate_log(path,limit=5,copies=2)
        self.assertEqual(path.with_name(path.name+'.1').read_text(),'5'*10)
        self.assertEqual(path.with_name(path.name+'.2').read_text(),'4'*10)
        self.assertFalse(path.with_name(path.name+'.3').exists())


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name).resolve()
        self.session='a'*32;self.pid=1234;self.port=9876
        launcher.write_state(self.root,self.port,dict(root=str(self.root),port=self.port,session=self.session,pid=self.pid,state='ready'))

    def test_live_process_and_normal_stop_never_restart(self):
        process=Mock();process.return_value.alive.return_value=True
        start=Mock()
        def stop(_):launcher.background_stopped(self.root,self.port,self.session)
        supervisor.supervise(self.root,self.port,self.session,self.pid,sleep=stop,process=process,start=start)
        start.assert_not_called();process.return_value.close.assert_called_once()

    def test_crashes_are_limited_to_three_restarts(self):
        process=Mock();process.return_value.alive.return_value=False
        sequence=[]
        def start(root,port,**kwargs):
            sequence.append(kwargs)
            launcher.write_state(root,port,dict(root=str(root),port=port,session=str(len(sequence))*32,pid=2000+len(sequence),state='ready'))
        supervisor.supervise(self.root,self.port,self.session,self.pid,sleep=lambda _:None,clock=lambda:100,process=process,start=start)
        self.assertEqual(len(sequence),3);self.assertTrue(all(not k['supervised'] for k in sequence))
        state=json.loads((self.root/'runtime'/f'guard-{self.port}.json').read_text(encoding='utf-8'))
        self.assertEqual(state['state'],'attention')

    def test_stop_marker_blocks_restart_even_if_process_already_exited(self):
        (launcher.runtime(self.root)/f'stop-{self.port}-{self.session}').touch()
        start=Mock();process=Mock()
        supervisor.supervise(self.root,self.port,self.session,self.pid,process=process,start=start)
        start.assert_not_called();process.assert_not_called()


if __name__=='__main__':unittest.main()
