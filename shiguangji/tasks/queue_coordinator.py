"""Queue and history coordination for TaskService: dispatch, retries and recorded outcomes."""
import json
import threading
import time
from shiguangji.platforms.platform_registry import platform_for
from shiguangji.storage.collection_lock import CollectionBusy
from shiguangji.storage.storage_guard import StorageLow, require_space
from shiguangji.system.reliability import settings
from shiguangji.tasks.task_history import TaskHistory
from shiguangji.tasks.task_queue import TaskQueue, retry_epoch


def canonical_handle(base, handle):
    return platform_for(handle).canonical(base,handle)


class QueueCoordinator:
    """Mixin owning the persistent queue and task history.

    Relies on TaskService for ``base``, ``lock``, ``job``, ``stopping``, ``options_for``,
    ``progress``, ``collection_busy`` and ``start``. Methods stay on the service so callers
    (and tests) can keep using and patching ``TaskService`` attributes.
    """

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

    def pause_queue(self):
        with self.lock:
            queue = self.task_queue()
            queue.pause()
            if self.job.get('running') and any(i['state'] == 'running' and i.get('run_id') == self.job.get('run_id') for i in queue.data['items']):
                self.stop.set()
                self.progress(pause_requested=True, message='正在暂停队列，当前账号保存完成后停止。')
