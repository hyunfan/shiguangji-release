"""Persist bounded retry cooldowns across batches, without storing request URLs."""
import hashlib
import json
import math
from pathlib import Path
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from atomic_io import atomic_write_text
from core_errors import ArchiveError


class DeferredRequest(ArchiveError):
    request_sent = False

    def __init__(self, until):
        self.retry_at = until
        super().__init__('请求暂缓，最早可重试时间：' + utc(until), reason='retry_cooldown')


class TrackedResponse:
    """Headers alone do not prove a successful download; wait for body consumption."""
    def __init__(self, response, policy, url):
        self.response, self.policy, self.request_url = response, policy, url

    def __getattr__(self, name):return getattr(self.response, name)

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, kind, error, traceback):
        result = self.response.__exit__(kind, error, traceback)
        if kind is None:self.policy.success(self.request_url)
        return result


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00', 'Z')


class RetryPolicy:
    def __init__(self, path, *, clock=time.time, force_run=''):
        self.path, self.clock = Path(path), clock
        self.data = {'urls':{}, 'hosts':{}, 'force_run':''}
        try:
            if self.path.stat().st_size < 4*1024*1024:
                saved = json.loads(self.path.read_text(encoding='utf-8'))
                for kind in ('urls', 'hosts'):
                    self.data[kind] = {k:v for k,v in saved.get(kind, {}).items()
                        if isinstance(v,dict) and type(v.get('count')) is int and 0 <= v['count'] <= 100
                        and isinstance(v.get('until'),(int,float)) and math.isfinite(v['until'])}
                self.data['force_run'] = str(saved.get('force_run',''))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if force_run and force_run != self.data['force_run']:
            self.data.update(urls={}, force_run=force_run)
            self.save()

    @staticmethod
    def key(url):
        return hashlib.sha256(url.encode()).hexdigest()

    def until(self, url):
        return max(self.data['urls'].get(self.key(url),{}).get('until',0),
                   self.data['hosts'].get(urlsplit(url).hostname,{}).get('until',0))

    def allowed(self, url):
        return self.until(url) <= self.clock()

    def check(self, url):
        # Older checkpoints stored only hashes. Associate a record when its
        # URL is encountered, without persisting the full request URL.
        entry = self.data['urls'].get(self.key(url))
        host = urlsplit(url).hostname or ''
        if entry is not None and entry.get('host') != host:
            entry['host'] = host
            self.save()
        until = self.until(url)
        if until > self.clock():
            raise DeferredRequest(until)

    def failure(self, url, status=0, retry_after=0):
        current = self.clock()
        key, host = self.key(url), urlsplit(url).hostname
        count = min(100, self.data['urls'].get(key,{}).get('count',0)+1)
        delay = 86400 if status in {404,410} else 3600 if status in {401,403,451} else min(3600,60*2**min(count-1,6))
        self.data['urls'][key] = {'count':count, 'until':current+max(delay,min(86400,max(0,retry_after))), 'status':status, 'host':host or ''}
        if not status or status in {429,500,502,503,504}:
            old = self.data['hosts'].get(host,{})
            failures = min(100, old.get('count',0)+1)
            self.data['hosts'][host] = {'count':failures, 'until':current+max(900,min(86400,retry_after)) if failures>=3 else 0}
        else:
            self.data['hosts'].pop(host,None)
        self.save()

    def success(self, url):
        changed = self.data['urls'].pop(self.key(url),None) is not None
        changed = self.data['hosts'].pop(urlsplit(url).hostname,None) is not None or changed
        if changed:self.save()

    def snapshot(self):
        current = self.clock()
        hosts = self.data['hosts']
        waits = [v['until'] for v in hosts.values() if v['until'] > current]
        for entry in self.data['urls'].values():
            host = entry.get('host', '')
            host_until = hosts.get(host, {}).get('until', 0) if isinstance(host, str) else 0
            until = max(entry['until'], host_until)
            if until > current:waits.append(until)
        return {'waiting':len(waits), 'next_retry_at':utc(min(waits)) if waits else ''}

    def save(self):
        # Bound disk and memory even after a long account scan. Older expired
        # records are evicted first; this is a retry hint, never archive data.
        for kind, limit in (('urls',10000),('hosts',100)):
            entries = self.data[kind]
            if len(entries)>limit:
                self.data[kind] = dict(sorted(entries.items(),key=lambda item:item[1]['until'],reverse=True)[:limit])
        atomic_write_text(self.path,json.dumps(self.data,separators=(',',':')))
