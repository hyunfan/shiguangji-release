"""Task lifecycle independent of HTTP: checkpoints, batches, ZIP and queue handoff."""
import json
from pathlib import Path
import threading
import time
import uuid
from shiguangji.core.archive_support import HOSTS, now
from shiguangji.storage.collection_store import Store
from shiguangji.storage.collection_export import export_collection, publish_manifest, make_zip
from shiguangji.storage.collection_lock import CollectionBusy, collection_lock, collection_is_busy
from shiguangji.platforms.platform_accounts import collection_key as username, is_instagram
from shiguangji.platforms.platform_registry import platform_for
from shiguangji.tasks.job_state import read_job, write_job
from shiguangji.tasks.auto_batches import AutoBatches, AUTO_REST_SECONDS
from shiguangji.system.power_guard import prevent_idle_sleep
from shiguangji.tasks.task_status import summary as task_summary, collection_summary, REASONS
from shiguangji.tasks.task_queue import TaskQueue, retry_epoch
from shiguangji.tasks.task_history import TaskHistory
from shiguangji.storage.storage_guard import StorageLow, require_space
from shiguangji.system.reliability import settings, worker_health
from shiguangji.storage.storage_policy import settings as storage_settings

SLEEP_MESSAGES = {'active': '任务防休眠已启用，屏幕可正常熄灭；任务结束后恢复正常休眠规则。',
                  'failed': 'Windows 防休眠状态设置失败，请接通电源并检查系统休眠设置；任务仍会继续。'}


def canonical_handle(base, handle):
    return platform_for(handle).canonical(base,handle)


class TaskService:
    """One isolated service per library; dependencies are supplied by the entrypoint.

    The job dictionary retains the public API/checkpoint schema. Its lock protects
    lifecycle transitions; the collection lock protects disk writes. A queue task
    may start only after the previous worker has released its sleep guard.
    """
    def __init__(self, base, *, collector_for, options_for):
        self.base = Path(base)
        self.collector_for = collector_for
        self.options_for = options_for
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.stopping = threading.Event()
        self.queue = None
        self.history = None
        self.job = {"running": False, "pause_requested": False, "username": "",
                    "stage": "idle", "status": "idle", "logs": [], "counts": {},
                    "sleep_prevention": "off"}
        self.last_saved = (None, 0.0)
        self.stage_clock = time.monotonic()
        self.activity_clock = self.progress_clock = self.stage_clock
        self.retry_timer = None

    def schedule_queue_retry(self):
        if self.retry_timer:
            self.retry_timer.cancel()
            self.retry_timer = None
        due = self.task_queue().next_retry()
        if self.stopping.is_set() or self.job.get('running') or self.task_queue().data['state'] != 'running' or not due:
            return
        def wake():
            with self.lock:
                self.retry_timer = None
                if not self.stopping.is_set():
                    try:
                        self.advance_queue()
                    except Exception as error:
                        self.task_queue().pause('retry_error')
                        self.progress(message='自动重试启动失败，队列已暂停：'+str(error)[:200])
        self.retry_timer = threading.Timer(max(1, min(60, retry_epoch(due)-time.time())), wake)
        self.retry_timer.daemon = True
        self.retry_timer.start()

    def health_snapshot(self):
        with self.lock:
            return worker_health(self.job, time.monotonic(), self.activity_clock, self.progress_clock)

    def recover_queue(self, run_id):
        """Only a supervisor-confirmed crash can opt in to resuming this exact run."""
        if not run_id or not settings(self.base)['resume_queue_after_crash']:
            return False
        with self.lock:
            try:
                raw = json.loads((self.base/'.task-queue.json').read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return False
            if (self.job.get('running') or not self.job.get('interrupted') or self.job.get('run_id') != run_id
                    or raw.get('state') != 'running' or any(i.get('state') == 'paused' for i in raw.get('items', []))
                    or not any(i.get('state') == 'running' and i.get('run_id') == run_id for i in raw.get('items', []))):
                return False
            self.task_queue().resume()
            self.advance_queue()
            return bool(self.job.get('running'))

    def sleep_status(self, state):
        updates = {'sleep_prevention': state}
        if state in SLEEP_MESSAGES:
            updates['message'] = SLEEP_MESSAGES[state]
        self.progress(**updates)

    def persist_job(self, force=False):
        """Bound checkpoint writes while always saving lifecycle transitions."""
        path = self.base / ".job-state.json"
        elapsed = time.monotonic()
        if not force and self.last_saved[0] == path and elapsed - self.last_saved[1] < 1:
            return
        try:
            write_job(path, self.job)
            self.last_saved = (path, elapsed)
        except OSError as error:
            warning = "任务状态文件暂时无法保存；请查看合集中的已保存进度。"
            if not any(entry.get("text", "").startswith(warning) for entry in self.job["logs"]):
                self.job["logs"].append({"time": now(), "text": f"{warning} {error}"})

    def collection_busy(self, handle):
        return collection_is_busy(self.base / handle)

    def task_queue(self):
        path = self.base / '.task-queue.json'
        if self.queue is None or self.queue.path != path:
            self.queue = TaskQueue(path)
        return self.queue

    def task_history(self):
        if self.history is None or self.history.base != self.base:
            history = TaskHistory(self.base)
            history.bootstrap(self.task_queue(), self.job.get('run_id', '') if self.job.get('running') else '')
            self.history = history
        return self.history

    def history_result(self, params):
        with self.lock:
            result = self.task_history().search(params)
            for item in result['items']:
                corrected = self.reconcile_summary({**item, 'run_id':item['id']})
                if corrected.get('reason') != item.get('reason'):
                    item.update(reason=corrected['reason'], message=corrected['reason_text'])
            return result

    def reconcile_summary(self, outcome):
        with self.lock:
            return self.task_queue().reconcile(outcome)

    def retry_history(self, item_id, current_options):
        """Retry just this account, preserving a paused queue's other entries."""
        with self.lock:
            if self.stopping.is_set() or self.job.get('running'):
                raise ValueError('请先等待当前任务完成或暂停保存，再继续历史任务。')
            item = self.task_history().get(item_id)
            handle = item['username']
            if self.collection_busy(handle):
                raise CollectionBusy('该合集正在被其他进程使用，请稍后重试。')
            if item['state'] == 'running':
                raise ValueError('该任务仍在运行，请刷新记录后重试。')
            # Recorded runs have complete options. Older evidence does not;
            # preserve its known mode but use current settings for unknown keys.
            options = self.options_for({**current_options, **item['options'],
                                        'mode':'full' if item['mode'] == 'zip' else item['mode'],
                                        'proxy':current_options.get('proxy', '')})
            folder = self.base / handle
            exists = (folder / 'manifest.json').is_file()
            zip_only = item['mode'] == 'zip' or item.get('reason') == 'zip_failed'
            if zip_only and not exists:
                raise ValueError('合集已不存在，无法重新打包；请重新添加博主。')
            if not exists and options['mode'] in {'repair', 'rescue', 'identity'}:
                raise ValueError('合集已不存在，请重新添加博主进行完整查找。')
            queued = next((i for i in self.task_queue().data['items']
                           if i['username'] == handle and i['state'] in {'pending', 'paused', 'running'}), None)
            if queued and queued['state'] == 'running':
                raise ValueError('该博主仍在运行队列中，请先暂停队列并等待保存。')
            # A narrow retry must not consume a queued full archive for the same account.
            queue_id = queued['id'] if queued and not zip_only and options['mode'] == 'full' else None
            self.start(handle, options, zip_only=zip_only, queue_id=queue_id)
            return {'username':handle, 'run_id':self.job['run_id']}

    def save_history_result(self):
        identity = None
        try:
            if self.job.get('username'):
                identity = platform_for(self.job['username']).identity(self.base, self.job['username'])
        except (OSError, ValueError):
            # Identity metadata must not prevent recording a task's outcome.
            pass
        try:
            self.task_history().finish(self.job, identity)
        except (OSError, ValueError) as error:
            self.progress(message='任务结果已保留，但历史记录暂未保存：' + str(error)[:200])

    def advance_queue(self):
        """Called under self.lock, after the previous worker released its sleep guard."""
        from shiguangji.tasks.job_dispatch import dispatch
        if self.task_queue().data.get('state') == 'running' and not self.job.get('running'):
            try:
                require_space(self.base)
            except StorageLow as error:
                self.task_queue().pause('storage_low')
                self.progress(message=str(error))
                return
        def prepare(item):
            try:
                return (canonical_handle(self.base, item['username']),
                        self.options_for({**item['options'], 'auto_continue':True}))
            except Exception as error:
                self.task_history().failed_start(item, str(error))
                raise
        dispatch(self.task_queue(), prepare, self.start,
                 lambda: not self.job.get('running') and not self.stopping.is_set())
        self.schedule_queue_retry()

    def restore_job(self):
        """Recover the last result; a restart never starts collection or a download."""
        with self.lock:
            saved = read_job(self.base / ".job-state.json")
            if saved:
                try:
                    if saved["username"]:
                        saved["username"] = username(saved["username"])
                except ValueError:
                    saved = None
            if not saved:
                candidates = sorted(self.base.glob("*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
                for path in candidates:
                    try:
                        manifest = json.loads(path.read_text(encoding="utf-8"))
                        saved = {"running": manifest["status"] == "running", "pause_requested": False,
                                 "username": username(manifest["username"]), "stage": "done",
                                 "status": manifest["status"], "logs": [], "counts": manifest["counts"],
                                 "updated_at": manifest.get("updated_at", "")}
                        saved.update(run_id=manifest.get('run_id', ''), task_summary=manifest.get('task_summary', {}))
                        break
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
            self.job.clear()
            self.job.update(running=False, pause_requested=False, username="", stage="idle", status="idle", logs=[], counts={}, sleep_prevention='off')
            if not saved:
                return
            self.job.update(saved)
            self.job.update(running=False, pause_requested=False, sleep_prevention='off')
            if saved.get("running") or saved["status"] == "running":
                if saved["username"] and self.collection_busy(saved["username"]):
                    self.job.update(status="running", stage="external", interrupted=False)
                    message = "该合集正由另一个工具进程处理；请在原窗口查看进度。"
                else:
                    self.job.update(status="paused", stage="done", interrupted=True)
                    message = "上次任务因工具关闭而中断，已保留保存过的进度。点击“开始整理 / 继续”续传；也可先重新生成 ZIP。"
                self.job["logs"].append({"time": now(), "text": message})
            elif not self.job["logs"]:
                self.job["logs"].append({"time": now(), "text": "已恢复上次保存的合集结果，可浏览、下载或继续整理。"})
            self.job["logs"] = self.job["logs"][-100:]
            self.persist_job(force=True)

    def progress(self, **updates):
        with self.lock:
            self.activity_clock = time.monotonic()
            metrics = ('posts','text','saved','parsed','captures','local_alias_checked','alias_search_checked')
            increased = any(updates.get('counts', {}).get(k,0) > self.job.get('counts', {}).get(k,0) for k in metrics)
            if increased:
                self.progress_clock = self.activity_clock
            if 'stage' in updates and updates['stage'] != self.job.get('stage'):
                current = time.monotonic()
                previous = self.job.get('stage', 'idle')
                timings = dict(self.job.get('stage_seconds', {}))
                if self.job.get('running') and previous not in {'idle', 'done'}:
                    timings[previous] = round(timings.get(previous, 0) + max(0, current-self.stage_clock), 2)
                self.stage_clock = current
                updates.update(stage_started_at=now(), stage_seconds=timings)
                if updates['stage'] != 'pages':
                    updates['page_progress'] = {}
            force = any(key in updates and updates[key] != self.job.get(key) for key in ("running", "stage", "status", "pause_requested"))
            if "message" in updates:
                self.job["logs"].append({"time": now(), "text": updates.pop("message")})
                self.job["logs"] = self.job["logs"][-100:]
            self.job.update(updates)
            self.job["updated_at"] = now()
            self.persist_job(force=force)

    def progress_callback(self, run_id):
        """Bind collector callbacks to one run, ignoring delayed older updates."""
        def report(**updates):
            with self.lock:
                if run_id and (self.job.get('run_id') != run_id or not self.job.get('running')):
                    return
                self.progress(**updates)
        return report

    def pause_current(self):
        """Return False when a manual task has reached its final publication."""
        with self.lock:
            if not self.job['running']:
                raise ValueError('当前没有运行中的任务。')
            if self.job['stage'] in {'export', 'zip', 'done'} and not self.job.get('automatic', {}).get('enabled'):
                return False
            if not self.job.get('pause_requested'):
                self.task_queue().pause()
                self.stop.set()
                self.progress(pause_requested=True, message='正在暂停，当前网络请求结束后保存进度。')
            return True

    def pause_queue(self):
        with self.lock:
            queue = self.task_queue()
            queue.pause()
            if self.job.get('running') and any(i['state'] == 'running' and i.get('run_id') == self.job.get('run_id') for i in queue.data['items']):
                self.stop.set()
                self.progress(pause_requested=True, message='正在暂停队列，当前账号保存完成后停止。')

    def refresh_external_job(self):
        """An observed CLI/second-window task can finish while this UI remains open."""
        with self.lock:
            handle = self.job.get('username')
            if not handle or self.job.get('running'):
                return
            busy = self.collection_busy(handle)
            if self.job.get('stage') != 'external':
                if not busy:
                    return
                try:
                    manifest = json.loads((self.base / handle / 'manifest.json').read_text(encoding='utf-8'))
                    counts = manifest['counts']
                except (OSError, ValueError, KeyError, TypeError):
                    counts = self.job.get('counts', {})
                self.progress(status='running', stage='external', counts=counts, interrupted=False,
                         message='该合集正由另一个工具进程继续整理；可在合集页查看已保存的帖子。')
                return
            if busy:
                try:
                    counts = json.loads((self.base / handle / 'manifest.json').read_text(encoding='utf-8'))['counts']
                    if counts != self.job.get('counts'):
                        self.progress(counts=counts)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                return
            try:
                manifest = json.loads((self.base / self.job["username"] / "manifest.json").read_text(encoding="utf-8"))
                status = manifest["status"]
                counts = manifest["counts"]
            except (OSError, ValueError, KeyError, TypeError):
                status, counts = "paused", self.job["counts"]
            interrupted = status == "running"
            self.progress(status="paused" if interrupted else status, stage="done", counts=counts,
                     task_summary=manifest.get('task_summary', {}) if 'manifest' in locals() else {},
                     run_id=manifest.get('run_id', '') if 'manifest' in locals() else '',
                     interrupted=interrupted, message="另一进程已结束；未完成部分可点击继续整理。" if interrupted
                     else "另一进程已结束处理，可刷新合集查看最新结果。")

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

    def collect_batches(self, handle, options):
        report = self.progress_callback(options.get('_run_id') or self.job.get('run_id'))
        if not options.get('auto_continue', False):
            return self.collector_for(handle)(handle, options, report, self.stop, base=self.base).run()
        try:
            initial = json.loads((self.base / handle / 'manifest.json').read_text(encoding='utf-8'))['counts']
        except (OSError, ValueError, KeyError):
            initial = {}
        session = AutoBatches(True, initial, max_minutes=options.get('auto_max_minutes', 0))
        settings = dict(options)
        # Existing media must get a download turn immediately. Otherwise a resumed
        # account can spend its entire first batch reading old snapshots again.
        media_first = options.get('mode') != 'discover' and initial.get('media', 0) > initial.get('saved', 0)
        status = 'paused'
        while True:
            if self.stop.is_set():
                session.finish('user_pause', '已暂停，不再自动开始下一批')
                break
            if not session.remaining():
                session.finish('session_limit', '达到设定的自动运行时间上限')
                break
            work_stage = ''
            def batch_progress(**updates):
                nonlocal work_stage
                if updates.get('stage') in {'identity_search', 'local_aliases', 'repair', 'index', 'content', 'pages', 'media', 'sources'}:
                    work_stage = updates['stage']
                automatic = session.snapshot()
                with self.lock:
                    batch_counts = updates.get('batch', self.job.get('batch', {}))
                automatic.update(current_batch=session.batches + 1,
                                 new_files=session.new_files + batch_counts.get('new_files', 0),
                                 reused_files=session.reused_files + batch_counts.get('reused_files', 0))
                report(**updates, automatic=automatic)
            self.progress(stage='repair', batch={}, completed=0, total=0, batch_started_at=now(),
                     automatic={**session.snapshot(), 'current_batch': session.batches + 1},
                     message=f'自动处理第 {session.batches + 1} 批，无需再次点击；可随时暂停。')
            settings['max_minutes'] = min(options.get('max_minutes', 10), session.remaining() / 60)
            batch_options = dict(settings)
            # The worker publishes a complete offline collection and ZIP when
            # the session ends (including pause/error). Intermediate batches
            # only need the database and online manifest checkpoint.
            batch_options['_defer_offline_export'] = True
            batch_options['_skip_pages'] = bool(media_first)
            discovery_deferred = media_first and options.get('mode', 'full') not in {'repair', 'rescue'}
            if discovery_deferred:
                batch_options['mode'] = 'repair'
            if media_first:
                self.progress(message='先下载已找到的媒体，再继续读取帖子快照。')
            collector = self.collector_for(handle)(handle, batch_options, batch_progress, self.stop, base=self.base)
            status = collector.run()
            batch, counts = dict(self.job.get('batch', {})), dict(self.job.get('counts', {}))
            if batch.get('stop_code') == 'storage_low':
                raise StorageLow(batch.get('stop_reason') or '磁盘空间不足，请释放空间后继续。')
            mode = settings.get('mode', 'full')
            pending = (is_instagram(handle) and bool(self.job.get('work_pending'))) or (mode not in {'repair', 'rescue'} and (bool(counts.get('identity_pending')) or bool(counts.get('alias_search_pending')))) or bool(counts.get('local_alias_pending')) or discovery_deferred or ((media_first or settings.get('max_pages')) and counts.get('parsed', 0) < counts.get('captures', 0))
            if mode not in {'discover', 'rescue'}:
                pending = pending or bool(counts.get('media_retry_pending'))
            if mode != 'discover' and (settings.get('multi_source') or mode == 'rescue'):
                pending = pending or bool(counts.get('source_retry_pending')) or (
                    settings.get('common_crawl', True) and bool(counts.get('cc_pending')))
            # Unread pages remain pending even when no explicit page cap was set.
            pending = bool(pending) or counts.get('parsed', 0) < counts.get('captures', 0)
            if mode in {'full','discover'} and settings.get('multi_source'):
                pending = pending or bool(counts.get('text_recovery_pending'))
            cooldown = dict(self.job.get('retry_policy', {}))
            alias = self.job.get('alias_discovery', {})
            if mode in {'full', 'discover'} and alias.get('failed_items') and alias.get('next_retry_at'):
                pending = True
                if not cooldown.get('next_retry_at') or retry_epoch(alias['next_retry_at']) < retry_epoch(cooldown['next_retry_at']):
                    cooldown.update(waiting=1, next_retry_at=alias['next_retry_at'])
            keep_going = session.after_batch(status, batch, counts, self.stop.is_set(), pending=bool(pending),
                                            cooldown=cooldown if not discovery_deferred else None)
            if session.code == 'retry_cooldown':
                with self.lock:
                    exhausted = self.task_queue().retry_limit_for(options.get('_queue_id'), options.get('_run_id'), session.next_retry_at)
                if exhausted:
                    session.finish('retry_limit', REASONS['retry_limit'])
                    session.next_retry_at = ''
            self.progress(automatic=session.snapshot(), work_pending=bool(pending))
            if not keep_going:
                break
            if not discovery_deferred and collector.index_ok == getattr(collector, 'index_expected', len(HOSTS)):
                settings['_skip_index'] = True
            # A slow page-reading batch must not indefinitely keep known media at
            # the back of the queue. The following batch gives downloads a turn.
            media_first = work_stage == 'pages' and settings.get('mode') != 'discover'
            self.progress(stage='between_batches', message=f'第 {session.batches} 批已保存，{AUTO_REST_SECONDS} 秒后自动继续；连续无进展 {session.empty_batches}/3 批。')
            if self.stop.wait(AUTO_REST_SECONDS):
                session.finish('user_pause', '已暂停，不再自动开始下一批')
                status = 'paused'
                break
        self.progress(automatic=session.snapshot())
        if session.code == 'user_pause' or session.code in {'session_limit', 'batch_limit'}:
            return 'paused'
        if session.code in {'retry_cooldown', 'retry_limit'}:
            return 'partial'
        if session.code in {'no_progress', 'no_new_media'}:
            counts = self.job.get('counts', {})
            return 'partial' if counts.get('missing') or counts.get('media', 0) > counts.get('saved', 0) or counts.get('text_missing') else 'complete'
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

    def worker(self, handle, options, zip_only=False):
        run_id = options.get('_run_id')
        with self.lock:
            if run_id and (self.job.get('run_id') != run_id or not self.job.get('running')):
                return
        try:
            with prevent_idle_sleep(self.sleep_status):
                self._worker_body(handle, options, zip_only)
                if (self.base/handle/'manifest.json').is_file() and settings(self.base)['backup_after_task'] and self.job.get('task_summary',{}).get('reason') != 'storage_low':
                    self.backup_result(handle)
        except Exception as error:
            self.progress(status='failed', stage='done', message=f'任务失败：{error}')
        finally:
            self.finish_worker(options)

    def backup_result(self, handle):
        from shiguangji.storage.backup_library import backup_collection
        self.progress(stage='backup', backup={'state':'running'}, message='正在校验增量备份；本轮结果已保存。')
        try:
            result = backup_collection(self.base, handle, self.base.parent/(self.base.name+'-backups'),
                                       lambda message:self.progress(message=message))
            self.progress(backup={'state':'verified', **result}, message=f"备份校验完成：{result['files']} 个文件，本次新增复制 {result['copied']} 个。")
        except Exception as error:
            self.task_queue().pause('backup_failed')
            self.progress(backup={'state':'failed','error':str(error)[:300]},
                          message='本轮归档仍已保存，但备份未完成，已暂停后续队列：' + str(error)[:300])
        finally:
            self.progress(stage='done')

    def finish_worker(self, options):
        # Called only after releasing the sleep guard. Until then, running=True
        # prevents the next task from publishing state alongside the old worker.
        with self.lock:
            if options.get('_run_id') and self.job.get('run_id') != options['_run_id']:
                return
            self.progress(running=False, pause_requested=False, finished_at=now())
            try:
                queue = self.task_queue()
                if options.get('_queue_id'):
                    queue.finish(options['_queue_id'], options['_run_id'], task_summary(self.job), self.stopping.is_set())
                elif options.get('auto_continue') and not self.stopping.is_set() and not self.stop.is_set():
                    queue.defer_manual(self.job['username'], options, task_summary(self.job))
                if self.stopping.is_set():
                    queue.pause('shutdown')
                outcome = task_summary(self.job)
                corrected = queue.reconcile(outcome)
                if corrected != outcome:
                    self.progress(task_summary=corrected)
                self.save_history_result()
                self.advance_queue()
            except Exception as error:
                # If durable queue bookkeeping fails, never start more work.
                self.task_queue().data.update(state='paused', reason='storage_error')
                self.task_queue().error = '队列暂无法保存，已停止自动继续：' + str(error)[:160]
                self.progress(message=self.task_queue().error)
                self.save_history_result()

    def _worker_body(self, handle, options, zip_only=False):
        status = "complete"
        zip_started = False
        try:
            with collection_lock(self.base / handle):
                if not zip_only:
                    status = self.collect_batches(handle, options)
                else:
                    self.progress(stage="export", message="正在从已保存数据库同步完整离线文件。")
                    status = self.rebuild_export(handle)
                if (self.base / handle / "manifest.json").exists():
                    self.progress(stage='export', message='正在保存本轮结束原因与恢复结果。')
                    self.finalize_result(handle, status, options, zip_only=zip_only)
                    if zip_only or not storage_settings(self.base)['zip_on_demand']:
                        self.progress(stage="zip", message="正在生成 ZIP，已下载媒体不会重复压缩。")
                        zip_started = True
                        make_zip(self.base / handle)
                        zip_started = False
            message = {"complete": "本轮整理完成。可打开合集或下载 ZIP。", "partial": "本轮整理完成，仍有未恢复内容，详见合集说明。", "paused": "已暂停并保存结果。输入同一用户名即可继续。", "failed": "本轮未能完成，请查看错误记录与网络设置。"}.get(status, status)
            if status == "partial":
                manifest = json.loads((self.base / handle / "manifest.json").read_text(encoding="utf-8"))
                counts = manifest["counts"]
                sources=manifest.get('last_run',{}).get('multi_source')
                summary=f"多源补回 {sources.get('recovered',0)} 项，" if sources is not None else ''
                message = f"本轮结束：{summary}仍缺 {counts.get('media',0)-counts.get('saved',0)} 项媒体、{counts.get('text_missing',0)} 条正文。"
                if counts.get("retryable") or manifest.get("errors"):
                    message += "另有网络或读取错误，可查看记录后重试。"
            if options.get("mode") == "identity":
                message = "历史用户名检查已保存。可在“更多 → 历史用户名”查看；继续整理会检索核对通过的名字。"
            if options.get("mode") != "identity" and status=='complete' and not self.job.get('counts',{}).get('posts'):
                message='本轮未发现可恢复的历史帖子。请核对历史用户名；未找到存档不代表该账号从未发帖。'
            batch = self.job.get('batch', {})
            if status == 'paused' and batch.get('stop_reason'):
                message = f"本批已结束：{batch['stop_reason']}；新增 {batch.get('new_files', 0)} 个媒体文件。结果已保存；点击“继续查找与下载”可继续，不会自动重启。"
            automatic = self.job.get('automatic', {})
            if not zip_only and automatic.get('enabled'):
                message = f"自动处理已结束：{automatic.get('stop_reason') or '本次任务结束'}。共处理 {automatic.get('batches', 0)} 批，新增 {automatic.get('new_files', 0)} 个媒体文件，结果已保存。"
            if not zip_only and storage_settings(self.base)['zip_on_demand']:
                message += ' ZIP 按需生成，需要下载时点击“生成下载包”。'
            self.progress(status=status, stage="done", message=message)
        except StorageLow as error:
            self.task_queue().pause('storage_low')
            self.progress(status='paused', stage='done',
                          batch={**self.job.get('batch',{}), 'stop_code':'storage_low','stop_reason':str(error)},
                          automatic={**self.job.get('automatic',{}),'stop_code':'storage_low','stop_reason':str(error)},
                          task_summary={}, message=str(error))
            self.progress(task_summary=task_summary(self.job, running=False))
            try:
                folder = self.base/handle
                with collection_lock(folder):
                    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
                    manifest.update(status='paused', task_summary=self.job['task_summary'])
                    publish_manifest(folder, manifest, offline=False)
            except (OSError, ValueError):
                pass
        except Exception as e:
            if zip_started:
                try:
                    with collection_lock(self.base / handle):
                        self.finalize_result(handle, status, options, zip_only=zip_only, zip_failed=True)
                except Exception:
                    self.progress(task_summary={'schema_version': 1, 'state': 'failed', 'reason': 'zip_failed'})
            elif not zip_only:
                try:
                    with collection_lock(self.base / handle):
                        self.record_collection_failure(handle)
                except (OSError, ValueError, KeyError, TypeError, CollectionBusy):
                    # The API can still use the persisted job's matching run ID
                    # when the collection destination itself remains unwritable.
                    pass
            self.progress(status="failed", stage="done", message=f"任务失败：{e}")

    def start(self, handle, options, zip_only=False, queue_id=None):
        with self.lock:
            require_space(self.base)
            if self.stopping.is_set():
                raise ValueError("后台正在保存进度并退出，请完成后重新启动工具。")
            if self.job["running"]:
                raise ValueError("已有任务运行，请先等待完成或暂停。")
            history = self.task_history()
            self.stop.clear()
            options = {**options, '_run_id': uuid.uuid4().hex}
            history.begin({'username':handle, 'run_id':options['_run_id'], 'running':True,
                           'status':'running', 'mode':'zip' if zip_only else options.get('mode', 'full'),
                           'started_at':now(), 'counts':{}}, options)
            if queue_id:
                options['_queue_id'] = queue_id
                try:
                    self.task_queue().mark_running(queue_id, handle, options['_run_id'])
                except Exception:
                    history.finish({'username':handle, 'run_id':options['_run_id'], 'mode':options.get('mode','full'),
                                    'running':False, 'status':'failed', 'finished_at':now(), 'counts':{}})
                    raise
            self.job.update(running=True, pause_requested=False, username=handle, mode=options.get("mode", "full"), stage="export" if zip_only else "repair", status="running", logs=[], counts={}, captures=0, completed=0, total=0, batch={}, automatic={}, sleep_prevention='pending', batch_started_at=now(), interrupted=False, started_at=now(), updated_at=now(), finished_at="")
            self.stage_clock = time.monotonic()
            self.activity_clock = self.progress_clock = self.stage_clock
            self.job.update(retry_policy={}, backup={})
            self.job.update(run_id=options['_run_id'], task_summary={}, scope={}, alias_discovery={},
                            stage_started_at=now(), stage_seconds={}, page_progress={})
            self.job.pop('work_pending', None)
            if zip_only:
                self.job['mode'] = 'zip'
            self.persist_job(force=True)
            try:
                threading.Thread(target=self.worker, args=(handle, options, zip_only), daemon=True).start()
            except Exception:
                self.progress(running=False, status="failed", stage="done", sleep_prevention='off', message="无法启动后台任务，请重启工具后重试。")
                self.save_history_result()
                raise
