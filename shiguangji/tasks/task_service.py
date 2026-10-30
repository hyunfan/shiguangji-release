"""Task lifecycle independent of HTTP: checkpoints, batches, ZIP and queue handoff."""
import json
from pathlib import Path
import threading
import time
import uuid
from shiguangji.core.archive_support import now
from shiguangji.storage.collection_export import publish_manifest, make_zip
from shiguangji.storage.collection_lock import CollectionBusy, collection_lock, collection_is_busy
from shiguangji.platforms.platform_accounts import collection_key as username
from shiguangji.tasks.job_state import read_job, write_job
from shiguangji.system.power_guard import prevent_idle_sleep
from shiguangji.tasks.task_status import summary as task_summary
from shiguangji.storage.storage_guard import StorageLow, require_space
from shiguangji.system.reliability import settings, worker_health
from shiguangji.storage.storage_policy import settings as storage_settings
from shiguangji.tasks.batch_runner import BatchRunner
from shiguangji.tasks.queue_coordinator import QueueCoordinator, canonical_handle
from shiguangji.tasks.result_finalizer import ResultFinalizer

__all__ = ['TaskService', 'SLEEP_MESSAGES', 'canonical_handle']

SLEEP_MESSAGES = {'active': '任务防休眠已启用，屏幕可正常熄灭；任务结束后恢复正常休眠规则。',
                  'failed': 'Windows 防休眠状态设置失败，请接通电源并检查系统休眠设置；任务仍会继续。'}


class TaskService(QueueCoordinator, BatchRunner, ResultFinalizer):
    """One isolated service per library; dependencies are supplied by the entrypoint.

    The job dictionary retains the public API/checkpoint schema. Its lock protects
    lifecycle transitions; the collection lock protects disk writes. A queue task
    may start only after the previous worker has released its sleep guard.

    TaskService is the facade: queue/history coordination, the automatic batch loop and
    result finalization live in mixins in sibling modules, and every method remains
    available (and patchable) on the service instance.
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

    def health_snapshot(self):
        with self.lock:
            return worker_health(self.job, time.monotonic(), self.activity_clock, self.progress_clock)

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

    def collection_active(self, handle, key=None):
        """True while this service collects ``handle`` or another process holds the collection lock.

        ``key`` is the on-disk collection key when it differs from the job's username.
        """
        return (self.job['running'] and self.job['username'] == handle) or self.collection_busy(key or handle)

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
