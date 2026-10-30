"""Finite, resumable lookup of handle history through numeric account evidence.

Public profile schema: https://docs.fxembed.com/api/twitter/operations/2profilehandle/
CDX samples: https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server
No search snippet, redirect, avatar, or similar spelling proves account ownership.
"""
from __future__ import annotations
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

from shiguangji.platforms.account_identity import observations, user_id
from shiguangji.core.core_errors import ArchiveError
from shiguangji.core.archive_support import replay as replay_url
from shiguangji.core.atomic_io import atomic_write_text
from shiguangji.platforms.extractor import original_url, username

POLICY = 1
SUCCESS_TTL = 7 * 86400
ERROR_TTL = 86400
MAX_UNITS = 8
MAX_SECONDS = 90


class SearchLimit(Exception):
    pass


class SearchBudget:
    """Keep the ordinary batch limit and a smaller identity-search allowance."""
    def __init__(self, parent):
        self.parent = parent
        self.started = time.monotonic()
        self.requests = self.units = 0

    def check(self):
        self.parent.check()
        if time.monotonic() - self.started >= MAX_SECONDS or self.units >= MAX_UNITS:
            raise SearchLimit()

    def timeout(self, suggested):
        self.check()
        return min(self.parent.timeout(suggested), 10, MAX_SECONDS - (time.monotonic() - self.started))

    def before_request(self):
        self.check()
        if self.requests >= MAX_UNITS:
            raise SearchLimit()
        self.parent.before_request()
        self.requests += 1


def endpoints(handle, uid):
    if not uid:
        return [f'https://twitter.com/{handle}', f'https://x.com/{handle}']
    return [f'https://twitter.com/intent/user?user_id={uid}',
            f'https://mobile.twitter.com/intent/user?user_id={uid}',
            f'https://twitter.com/i/user/{uid}', f'https://x.com/i/user/{uid}',
            f'https://api.twitter.com/1.1/users/show.json?user_id={uid}',
            f'https://api.twitter.com/1/users/show.json?user_id={uid}',
            f'https://cdn.syndication.twimg.com/widgets/followbutton/info.json?user_ids={uid}']


def same_endpoint(expected, actual):
    try:
        a, b = urlsplit(expected), urlsplit(actual)
        return (b.scheme in {'http', 'https'} and not b.username and not b.password
                and b.port in (None, 80, 443)
                and (a.hostname, a.path.rstrip('/'), parse_qs(a.query))
                == (b.hostname, b.path.rstrip('/'), parse_qs(b.query)))
    except (TypeError, ValueError):
        return False


def safe_replay_source(expected, actual, anchor):
    if same_endpoint(expected, actual):
        return True
    try:
        u = urlsplit(actual)
        return bool(anchor and u.scheme in {'http', 'https'} and not u.username and not u.password
                    and u.port in (None, 80, 443) and not u.query
                    and u.hostname in {'twitter.com', 'x.com', 'mobile.twitter.com', 'mobile.x.com', 'm.twitter.com', 'm.x.com'}
                    and re.fullmatch(r'/[A-Za-z0-9_]{1,15}/?', u.path))
    except (ValueError, TypeError):
        return False


def fresh(entry, now):
    try:
        age = now - float(entry['at'])
        return 0 <= age < (ERROR_TTL if entry.get('status') == 'unavailable' else SUCCESS_TTL)
    except (KeyError, TypeError, ValueError):
        return False


def profile_pairs(raw, handle='', uid=''):
    try:
        data = json.loads(raw)
        profile = data['user']
        if data.get('code') != 200 or profile.get('type') != 'profile':
            return []
        name, found = username(profile['screen_name']), user_id(profile['id'])
        if not found or (uid and found != uid) or (handle and name != handle):
            return []
        return [(found, name, 'current_profile')]
    except (ValueError, KeyError, TypeError, AttributeError):
        return []


class _AliasSearch:
    """One batch of the persistent, finite handle-history search for runner.handle."""

    def __init__(self, runner, public):
        self.runner, self.public = runner, public
        self.catalog, self.handle = runner.identities, runner.handle
        self.key = 'alias_search:' + self.handle
        self.state = self.load()
        self.entries = self.state['entries']
        if not hasattr(runner, '_alias_search_budget'):
            runner._alias_search_budget = SearchBudget(runner.budget)
        self.budget = runner._alias_search_budget
        self.transport = runner.client.identity_client(self.budget)

    def load(self):
        row = self.catalog.db.execute('SELECT value FROM settings WHERE key=?', (self.key,)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
        except (ValueError, TypeError):
            state = {}
        if state.get('policy') != POLICY:
            state = {'policy': POLICY, 'entries': {}, 'checked': 0}
        return state

    def save(self):
        self.state['updated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        with self.catalog.db:
            self.catalog.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (self.key, json.dumps(self.state)))

    def verified(self, identity=None):
        identity = identity or self.catalog.snapshot(self.handle)
        return {a['username'] for a in identity['aliases'] if a['state'] in {'verified', 'reused'}}

    def tasks(self):
        identity = self.catalog.snapshot(self.handle)
        if identity['status'] == 'conflict':
            return []
        uid = identity['user_id']
        todo = []
        if self.runner.options.get('public_mirrors', True):
            url = 'https://api.fxtwitter.com/2/profile/' + ('id:' + uid if uid else self.handle)
            todo.append(('public', url, '', uid))
        for url in endpoints(self.handle, uid):
            for side in ('first', 'last'):
                todo.append(('index', url, side, uid))
                entry = self.entries.get('index|' + url + '|' + side, {})
                if fresh(entry, time.time()):
                    for capture in entry.get('records', []):
                        todo.append(('replay', capture['original'], capture['timestamp'], uid))
        return todo

    @staticmethod
    def task_key(task):
        return '|'.join(task[:3])

    def record(self, raw, pairs, timestamp, source):
        if not pairs:
            return
        # The raw response is kept separately from tweet snapshots. Identity
        # metadata must not inflate the number of recovered tweets/captures.
        name = 'network-' + hashlib.sha256((timestamp + source).encode()).hexdigest()[:24] + '.txt'
        path = self.runner.folder / 'identity-evidence' / name
        path.parent.mkdir(exist_ok=True)
        atomic_write_text(path, raw)
        self.catalog.record_pairs(self.handle, name, pairs, timestamp, source)

    def public_client(self):
        if self.public is None:
            from shiguangji.sources.source_http import SourceClient
            runner = self.runner
            self.public = SourceClient(runner.stop, runner.log, runner.options.get('proxy', ''),
                                       max_requests=MAX_UNITS, budget=self.budget)
            self.public.retry_policy = getattr(runner,'retry_policy',None)
        return self.public

    def check_source(self, task, entry):
        kind, url, side, anchor = task
        if kind == 'public':
            raw = self.public_client().fetch(url, 1024 * 1024)[0].decode('utf-8')
            pairs = profile_pairs(raw, uid=anchor, handle='' if anchor else self.handle)
            self.record(raw, pairs, datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'), url)
            entry['matches'] = len(pairs)
        elif kind == 'index':
            rows = self.transport.identity_index(url, latest=side == 'last')
            entry['records'] = [dict(original=r['original'], timestamp=r['timestamp']) for r in rows
                if r.get('statuscode') == '200' and re.fullmatch(r'[0-9]{14}', r.get('timestamp', ''))
                and same_endpoint(url, r.get('original'))][:3]
        else:
            raw, meta = self.transport.text_with_meta(replay_url(side, url), limit=4 * 1024 * 1024)
            actual = meta.get('url', replay_url(side, url))
            if not safe_replay_source(url, original_url(actual), anchor):
                raise ArchiveError('身份快照跳转到无关来源，未采用。')
            timestamp = re.search(r'/web/([0-9]{14})', actual)
            pairs = [pair for pair in observations(raw)
                     if (pair[0] == anchor if anchor else pair[1] == self.handle)]
            self.record(raw, pairs, timestamp[1] if timestamp else side, original_url(actual))
            entry['matches'] = len(pairs)

    def run(self):
        runner, budget = self.runner, self.budget
        while True:
            available = [task for task in self.tasks() if not fresh(self.entries.get(self.task_key(task), {}), time.time())]
            if not available:
                break
            runner.client.check()
            budget.check()
            task = available[0]
            name = self.task_key(task)
            entry = {'at': time.time(), 'status': 'checked'}
            try:
                self.check_source(task, entry)
            except (ArchiveError, ValueError, RecursionError) as error:
                entry.update(status='unavailable', error=str(error)[:240])
                runner.log('历史用户名来源暂未完成：' + str(error)[:160] + '；已缓存本次结果，继续其他整理。')
            self.entries[name] = entry
            self.state['checked'] += 1
            budget.units += 1
            self.save()
            runner.progress(completed=budget.units, total=MAX_UNITS)

    def finish(self, before):
        """Persist the batch summary, publish it on the runner, and return verified names."""
        runner, entries = self.runner, self.entries
        planned = {self.task_key(task) for task in self.tasks()}
        current = time.time()
        pending_items = sum(not fresh(entries.get(key, {}), current) for key in planned)
        pending = bool(pending_items)
        failures = [entries[key] for key in planned if key in entries
                    and entries[key].get('status') == 'unavailable' and fresh(entries[key], current)]
        identity = self.catalog.snapshot(self.handle)
        after = self.verified(identity)
        self.state.update(status='searching' if pending else 'conflict' if identity['status'] == 'conflict'
                          else 'no_id' if not identity['user_id'] else 'checked' if after else 'not_found',
                          new_aliases=sorted(after - before),
                          errors=len(failures), checked_items=len(planned) - pending_items,
                          pending_items=pending_items, failed_items=len(failures), verified_aliases=sorted(after),
                          state='blocked' if identity['status'] == 'conflict' else 'pending' if pending
                                else 'blocked' if not identity['user_id'] else 'partial' if failures else 'completed',
                          reason='uid_conflict' if identity['status'] == 'conflict' else 'uid_missing' if not identity['user_id']
                                 else 'sources_unavailable' if failures else '',
                          checked_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z') if not pending else '',
                          next_retry_at=datetime.fromtimestamp(min(float(e['at']) + ERROR_TTL for e in failures),
                              timezone.utc).isoformat().replace('+00:00', 'Z') if failures else '')
        self.save()
        runner.alias_search_pending = pending
        runner.identity = identity
        runner.run_info['alias_search'] = self.catalog.search_report(self.handle)
        runner.progress(alias_discovery=runner.run_info['alias_search'])
        return after


def discover_aliases(runner, public=None):
    # Custom transports opt into identity lookup explicitly. This also keeps
    # offline fixture collectors offline instead of making real mirror calls.
    if not hasattr(runner.client, 'identity_client'):
        return {}
    search = _AliasSearch(runner, public)
    before = search.verified()
    try:
        runner.progress(stage='identity_search', completed=0, total=0,
                        alias_discovery={**search.catalog.search_report(search.handle), 'state': 'running'})
        search.run()
    except SearchLimit:
        pass
    finally:
        after = search.finish(before)
    if after - before:
        runner.log('自动核实同一账号的其他用户名：' + '、'.join('@' + name for name in sorted(after - before)) + '；将一起检索。')
    return runner.run_info['alias_search']
