"""Terminal results for TaskService: summaries, export rebuilds and failure records."""
import json
from shiguangji.core.archive_support import now
from shiguangji.storage.collection_export import export_collection, publish_manifest
from shiguangji.storage.collection_store import Store
from shiguangji.tasks.task_status import summary as task_summary, collection_summary


class ResultFinalizer:
    """Mixin interpreting a finished run and publishing its manifest and exports.

    Relies on TaskService for ``base``, ``job`` and ``progress``.
    """

    def export_summary(self, manifest):
        """Retrying publication must restore the capture outcome, including without SQLite."""
        if manifest.get('task_summary', {}).get('reason') != 'zip_failed':
            return collection_summary(manifest)
        info = dict(manifest.get('last_run', {}))
        saved = info.get('capture_summary') or {}
        if saved.get('reason') == 'zip_failed':
            # Older retries could overwrite the original outcome with ZIP failure.
            # Reinterpret the remaining run evidence instead of keeping that loop.
            saved = {}
        info['task_summary'] = saved
        return task_summary({**manifest, 'last_run': info, 'task_summary': saved}, running=False)

    def rebuild_export(self, handle):
        """A running checkpoint can lack CSV/text exports; rebuild all formats from SQLite."""
        folder = self.base / handle
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        status = manifest["status"]
        if status == "running":
            status = "paused"
        if (folder / "archive.sqlite").is_file():
            store = Store(folder, handle)
            try:
                if status == "complete" and (manifest.get("errors")
                        or store.db.execute("SELECT 1 FROM captures WHERE status!='done' LIMIT 1").fetchone()
                        or any(not post["text"] or any(media["status"] != "saved" for media in post["media"])
                               for post in store.posts())):
                    status = "partial"
                manifest = export_collection(store, status, manifest.get("errors", []),
                                             full=True, run_info={**manifest.get("last_run", {}),
                                                 'task_summary': self.export_summary(manifest)})
            finally:
                store.close()
        elif manifest["status"] == "running":
            raise ValueError("该合集上次未完成，且缺少续传数据库 archive.sqlite，无法生成一致的 ZIP。")
        self.progress(counts=manifest["counts"])
        return status

    def finalize_result(self, handle, status, options, *, zip_only=False, zip_failed=False):
        """Persist a terminal interpretation before publishing the downloadable ZIP."""
        folder = self.base / handle
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        info = dict(manifest.get('last_run', {}))
        if zip_only:
            final = self.export_summary(manifest)
        else:
            info.pop('task_summary', None)
            info.update(run_id=self.job.get('run_id', ''), automatic=self.job.get('automatic', {}),
                        options={k: options[k] for k in ('mode', 'multi_source', 'common_crawl', 'public_mirrors', 'max_pages') if k in options})
            final = task_summary({**manifest, **{k: self.job[k] for k in ('run_id', 'automatic', 'batch', 'work_pending', 'alias_discovery') if k in self.job},
                                  'last_run': info, 'task_summary': {}, 'status': status, 'mode': options.get('mode', 'full'),
                                  'errors': [] if options.get('mode') == 'identity' else manifest.get('errors', []),
                                  'finished_at': now()}, running=False)
        if zip_failed:
            info['capture_summary'] = final
            final = task_summary({**manifest, 'status': 'failed', 'task_summary': {**final, 'reason': 'zip_failed'}}, running=False)
        info['task_summary'] = final
        if not (folder / 'archive.sqlite').is_file():
            if not zip_only:
                raise ValueError('续传数据库缺失，已保留现有合集；请查看处理记录。')
            manifest.update(task_summary=final, last_run=info)
            publish_manifest(folder, manifest)
            self.progress(counts=manifest['counts'], task_summary=final)
            return manifest
        store = Store(folder, handle)
        try:
            manifest = export_collection(store, status, manifest.get('errors', []), full=not zip_failed, run_info=info)
        finally:
            store.close()
        self.progress(counts=manifest['counts'], task_summary=manifest['task_summary'])
        return manifest

    def record_collection_failure(self, handle):
        """Keep a same-run terminal failure even when full export cannot complete."""
        folder = self.base / handle
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if not self.job.get('run_id') or manifest.get('run_id') != self.job['run_id']:
            return
        info = {**manifest.get('last_run', {}), 'task_summary': {}}
        final = task_summary({**manifest, **self.job, 'status': 'failed', 'task_summary': {}, 'last_run': info,
                              'counts': manifest['counts'], 'finished_at': now()}, running=False)
        manifest.update(status='failed', task_summary=final,
                        last_run={**info, 'task_summary': final})
        publish_manifest(folder, manifest)
        self.progress(task_summary=final)
