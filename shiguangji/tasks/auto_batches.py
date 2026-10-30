"""Automatic continuation lives in the server, not in the browser's polling loop."""
import time

AUTO_REST_SECONDS = 5


class AutoBatches:
    def __init__(self, enabled, counts=None, clock=None, max_minutes=0, max_batches=0):
        self.enabled = enabled
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.max_seconds = max_minutes * 60
        self.max_batches = max_batches
        self.previous = dict(counts or {})
        self.initial = dict(counts or {})
        self.requests = 0
        self.batches = self.empty_batches = self.new_files = 0
        self.reused_files = 0
        self.reason = self.code = ''

    def remaining(self):
        return max(0, self.max_seconds - (self.clock() - self.started)) if self.max_seconds else float('inf')

    def finish(self, code, reason):
        self.code, self.reason = code, reason
        return False

    def after_batch(self, status, batch, counts, stopped=False, pending=False, cooldown=None):
        self.batches += 1
        self.requests += int(batch.get('requests',0))
        files = int(batch.get('new_files', 0))
        self.new_files += files
        self.reused_files += int(batch.get('reused_files', 0))
        progressed = files > 0 or any(counts.get(key, 0) > self.previous.get(key, 0)
                                     for key in ('posts', 'text', 'captures', 'parsed', 'local_alias_checked', 'alias_search_checked','hls_segments'))
        waiting = bool((cooldown or {}).get('waiting') and (cooldown or {}).get('next_retry_at'))
        # Blocked requests are not unsuccessful searches. Yield to other accounts.
        self.empty_batches = 0 if progressed else self.empty_batches + (0 if waiting else 1)
        self.previous = dict(counts)
        if stopped or (status == 'paused' and not batch.get('stop_code')):
            return self.finish('user_pause', '已暂停，不再自动开始下一批')
        if status == 'failed':
            return self.finish('failed', '任务出错，已停止自动继续')
        if not self.enabled:
            return False
        if not self.remaining():
            return self.finish('session_limit', f'达到设定的自动运行 {self.max_seconds / 60:g} 分钟上限')
        if pending and waiting and not progressed:
            self.next_retry_at = cooldown['next_retry_at']
            return self.finish('retry_cooldown', '请求暂缓，已保存进度并安排延后重试')
        if status in {'complete', 'partial'} and not pending:
            return self.finish('pass_finished', '本轮可执行检查已完成')
        if self.empty_batches >= 3:
            return self.finish('no_progress', '连续 3 批没有新增文件、帖子或解析进展')
        if self.max_batches and self.batches >= self.max_batches:
            return self.finish('batch_limit', f'达到本次自动处理 {self.max_batches} 批上限')
        return status in {'complete', 'partial'} or batch.get('stop_code') in {
            'time_limit', 'request_limit', 'no_new_media', 'source_limit'}

    def snapshot(self):
        return {'enabled': self.enabled, 'batches': self.batches, 'empty_batches': self.empty_batches,
                'new_files': self.new_files, 'reused_files': self.reused_files, 'max_seconds': self.max_seconds,
                'elapsed_seconds': round(self.clock() - self.started, 1),
                'requests': self.requests,
                'new_posts': max(0,self.previous.get('posts',0)-self.initial.get('posts',0)),
                'new_text': max(0,self.previous.get('text',0)-self.initial.get('text',0)),
                'stop_code': self.code, 'stop_reason': self.reason,
                'next_retry_at': getattr(self, 'next_retry_at', '')}
