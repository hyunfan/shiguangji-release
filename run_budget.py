"""One finite budget shared by all transports and recovery phases in a run."""
import time


class RunLimit(Exception):
    batch_stop = True
    request_sent = False


class RunBudget:
    def __init__(self, minutes=10, max_requests=200, idle_seconds=180, clock=None):
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.seconds = float(minutes) * 60
        self.max_requests = int(max_requests)
        self.idle_seconds = idle_seconds
        self.requests = 0
        self.saved_paths = set()
        self.reused_paths = set()
        self.last_saved = self.started
        self.media_started = None
        self.media_requests = 0
        self.reason = self.code = ''

    def finish(self, code, reason):
        self.code, self.reason = code, reason
        raise RunLimit(reason)

    def check(self):
        if self.reason:
            raise RunLimit(self.reason)
        current = self.clock()
        if current - self.started >= self.seconds:
            self.finish('time_limit', f'达到本批 {self.seconds / 60:g} 分钟检索时限')
        if (self.media_started is not None and self.requests - self.media_requests >= 12
                and current - max(self.media_started, self.last_saved) >= self.idle_seconds):
            self.finish('no_new_media', f'连续 {self.idle_seconds // 60:g} 分钟未新增媒体，停止本批检索')

    def before_request(self):
        self.before_work()
        self.requests += 1

    def before_work(self):
        """Do not start another item after the last allowed request returned."""
        self.check()
        if self.requests >= self.max_requests:
            self.finish('request_limit', f'达到本批 {self.max_requests} 次网络请求上限')

    def timeout(self, maximum):
        self.check()
        return max(0.1, min(maximum, self.seconds - (self.clock() - self.started)))

    def media_phase(self):
        # Do not reset inactivity when switching recovery providers.
        if self.media_started is None:
            self.media_started = self.clock()
            self.media_requests = self.requests

    def saved(self, path, local=False):
        if path not in self.saved_paths:
            self.saved_paths.add(path)
            if local:
                self.reused_paths.add(path)
            self.last_saved = self.clock()

    def snapshot(self):
        elapsed = max(0, self.clock() - self.started)
        return {'elapsed_seconds': round(elapsed, 1), 'max_seconds': self.seconds,
                'remaining_seconds': round(max(0, self.seconds - elapsed), 1),
                'requests': self.requests, 'max_requests': self.max_requests,
                'new_files': len(self.saved_paths), 'reused_files': len(self.reused_paths),
                'stop_code': self.code, 'stop_reason': self.reason}
