"""Guard decisions on a temporary installation; no real server is started."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import launcher
import supervisor
from shiguangji.system.reliability import save_settings

PORT = 9876
SESSION = 'a' * 32
PID = 1234


class GuardFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / 'collections').mkdir()

    def ready(self, session=SESSION, pid=PID, state='ready'):
        launcher.write_state(self.root, PORT, dict(root=str(self.root), port=PORT, session=session, pid=pid, state=state))

    def guard(self, **values):
        data = dict(state='watching', message='', pid=os.getpid(), session=SESSION, restarts=0)
        data.update(values)
        (launcher.runtime(self.root) / f'guard-{PORT}.json').write_text(json.dumps(data), encoding='utf-8')

    def job(self, **job):
        path = self.root / 'collections' / '.job-state.json'
        path.write_text(json.dumps({'job': job}), encoding='utf-8')


class SupervisorStateTests(GuardFixture):
    def test_restart_requires_the_same_ready_session_and_no_stop_request(self):
        self.assertFalse(supervisor.may_restart(self.root, PORT, SESSION, PID))
        self.ready()
        self.assertTrue(supervisor.may_restart(self.root, PORT, SESSION, PID))
        self.assertFalse(supervisor.may_restart(self.root, PORT, 'b' * 32, PID))
        self.assertFalse(supervisor.may_restart(self.root, PORT, SESSION, PID + 1))
        self.ready(state='stopped')
        self.assertFalse(supervisor.may_restart(self.root, PORT, SESSION, PID))
        self.ready()
        (launcher.runtime(self.root) / f'stop-{PORT}-{SESSION}').touch()
        self.assertFalse(supervisor.may_restart(self.root, PORT, SESSION, PID))

    def test_queue_is_resumed_only_when_opted_in_and_job_was_running(self):
        self.job(running=True, run_id='run-1')
        self.assertEqual(supervisor.recovery_run(self.root), '')
        save_settings(self.root / 'collections', {'resume_queue_after_crash': True})
        self.assertEqual(supervisor.recovery_run(self.root), 'run-1')
        self.job(running=True, run_id='run-1', pause_requested=True)
        self.assertEqual(supervisor.recovery_run(self.root), '')
        self.job(running=False, run_id='run-1')
        self.assertEqual(supervisor.recovery_run(self.root), '')
        (self.root / 'collections' / '.job-state.json').write_text('{broken', encoding='utf-8')
        self.assertEqual(supervisor.recovery_run(self.root), '')
        (self.root / 'collections' / '.job-state.json').unlink()
        self.assertEqual(supervisor.recovery_run(self.root), '')

    def test_guard_status_only_describes_the_current_session(self):
        self.assertEqual(supervisor.guard_status(self.root, PORT), {'state': 'off'})
        self.ready()
        self.guard(session='b' * 32)
        self.assertEqual(supervisor.guard_status(self.root, PORT), {'state': 'off'})
        self.guard(state='attention', message='请查看日志', restarts=3)
        self.assertEqual(supervisor.guard_status(self.root, PORT),
                         {'state': 'attention', 'message': '请查看日志', 'restarts': 3})
        (launcher.runtime(self.root) / f'guard-{PORT}.json').write_text('not json', encoding='utf-8')
        self.assertEqual(supervisor.guard_status(self.root, PORT), {'state': 'off'})

    @unittest.skipIf(os.name == 'nt', 'POSIX process probe')
    def test_watching_guard_is_reported_only_while_its_process_lives(self):
        self.ready()
        self.guard()
        self.assertEqual(supervisor.guard_status(self.root, PORT)['state'], 'watching')
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        self.guard(pid=child.pid)
        self.assertEqual(supervisor.guard_status(self.root, PORT), {'state': 'off'})

    @unittest.skipIf(os.name == 'nt', 'POSIX process probe')
    def test_posix_process_reference_tracks_liveness(self):
        reference = supervisor.ProcessReference(os.getpid())
        self.assertTrue(reference.alive())
        reference.close()
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        self.assertFalse(supervisor.ProcessReference(child.pid).alive())

    @unittest.skipUnless(os.name == 'nt', 'Windows process handle')
    def test_windows_process_reference_tracks_liveness(self):
        reference = supervisor.ProcessReference(os.getpid())
        try:
            self.assertTrue(reference.alive())
        finally:
            reference.close()
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        reference = supervisor.ProcessReference(child.pid)
        try:
            self.assertFalse(reference.alive())
        finally:
            reference.close()


class SuperviseTests(GuardFixture):
    def setUp(self):
        super().setUp()
        self.ready()
        self.sleeps = []

    def status(self):
        return json.loads((launcher.runtime(self.root) / f'guard-{PORT}.json').read_text(encoding='utf-8'))

    def run_guard(self, *, alive, start, sleep=None):
        process = Mock()
        process.return_value.alive.side_effect = alive
        supervisor.supervise(self.root, PORT, SESSION, PID, sleep=sleep or self.sleeps.append,
                             clock=lambda: 100, process=process, start=start)
        return process

    def test_crash_restarts_with_backoff_and_resumes_queue_then_watches_new_session(self):
        save_settings(self.root / 'collections', {'resume_queue_after_crash': True})
        self.job(running=True, run_id='run-7')
        calls = []

        def start(root, port, **kwargs):
            calls.append(kwargs)
            self.ready(session='b' * 32, pid=PID + 1)

        def stop_new_session(_):
            launcher.background_stopped(self.root, PORT, 'b' * 32)

        alive = iter([False, True, True])
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                stop_new_session(seconds)

        process = self.run_guard(alive=lambda: next(alive), start=start, sleep=sleep)
        self.assertEqual(calls, [{'supervised': False, 'recovery_run': 'run-7'}])
        self.assertEqual([c.args for c in process.call_args_list], [(PID,), (PID + 1,)])
        self.assertEqual(sleeps, [1, 1, 1])
        status = self.status()
        self.assertEqual((status['state'], status['session'], status['server_pid']), ('stopped', 'b' * 32, PID + 1))
        self.assertEqual(status['restarts'], 1)

    def test_stop_request_during_backoff_cancels_restart(self):
        start = Mock()

        def sleep(_):
            (launcher.runtime(self.root) / f'stop-{PORT}-{SESSION}').touch()

        self.run_guard(alive=lambda: False, start=start, sleep=sleep)
        start.assert_not_called()
        self.assertEqual(self.status()['state'], 'stopped')

    def test_unconfirmed_new_session_needs_attention(self):
        start = Mock()
        self.run_guard(alive=lambda: False, start=start)
        start.assert_called_once()
        status = self.status()
        self.assertEqual(status['state'], 'attention')
        self.assertIn('无法确认新的后台会话', status['message'])

    def test_competing_launcher_is_left_alone_but_other_failures_are_reported(self):
        self.run_guard(alive=lambda: False, start=Mock(side_effect=RuntimeError('Another launcher is still starting.')))
        self.assertEqual(self.status()['state'], 'restarting')
        self.run_guard(alive=lambda: False, start=Mock(side_effect=RuntimeError('app.py was not found')))
        self.assertEqual((self.status()['state'], self.status()['message']), ('attention', 'app.py was not found'))
        self.run_guard(alive=Mock(side_effect=OSError('无法读取后台进程状态。')), start=Mock())
        self.assertEqual((self.status()['state'], self.status()['message']), ('attention', '无法读取后台进程状态。'))


if __name__ == '__main__':
    unittest.main()
