"""Reuse local historical-account data without trusting a name alone."""
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from shiguangji.core.atomic_io import atomic_write_bytes, replace_file
from shiguangji.storage.collection_lock import collection_lock, CollectionBusy
from shiguangji.platforms.extractor import extract
from shiguangji.storage import compressed_files as raw_files

VERSION = 2


def contained(folder, relative, section):
    path = folder / relative
    boundary = folder / section
    if not boundary.resolve().is_relative_to(folder.resolve()) or not path.resolve().is_relative_to(boundary.resolve()):
        raise ValueError('本地文件路径不在合集内')
    return path


def stamp(path):
    try:
        stat = path.stat()
        return [stat.st_size, stat.st_mtime_ns]
    except OSError:
        return None


def target_saved(c, item):
    try:
        contained(c.folder, item.get('path', ''), 'media')
        return c.saved_file_valid(item)
    except (OSError, TypeError, ValueError):
        return False


def copy_media(c, folder, saved, post, item):
    """Validate bytes before publishing; pause never leaves a saved partial file."""
    from shiguangji.media.media_files import media_extension, file_valid
    from shiguangji.core.archive_support import now
    expected = saved.get('sha256', '')
    size = saved.get('bytes', 0)
    if (saved.get('status') != 'saved' or saved.get('kind') != item['kind']
            or not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected) or not isinstance(size, int)
            or size <= 0 or (c.options.get('max_media_mb', 512) and size > int(c.options.get('max_media_mb', 512)) * 1024 * 1024)):
        return None
    source = contained(folder, saved.get('path', ''), 'media')
    if not source.is_file() or source.stat().st_size != size:
        return None
    # Derive the destination from verified post/media identity, never from a source path.
    month = post.get('created_at', '')[:7]
    month = month.replace('-', '/') if re.fullmatch(r'\d{4}-\d{2}', month) else 'unknown-date'
    temporary = None
    try:
        with source.open('rb') as stream:
            first = stream.read(8192)
            extension = media_extension(first)
            if not extension or (extension in {'mp4', 'webm'}) != (item['kind'] == 'video'):
                return None
            key = hashlib.sha256(item['key'].encode()).hexdigest()[:12]
            relative = f"media/{month}/{post['id']}_{key}_{expected[:12]}.{extension}"
            target = contained(c.folder, relative, 'media')
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, total = hashlib.sha256(), 0
            with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.part', delete=False) as output:
                temporary = Path(output.name)
                block = first
                while block:
                    c.identity_check()
                    digest.update(block)
                    total += len(block)
                    if total > size:
                        return None
                    output.write(block)
                    block = stream.read(1024 * 1024)
        if total != size or digest.hexdigest() != expected or not file_valid(temporary, item['kind']):
            return None
        c.identity_check()
        if target.exists():
            # Never overwrite an existing file, including an unexpected path collision.
            existing = hashlib.sha256()
            with target.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    c.identity_check()
                    existing.update(block)
            if existing.hexdigest() != expected:
                return None
        else:
            replace_file(temporary, target)
            temporary = None
        result = deepcopy(saved)
        result.update(path=relative, urls=list(dict.fromkeys(item['urls'] + saved.get('urls', []))),
                      error='', error_kind='', reused_from_collection=folder.name,
                      verified_author_id=post['author_ids'][0], locally_reused_at=now())
        return result
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_source(c, db, folder, alias, anchor, downloads):
    """Read-only view of an alias collection: its posts, parser cache and capture rows.

    ``source_revision`` changes whenever the source gains posts or media or its raw
    files change, which invalidates this account's per-snapshot import fingerprints.
    """
    from shiguangji.platforms.account_identity import revision
    source_rows = db.execute('SELECT id,data FROM posts ORDER BY id').fetchall()
    source_posts = {pid: json.loads(raw) for pid, raw in source_rows}
    # The source collector already parsed these same bytes. Reuse
    # its author-bearing models only while its parser fingerprint
    # still matches the snapshot; otherwise parse the raw again.
    source_checks = {row[0]: tuple(row[1:]) for row in db.execute(
        'SELECT name,size,mtime_ns,parser_version FROM raw_checks')}
    parsed_identity = db.execute("SELECT value FROM settings WHERE key='parsed_identity'").fetchone()
    cache_context_valid = bool(parsed_identity and parsed_identity[0] == revision(c.identities.snapshot(alias)))
    cached_posts = {}
    for post in source_posts.values():
        if set(post.get('author_ids', [])) != {anchor}:
            continue
        for capture in post.get('captures', []):
            if isinstance(capture.get('timestamp'), str) and isinstance(capture.get('url'), str):
                key = hashlib.sha256((capture['timestamp'] + capture['url']).encode()).hexdigest()[:24] + '.txt'
                cached_posts.setdefault(key, []).append(post)
    # Invalidate when the source gains media or its file bytes change.
    media_stamps = []
    if downloads:
        for post in source_posts.values():
            c.identity_check()
            for item in post.get('media', []):
                if item.get('status') == 'saved':
                    try:
                        media_stamps.append([item.get('path'), stamp(contained(folder, item.get('path', ''), 'media'))])
                    except ValueError:
                        media_stamps.append([item.get('path'), None])
    source_revision = hashlib.sha256(json.dumps([source_rows, media_stamps,
        sorted(source_checks.items()), cache_context_valid]).encode()).hexdigest()
    rows = []
    for ts, url, mime, digest in db.execute("SELECT timestamp,original,mimetype,digest FROM captures WHERE status='done'"):
        name = hashlib.sha256((ts + url).encode()).hexdigest()[:24] + '.txt'
        rows.append((name, ts, url, mime, digest))
    rows.sort()
    return source_posts, source_checks, cache_context_valid, cached_posts, source_revision, rows


def _reuse_media(c, folder, alias, post, merged, source_post, available, failed_media, stats):
    """Fill this account's proven media of ``post`` from local files; return [path, stamp] pairs."""
    files = []
    source_media = {m['key']: m for m in source_post.get('media', [])}
    proven_keys = {m['key'] for m in post['media']}
    for item in merged['media']:
        if item['key'] not in proven_keys:
            continue
        if target_saved(c, item):
            files.append([item['path'], stamp(contained(c.folder, item['path'], 'media'))])
            continue
        already = available.get(item['key'])
        if already and already['kind'] == item['kind'] and target_saved(c, already):
            urls = list(dict.fromkeys(item['urls'] + already.get('urls', [])))
            item.update(deepcopy(already), urls=urls)
            files.append([item['path'], stamp(contained(c.folder, item['path'], 'media'))])
            continue
        saved = source_media.get(item['key'], {})
        if saved.get('status') != 'saved':
            continue
        failure_key = (alias, post['id'], item['key'])
        if failure_key in failed_media:
            continue
        try:
            result = copy_media(c, folder, saved, post, item)
        except (OSError, ValueError):
            result = None
        if result:
            item.update(result)
            before_files = len(c.budget.reused_paths)
            c.mark_saved(item, local=True)
            stats['reused_files'] += len(c.budget.reused_paths) - before_files
            available[item['key']] = deepcopy(item)
            files.append([item['path'], stamp(contained(c.folder, item['path'], 'media'))])
        else:
            stats['skipped_media'] += 1
            failed_media.add(failure_key)
    return files


def _keep_raw(c, name, raw, ts, url, mime, digest):
    """Store the snapshot that proved imported posts, and mark its capture as read."""
    from shiguangji.core.archive_support import PARSER_CACHE_VERSION
    destination = contained(c.folder, 'raw/' + name, 'raw')
    if not raw_files.exists(destination):
        raw_files.write_bytes(destination, raw)
    if raw_files.read_bytes(destination) == raw:
        c.store.db.execute('INSERT OR IGNORE INTO captures VALUES(?,?,?,?,?,?)', (ts, url, mime, digest, 'done', ''))
        c.store.db.execute("UPDATE captures SET status='done',error='' WHERE timestamp=? AND original=?", (ts, url))
        st = raw_files.logical_stat(destination)
        c.store.db.execute('INSERT OR REPLACE INTO raw_checks VALUES(?,?,?,?)', (name, st.st_size, st.st_mtime_ns, PARSER_CACHE_VERSION))


def _snapshot_posts(raw, cached, url, ts, alias):
    """Posts of one alias snapshot: the source's parsed models when still valid, else a fresh parse."""
    if cached is None:
        return extract(raw.decode('utf-8', errors='replace'), url, ts, alias)
    parsed = deepcopy(cached)
    for post in parsed:
        for item in post['media']:
            # Source paths are never treated as destination
            # files. The normal copy below validates bytes.
            if item.get('status') == 'saved':
                item.update(status='pending', path='', bytes=0, sha256='')
    return parsed


def reuse_local_aliases(c, max_files=200, seconds=20):
    """Bounded, resumable import. Source libraries stay intact and remain separate."""
    from shiguangji.core.archive_support import PARSER_CACHE_VERSION
    started = time.monotonic()
    info = c.identities.snapshot(c.handle)
    anchor = info['user_id']
    stats = {'checked': 0, 'cached': 0, 'new_posts': 0, 'reused_files': 0, 'pending': False, 'skipped_media': 0}
    c.run_info['local_aliases'] = stats
    c.local_alias_pending = False
    if not anchor:
        return stats
    aliases = sorted(a['username'] for a in info['aliases'] if a['state'] in {'verified', 'reused'})
    if not aliases:
        return stats
    resume = c.setting('local_alias_next')
    if resume in aliases:
        n = aliases.index(resume)
        aliases = aliases[n:] + aliases[:n]
    c.store.db.execute('''CREATE TABLE IF NOT EXISTS local_alias_checks(
        source TEXT,name TEXT,fingerprint TEXT,outputs TEXT,PRIMARY KEY(source,name))''')
    c.store.db.commit()
    downloads = c.options.get('mode') != 'discover'
    known_only = c.options.get('mode') in {'repair', 'rescue'}
    existing_posts = c.store.posts()
    existing_ids = {p['id'] for p in existing_posts}
    known_revision = hashlib.sha256(json.dumps(sorted(existing_ids)).encode()).hexdigest() if known_only else ''
    available = {m['key']: deepcopy(m) for p in existing_posts for m in p['media'] if target_saved(c, m)}
    failed_media = set()
    total_checked = int(c.setting('local_alias_checked') or 0)
    try:
        for alias in aliases:
            folder = c.folder.parent / alias
            if (folder.is_symlink() or folder.resolve().parent != c.folder.parent.resolve()
                    or not (folder / 'archive.sqlite').is_file()
                    or (folder / 'archive.sqlite').resolve().parent != folder.resolve()):
                continue
            last = c.setting('local_alias_cursor_' + alias)
            try:
                with collection_lock(folder), closing(sqlite3.connect(
                        'file:' + (folder / 'archive.sqlite').as_posix() + '?mode=ro', uri=True, timeout=1)) as db:
                    (source_posts, source_checks, cache_context_valid, cached_posts,
                     source_revision, rows) = _read_source(c, db, folder, alias, anchor, downloads)
                    rows = [r for r in rows if r[0] > last] + [r for r in rows if r[0] <= last]
                    c.progress(stage='local_aliases', completed=0, total=len(rows))
                    c.setting('local_alias_next', alias)
                    try:
                        for number, (name, ts, url, mime, digest) in enumerate(rows, 1):
                            c.identity_check()
                            path = contained(folder, 'raw/' + name, 'raw')
                            try:
                                raw_stat = raw_files.logical_stat(path)
                                file_stamp = [raw_stat.st_size, raw_stat.st_mtime_ns]
                            except FileNotFoundError:
                                file_stamp = None
                            fingerprint = json.dumps([VERSION, PARSER_CACHE_VERSION, anchor, downloads, known_only, known_revision,
                                                      c.options.get('max_media_mb', 512), source_revision, file_stamp])
                            prior = c.store.db.execute('SELECT fingerprint,outputs FROM local_alias_checks WHERE source=? AND name=?', (alias, name)).fetchone()
                            if prior and prior['fingerprint'] == fingerprint:
                                outputs = json.loads(prior['outputs'])
                                if (set(outputs['posts']) <= existing_ids and all(stamp(contained(c.folder, p, 'media')) == s for p, s in outputs['files'])):
                                    stats['cached'] += 1
                                    last = name
                                    continue
                            if stats['checked'] >= max_files or (stats['checked'] and time.monotonic() - started >= seconds):
                                stats['pending'] = True
                                return stats
                            posts = []
                            if file_stamp:
                                raw = raw_files.read_bytes(path)
                                cached = cached_posts.get(name, []) if cache_context_valid and source_checks.get(name) == tuple([*file_stamp, PARSER_CACHE_VERSION]) else None
                                posts = [p for p in _snapshot_posts(raw, cached, url, ts, alias)
                                         if set(p.get('author_ids', [])) == {anchor}
                                         and (not known_only or p['id'] in existing_ids)]
                            # Recheck ownership before writing if another collector has added evidence.
                            current = c.identities.snapshot(c.handle)
                            if current['user_id'] != anchor or not any(a['username'] == alias and a['state'] in {'verified', 'reused'} for a in current['aliases']):
                                return stats
                            files = []
                            for post in posts:
                                c.identity_check()
                                prior_post = c.store.db.execute('SELECT data FROM posts WHERE id=?', (post['id'],)).fetchone()
                                if prior_post:
                                    prior_ids = set(json.loads(prior_post[0]).get('author_ids', []))
                                    if prior_ids and prior_ids != {anchor}:
                                        continue
                                c.store.put_post(post)
                                row = c.store.db.execute('SELECT data FROM posts WHERE id=?', (post['id'],)).fetchone()
                                merged = json.loads(row[0])
                                if post['id'] not in existing_ids:
                                    existing_ids.add(post['id'])
                                    stats['new_posts'] += 1
                                if downloads:
                                    files.extend(_reuse_media(c, folder, alias, post, merged, source_posts.get(post['id'], {}),
                                                              available, failed_media, stats))
                                c.store.save_post(merged)
                            if posts:
                                _keep_raw(c, name, raw, ts, url, mime, digest)
                            with c.store.db:
                                c.store.db.execute('INSERT OR REPLACE INTO local_alias_checks VALUES(?,?,?,?)',
                                    (alias, name, fingerprint, json.dumps({'posts': [p['id'] for p in posts], 'files': files})))
                            stats['checked'] += 1
                            total_checked += 1
                            last = name
                            c.progress(completed=number, total=len(rows))
                            if stats['checked'] % 25 == 0:
                                c.checkpoint('running')
                    finally:
                        c.setting('local_alias_cursor_' + alias, last)
            except CollectionBusy:
                stats['pending'] = True
                c.log(f'@{alias} 的本地合集正在使用，稍后再复用。')
            except (OSError, ValueError, sqlite3.Error, EOFError) as error:
                c.log(f'@{alias} 的部分本地资料未能复用（{type(error).__name__}），继续正常检索。')
        c.setting('local_alias_next', '')
        return stats
    finally:
        c.local_alias_pending = stats['pending']
        c.setting('local_alias_checked', str(total_checked))
        if stats['checked'] or stats['new_posts'] or stats['reused_files']:
            c.log(f"本地旧名合集：核对 {stats['checked']} 份快照，新增 {stats['new_posts']} 条帖子，复用 {stats['reused_files']} 个媒体文件。")
        if stats['skipped_media']:
            c.log(f"{stats['skipped_media']} 项本地媒体未通过完整性检查或超过大小限制，保留为待恢复。")
