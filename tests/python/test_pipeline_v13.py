"""Interrupted scans, cache upgrades and multi-process collection ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import archive
from archive import Cancelled, Collector, Store
from collection_lock import CollectionBusy, collection_is_busy, collection_lock
from test_archive import FixtureClient, HTML, PID, TS, URL


def record(number=0):
    pid = str(int(PID) + number)
    return {'timestamp': TS, 'original': URL.replace(PID, pid), 'mimetype': 'text/html', 'statuscode': '200', 'digest': str(number)}


class PagedClient(FixtureClient):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.interrupt = True
    def cdx_pages(self, url, match='exact', resume=''):
        self.calls.append((url, resume))
        if url == 'twitter.com/alice':
            yield [record()], ''
        elif url == 'mobile.twitter.com/alice':
            if not resume:
                yield [record(1)], 'cursor-next'
                if self.interrupt:
                    raise Cancelled()
            yield [record(2)], ''
        else:
            yield [], ''
    def text(self, url):
        self.page_reads += 1
        return HTML.replace(PID, archive.post_id(url) or PID)


class PipelineTests(unittest.TestCase):
    def test_restart_resumes_page_and_skips_completed_host_of_same_scan(self):
        with tempfile.TemporaryDirectory() as d:
            client = PagedClient()
            self.assertEqual(Collector('alice', base=Path(d), client=client).run(), 'paused')
            store = Store(Path(d)/'alice', 'alice')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 2)
            saved = {r['url']: (r['resume_key'], r['completed']) for r in store.db.execute('SELECT * FROM index_progress')}
            self.assertEqual(saved['twitter.com/alice'], ('', 1))
            self.assertEqual(saved['mobile.twitter.com/alice'], ('cursor-next', 0))
            store.close()
            client.interrupt = False
            client.calls.clear()
            self.assertEqual(Collector('alice', base=Path(d), client=client).run(), 'complete')
            self.assertEqual(client.calls[0], ('mobile.twitter.com/alice', 'cursor-next'))
            self.assertNotIn(('twitter.com/alice', ''), client.calls)
            store = Store(Path(d)/'alice', 'alice')
            self.assertEqual(len(store.posts()), 3)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM index_progress').fetchone()[0], 0)
            store.close()
            client.calls.clear()
            Collector('alice', base=Path(d), client=client).run()
            self.assertEqual(client.calls[0], ('twitter.com/alice', ''))

    def test_page_records_and_cursor_roll_back_together(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d, 'alice')
            store.add_captures([], index_url='twitter.com/alice', next_key='old')
            with self.assertRaises(KeyError):
                store.add_captures([record(), {'original': URL}], index_url='twitter.com/alice', next_key='new')
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 0)
            self.assertEqual(store.posts(), [])
            self.assertEqual(store.db.execute('SELECT resume_key FROM index_progress').fetchone()[0], 'old')
            store.close()

    def test_unchanged_raw_is_skipped_but_parser_upgrade_reprocesses(self):
        with tempfile.TemporaryDirectory() as d:
            client = FixtureClient()
            Collector('alice', base=Path(d), client=client).run()
            with patch.object(archive, 'extract', side_effect=AssertionError('Unchanged cache should be reused')):
                self.assertEqual(Collector('alice', {'mode': 'repair'}, base=Path(d), client=client).run(), 'complete')
            with patch.object(archive, 'PARSER_CACHE_VERSION', archive.PARSER_CACHE_VERSION + 1):
                Collector('alice', {'mode': 'repair'}, base=Path(d), client=client).run()
                m = json.loads((Path(d)/'alice/manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(m['last_run']['cache_reparsed'], 1)
                Collector('alice', {'mode': 'repair'}, base=Path(d), client=client).run()
                m = json.loads((Path(d)/'alice/manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(m['last_run']['cache_reused'], 1)
                self.assertEqual(client.page_reads, 1)
                self.assertEqual(client.downloads, 1)

    def test_expired_scan_restarts_all_hosts(self):
        with tempfile.TemporaryDirectory() as d:
            client = PagedClient()
            Collector('alice', base=Path(d), client=client).run()
            store = Store(Path(d)/'alice', 'alice')
            store.db.execute("UPDATE index_progress SET updated='2000-01-01T00:00:00Z'")
            store.db.commit();store.close()
            client.calls.clear();client.interrupt = False
            Collector('alice', base=Path(d), client=client).run()
            self.assertEqual(client.calls[0], ('twitter.com/alice', ''))

    def test_identical_post_write_does_not_rewrite_database_row(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(d, 'alice')
            store.add_captures([record()])
            before = store.db.total_changes
            store.save_post(store.posts()[0])
            self.assertEqual(store.db.total_changes, before)
            store.close()


class LockTests(unittest.TestCase):
    def child(self, folder, crash=False):
        source = 'import os,sys;from collection_lock import collection_lock,CollectionBusy\ntry:\n with collection_lock(sys.argv[1]):\n  '+('os._exit(0)' if crash else 'pass')+'\nexcept CollectionBusy:\n sys.exit(7)\n'
        return subprocess.run([sys.executable, '-c', source, str(folder)], cwd=Path(__file__).resolve().parents[2], capture_output=True, timeout=10)
    def test_lock_reentrant_and_other_thread_cannot_write(self):
        with tempfile.TemporaryDirectory() as d:
            results = []
            with collection_lock(d):
                with collection_lock(d):
                    self.assertFalse(collection_is_busy(d))
                t = threading.Thread(target=lambda: results.append(collection_is_busy(d)))
                t.start();t.join(5)
                self.assertEqual(results, [True])
            self.assertFalse(collection_is_busy(d))
    def test_other_process_is_rejected_and_crash_releases_os_lock(self):
        with tempfile.TemporaryDirectory() as d:
            with collection_lock(d):
                child = self.child(d)
                self.assertEqual(child.returncode, 7, child.stderr)
            self.assertEqual(self.child(d, crash=True).returncode, 0)
            with collection_lock(d):
                pass


if __name__ == '__main__':
    unittest.main()
