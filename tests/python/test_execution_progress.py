"""Checkpoint cost and progress regressions using local deterministic fixtures."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from archive import ArchiveError, Collector
from instagram_archive import InstagramCollector
from task_service import TaskService
from test_archive import FixtureClient, TS, URL
from test_instagram import FakeArchive


class ExecutionProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_intermediate_checkpoints_keep_database_and_media_then_terminal_worker_exports(self):
        for platform, collector, client, handle in (
            ('x', Collector, FixtureClient, 'alice'),
            ('instagram', InstagramCollector, FakeArchive, 'instagram--alice'),
        ):
            for final_status in ('complete', 'paused', 'failed'):
                with self.subTest(platform=platform, status=final_status):
                    base = self.base / platform / final_status
                    service = TaskService(base, collector_for=lambda _: collector, options_for=dict)
                    service.job.update(running=True, username=handle, status='running', run_id='test-run')
                    def collect(*_):
                        c = collector(handle, {'_run_id':'test-run', '_defer_offline_export':True},
                                      progress=service.progress, base=base, client=client())
                        self.assertEqual(c.run(), 'complete')
                        folder = base / handle
                        m = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
                        self.assertEqual(m['counts']['saved'], 1)
                        self.assertTrue((folder/'archive.sqlite').is_file())
                        self.assertTrue((folder/m['posts'][0]['media'][0]['path']).is_file())
                        for name in ('index.html', 'data.js', 'posts.csv', 'generation.json'):
                            self.assertFalse((folder/name).exists(), name)
                        return final_status
                    with patch.object(service, 'collect_batches', side_effect=collect):
                        service._worker_body(handle, {'_run_id':'test-run', 'auto_continue':True})
                    self.assertEqual(service.job['status'], final_status, service.job['logs'])
                    folder = base / handle
                    self.assertTrue((folder/'index.html').is_file())
                    self.assertTrue((folder/'posts.csv').is_file())
                    archives = list(base.glob('*.zip'))
                    self.assertEqual(len(archives), 0)  # Default keeps offline browsing, builds ZIP only on request.
                    service._worker_body(handle, {'_run_id':'test-run'}, zip_only=True)
                    archives = list(base.glob('*.zip'))
                    self.assertEqual(len(archives), 1)
                    with zipfile.ZipFile(archives[0]) as archive:
                        self.assertIsNone(archive.testzip())
                        manifest = json.loads(archive.read(handle+'/manifest.json'))
                        self.assertEqual(manifest['counts']['saved'], 1)
                        self.assertEqual(manifest['status'], final_status)
                        self.assertIn(handle+'/'+manifest['posts'][0]['media'][0]['path'], archive.namelist())

    def test_manual_single_batch_still_produces_offline_export(self):
        c = Collector('alice', base=self.base, client=FixtureClient())
        self.assertEqual(c.run(), 'complete')
        self.assertTrue((self.base/'alice'/'index.html').is_file())

    def test_page_progress_is_cumulative_and_does_not_count_failed_reads_as_success(self):
        updates = []
        client = FixtureClient()
        original_text = client.text
        client.text = lambda url: (_ for _ in ()).throw(ArchiveError('HTTP 404')) if '202104' in url else original_text(url)
        c = Collector('alice', {'max_pages':1}, progress=lambda **u:updates.append(u), base=self.base, client=client)
        self.addCleanup(c.identities.close)
        self.addCleanup(c.store.close)
        c.store.db.executemany('INSERT INTO captures VALUES(?,?,?,?,?,?)', [
            (TS, URL, 'text/html', 'A', 'done', ''),
            ('20210301000000', URL, 'text/html', 'B', 'pending', ''),
            ('20210401000000', URL, 'text/html', 'C', 'failed', 'HTTP 404'),
        ])
        c.store.db.commit()
        c.pages()
        progress = [u['page_progress'] for u in updates if 'page_progress' in u]
        self.assertEqual(progress[0], {'parsed':1, 'total':3, 'batch_checked':0})
        self.assertEqual(progress[-1], {'parsed':2, 'total':3, 'batch_checked':1})
        updates.clear()
        c.options['max_pages'] = 0
        c.pages()
        progress = [u['page_progress'] for u in updates if 'page_progress' in u]
        self.assertEqual(progress[0]['parsed'], 2)
        self.assertEqual(progress[-1], {'parsed':2, 'total':3, 'batch_checked':1})
        # Identical previously parsed captures advance without a network request.
        c.store.db.execute('INSERT INTO captures VALUES(?,?,?,?,?,?)', ('20210501000000', URL, 'text/html', 'A', 'pending', ''))
        c.store.db.commit()
        c.pages()
        self.assertEqual([u['page_progress'] for u in updates if 'page_progress' in u][-1]['parsed'], 3)
        self.assertEqual(client.page_reads, 1)

    def test_stage_timings_accumulate_only_until_transition_and_reset_for_new_run(self):
        service = TaskService(self.base, collector_for=lambda _: Collector, options_for=dict)
        service.job.update(running=True, stage='pages', page_progress={'parsed':2, 'total':3})
        service.stage_clock = 100
        with patch('task_service.time.monotonic', return_value=112):
            service.progress(stage='media')
        self.assertEqual(service.job['stage_seconds'], {'pages':12})
        self.assertEqual(service.job['page_progress'], {})
        with patch('task_service.time.monotonic', return_value=119):
            service.progress(stage='pages')
        with patch('task_service.time.monotonic', return_value=122):
            service.progress(stage='done')
        self.assertEqual(service.job['stage_seconds'], {'pages':15, 'media':7})
        service.job['running'] = False
        with patch('task_service.threading.Thread'):
            service.start('bob', {})
        self.assertEqual(service.job['stage_seconds'], {})
        self.assertEqual(service.job['page_progress'], {})


if __name__ == '__main__':
    unittest.main()
