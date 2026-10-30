"""Wayback indexing, checkpoints, media downloads, and offline export."""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit, urljoin, unquote_plus, parse_qs
from shiguangji.storage.collection_lock import collection_lock
from shiguangji.storage import compressed_files as raw_files
from shiguangji.core.atomic_io import atomic_write_bytes, atomic_write_text, publish_if_changed, replace_file, temporary_lock
from shiguangji.platforms.extractor import belongs, blank_post, extract, media_item, merge_post, original_url, post_id
from shiguangji.platforms.account_identity import Catalog, search_handles, revision, snapshot as identity_snapshot
from shiguangji.collect.run_budget import RunBudget, RunLimit

from shiguangji.core.archive_support import (ROOT, DATA, HOSTS, CDX, CDX_SUCCESS_FILTER, MISSING_CACHE_SECONDS,
    PARSER_CACHE_VERSION, INDEX_POLICY_VERSION, now, replay, write_json,
    neighboring_handle, media_context, missing_cache_valid)
from shiguangji.core.core_errors import Cancelled, ArchiveError, failure_kind
from shiguangji.sources.archive_http import Client, ArchiveRedirect, archive_only, decode_page, corrupt_page, media_search_prefix, validate_replay_target, parse_cdx
from shiguangji.media.media_files import media_extension, saved_file_valid
from shiguangji.storage.collection_store import Store
from shiguangji.sources.retry_policy import RetryPolicy
from shiguangji.storage.storage_guard import StorageLow, require_space, rotate_log
from shiguangji.storage.collection_export import publish_manifest, export_collection, make_zip, zip_is_current

class Collector:
    def __init__(self, handle, options=None, progress=lambda **k: None, stop=None, base=DATA, client=None):
        self.handle, self.options = handle, options or {}
        self.budget = RunBudget(self.options.get('max_minutes', 10), self.options.get('max_requests', 200))
        self.progress_callback, self.stop = progress, stop or threading.Event()
        self.client = client or Client(self.stop, self.log, self.options.get("proxy", ""), budget=self.budget)
        with collection_lock(Path(base) / handle):
            self.store = Store(Path(base) / handle, handle)
        self.folder = self.store.folder
        try:
            self.retry_policy = RetryPolicy(self.folder/'.network-retry.json',
                force_run=(self.options.get('_run_id') or now()) if self.options.get('force_recheck') else '')
            self.client.retry_policy = self.retry_policy
            self.identities = Catalog(base)
            self.store.identity_catalog = self.identities
            self.identity = self.identities.snapshot(handle)
            self.index_expected = len(HOSTS) * len(search_handles(self.identity))
            self.errors, self.index_ok, self.processed = [], 0, 0
            self.last_checkpoint = 0
            self.checkpoint_deferred = False
            self.started = time.monotonic()
            self.run_info = {"cached_missing": 0, "force_recheck": bool(self.options.get("force_recheck")),
                             'run_id': self.options.get('_run_id', ''),
                             'options': {k: self.options[k] for k in ('mode', 'multi_source', 'common_crawl',
                                        'public_mirrors', 'max_pages') if k in self.options}}
            self.local_alias_pending = False
            self.alias_search_pending = False
            self.resource_captures = set()
            self.neighbor_handles = set()
            self.original_paths = {m.get('path') for p in self.store.posts() for m in p['media'] if self.saved_file_valid(m)}
            if self.options.get('mode') not in {'repair', 'rescue'}:
                posts = self.store.posts()
                captures = self.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0]
                self.run_info['discovery'] = {'posts_before': len(posts), 'text_before': sum(bool(p['text']) for p in posts),
                                              'captures_before': captures, 'resource_captures': 0,
                                              'queried_hosts': 0, 'failed_hosts': 0}
        except BaseException:
            self.store.close()
            raise
    def identity_check(self):
        self.client.check()
        self.budget.before_work()
    def identity_pending(self):
        identity = self.identities.snapshot(self.handle)
        aliases = [a['username'] for a in identity['aliases'] if a['state'] in {'verified', 'reused'}]
        return bool(aliases and (revision(identity) != self.setting('parsed_identity') or any(
            not self.store.db.execute("SELECT 1 FROM queries WHERE url=? AND error=''", (f"twitter.com/{h}",)).fetchone()
            for h in aliases)))
    def parse_capture(self, raw, url, timestamp, raw_name, stat=None):
        self.identities.record(self.handle, raw_name, raw, timestamp, url, stat)
        self.identity = self.identities.snapshot(self.handle)
        result = []
        anchor = self.identity['user_id']
        for handle in search_handles(self.identity):
            for post in extract(raw, url, timestamp, handle):
                ids = set(post.get('author_ids', []))
                if handle != self.handle:
                    # A renamed URL alone proves no ownership. Each imported
                    # post must itself name the verified numeric author ID.
                    alias = next(a for a in self.identity['aliases'] if a['username'] == handle)
                    if alias['state'] not in {'verified', 'reused'} or not anchor or ids != {anchor}:
                        continue
                elif anchor and ids and ids != {anchor}:
                    continue
                result.append(post)
        return result
    def progress(self, **updates):
        updates['batch'] = self.budget.snapshot()
        self.progress_callback(**updates)
    def mark_saved(self, item, local=False):
        if item.get('path') and item['path'] not in self.original_paths:
            self.budget.saved(item['path'], local=local)
    def setting(self, key, value=None):
        if value is not None:
            with self.store.db:
                self.store.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
            return value
        row = self.store.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else ''
    def ordered_posts(self, phase):
        posts = self.store.posts()
        previous = self.setting('batch_cursor_' + phase)
        for index, post in enumerate(posts):
            if post['id'] == previous:
                # Rotate after the last started post. Interrupted work remains
                # pending and is revisited after the other posts get a chance.
                return posts[index+1:] + posts[:index+1]
        return posts
    def visit_post(self, phase, post):
        self.budget.before_work()
        self.setting('batch_cursor_' + phase, post['id'])
    def log(self, message):
        try:
            rotate_log(self.folder / 'run-log.jsonl')
            with (self.folder / "run-log.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"time": now(), "text": message}, ensure_ascii=False) + "\n")
        except OSError as error:
            if not temporary_lock(error):
                raise
            self.run_info['log_entries_ui_only'] = self.run_info.get('log_entries_ui_only', 0) + 1
        self.progress(message=message)
    def issue(self, stage, source, error):
        entry={"stage": stage, "source": source, "error": str(error)}
        if entry not in self.errors:self.errors.append(entry)
        self.log(f"{stage}：{error}")
    def checkpoint(self, status):
        if status == "running" and time.monotonic() - self.last_checkpoint < 4:
            return
        self.last_checkpoint = time.monotonic()
        self.run_info['retry_policy'] = self.retry_policy.snapshot()
        self.progress(retry_policy=self.run_info['retry_policy'])
        self.clear_resolved_errors()
        self.run_info.update(network_requests=getattr(self.client, "requests", None), elapsed_seconds=round(time.monotonic() - self.started, 2))
        self.run_info['batch'] = self.budget.snapshot()
        self.run_info['scope'] = {'identity_pending': int(self.identity_pending()),
                                 'alias_search_pending': int(self.alias_search_pending),
                                 'local_alias_pending': int(self.local_alias_pending)}
        if 'discovery' in self.run_info:
            info, posts = self.run_info['discovery'], self.store.posts()
            info.update(posts_after=len(posts), text_after=sum(bool(p['text']) for p in posts),
                        captures_after=self.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0],
                        resource_captures=len(self.resource_captures),
                        nearby_handles=sorted(self.neighbor_handles)[:3] if not posts else [])
            info.update(new_posts=max(0, info['posts_after']-info['posts_before']),
                        new_text=max(0, info['text_after']-info['text_before']))
        try:
            manifest = export_collection(self.store, status, self.errors,
                                         full=status != "running" and self.budget.code != 'storage_low' and not self.options.get('_defer_offline_export'),
                                         run_info=self.run_info)
        except OSError as error:
            if status != 'running' or not temporary_lock(error):
                raise
            self.run_info['deferred_checkpoints'] = self.run_info.get('deferred_checkpoints', 0) + 1
            if not self.checkpoint_deferred:
                self.checkpoint_deferred = True
                self.log('进度展示文件暂被占用，下载记录已保存在数据库；继续下载，稍后重试更新页面。')
            return
        if self.checkpoint_deferred:
            self.checkpoint_deferred = False
            self.log('进度展示文件已恢复保存。')
        self.progress(counts={**manifest['counts'], 'hls_segments':int(self.setting('hls_segments') or 0),
                              'identity_pending':int(self.identity_pending()),'alias_search_pending':int(self.alias_search_pending),
                              'alias_search_checked':int(self.identities.search_report(self.handle).get('checked',0)),
                              'local_alias_pending':int(self.local_alias_pending),'local_alias_checked':int(self.setting('local_alias_checked') or 0)})
    def clear_resolved_errors(self):
        self.errors = [e for e in self.errors if e["stage"] != "快照读取失败" or self.store.db.execute("SELECT 1 FROM captures WHERE original=? AND status!='done' LIMIT 1", (e["source"],)).fetchone()]
    def repair_cache(self):
        """Reparse once after parser upgrades, then reuse unchanged local cache checks."""
        damaged = reparsed = reused = 0
        self.identity = self.identities.snapshot(self.handle)
        parsed_revision = revision(self.identity)
        identity_changed = parsed_revision != self.setting("parsed_identity")
        rows = self.store.db.execute("SELECT * FROM captures WHERE status='done'").fetchall()
        for number, row in enumerate(rows, 1):
            self.client.check()
            self.progress(completed=number, total=len(rows))
            key = hashlib.sha256((row["timestamp"] + row["original"]).encode()).hexdigest()[:24]
            raw_path = self.folder / "raw" / (key + ".txt")
            if not raw_files.exists(raw_path):
                continue
            try:
                stat = raw_files.logical_stat(raw_path)
                checked = self.store.db.execute("SELECT * FROM raw_checks WHERE name=?", (raw_path.name,)).fetchone()
                if not identity_changed and checked and (checked['size'], checked['mtime_ns'], checked['parser_version']) == (stat.st_size, stat.st_mtime_ns, PARSER_CACHE_VERSION):
                    reused += 1
                    continue
                raw = raw_files.read_text(raw_path)
            except (OSError, ValueError, EOFError) as error:
                self.store.db.execute("UPDATE captures SET status='pending',error=? WHERE timestamp=? AND original=?", ('本地网页损坏，需重新读取。', row['timestamp'], row['original']))
                self.issue('缓存读取失败', row['original'], error)
                damaged += 1
                continue
            if corrupt_page(raw):
                self.store.db.execute("UPDATE captures SET status='pending',error=? WHERE timestamp=? AND original=?", ("旧版缓存未正确解压，需要重新读取。", row["timestamp"], row["original"]))
                damaged += 1
            else:
                try:
                    self.store.put_posts(self.parse_capture(raw, row["original"], row["timestamp"], raw_path.name, stat))
                    self.store.db.execute("INSERT OR REPLACE INTO raw_checks VALUES(?,?,?,?)", (raw_path.name, stat.st_size, stat.st_mtime_ns, PARSER_CACHE_VERSION))
                    reparsed += 1
                except (ValueError, RecursionError) as error:
                    self.issue("缓存解析失败", row['original'], error)
            if number % 100 == 0:
                self.run_info.update(cache_reparsed=reparsed, cache_reused=reused)
                self.log(f"重新检查本地快照 {number}/{len(rows)}，已用新版解析器处理 {reparsed} 份。")
                self.checkpoint('running')
        self.store.db.commit()
        # Normalize the full-size and query-style URLs, including video thumbnails.
        for p in self.store.posts():
            updated = False
            for m in p["media"]:
                # Migrate previous checks before indexing can add any new sources.
                if m.get("checked_at") and "checked_context" not in m:
                    m["checked_context"] = media_context(p, m)
                    updated = True
                if m.get("reference_only"):
                    continue
                variants = list(m["urls"])
                for url in list(m["urls"]):
                    normalized = media_item(url)
                    if normalized:
                        variants.extend(normalized["urls"])
                        if m["key"] != normalized["key"]:
                            m["key"] = normalized["key"]
                            updated = True
                variants = list(dict.fromkeys(variants))
                if m["urls"] != variants:
                    m["urls"] = variants
                    updated = True
            if updated:
                self.store.save_post(p)
        self.setting("parsed_identity", parsed_revision)
        self.run_info.update(cache_reparsed=reparsed, cache_reused=reused)
        self.log(f"缓存检查完成：重解析 {reparsed} 份，复用 {reused} 份；{damaged} 份损坏快照需要重新读取。")
    def index(self):
        # Resume one incomplete scan cycle, including its already completed host queries.
        checkpoints = self.store.db.execute('SELECT updated FROM index_progress').fetchall()
        for row in checkpoints:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0].replace('Z', '+00:00'))).total_seconds()
            except (TypeError, ValueError):
                age = 8 * 86400
            if not 0 <= age < 7 * 86400:
                self.store.db.execute('DELETE FROM index_progress')
                self.store.db.commit()
                self.log('旧索引续页记录已超过 7 天，将重新检索；已保存的帖子和媒体仍会复用。')
                break
        self.identity = self.identities.snapshot(self.handle)
        handles = search_handles(self.identity)
        self.index_expected = len(HOSTS) * len(handles)
        for host, handle in [(host, handle) for handle in handles for host in HOSTS]:
            self.client.check()
            url = f"{host}/{handle}"
            checkpoint = self.store.db.execute("SELECT resume_key,completed FROM index_progress WHERE url=?", (url,)).fetchone()
            if checkpoint and checkpoint['completed']:
                self.index_ok += 1
                self.log(f"复用本轮已完成的 {host} 索引。")
                continue
            self.log(f"检索 @{handle} 在 {host} 的历史页面…")
            discovery = self.run_info.get('discovery')
            if discovery is not None:
                discovery['queried_hosts'] += 1
            try:
                if hasattr(self.client, "cdx_pages"):
                    resume = checkpoint['resume_key'] if checkpoint else ""
                    if resume:
                        self.log("从上次已保存的索引页继续。")
                    for attempt in range(2):
                        try:
                            for records, next_key in self.client.cdx_pages(url, "prefix", resume=resume):
                                self.neighbor_handles.update(candidate for r in records
                                                             if (candidate := neighboring_handle(r.get('original'), handle)))
                                self.resource_captures.update((r['timestamp'], r['original']) for r in records
                                                             if r.get('statuscode') == '-' and belongs(r['original'], handle))
                                self.store.add_captures(records, index_url=url, next_key=next_key, handles=handles)
                                self.progress(captures=self.store.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
                                self.checkpoint('running')
                            break
                        except ArchiveError as error:
                            if attempt == 0 and resume and '400' in str(error):
                                resume = ''
                                self.store.db.execute('DELETE FROM index_progress WHERE url=?', (url,))
                                self.store.db.commit()
                                self.log('服务不再接受旧续页标记，正在安全地重查该域名索引。')
                            else:
                                raise
                else:
                    for records in self.client.cdx(url, "prefix"):
                        self.neighbor_handles.update(candidate for r in records
                                                     if (candidate := neighboring_handle(r.get('original'), handle)))
                        self.resource_captures.update((r['timestamp'], r['original']) for r in records
                                                     if r.get('statuscode') == '-' and belongs(r['original'], handle))
                        self.store.add_captures(records, handles=handles)
                        self.progress(captures=self.store.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0])
                self.index_ok += 1
                self.store.db.execute("INSERT OR REPLACE INTO queries VALUES(?,?,?)", (url, now(), ""))
            except ArchiveError as e:
                if discovery is not None:
                    discovery['failed_hosts'] += 1
                self.issue("索引失败", url, e)
                self.store.db.execute("INSERT OR REPLACE INTO queries VALUES(?,?,?)", (url, now(), str(e)))
            self.store.db.commit()
        if self.index_ok == self.index_expected:
            self.store.db.execute('DELETE FROM index_progress')
            self.store.db.commit()
    def pages(self):
        rows = self.store.db.execute("SELECT * FROM captures WHERE status!='done' ORDER BY (status='failed'),timestamp,original").fetchall()
        total = self.store.db.execute('SELECT COUNT(*) FROM captures').fetchone()[0]
        parsed = total - len(rows)
        ready = [r for r in rows if self.retry_policy.allowed(replay(r['timestamp'],r['original']))]
        if len(ready)<len(rows):
            self.log(f'暂缓 {len(rows)-len(ready)} 份失败快照，冷却结束后可重试；继续处理其他内容。')
        rows = ready
        self.progress(completed=parsed, total=total, page_progress={'parsed': parsed, 'total': total, 'batch_checked': 0})
        limit = int(self.options.get("max_pages", 0))
        if limit and len(rows) > limit:
            rows = rows[:limit]
            self.issue("扫描范围", "", f"本轮最多读取 {limit} 份快照；其余可继续整理。")
        for number, r in enumerate(rows, 1):
            self.client.check()
            key = (r["original"], r["digest"])
            # A prior successful parse of identical URL+digest makes rereading redundant.
            duplicate = not r["error"] and r["digest"] != "-" and self.store.db.execute("SELECT 1 FROM captures WHERE original=? AND digest=? AND status='done'", key).fetchone()
            if duplicate:
                self.store.db.execute("UPDATE captures SET status='done',error='' WHERE timestamp=? AND original=?", (r["timestamp"], r["original"]))
                self.store.db.commit()
                self.clear_resolved_errors()
                parsed += 1
                self.progress(completed=parsed, total=total, page_progress={'parsed': parsed, 'total': total, 'batch_checked': number})
                continue
            try:
                self.progress(completed=parsed, total=total)
                self.log(f"读取快照 {number}/{len(rows)} · {r['timestamp'][:8]}")
                raw = self.client.text(replay(r["timestamp"], r["original"]))
                if corrupt_page(raw):
                    raise ArchiveError("快照未正确解码，等待重新读取。")
                raw_name = hashlib.sha256((r["timestamp"] + r["original"]).encode()).hexdigest()[:24] + ".txt"
                raw_folder = self.folder / "raw"
                raw_folder.mkdir(exist_ok=True)
                raw_path = raw_folder / raw_name
                require_space(raw_folder, len(raw.encode('utf-8')))
                raw_files.write_text(raw_path, raw)
                stat = raw_files.logical_stat(raw_path)
                self.store.put_posts(self.parse_capture(raw, r["original"], r["timestamp"], raw_name, stat))
                self.store.db.execute("INSERT OR REPLACE INTO raw_checks VALUES(?,?,?,?)", (raw_name, stat.st_size, stat.st_mtime_ns, PARSER_CACHE_VERSION))
                self.store.db.execute("UPDATE captures SET status='done',error='' WHERE timestamp=? AND original=?", (r["timestamp"], r["original"]))
                parsed += 1
            except (ArchiveError, ValueError, RecursionError) as e:
                self.issue("快照读取失败", r["original"], e)
                self.store.db.execute("UPDATE captures SET status='failed',error=? WHERE timestamp=? AND original=?", (str(e), r["timestamp"], r["original"]))
            self.store.db.commit()
            self.progress(completed=parsed, total=total, page_progress={'parsed': parsed, 'total': total, 'batch_checked': number})
            self.clear_resolved_errors()
            self.processed += 1
            if self.processed % 10 == 0:
                self.checkpoint("running")
    def linked_pages(self):
        # Timeline captures may contain posts missing from the account-prefix index.
        known = {post_id(r[0]) for r in self.store.db.execute("SELECT original FROM captures")}
        for p in self.store.posts():
            if p["id"] in known or p["content_status"] != "link_only":
                continue
            self.client.check()
            for host in ("twitter.com", "x.com"):
                try:
                    for records in self.client.cdx(f"{host}/{p['username']}/status/{p['id']}"):
                        self.store.add_captures(records, handles=search_handles(self.identity))
                except ArchiveError as e:
                    self.issue("帖子链接检索失败", p["url"], e)
    def download_media(self):
        max_bytes = int(self.options.get("max_media_mb", 512)) * 1024 * 1024
        saved = {}
        for p in self.store.posts():
            for m in p["media"]:
                if self.saved_file_valid(m):
                    saved[m["key"]] = m
        posts = self.ordered_posts('media')
        self.run_info["cached_missing"] = sum(self.cached(p, m) for p in posts for m in p["media"] if m["key"] not in saved)
        if self.run_info["cached_missing"]:
            self.log(f"复用 24 小时内的空索引检查：{self.run_info['cached_missing']} 项暂不重复请求；可勾选“重新检查暂无存档项”立即复查。")
        pending_posts = [p for p in posts if any(not self.saved_file_valid(m) and not self.cached(p, m) for m in p["media"])]
        self.progress(completed=0, total=len(pending_posts))
        failed_cache = {}
        for number, p in enumerate(pending_posts, 1):
            self.client.check()
            self.visit_post('media', p)
            self.progress(completed=number - 1, total=len(pending_posts))
            self.log(f"补全媒体 · {number}/{len(pending_posts)} 条含未恢复媒体的帖子")
            self.discover_video_files(p)
            for m in p["media"]:
                self.client.check()
                if m["key"] in saved:
                    m.update(saved[m["key"]])
                    continue
                if self.cached(p, m):
                    continue
                if m.get("reference_only"):
                    continue
                if m["key"] in failed_cache:
                    m.update(failed_cache[m["key"]])
                    m["checked_context"] = media_context(p, m)
                    continue
                if m["kind"] == "hls":
                    m.update(status="link_only", error="发现 HLS 播放列表，多源恢复阶段将检查并合并完整分片。", error_kind="hls_pending", checked_at=now())
                    ts = m.get("hint_timestamp") or next((c["timestamp"] for c in p["captures"]), "*")
                    m["archive_url"] = replay(ts, m["urls"][0], raw=False)
                    continue
                candidates = []
                timestamps = sorted({c["timestamp"] for c in p["captures"]}, reverse=True)
                if m.get("hint_timestamp"):
                    timestamps = list(dict.fromkeys([m["hint_timestamp"]] + timestamps))
                # Exact original URL first; no request ever goes to live Twitter/CDNs.
                for url in m["urls"][:2]:
                    for ts in timestamps[:2]:
                        candidates.append(replay(ts, url))
                reason = "没有找到可下载的媒体存档。"
                failure = 'not_found'
                failures = []
                indexed = None
                success = False
                tried = set()
                def attempt(url):
                    nonlocal reason, failure
                    if url in tried:
                        return False
                    tried.add(url)
                    m["attempts"] = m.get("attempts", 0) + 1
                    try:
                        month = p["created_at"][:7].replace("-", "/") if p["created_at"] else "unknown-date"
                        key = hashlib.sha256(m["key"].encode()).hexdigest()[:12]
                        result = self.client.download(url, self.folder / "media" / month / f"{p['id']}_{key}.bin", max_bytes)
                        m.update(status="saved", path=result["file"].relative_to(self.folder).as_posix(),
                                 bytes=result["bytes"], sha256=result["sha256"], archive_url=result["archive_url"], error="")
                        m.update(error_kind="", checked_at=now())
                        saved[m["key"]] = dict(m)
                        self.mark_saved(m)
                        return True
                    except ArchiveError as e:
                        reason = str(e)
                        failure = failure_kind(e)
                        failures.append((failure,reason))
                        return False
                for candidate in candidates:
                    if attempt(candidate):
                        success = True
                        break
                if not success:
                    # A CDN resource may only be captured in a different size or much later.
                    prefix = media_search_prefix(m)
                    try:
                        indexed = False
                        for records in self.client.cdx(prefix, "prefix"):
                            for r in records:
                                variant = media_item(r["original"])
                                if variant and variant["key"] == m["key"]:
                                    indexed = True
                                    if attempt(replay(r["timestamp"], r["original"])):
                                        success = True
                                        break
                            if success:
                                break
                    except ArchiveError as e:
                        indexed = None
                        reason = str(e)
                        failure = failure_kind(e)
                        failures.append((failure,reason))
                if not success:
                    uncertain = [entry for entry in failures if entry[0] != 'not_found']
                    if uncertain:
                        failure,reason = uncertain[-1]
                    category = "not_indexed" if indexed is False and failure == "not_found" else failure
                    if category == "not_indexed":
                        reason = "原链接返回 404，当前索引也未找到同一媒体的其他尺寸存档。"
                    m.update(status="failed", path="", error=reason, error_kind=category, checked_at=now(), checked_context=media_context(p, m))
                    m["archive_url"] = replay(timestamps[0], m["urls"][0], raw=False) if timestamps else ""
                    failed_cache[m["key"]] = {k: m[k] for k in ("status", "path", "error", "error_kind", "checked_at", "archive_url")}
                self.store.save_post(p)
            self.store.save_post(p)
            self.progress(completed=number, total=len(pending_posts))
            self.checkpoint("running")
        self.progress(completed=len(pending_posts), total=len(pending_posts))
    def saved_file_valid(self, m):
        return saved_file_valid(self.folder, m, self.client.check)
    def cached(self, p, m):
        return missing_cache_valid(p, m, self.options.get("force_recheck", False))
    def discover_video_files(self, p):
        """Use the video ID in a saved thumbnail to look up its narrowly scoped MP4 index."""
        prefixes = set()
        for m in p["media"]:
            for url in m.get("urls", []):
                u = urlsplit(url)
                match = re.match(r"/(ext_tw_video|amplify_video)_thumb/(\d+)/", u.path)
                if match:
                    prefixes.add(f"video.twimg.com/{match[1]}/{match[2]}/")
                match = re.match(r"/tweet_video_thumb/([\w-]+)(?:\.|$)", u.path)
                if match:
                    prefixes.add(f"video.twimg.com/tweet_video/{match[1]}.mp4")
        for prefix in prefixes:
            if any(m['kind']=='video' and m['key'].startswith(prefix) and self.saved_file_valid(m) for m in p['media']):
                continue
            if any(not m.get("reference_only") and m["kind"] in {"video", "hls"} and m["key"].startswith(prefix) for m in p["media"]):
                continue
            if any(m.get("reference_only") and m["key"] == prefix and self.cached(p, m) for m in p["media"]):
                continue
            found = []
            reason = "页面留有视频封面，但未检索到对应的 MP4 存档。"
            error_kind = "not_indexed"
            try:
                for records in self.client.cdx(prefix, "prefix"):
                    for record in records:
                        item = media_item(record["original"])
                        if item and item["key"].startswith(prefix) and item["kind"] in {"video", "hls"}:
                            item["hint_timestamp"] = record["timestamp"]
                            found.append(item)
            except ArchiveError as e:
                reason = str(e)
                error_kind = "network"
            p["media"] = [m for m in p["media"] if not (m.get("reference_only") and m["key"] == prefix)]
            if found:
                addition = blank_post(p["id"], self.handle)
                addition["media"] = found
                merge_post(p, addition)
            else:
                p["media"].append({"key": prefix, "kind": "video", "urls": ["https://" + prefix], "status": "link_only", "path": "", "error": reason,
                                   "reference_only": True, "archive_url": "https://web.archive.org/web/*/https://" + prefix + "*"})
                p["media"][-1].update(error_kind=error_kind, checked_at=now())
                p["media"][-1]["checked_context"] = media_context(p, p["media"][-1])
            self.store.save_post(p)
    def run(self):
        try:
            with collection_lock(self.folder):
                return self._run_locked()
        finally:
            self.store.close()
            self.identities.close()
    def _scan_identities(self):
        try:
            previous = json.loads((self.folder / 'manifest.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            previous = {}
        status, report = 'complete', {}
        try:
            self.progress(stage='identity', completed=0, total=0)
            report = self.identities.scan(self.identity_check, self.progress, seconds=self.budget.seconds, max_files=1000000)
            if report.get('remaining'):
                status = 'paused'
                self.budget.code, self.budget.reason = 'time_limit', '达到本批本地检查范围，已保存进度'
            self.log('历史用户名检查已保存；已检查现有本地快照。')
        except (RunLimit, Cancelled) as error:
            status = 'paused'
            if isinstance(error, Cancelled):
                self.budget.code, self.budget.reason = 'user_pause', '已请求暂停并保存'
            self.log('已保存历史用户名检查进度，下次从未检查的快照继续。')
        finally:
            self.progress(stage='export')
            manifest = export_collection(self.store, previous.get('status', 'partial'), previous.get('errors', []),
                run_info={**previous.get('last_run', {}), 'identity_scan': report})
            self.progress(counts=manifest['counts'], work_pending=bool(report.get('remaining') or status == 'paused'))
        return status
    def _run_locked(self):
        if self.options.get("mode") == "identity":
            return self._scan_identities()
        status = "complete"
        try:
            self.log("开始本轮任务：" + ("扩展查找帖子（先整理正文，媒体可随后补全）" if self.options.get('mode')=='discover' else "多源深度恢复" if self.options.get('mode')=='rescue' else "仅补全未恢复内容" if self.options.get("mode") == "repair" else "完整检索与整理"))
            self.log(f'本批最多检索 {self.budget.seconds / 60:g} 分钟 / {self.budget.max_requests} 次请求；媒体阶段连续 3 分钟无新增会提前结束。' + ('后台会按自动继续规则安排下一批。' if self.options.get('auto_continue') else '之后保存并打包，不自动开启下一批。'))
            self.checkpoint("running")
            self.progress(stage="repair", completed=0, total=0)
            if self.options.get('mode', 'full') in {'full', 'discover'}:
                self.progress(stage='identity', completed=0, total=0)
                self.run_info['identity_scan'] = self.identities.scan(self.identity_check, self.progress)
            self.progress(stage='repair', completed=0, total=0)
            self.repair_cache()
            if self.options.get('mode', 'full') in {'full', 'discover'}:
                from shiguangji.platforms.identity_discovery import discover_aliases
                discover_aliases(self)
            from shiguangji.platforms.local_aliases import reuse_local_aliases
            reuse_local_aliases(self)
            if self.options.get("mode") not in {"repair","rescue"}:
                if self.options.get('_skip_index') and not self.identity_pending():
                    self.index_ok = self.index_expected
                else:
                    self.progress(stage="index", completed=0, total=0)
                    self.index()
            self.checkpoint("running")
            if not self.index_ok and not self.store.db.execute("SELECT 1 FROM captures LIMIT 1").fetchone() and not self.store.posts():
                if self.options.get('mode') in {'repair', 'rescue'}:
                    self.issue('未发现帖子', self.handle, '此合集还没有可补全的帖子。请先点击“扩展查找帖子”或“开始整理”。')
                status = "failed"
            else:
                if not self.options.get('_skip_pages'):
                    from shiguangji.collect.post_recovery import recover_posts
                    recover_posts(self)
                    self.progress(stage="pages", completed=0, total=0)
                    self.pages()
                if not self.options.get('_skip_pages') and not self.options.get("max_pages") and self.options.get("mode") not in {"repair","rescue"}:
                    self.linked_pages()
                    self.pages()
                if self.options.get('mode', 'full') in {'full', 'discover'}:
                    # The first page may be the first usable numeric ID. Search
                    # that identity before media work consumes the batch budget.
                    discover_aliases(self)
                self.checkpoint("running")
                phases = []
                if self.options.get('mode') not in {'rescue', 'discover'}:
                    phases.append('media')
                if self.options.get('mode') != 'discover' and (self.options.get('multi_source') or self.options.get('mode') == 'rescue'):
                    phases.append('sources')
                if len(phases) == 2 and self.setting('batch_next_phase') == 'sources':
                    phases.reverse()
                for phase in phases:
                    self.budget.before_work()
                    self.budget.media_phase()
                    self.setting('batch_next_phase', 'sources' if phase == 'media' else 'media')
                    self.progress(stage=phase, completed=0, total=0)
                    if phase == 'media':
                        self.download_media()
                    else:
                        from shiguangji.media.media_recovery import RecoveryEngine
                        RecoveryEngine(self).run()
                self.clear_resolved_errors()
                if self.errors or any(p["content_status"] == "link_only" or any(m["status"] != "saved" for m in p["media"]) for p in self.store.posts()):
                    status = "partial"
        except StorageLow as error:
            status = 'paused'
            self.budget.code, self.budget.reason = 'storage_low', str(error)
            self.progress(message=str(error))
        except RunLimit as error:
            status = 'paused'
            self.log(f'{error}；本批新增 {len(self.budget.saved_paths)} 个媒体文件。' + ('未完成项目已保留，后台将判断是否自动继续。' if self.options.get('auto_continue') else '未完成项目已保留，手动点击补全可继续。'))
        except Cancelled:
            status = "paused"
            self.budget.code, self.budget.reason = 'user_pause', '已请求暂停并保存'
        except Exception as e:
            self.issue("任务异常", "", e)
            status = "failed"
        finally:
            self.progress(stage="export")
            self.checkpoint(status)
        return status
