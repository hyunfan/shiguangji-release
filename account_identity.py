"""Evidence-based handle history. No network, fuzzy matching, or destructive merging."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import time
from html.parser import HTMLParser
from pathlib import Path
from extractor import mapping, username, walk_json

VERSION = 1
LIMIT = 12


def user_id(value):
    value = str(value or '')
    return value if re.fullmatch(r'[0-9]{1,20}', value) and 0 < int(value) < 2**64 else ''


class IdentityParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs, self.scripts, self.script = set(), [], None

    def pair(self, handle, uid, kind):
        try:
            handle = username(str(handle or ''))
        except ValueError:
            return
        uid = user_id(uid)
        if uid:
            self.pairs.add((uid, handle, kind))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.pair(attrs.get('data-screen-name'), attrs.get('data-user-id'), 'html')
        if tag == 'script':
            self.script = []

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)

    def handle_endtag(self, tag):
        if tag == 'script' and self.script is not None:
            self.scripts.append(''.join(self.script))
            self.script = None


def observations(source):
    parser = IdentityParser()
    parser.feed(source)
    for raw in [source, *parser.scripts]:
        raw = raw.strip()
        raw = re.sub(r'^(?:window\.)?__[A-Z_]+__\s*=\s*', '', raw)
        try:
            payload = json.JSONDecoder().raw_decode(raw)[0]
        except (ValueError, RecursionError):
            continue
        for obj in walk_json(payload):
            details = mapping(obj.get('legacy')) or mapping(obj.get('core')) or obj
            # A post's rest_id is never a user ID: only objects with an explicit
            # user screen_name/username can contribute identity evidence.
            handle = details.get('screen_name') or obj.get('screen_name') or obj.get('username')
            uid = obj.get('rest_id') or obj.get('id_str') or details.get('id_str') or obj.get('id') or details.get('id')
            parser.pair(handle, uid, 'json')
    return sorted(parser.pairs)


class Catalog:
    def __init__(self, base):
        self.base = Path(base)
        self.base.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.base / '.account-identities.sqlite', timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS observations(collection TEXT, name TEXT, uid TEXT,
                handle TEXT, kind TEXT, timestamp TEXT, source TEXT,
                PRIMARY KEY(collection,name,uid,handle,kind));
            CREATE INDEX IF NOT EXISTS identities_handle ON observations(handle,uid);
            CREATE INDEX IF NOT EXISTS identities_uid ON observations(uid,handle);
            CREATE TABLE IF NOT EXISTS checked(collection TEXT,name TEXT,size INTEGER,
                mtime INTEGER,version INTEGER,PRIMARY KEY(collection,name));
            CREATE TABLE IF NOT EXISTS choices(collection TEXT,handle TEXT,state TEXT,
                PRIMARY KEY(collection,handle));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        ''')

    def close(self):
        self.db.close()

    def record(self, collection, name, raw, timestamp, source, stat=None):
        self.record_pairs(collection, name, observations(raw), timestamp, source, stat)

    def record_pairs(self, collection, name, pairs, timestamp, source, stat=None):
        pairs = [(user_id(uid), username(handle), kind) for uid, handle, kind in pairs if user_id(uid)]
        with self.db:
            self.db.execute('DELETE FROM observations WHERE collection=? AND name=?', (collection, name))
            self.db.executemany('INSERT INTO observations VALUES(?,?,?,?,?,?,?)',
                [(collection, name, uid, handle, kind, timestamp, source)
                 for uid, handle, kind in pairs])
            if stat:
                self.db.execute('INSERT OR REPLACE INTO checked VALUES(?,?,?,?,?)',
                                (collection, name, stat.st_size, stat.st_mtime_ns, VERSION))

    def choice(self, collection, handle, state):
        collection, handle = username(collection), username(handle)
        if collection == handle:
            raise ValueError('不能移除合集的主要用户名。')
        if state not in {'candidate', 'excluded', 'auto'}:
            raise ValueError('无法识别的历史用户名操作。')
        if state == 'candidate':
            count = self.db.execute("SELECT COUNT(*) FROM choices WHERE collection=? AND state='candidate'", (collection,)).fetchone()[0]
            if count >= LIMIT and not self.db.execute('SELECT 1 FROM choices WHERE collection=? AND handle=?', (collection, handle)).fetchone():
                raise ValueError(f'最多补充 {LIMIT} 个待核对名字，请先移除无关候选。')
        with self.db:
            if state == 'auto':
                self.db.execute('DELETE FROM choices WHERE collection=? AND handle=?', (collection, handle))
            else:
                self.db.execute('INSERT OR REPLACE INTO choices VALUES(?,?,?)', (collection, handle, state))

    def snapshot(self, handle):
        choices = dict(self.db.execute('SELECT handle,state FROM choices WHERE collection=?', (handle,)))
        ids = [r[0] for r in self.db.execute('SELECT DISTINCT uid FROM observations WHERE handle=? ORDER BY uid', (handle,))]
        anchor = ids[0] if len(ids) == 1 else ''
        names = set(choices)
        if anchor:
            names.update(r[0] for r in self.db.execute('SELECT DISTINCT handle FROM observations WHERE uid=?', (anchor,)))
        names.discard(handle)
        aliases = []
        for name in sorted(names):
            other_ids = [r[0] for r in self.db.execute('SELECT DISTINCT uid FROM observations WHERE handle=? ORDER BY uid', (name,))]
            state = ('verified' if anchor and other_ids == [anchor]
                     else 'reused' if anchor and anchor in other_ids
                     else 'conflict' if other_ids else 'candidate')
            if choices.get(name) == 'excluded':
                state = 'excluded'
            proofs = [dict(r) for r in self.db.execute('SELECT DISTINCT uid,timestamp,source,kind FROM observations WHERE handle=? ORDER BY timestamp LIMIT 3', (name,))]
            aliases.append({'username': name, 'state': state, 'user_ids': other_ids, 'evidence': proofs})
        primary_proofs = [dict(r) for r in self.db.execute('SELECT DISTINCT uid,timestamp,source,kind FROM observations WHERE handle=? ORDER BY timestamp LIMIT 3', (handle,))]
        return {'username': handle, 'user_id': anchor, 'user_ids': ids,
                'status': 'verified' if anchor else 'conflict' if ids else 'unknown',
                'aliases': aliases, 'evidence': primary_proofs,
                'checked_files': self.db.execute('SELECT COUNT(*) FROM checked').fetchone()[0],
                'search': self.search_report(handle)}

    def search_report(self, handle):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', ('alias_search:' + handle,)).fetchone()
        try:
            state = json.loads(row[0]) if row else {}
            return {key: state[key] for key in ('status', 'checked', 'updated_at', 'new_aliases', 'errors',
                'state', 'reason', 'verified_aliases', 'checked_items', 'pending_items', 'failed_items',
                'checked_at', 'next_retry_at') if key in state}
        except (ValueError, TypeError):
            return {}

    def scan(self, check=lambda: None, progress=lambda **kw: None, seconds=12, max_files=200):
        """Incremental scan of existing local captures; bounded and resumable."""
        started = time.monotonic()
        cursor = self.db.execute("SELECT value FROM settings WHERE key='scan_cursor'").fetchone()
        cursor = cursor[0] if cursor else ''
        entries = []
        for folder in sorted(self.base.iterdir()):
            check()
            if folder.is_symlink() or folder.resolve().parent != self.base.resolve():
                continue
            if not folder.is_dir() or not re.fullmatch(r'[a-z0-9_]{1,15}', folder.name):
                continue
            dbpath = folder / 'archive.sqlite'
            if not dbpath.is_file():
                continue
            try:
                with closing(sqlite3.connect(f'file:{dbpath.as_posix()}?mode=ro', uri=True, timeout=2)) as db:
                    for ts, url in db.execute("SELECT timestamp,original FROM captures WHERE status='done'"):
                        name = hashlib.sha256((ts + url).encode()).hexdigest()[:24] + '.txt'
                        entries.append((folder.name + '/' + name, folder, name, ts, url))
            except sqlite3.OperationalError:
                continue
        entries.sort()
        pending = [row for row in entries if row[0] > cursor]
        processed = 0
        for number, (key, folder, name, ts, url) in enumerate(pending):
            check()
            if time.monotonic() - started >= seconds or processed >= max_files:
                return {'processed': processed, 'remaining': len(pending) - number}
            path = folder / 'raw' / name
            try:
                from compressed_files import logical_stat, read_text
                stat = logical_stat(path)
                prior = self.db.execute('SELECT size,mtime,version FROM checked WHERE collection=? AND name=?', (folder.name, name)).fetchone()
                if not prior or tuple(prior) != (stat.st_size, stat.st_mtime_ns, VERSION):
                    self.record(folder.name, name, read_text(path), ts, url, stat)
                    processed += 1
            except (OSError, ValueError, RecursionError, EOFError):
                pass
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO settings VALUES('scan_cursor',?)", (key,))
            if number % 25 == 0:
                progress(completed=len(entries)-len(pending)+number+1, total=len(entries))
        with self.db:
            self.db.execute("DELETE FROM settings WHERE key='scan_cursor'")
        return {'processed': processed, 'remaining': 0}


def snapshot(base, handle):
    catalog = Catalog(base)
    try:
        return catalog.snapshot(handle)
    finally:
        catalog.close()


def search_handles(identity):
    # A recycled alias can contain both owners' posts. It may broaden discovery
    # only when the primary owner is known; each imported post still needs that ID.
    return [identity['username']] + [a['username'] for a in identity['aliases']
                                    if a['state'] in {'verified', 'reused', 'candidate'}]


def revision(identity):
    return json.dumps([identity['user_ids'], [(a['username'], a['state']) for a in identity['aliases']]], sort_keys=True)


def canonical_handle(base, handle):
    """Reuse a single existing verified collection; never merge existing folders."""
    base = Path(base)
    if (base / handle / 'manifest.json').is_file() or not (base / '.account-identities.sqlite').is_file():
        return handle
    catalog = Catalog(base)
    try:
        matches = []
        for path in base.glob('*/manifest.json'):
            name = path.parent.name
            if not re.fullmatch(r'[a-z0-9_]{1,15}', name):
                continue
            info = catalog.snapshot(name)
            if any(a['username'] == handle and a['state'] == 'verified' for a in info['aliases']):
                matches.append(name)
        if len(matches) > 1:
            raise ValueError('这个名字关联了多个已有合集，请先从左侧选择要继续的合集：' + '、'.join('@'+h for h in matches))
        return matches[0] if matches else handle
    finally:
        catalog.close()
