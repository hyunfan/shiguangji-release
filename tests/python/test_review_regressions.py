"""Deterministic regressions from the October full-project review. No network."""
import hashlib
from contextlib import nullcontext
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from collection_export import export_collection, make_zip
from core_errors import Cancelled
from collection_lock import collection_lock
from collection_store import Store
from extractor import blank_post
from instagram_archive import InstagramCollector
from media_files import saved_file_valid
from test_instagram import FakeArchive
from test_media_fixtures import VIDEO


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_preserved_metadata_never_substitutes_for_content_digest(self):
        path = self.base / 'video.mp4'
        path.write_bytes(VIDEO)
        stat = path.stat()
        item = dict(status='saved', path=path.name, kind='video', bytes=len(VIDEO),
                    sha256=hashlib.sha256(VIDEO).hexdigest())
        # Freeze the metadata on every platform, including Windows creation time.
        with patch.object(Path, 'stat', return_value=stat):
            self.assertTrue(saved_file_valid(self.base, item))
            path.write_bytes(VIDEO[:-1] + b'!')
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertFalse(saved_file_valid(self.base, item))

    def test_zip_only_includes_current_post_paths_after_date_corrections(self):
        store = Store(self.base / 'alice', 'alice')
        self.addCleanup(store.close)
        post = blank_post('1234567890', 'alice')
        for date, text in [('', 'old unknown'), ('2020-01-01T00:00:00Z', 'old date'),
                           ('2021-02-01T00:00:00Z', 'current body')]:
            post.update(created_at=date, date_source='page', text=text)
            store.save_post(post)
            export_collection(store, 'complete', [])
        old = store.folder / 'posts/unknown-date/1234567890.txt'
        note = store.folder / 'my-note.txt'
        note.write_text('personal note', encoding='utf-8')
        with zipfile.ZipFile(make_zip(store.folder)) as package:
            posts = [name for name in package.namelist() if '/posts/' in name]
            self.assertEqual(posts, ['alice/posts/2021/02/1234567890.txt'])
            self.assertIn('current body', package.read(posts[0]).decode('utf-8'))
            generation = json.loads(package.read('alice/generation.json'))
            self.assertIn('posts/2021/02/1234567890.txt', generation['files'])
            self.assertEqual(package.read('alice/my-note.txt'), b'personal note')
        self.assertTrue(old.is_file(), 'Do not delete old or user files as a side effect')

    def test_media_validation_can_pause_between_bounded_reads(self):
        from test_media_fixtures import box
        content = VIDEO + box(b'free', b'x' * (3 * 1024 * 1024))
        path = self.base / 'large.mp4'
        path.write_bytes(content)
        item = dict(status='saved', path=path.name, kind='video', bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest())
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls == 3:
                raise Cancelled()
        with self.assertRaises(Cancelled):
            saved_file_valid(self.base, item, check)
        self.assertEqual(calls, 3)

    def test_failed_zip_keeps_previous_package_without_marking_it_current(self):
        from collection_export import zip_is_current
        store = Store(self.base / 'alice', 'alice')
        self.addCleanup(store.close)
        post = blank_post('1234567890', 'alice')
        post.update(text='previous body')
        store.save_post(post)
        export_collection(store, 'complete', [])
        target = make_zip(store.folder)
        before = target.read_bytes()
        post.update(text='corrected body')
        store.save_post(post)
        export_collection(store, 'complete', [])
        with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                make_zip(store.folder)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(zip_is_current(store.folder))

    def hold_collection(self, folder):
        held, release = threading.Event(), threading.Event()
        def owner():
            with collection_lock(folder):
                held.set()
                release.wait(20)
        thread = threading.Thread(target=owner)
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(5)))
        self.assertTrue(held.wait(5))

    def test_instagram_construction_and_lock_failure_leave_existing_files_intact(self):
        key = 'instagram--alice'
        InstagramCollector(key, base=self.base, client=FakeArchive()).run()
        folder = self.base / key
        before = {name: (folder / name).read_bytes() for name in ('manifest.json', 'archive.sqlite')}
        self.hold_collection(folder)
        collector = InstagramCollector(key, {'_run_id':'another-run'}, base=self.base, client=FakeArchive())
        self.assertEqual(collector.run(), 'failed')
        for name, content in before.items():
            self.assertEqual((folder / name).read_bytes(), content, name)

    def test_instagram_final_checkpoint_still_owns_the_lock(self):
        for expected, nested in (('complete', False), ('paused', False), ('failed', False), ('complete', True)):
            with self.subTest(expected=expected, nested=nested):
                stop = threading.Event()
                if expected == 'paused': stop.set()
                client = FakeArchive(stop=stop)
                if expected == 'failed':
                    client.text = lambda url: (_ for _ in ()).throw(RuntimeError('read failed'))
                collector = InstagramCollector('instagram--alice', base=self.base / (expected + str(nested)), client=client)
                checkpoint = collector.checkpoint
                owners = []
                def checked(status, full=True):
                    if full:
                        from collection_lock import collection_is_busy
                        thread = threading.Thread(target=lambda: owners.append(collection_is_busy(collector.folder)))
                        thread.start(); thread.join(5)
                    return checkpoint(status, full)
                # Outer callers may already hold this same lock.
                with collection_lock(collector.folder) if nested else nullcontext(), patch.object(collector, 'checkpoint', side_effect=checked):
                    self.assertEqual(collector.run(), expected)
                self.assertTrue(owners)
                self.assertTrue(all(owners))


if __name__ == '__main__':
    unittest.main()
