"""Restart and interruption regressions, isolated from the user's collections."""
import copy
import http.client
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import app
from shiguangji.tasks import task_service
from shiguangji.collect.archive import Store, export_collection, make_zip
from shiguangji.storage.collection_lock import collection_lock
from shiguangji.platforms.extractor import blank_post
from shiguangji.tasks.job_state import read_job, write_job


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.data_patch = patch.object(app.TASKS, 'base', self.folder)
        self.data_patch.start()
        self.job_patch = patch.dict(app.TASKS.job, {"running": False, "pause_requested": False,
                                            "username": "", "stage": "idle", "status": "idle",
                                            "logs": [], "counts": {}}, clear=True)
        self.job_patch.start()

    def tearDown(self):
        self.job_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def collection(self, status="complete", full=True):
        store = Store(self.folder / "alice", "alice")
        post = blank_post("1500000000000000000", "alice")
        post["text"] = "Previously committed post"
        store.put_post(post)
        manifest = export_collection(store, status, [], full=full)
        store.close()
        return manifest

    def test_restart_preserves_completed_result_and_never_collects(self):
        app.TASKS.job.update(username="alice", status="partial", stage="done", counts={"posts": 7})
        app.TASKS.progress(message="七条帖子已保存。")
        with patch.object(app, "Collector") as collector, patch.object(app.threading, "Thread") as thread:
            app.TASKS.restore_job()
        collector.assert_not_called()
        thread.assert_not_called()
        self.assertEqual(app.TASKS.job["status"], "partial")
        self.assertEqual(app.TASKS.job["counts"], {"posts": 7})
        self.assertEqual(app.TASKS.job["logs"][-1]["text"], "七条帖子已保存。")

    def test_interrupted_job_becomes_paused_without_modifying_collection(self):
        self.collection("running", full=False)
        before = (self.folder / "alice/manifest.json").read_bytes()
        app.TASKS.job.update(username="alice", running=True, status="running", stage="media")
        app.TASKS.persist_job(force=True)
        with patch.object(app, "Collector") as collector:
            app.TASKS.restore_job()
        collector.assert_not_called()
        self.assertFalse(app.TASKS.job["running"])
        self.assertEqual(app.TASKS.job["status"], "paused")
        self.assertTrue(app.TASKS.job["interrupted"])
        self.assertEqual((self.folder / "alice/manifest.json").read_bytes(), before)
        saved = copy.deepcopy(app.TASKS.job)
        app.TASKS.restore_job()
        self.assertEqual(app.TASKS.job, saved, "Restarting twice must not duplicate interruption messages")

    def test_legacy_manifest_is_recovered_if_state_is_missing_or_damaged(self):
        self.collection("running", full=False)
        for content in (None, "{incomplete write", '{"schema":1,"job":{}}'):
            with self.subTest(content=content):
                state = self.folder / ".job-state.json"
                if content is None:
                    state.unlink(missing_ok=True)
                else:
                    state.write_text(content, encoding="utf-8")
                app.TASKS.restore_job()
                self.assertEqual(app.TASKS.job["username"], "alice")
                self.assertEqual(app.TASKS.job["status"], "paused")
                self.assertFalse(app.TASKS.job["running"])

    def test_job_checkpoint_excludes_tokens_and_proxy_options(self):
        state = self.folder / ".job-state.json"
        write_job(state, {**app.TASKS.job, "token": "SECRET_TOKEN", "proxy": "SECRET_PROXY", "options": {"proxy": "SECRET_PROXY"}})
        raw = state.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", raw)
        self.assertIsNotNone(read_job(state))
        self.assertFalse(list(self.folder.glob("*.tmp")))

    def test_failed_atomic_replace_keeps_last_state(self):
        state = self.folder / ".job-state.json"
        write_job(state, app.TASKS.job)
        before = state.read_bytes()
        with patch('shiguangji.core.atomic_io.os.replace', side_effect=OSError("simulated file lock")):
            with self.assertRaises(OSError):
                write_job(state, {**app.TASKS.job, "status": "failed"})
        self.assertEqual(state.read_bytes(), before)
        self.assertFalse(list(self.folder.glob("*.tmp")))

    def test_restart_does_not_claim_another_process_task_was_interrupted(self):
        self.collection("running", full=False)
        acquired, release = threading.Event(), threading.Event()
        def occupy():
            with collection_lock(self.folder / "alice"):
                acquired.set()
                release.wait(5)
        thread = threading.Thread(target=occupy)
        thread.start()
        self.assertTrue(acquired.wait(5))
        try:
            app.TASKS.restore_job()
            self.assertFalse(app.TASKS.job["running"], "This server must not offer pause for another worker")
            self.assertEqual(app.TASKS.job["stage"], "external")
            self.assertFalse(app.TASKS.job["interrupted"])
        finally:
            release.set()
            thread.join(5)

    def test_zip_only_rebuilds_all_formats_from_latest_committed_database(self):
        self.collection("running", full=False)
        store = Store(self.folder / "alice", "alice")
        newer = blank_post("1500000000000000001", "alice")
        newer["text"] = "Committed after the last checkpoint"
        store.put_post(newer)
        store.close()
        app.TASKS.worker("alice", {}, zip_only=True)
        self.assertEqual(app.TASKS.job["status"], "paused")
        self.assertFalse(app.TASKS.job["running"])
        manifest = json.loads((self.folder / "alice/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["counts"]["posts"], 2)
        self.assertEqual(manifest["status"], "paused")
        with zipfile.ZipFile(self.folder / "alice-合集.zip") as bundle:
            self.assertEqual(json.loads(bundle.read("alice/manifest.json"))["counts"]["posts"], 2)
            self.assertEqual(len(bundle.read("alice/posts.jsonl").splitlines()), 2)
            self.assertIn(b"Committed after", bundle.read("alice/posts.csv"))
            self.assertTrue(any(name.endswith(newer["id"] + ".txt") for name in bundle.namelist()))
        self.assertEqual(read_job(self.folder / ".job-state.json")["status"], "paused")

    def test_external_result_updates_after_other_worker_finishes(self):
        self.collection("partial")
        app.TASKS.job.update(username="alice", status="running", stage="external")
        app.TASKS.refresh_external_job()
        self.assertEqual(app.TASKS.job["status"], "partial")
        self.assertEqual(app.TASKS.job["stage"], "done")
        self.assertFalse(app.TASKS.job["running"])
        self.assertEqual(app.TASKS.job["counts"]["posts"], 1)

    def test_interrupted_manifest_without_database_is_not_zipped_as_complete(self):
        self.collection("running", full=False)
        (self.folder / "alice/archive.sqlite").unlink()
        app.TASKS.worker("alice", {}, zip_only=True)
        self.assertEqual(app.TASKS.job["status"], "failed")
        self.assertFalse((self.folder / "alice-合集.zip").exists())
        self.assertFalse((self.folder / "alice/archive.sqlite").exists())

    def test_worker_lock_precedes_collector_construction(self):
        with patch.object(task_service, 'collection_lock', side_effect=app.CollectionBusy("busy")), patch.object(app, "Collector") as collector:
            app.TASKS.worker("alice", {})
        collector.assert_not_called()
        self.assertEqual(app.TASKS.job["status"], "failed")


    # A separate HTTP check covers read-only status normalization and stale ZIP suppression.
    def test_orphan_running_checkpoint_is_paused_and_not_downloadable(self):
        self.collection("running", full=False)
        make_zip(self.folder / "alice")
        original = (self.folder / "alice/manifest.json").read_bytes()
        server = app.LocalServer(("127.0.0.1", 0), app.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(path):
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("GET", path)
            response = client.getresponse()
            result = response.status, json.loads(response.read())
            client.close()
            return result
        try:
            _, manifest = request("/api/collection/alice")
            self.assertEqual(manifest["status"], "paused")
            self.assertTrue(manifest["interrupted"])
            self.assertFalse(manifest["zip_ready"])
            self.assertEqual(request("/api/collections")[1][0]["status"], "paused")
            self.assertEqual(request("/download/alice")[0], 409)
            self.assertEqual((self.folder / "alice/manifest.json").read_bytes(), original)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
