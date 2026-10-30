"""Automatic batch loop for TaskService: repeated collector runs until work is done or paused."""
import json
from shiguangji.core.archive_support import HOSTS, now
from shiguangji.platforms.platform_accounts import is_instagram
from shiguangji.storage.storage_guard import StorageLow
from shiguangji.tasks.auto_batches import AutoBatches, AUTO_REST_SECONDS
from shiguangji.tasks.task_queue import retry_epoch
from shiguangji.tasks.task_status import REASONS


class BatchRunner:
    """Mixin running one collection, optionally as automatic batches.

    Relies on TaskService for ``base``, ``lock``, ``job``, ``stop``, ``collector_for``,
    ``progress``, ``progress_callback`` and ``task_queue``.
    """

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
