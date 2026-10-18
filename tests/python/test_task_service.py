"""Task lifecycle exercised directly, without an HTTP app or real sources."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from task_service import TaskService


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.factory = Mock()
        self.service = TaskService(self.base, collector_for=self.factory, options_for=dict)

    def test_libraries_do_not_share_state_cancellation_or_queue(self):
        other = TaskService(self.base / 'other', collector_for=self.factory, options_for=dict)
        self.service.stop.set()
        self.service.progress(status='paused')
        self.service.task_queue().add(['alice'], {})
        self.assertFalse(other.stop.is_set())
        self.assertEqual(other.job['status'], 'idle')
        self.assertEqual(other.task_queue().data['items'], [])

    def test_old_progress_and_worker_cannot_change_a_new_run(self):
        service = self.service
        with patch('task_service.threading.Thread'):
            service.start('alice', {})
            old_id = service.job['run_id']
            old_report = service.progress_callback(old_id)
            old_report(counts={'posts': 2})
            self.assertEqual(service.job['counts']['posts'], 2)
            service.job['running'] = False
            service.start('bob', {})
        before = copy.deepcopy(service.job)
        old_report(counts={'posts': 999}, message='late update')
        with patch.object(service, '_worker_body') as body:
            service.worker('alice', {'_run_id':old_id})
            body.assert_not_called()
        self.assertEqual(service.job, before)
        self.assertFalse((self.base / 'alice').exists())

    def test_finished_run_ignores_late_collector_progress(self):
        self.service.job.update(run_id='one', running=True)
        report = self.service.progress_callback('one')
        self.service.job['running'] = False
        before = copy.deepcopy(self.service.job)
        report(status='running', counts={'posts':999})
        self.assertEqual(self.service.job, before)

    def test_thread_start_failure_can_be_retried_without_stuck_running_state(self):
        with patch('task_service.threading.Thread', side_effect=RuntimeError('cannot start')):
            with self.assertRaises(RuntimeError):
                self.service.start('alice', {})
        self.assertFalse(self.service.job['running'])
        self.assertEqual(self.service.job['sleep_prevention'], 'off')
        with patch('task_service.threading.Thread') as thread:
            self.service.start('bob', {})
            thread.return_value.start.assert_called_once()
        self.assertTrue(self.service.job['running'])

    def test_restore_of_running_checkpoint_does_not_restart_collection(self):
        with patch('task_service.threading.Thread'):
            self.service.start('alice', {})
        restored = TaskService(self.base, collector_for=self.factory, options_for=dict)
        restored.restore_job()
        self.assertEqual(restored.job['status'], 'paused')
        self.assertFalse(restored.job['running'])
        self.factory.assert_not_called()

    def test_pause_stops_queue_and_preserves_final_manual_export(self):
        service = self.service
        service.job.update(running=True, stage='pages')
        service.task_queue().add(['bob'], {})
        self.assertTrue(service.pause_current())
        self.assertTrue(service.stop.is_set())
        self.assertEqual(service.task_queue().data['state'], 'paused')
        service.stop.clear()
        service.job.update(stage='zip', pause_requested=False)
        self.assertFalse(service.pause_current())
        self.assertFalse(service.stop.is_set())


if __name__ == '__main__':
    unittest.main()
