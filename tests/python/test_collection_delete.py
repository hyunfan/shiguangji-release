"""Deletion tests operate only on synthetic collections in temporary folders."""
import http.client
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app
from shiguangji.tasks import task_service
from shiguangji.storage.collection_delete import delete_collection
from shiguangji.storage.collection_lock import collection_lock, CollectionBusy


def fixture(base, handle):
    folder = base / handle
    (folder / 'media').mkdir(parents=True)
    (folder / 'media' / 'image.jpg').write_bytes(b'synthetic media')
    (folder / 'raw').mkdir()
    (folder / 'raw' / 'snapshot.txt').write_text('synthetic snapshot')
    (folder / 'archive.sqlite').write_bytes(b'synthetic DB')
    (folder / 'manifest.json').write_text(json.dumps({'username': handle, 'status': 'partial',
                'updated_at': '2026-09-25T00:00:00Z', 'counts': {'posts': 1, 'saved': 1}, 'posts': []}))
    for suffix in ('-合集.zip', '-合集.zip.json', '-合集.zip.part', '-合集.zip.tmp'):
        (base / (handle + suffix)).write_bytes(b'export')


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fixture(self.root, 'alice'); fixture(self.root, 'bob')
    def tearDown(self):
        self.temp.cleanup()
    def test_deletes_only_confirmed_collection_exports_and_cache_and_is_idempotent(self):
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and ('bob' in p.parts or p.name.startswith('bob-'))}
        delete_collection(self.root, 'alice')
        self.assertEqual([p.name for p in (self.root/'alice').iterdir()], ['.collection.lock'])
        self.assertFalse(list(self.root.glob('alice-合集*')))
        for path, content in before.items():
            self.assertEqual((self.root/path).read_bytes(), content)
        delete_collection(self.root, 'alice')
    def test_invalid_paths_cannot_remove_any_collection(self):
        for value in ('../alice', '', 'alice/../bob', 'C:\\', '.', 'alice-合集.zip'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                delete_collection(self.root, value)
        self.assertTrue((self.root/'alice/media/image.jpg').exists())
    def test_link_inside_collection_is_rejected_before_any_deletion(self):
        outside = self.root/'outside.txt'; outside.write_text('must survive')
        link = self.root/'alice/media/link.txt'
        try:
            link.symlink_to(outside)
        except OSError:
            if os.name != 'nt':
                raise
            outside_dir = self.root/'outside_dir'; outside_dir.mkdir()
            target = str(outside_dir).replace("'", "''")
            location = str(link).replace("'", "''")
            subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command',
                            f"New-Item -ItemType Junction -Path '{location}' -Target '{target}' | Out-Null"], check=True, capture_output=True)
        with self.assertRaises(ValueError):
            delete_collection(self.root, 'alice')
        self.assertEqual(outside.read_text(), 'must survive')
        self.assertTrue((self.root/'alice/media/image.jpg').exists())
        self.assertTrue((self.root/'alice-合集.zip').exists())
    def test_writer_lock_prevents_deletion(self):
        entered, release = threading.Event(), threading.Event()
        def occupy():
            with collection_lock(self.root/'alice'):
                entered.set(); release.wait(5)
        thread = threading.Thread(target=occupy); thread.start()
        self.assertTrue(entered.wait(5))
        try:
            with self.assertRaises(CollectionBusy):
                delete_collection(self.root, 'alice')
            self.assertTrue((self.root/'alice/manifest.json').is_file())
        finally:
            release.set(); thread.join(5)


class DeleteHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name); fixture(self.root, 'alice'); fixture(self.root, 'bob')
        self.data = patch.object(app.TASKS, 'base', self.root)
        self.job = patch.dict(app.TASKS.job, {'running': False, 'username': 'alice', 'status': 'paused', 'stage': 'done', 'logs': [], 'counts': {}}, clear=True)
        self.data.start(); self.job.start()
        self.server = app.LocalServer(('127.0.0.1', 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(5)
        self.job.stop(); self.data.stop(); self.temp.cleanup()
    def post(self, body, token=None, path='/api/delete'):
        client = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        client.request('POST', path, json.dumps(body), {'X-Local-Token': app.TOKEN if token is None else token, 'Content-Type': 'application/json'})
        response = client.getresponse(); value = response.status, json.loads(response.read()); client.close(); return value
    def test_requires_token_and_explicit_matching_confirmation(self):
        body = {'username': 'alice', 'confirm_username': 'alice'}
        self.assertEqual(self.post(body, 'invalid')[0], 403)
        self.assertEqual(self.post({'username': 'alice'})[0], 400)
        self.assertEqual(self.post({**body, 'confirm_username': 'bob'})[0], 400)
        self.assertTrue((self.root/'alice/manifest.json').exists())
    def test_running_account_must_be_paused_and_other_accounts_survive(self):
        app.TASKS.job['running'] = True
        self.assertEqual(self.post({'username': 'alice', 'confirm_username': 'alice'})[0], 409)
        app.TASKS.job['running'] = False
        self.assertEqual(self.post({'username': 'alice', 'confirm_username': 'alice'})[0], 200)
        self.assertEqual(app.TASKS.job['status'], 'idle')
        self.assertEqual(task_service.read_job(self.root/'.job-state.json')['username'], '')
        self.assertTrue((self.root/'bob/manifest.json').exists())
        self.assertFalse((self.root/'alice/manifest.json').exists())
    def test_file_lock_error_is_reported_without_claiming_success_or_forgetting_the_account(self):
        with patch('shiguangji.storage.collection_delete.shutil.rmtree', side_effect=PermissionError('fixture file lock')):
            status, result = self.post({'username': 'alice', 'confirm_username': 'alice'})
        self.assertEqual(status, 409)
        self.assertIn('删除未完成', result['error'])
        self.assertEqual(app.TASKS.job['username'], 'alice')
        self.assertTrue((self.root/'alice/manifest.json').exists())
        self.assertTrue((self.root/'bob/manifest.json').exists())
    def test_auto_mode_is_default_and_pause_is_allowed_between_batches_and_during_export(self):
        with patch.object(app.TASKS, 'start') as start:
            self.assertEqual(self.post({'username': 'alice'}, path='/api/start')[0], 200)
            self.assertTrue(start.call_args.args[1]['auto_continue'])
            self.assertEqual(self.post({'username': 'alice', 'auto_continue': 'yes'}, path='/api/start')[0], 400)
        with patch.object(app.TASKS, 'stop', threading.Event()):
            app.TASKS.job.update(running=True, stage='export', pause_requested=False, automatic={'enabled': True})
            self.assertEqual(self.post({}, path='/api/pause')[0], 200)
            self.assertTrue(app.TASKS.stop.is_set())


if __name__ == '__main__':
    unittest.main()
