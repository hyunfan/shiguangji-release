"""Durable search/task history, independent of queue retention and collection files."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3

from archive_support import now
from collection_reader import read_summary
from job_state import read_job
from platform_accounts import collection_key, account_info
from platform_registry import platform_for
from task_status import summary

OPTION_KEYS = {'mode', 'max_pages', 'max_media_mb', 'force_recheck', 'multi_source',
               'common_crawl', 'public_mirrors', 'auto_continue', 'cc_indexes',
               'max_minutes', 'max_requests', 'auto_max_minutes', 'instagram_links'}
STATES = {'running', 'paused', 'interrupted', 'failed', 'finished', 'unknown'}


def safe_options(options):
    # Never persist proxy credentials, local API tokens, or internal run fields.
    return {k: v for k, v in options.items() if k in OPTION_KEYS}


def record(value, options=None, *, origin='recorded', identity=None):
    handle = collection_key(value['username'])
    outcome = summary(value, running=bool(value.get('running')))
    if origin == 'queue' and outcome['state'] == 'unknown' and value.get('status') == 'paused':
        # Older queue entries may retain a definite paused state but no reason.
        outcome = summary({**value, 'task_summary':{'schema_version':1, 'state':'paused', 'reason':'unrecorded'}})
    state = outcome['state']
    if state in {'pausing', 'finalizing'}: state = 'running'
    if state not in STATES: state = 'unknown'
    mode = value.get('mode') or (options or {}).get('mode') or outcome.get('mode') or 'full'
    when = value.get('started_at') or value.get('updated_at') or value.get('added_at') or ''
    run_id = value.get('run_id') or 'legacy-' + hashlib.sha256((handle + when).encode()).hexdigest()
    info = identity or value.get('identity') or {}
    aliases = [a['username'] for a in info.get('aliases', [])
               if a.get('state') in {'verified', 'reused'} and isinstance(a.get('username'), str)]
    uid = str(info.get('user_id') or '')
    result = dict(id=run_id, username=handle, state=state, mode=mode,
                  started_at=when, finished_at=value.get('finished_at') or outcome.get('finished_at') or '',
                  updated_at=value.get('updated_at') or when, origin=origin,
                  options=safe_options(options or {}), aliases=aliases, user_id=uid,
                  counts=value.get('counts') or {}, label=outcome.get('label', ''),
                  reason=outcome.get('reason', ''), message=outcome.get('reason_text', ''),
                  result_label=outcome.get('result_label', ''), **account_info(handle))
    result['terms'] = ' '.join([handle, *aliases, uid]).lower()
    return result


class TaskHistory:
    def __init__(self, base):
        self.base = Path(base)
        self.path = self.base / '.task-history.sqlite'

    @contextmanager
    def connect(self):
        self.base.mkdir(parents=True, exist_ok=True)
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=5)
            db.row_factory = sqlite3.Row
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS history(id TEXT PRIMARY KEY, username TEXT, state TEXT, platform TEXT, started_at TEXT, terms TEXT, data TEXT)')
                db.execute('CREATE INDEX IF NOT EXISTS history_time ON history(started_at DESC, id)')
                db.execute('CREATE INDEX IF NOT EXISTS history_handle ON history(username)')
                db.execute('CREATE TABLE IF NOT EXISTS history_meta(key TEXT PRIMARY KEY, value TEXT)')
                yield db
        except sqlite3.Error as error:
            raise OSError('历史记录无法读写，原文件已保留：' + str(error)) from error
        finally:
            if db is not None: db.close()

    @staticmethod
    def put(db, item, replace=False):
        db.execute(('INSERT OR REPLACE' if replace else 'INSERT OR IGNORE') +
                   ' INTO history VALUES(?,?,?,?,?,?,?)',
                   (item['id'], item['username'], item['state'], item['platform'],
                    item['started_at'], item['terms'], json.dumps(item, ensure_ascii=False)))

    def bootstrap(self, queue, active_run_id=''):
        """One-time evidence-based import; never invent overwritten older runs."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM history_meta WHERE key='legacy_imported'").fetchone():
                candidates = []
                for path in sorted(self.base.glob('*/manifest.json')):
                    try:
                        value = read_summary(path.parent)
                        # Collection directory, not a self-declared path in a manifest.
                        value['username'] = collection_key(path.parent.name)
                        value['identity'] = platform_for(value['username']).identity(self.base, value['username'])
                        candidates.append((value, value.get('last_run', {}).get('options', {}), 'collection'))
                    except (OSError, ValueError, TypeError, KeyError):
                        continue
                for item in queue.data['items']:
                    if item['state'] == 'pending': continue
                    status = {'finished':'complete', 'running':'running', 'paused':'paused', 'failed':'failed'}[item['state']]
                    outcome = dict(item.get('outcome') or {})
                    if outcome.get('state') in STATES: outcome['schema_version'] = 1
                    candidates.append(({**item, 'status':status, 'running':False,
                                        'counts':outcome.get('counts', {}),
                                        'task_summary':outcome}, item['options'], 'queue'))
                saved = read_job(self.base / '.job-state.json')
                if saved and saved.get('username'):
                    candidates.append(({**saved, 'running':False}, {}, 'checkpoint'))
                for value, options, origin in candidates:
                    try:
                        # A legacy running checkpoint is interruption evidence,
                        # not proof of a live process or a successful completion.
                        value = {**value, 'running':False}
                        item = record(value, options, origin=origin)
                        previous = db.execute('SELECT data FROM history WHERE id=?', (item['id'],)).fetchone()
                        if previous:
                            old = json.loads(previous['data'])
                            for key in ('aliases', 'user_id', 'options'):
                                if not item[key]: item[key] = old[key]
                            if not value.get('mode'): item['mode'] = old['mode']
                            item['terms'] = ' '.join([item['username'], *item['aliases'], item['user_id']]).lower()
                        self.put(db, item, replace=True)
                    except (ValueError, TypeError, KeyError):
                        continue
                db.execute("INSERT INTO history_meta VALUES('legacy_imported','1')")
            for row in db.execute("SELECT data FROM history WHERE state='running' AND id!=?", (active_run_id,)).fetchall():
                item = json.loads(row['data'])
                item.update(state='interrupted', label='意外中断，等待继续', reason='process_lost',
                            message='上次后台已结束，保存过的内容仍保留。可继续此任务。')
                self.put(db, item, replace=True)

    def begin(self, job, options):
        with self.connect() as db:
            self.put(db, record(job, options), replace=True)

    def finish(self, job, identity=None):
        if not job.get('run_id'): return
        with self.connect() as db:
            row = db.execute('SELECT data FROM history WHERE id=?', (job['run_id'],)).fetchone()
            if row is None: return
            previous = json.loads(row['data'])
            item = record(job, previous['options'], identity=identity)
            if identity is None:
                item.update(aliases=previous['aliases'], user_id=previous['user_id'])
                item['terms'] = ' '.join([item['username'], *item['aliases'], item['user_id']]).lower()
            item.update(started_at=previous['started_at'], origin=previous['origin'])
            self.put(db, item, replace=True)

    def get(self, item_id):
        if not isinstance(item_id, str) or len(item_id) > 128:
            raise ValueError('历史任务编号无效。')
        with self.connect() as db:
            row = db.execute('SELECT data FROM history WHERE id=?', (item_id,)).fetchone()
        if row is None: raise ValueError('这条历史记录已不存在，请刷新。')
        item = json.loads(row['data'])
        item['username'] = collection_key(item['username'])
        return item

    def failed_start(self, item, message):
        value = record({'username':item['username'], 'run_id':'queue-'+item['id'],
                        'status':'failed', 'started_at':item['added_at'], 'counts':{},
                        'mode':item['options'].get('mode', 'full')}, item['options'], origin='queue')
        value.update(message='队列启动失败：'+message[:240], finished_at=now())
        with self.connect() as db: self.put(db, value)

    def search(self, params):
        query = str(params.get('q', '')).strip().lstrip('@').lower()
        platform, state = params.get('platform', 'all'), params.get('state', 'all')
        if len(query) > 100 or platform not in {'all', 'x', 'instagram'} or state not in STATES | {'all', 'unfinished'}:
            raise ValueError('历史查询条件无效。')
        offset, limit = int(params.get('offset', 0)), int(params.get('limit', 20))
        if offset < 0 or not 1 <= limit <= 100: raise ValueError('历史分页参数无效。')
        clauses, values = [], []
        if query:
            # instr treats %, _ and quotes literally rather than as wildcards.
            clauses.append('instr(terms, ?) > 0'); values.append(query)
        if platform != 'all': clauses.append('platform=?'); values.append(platform)
        if state == 'unfinished': clauses.append("state IN ('paused','interrupted','failed')")
        elif state != 'all': clauses.append('state=?'); values.append(state)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connect() as db:
            total = db.execute('SELECT COUNT(*) FROM history' + where, values).fetchone()[0]
            rows = db.execute('SELECT data FROM history' + where + ' ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = json.loads(row['data'])
            item.pop('options', None); item.pop('terms', None)
            item['has_collection'] = (self.base / item['username'] / 'manifest.json').is_file()
            items.append(item)
        return dict(items=items, total=total, offset=offset, has_more=offset + len(items) < total,
                    note='旧版本仅能补录现存合集、最近任务及保留的队列结果，无法还原已被覆盖的记录。此后从网页启动的任务会持续记录。')

    def forget(self, handle):
        with self.connect() as db:
            db.execute('DELETE FROM history WHERE username=?', (handle,))
