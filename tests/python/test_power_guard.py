"""Sleep requests are bounded to the worker, including failure and final exports."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import app
from shiguangji.tasks import result_finalizer, task_service
from shiguangji.system import power_guard as power


class PowerGuardTests(unittest.TestCase):
    def test_system_only_request_is_released_on_exception_in_same_thread(self):
        calls, states = [], []
        def setter(flags):
            calls.append((threading.get_ident(), flags))
            return power.ES_CONTINUOUS
        def task():
            with self.assertRaisesRegex(RuntimeError, 'failure'):
                with power.prevent_idle_sleep(states.append) as active:
                    self.assertTrue(active)
                    raise RuntimeError('failure')
        with patch.object(power.sys, 'platform', 'win32'), patch.object(power, 'execution_state_api', return_value=setter):
            thread = threading.Thread(target=task)
            thread.start(); thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([flags for _, flags in calls], [0x80000001, 0x80000000])
        self.assertEqual(len({identity for identity, _ in calls}), 1)
        self.assertNotEqual(calls[0][0], threading.get_ident())
        self.assertEqual(states, ['active', 'off'])

    def test_failure_does_not_abort_work_or_claim_active(self):
        for result in (0, OSError('not supported')):
            setter = Mock(return_value=result) if not isinstance(result, Exception) else Mock(side_effect=result)
            states = []
            with patch.object(power.sys, 'platform', 'win32'), patch.object(power, 'execution_state_api', return_value=setter):
                with power.prevent_idle_sleep(states.append) as active:
                    self.assertFalse(active)
                    states.append('work')
            self.assertEqual(states, ['failed', 'work', 'off'])
            setter.assert_called_once_with(0x80000001)

    def test_non_windows_does_not_load_windows_api(self):
        states = []
        with patch.object(power.sys, 'platform', 'linux'), patch.object(power, 'execution_state_api') as load:
            with power.prevent_idle_sleep(states.append) as active:
                self.assertFalse(active)
        load.assert_not_called()
        self.assertEqual(states, ['unsupported', 'off'])

    def test_reporting_exception_still_releases_successful_request(self):
        setter = Mock(return_value=power.ES_CONTINUOUS)
        def report(state):
            if state == 'active':
                raise RuntimeError('report unavailable')
        with patch.object(power.sys, 'platform', 'win32'), patch.object(power, 'execution_state_api', return_value=setter):
            with self.assertRaisesRegex(RuntimeError, 'report unavailable'):
                with power.prevent_idle_sleep(report):
                    self.fail('body must not be entered after reporting failed')
        self.assertEqual(setter.call_args_list, [call(0x80000001), call(0x80000000)])

    def test_release_failure_is_reported_without_hiding_work_exception(self):
        states = []
        setter = Mock(side_effect=[power.ES_CONTINUOUS, 0])
        with patch.object(power.sys, 'platform', 'win32'), patch.object(power, 'execution_state_api', return_value=setter):
            with self.assertRaisesRegex(ValueError, 'original error'):
                with power.prevent_idle_sleep(states.append):
                    raise ValueError('original error')
        self.assertEqual(states, ['active', 'failed'])


class WorkerPowerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        from shiguangji.storage.storage_policy import save_settings
        save_settings(self.base,{'zip_on_demand':False})  # Verify the optional automatic ZIP sleep guard.
        from shiguangji.platforms.extractor import blank_post
        store = result_finalizer.Store(self.base / 'alice', 'alice')
        post = blank_post('1500000000000000000', 'alice'); post['text'] = 'Saved fixture'
        store.put_post(post)
        result_finalizer.export_collection(store, 'complete', [])
        store.close()
        self.data = patch.object(app.TASKS, 'base', self.base)
        self.state = patch.dict(app.TASKS.job, {'running': True, 'pause_requested': False, 'username': 'alice',
                                         'stage': 'repair', 'status': 'running', 'logs': [], 'counts': {'posts': 1}}, clear=True)
        self.platform = patch.object(power.sys, 'platform', 'win32')
        self.calls = []
        def setter(flags):
            self.assertTrue(app.TASKS.job['running'], 'Release must precede allowing another worker')
            self.calls.append(flags)
            return power.ES_CONTINUOUS
        self.api = patch.object(power, 'execution_state_api', return_value=setter)
        for context in (self.data, self.state, self.platform, self.api): context.start()

    def tearDown(self):
        for context in (self.api, self.platform, self.state, self.data): context.stop()
        self.temp.cleanup()

    def test_completion_and_pause_keep_protection_through_zip_then_release(self):
        for status in ('complete', 'paused'):
            with self.subTest(status=status):
                self.calls.clear(); app.TASKS.job['running'] = True
                def collect(*args):
                    self.assertEqual(app.TASKS.job['sleep_prevention'], 'active')
                    return status
                def pack(*args):
                    self.assertEqual(app.TASKS.job['sleep_prevention'], 'active')
                    self.assertEqual(self.calls, [0x80000001])
                with patch.object(app.TASKS, 'collect_batches', side_effect=collect), patch.object(task_service, 'make_zip', side_effect=pack) as zipper:
                    app.TASKS.worker('alice', {})
                zipper.assert_called_once()
                self.assertEqual(app.TASKS.job['status'], status)
                self.assertFalse(app.TASKS.job['running'])
                self.assertEqual(app.TASKS.job['sleep_prevention'], 'off')
                self.assertEqual(self.calls, [0x80000001, 0x80000000])

    def test_download_or_zip_exception_releases_protection(self):
        for fail_zip in (False, True):
            with self.subTest(fail_zip=fail_zip):
                self.calls.clear(); app.TASKS.job['running'] = True
                with patch.object(app.TASKS, 'collect_batches', return_value='complete', side_effect=None if fail_zip else RuntimeError('fixture')), \
                     patch.object(task_service, 'make_zip', side_effect=RuntimeError('fixture')):
                    app.TASKS.worker('alice', {})
                self.assertEqual(app.TASKS.job['status'], 'failed')
                self.assertFalse(app.TASKS.job['running'])
                self.assertEqual(app.TASKS.job['sleep_prevention'], 'off')
                self.assertEqual(self.calls, [0x80000001, 0x80000000])

    def test_zip_only_has_protection_and_restart_never_restores_it(self):
        def rebuild(*args):
            self.assertEqual(app.TASKS.job['sleep_prevention'], 'active')
            return 'complete'
        with patch.object(app.TASKS, 'rebuild_export', side_effect=rebuild), patch.object(task_service, 'make_zip'):
            app.TASKS.worker('alice', {}, zip_only=True)
        self.assertEqual(self.calls, [0x80000001, 0x80000000])
        app.TASKS.job['sleep_prevention'] = 'active'
        app.TASKS.persist_job(force=True)
        self.calls.clear()
        app.TASKS.restore_job()
        self.assertEqual(self.calls, [])
        self.assertEqual(app.TASKS.job['sleep_prevention'], 'off')
