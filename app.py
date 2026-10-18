#!/usr/bin/env python3
"""Local-only web interface for X Wayback Library. Requires Python 3.10+, no pip packages."""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import webbrowser
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, unquote, parse_qs
from urllib.request import build_opener, ProxyHandler
from archive import Collector
from archive_support import ROOT, DATA
from collection_export import make_zip, zip_is_current
from platform_registry import platform_for
from collection_lock import CollectionBusy, collection_lock
from platform_accounts import collection_key as username, account_input, account_info, is_instagram
from instagram_archive import InstagramCollector
from instagram_extract import post_links
from account_identity import Catalog
from collection_delete import delete_collection
from power_guard import prevent_idle_sleep
from windows_integration import protocol_available
from task_status import summary as task_summary, collection_summary
from task_queue import parse_handles, OPEN as QUEUE_OPEN

from app_version import VERSION
from collection_reader import read_summary, read_document, post_page

from task_service import TaskService, SLEEP_MESSAGES
from reliability import settings as reliability_settings, save_settings
from storage_guard import StorageLow
from cloud_backup import CloudBackup
from storage_service import StorageService
from storage_policy import settings as storage_settings
from cloud_media import open_media

TOKEN = secrets.token_urlsafe(32)
CLOUD = None
STORAGE = None


def storage_service():
    global STORAGE
    if STORAGE is None or STORAGE.base != TASKS.base:
        STORAGE = StorageService(TASKS.base)
    return STORAGE

STABILITY_STAGE = '校验增量备份'
CONTENT_STAGE = '补取已发现帖子的正文'
STAGES = {"identity_search": "自动查找历史用户名", "local_aliases": "复用旧名合集", "identity": "核对历史用户名", "repair": "检查并修复已有缓存", "index": "检索存档", "pages": "读取历史帖子", "media": "补全媒体", "sources":"多来源恢复媒体", "export": "生成离线合集", "zip": "打包 ZIP", "between_batches": "准备自动继续下一批"}



STAGES['content'] = CONTENT_STAGE


def canonical_handle(base, handle):
    return platform_for(handle).canonical(base,handle)


def identity_snapshot(base, handle):
    return platform_for(handle).identity(base,handle)


def collector_for(handle):
    return {'x':Collector,'instagram':InstagramCollector}[platform_for(handle).name]


def collect_options(body):
    options = {'proxy':str(body.get('proxy','')), 'max_pages':int(body.get('max_pages',0)),
               'max_media_mb':int(body.get('max_media_mb',512)), 'mode':body.get('mode','full')}
    if len(options['proxy']) > 2048:
        raise ValueError('代理地址过长。')
    for key, default in (('force_recheck',False), ('multi_source',False), ('common_crawl',True),
                         ('public_mirrors',True), ('auto_continue',True)):
        options[key] = body.get(key, default)
        if not isinstance(options[key], bool):
            raise ValueError('来源和继续选项应为 true 或 false。')
    for key, default, low, high in (('cc_indexes',6,1,12), ('max_minutes',10,1,30),
                                    ('max_requests',200,1,650), ('auto_max_minutes',0,0,1440)):
        options[key] = int(body.get(key, default))
        if not low <= options[key] <= high:
            raise ValueError(f'{key} 超出范围：应为 {low}–{high}。')
    if options['mode'] not in {'full','repair','rescue','discover','identity'}:
        raise ValueError('无法识别的整理方式。')
    if not 0 <= options['max_pages'] <= 1000000 or not 0 <= options['max_media_mb'] <= 100000:
        raise ValueError('数量限制超出范围。')
    if options['mode'] == 'identity':
        options['auto_continue'] = False
    options['instagram_links'] = post_links(body.get('instagram_links', '')) if isinstance(body.get('instagram_links',''),str) else post_links('\n'.join(body['instagram_links']))
    return options


TASKS = TaskService(DATA, collector_for=lambda handle: collector_for(handle), options_for=collect_options)


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR can let two servers bind the same port. Require one owner.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server_version = "XWaybackLibrary/" + VERSION
    def log_message(self, fmt, *args):
        pass
    def valid_host(self):
        return self.headers.get("Host", "") in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
    def headers_common(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
    def json(self, value, code=200, etag=""):
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "private, no-cache" if etag else "no-store")
        if etag:
            self.send_header("ETag", etag)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:
            self.json({"error": str(e)}, 500)
    def get(self):
        if not self.valid_host():
            return self.json({"error": "仅允许本机访问。"}, 403)
        path = unquote(urlsplit(self.path).path)
        if path == "/api/state":
            with TASKS.lock:
                TASKS.refresh_external_job()
                capabilities = ["reliability", "repair", "force_recheck", "restart_recovery", "multi_source", "post_discovery", "bounded_batches", "auto_batches", "delete_collection", "prevent_sleep", "account_identity", "task_history"]
                if protocol_available(ROOT):
                    capabilities.append("desktop_launcher")
                identity = identity_snapshot(TASKS.base, TASKS.job['username']) if TASKS.job.get('username') else {}
                alias = TASKS.job.get('alias_discovery', {}) if TASKS.job.get('running') and TASKS.job.get('stage') == 'identity_search' else identity.get('search') or TASKS.job.get('alias_discovery', {})
                job = {**TASKS.job, **account_info(TASKS.job.get('username','')), 'identity': identity, 'task_summary': TASKS.reconcile_summary(task_summary(TASKS.job, alias=alias))}
                from supervisor import guard_status
                return self.json({"app": "x-wayback-library", "version": VERSION, "capabilities": capabilities + ['auto_alias_search', 'task_status', 'task_queue', 'instagram', 'post_pagination'], "token": TOKEN, "job": job, "queue": TASKS.task_queue().public(), "stages": {**STAGES,'backup':STABILITY_STAGE}, "data_dir": str(TASKS.base),
                                  'health':TASKS.health_snapshot(), 'guard':guard_status(ROOT,self.server.server_address[1]), 'reliability':reliability_settings(TASKS.base),
                                  'cloud_backup':CLOUD.snapshot() if CLOUD else None, 'storage':storage_service().snapshot()})
        if path.startswith('/api/cloud-media/'):
            parts=path[len('/api/cloud-media/'):].split('/',1)
            if len(parts)!=2:return self.json({'error':'媒体地址无效。'},400)
            try:
                with open_media(TASKS.base,username(parts[0]),parts[1]) as (cached,record,_):
                    return self.file(cached,mime=record['mime'])
            except (OSError,ValueError,KeyError) as error:
                return self.json({'error':str(error)},503)
        if path == '/api/history':
            try:
                return self.json(TASKS.history_result({k:v[-1] for k,v in parse_qs(urlsplit(self.path).query).items()}))
            except ValueError as error:
                return self.json({'error':str(error)}, 400)
        if path.startswith("/api/identity/"):
            handle = username(path.rsplit("/", 1)[1])
            return self.json(identity_snapshot(TASKS.base, handle))
        if path == "/api/collections":
            result = []
            for p in sorted(TASKS.base.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    m = read_summary(p.parent)
                    busy = (TASKS.job['running'] and TASKS.job['username'] == m['username']) or TASKS.collection_busy(username(m['username']))
                    m['identity'] = identity_snapshot(TASKS.base, username(m['username']))
                    entry_summary = TASKS.reconcile_summary(collection_summary(m, TASKS.job, busy))
                    if m["status"] == "running" and not ((TASKS.job["running"] and TASKS.job["username"] == m["username"]) or TASKS.collection_busy(username(m["username"]))):
                        m["status"] = "paused"
                    entry = {k: m[k] for k in ("username", "status", "updated_at", "counts")}
                    identity = m['identity']
                    entry['identity'] = {k: identity[k] for k in ('status', 'user_id', 'aliases', 'search')}
                    entry['task_summary'] = entry_summary
                    entry.update(account_info(m['username']))
                    result.append(entry)
                except (OSError, ValueError, KeyError):
                    continue
            return self.json(result)
        if path.startswith('/api/posts/'):
            handle = username(path.rsplit('/',1)[1])
            if not (TASKS.base / handle / 'manifest.json').is_file():
                return self.json({'error':'尚未生成合集。'},404)
            params = {k:v[-1] for k,v in parse_qs(urlsplit(self.path).query).items()}
            try:
                return self.json(post_page(TASKS.base / handle, params))
            except ValueError as error:
                return self.json({'error':str(error)},400)
        if path.startswith("/api/collection/"):
            handle = username(path.rsplit("/", 1)[1])
            target = TASKS.base / handle / "manifest.json"
            if not target.exists():
                return self.json({"error": "尚未生成合集。"}, 404)
            paginated = parse_qs(urlsplit(self.path).query).get('view') == ['summary']
            if paginated:
                m = read_summary(target.parent)
                raw = None
            else:
                document, raw = read_document(target.parent)
                m = dict(document)
            # Evidence can arrive through another collection after this manifest
            # was exported. Serve the current catalog and invalidate its ETag.
            identity = identity_snapshot(TASKS.base, handle)
            m['identity'] = identity
            running = (TASKS.job["running"] and TASKS.job["username"] == handle) or TASKS.collection_busy(handle)
            outcome = TASKS.reconcile_summary(collection_summary(m, TASKS.job, running))
            interrupted = m.get("status") == "running" and not running
            ready = not running and not interrupted and zip_is_current(TASKS.base / handle, raw, manifest_sha256=m.get('content_revision') if paginated else None)
            offline_ready = all((target.parent / name).is_file() for name in ('index.html', 'data.js'))
            etag = '"' + hashlib.sha256((m['content_revision'].encode() if paginated else raw) + str((ready, offline_ready, running, interrupted, storage_settings(TASKS.base)['zip_on_demand'])).encode()
                                       + json.dumps([identity, outcome], sort_keys=True).encode()).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.headers_common()
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "private, no-cache")
                self.end_headers()
                return
            if interrupted:
                m["status"] = "paused"
            m.update(zip_ready=ready, zip_on_demand=storage_settings(TASKS.base)['zip_on_demand'], offline_ready=offline_ready, collecting=running, interrupted=interrupted, identity=identity, task_summary=outcome)
            return self.json(m, etag=etag)
        if path.startswith("/download/"):
            handle = username(path.rsplit("/", 1)[1])
            if (TASKS.job["running"] and TASKS.job["username"] == handle) or TASKS.collection_busy(handle):
                return self.json({"error": "合集正在更新，请稍后下载。"}, 409)
            manifest_path = TASKS.base / handle / "manifest.json"
            manifest_running = manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")).get("status") == "running"
            if manifest_running or not zip_is_current(TASKS.base / handle):
                return self.json({"error": "ZIP 尚未生成或未包含最新结果，请点击“重新生成 ZIP”。"}, 409)
            return self.file(TASKS.base / (handle + "-合集.zip"), attachment=f"{handle}-wayback.zip")
        if path.startswith("/collections/"):
            relative = path[len("/collections/"):]
            target = (TASKS.base / relative).resolve()
            if not target.is_relative_to(TASKS.base.resolve()) or target.suffix.lower() not in {".html", ".css", ".js", ".json", ".jsonl", ".csv", ".txt", ".jpg", ".png", ".gif", ".webp", ".avif", ".mp4", ".webm"}:
                return self.json({"error": "文件不可访问。"}, 403)
            return self.file(target)
        filename = "index.html" if path == "/" else path.removeprefix("/static/")
        if filename not in {"index.html", "app.js", "gallery.js", "history.js", "storage.js", "style.css"}:
            return self.json({"error": "未找到页面。"}, 404)
        return self.file(ROOT / "static" / filename)
    def file(self, path, attachment="", mime=None):
        if not path.is_file():
            return self.json({"error": "文件尚未生成。"}, 404)
        size, begin, end = path.stat().st_size, 0, path.stat().st_size - 1
        range_header = self.headers.get("Range", "")
        code = 200
        if range_header:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not m or not any(m.groups()):
                return self.json({"error": "无效的文件范围。"}, 416)
            if m[1]:
                begin, end = int(m[1]), min(int(m[2]), size - 1) if m[2] else size - 1
            else:
                begin = max(0, size - int(m[2]))
            if begin >= size or begin > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        self.send_response(code)
        self.headers_common()
        mime = mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime in {"application/json", "application/javascript"} else ""))
        self.send_header("Content-Length", str(max(0, end - begin + 1)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        if code == 206:
            self.send_header("Content-Range", f"bytes {begin}-{end}/{size}")
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self.end_headers()
        with path.open("rb") as f:
            f.seek(begin)
            remaining = end - begin + 1
            while remaining > 0:
                chunk = f.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
    def do_POST(self):
        if not self.valid_host() or not secrets.compare_digest(self.headers.get("X-Local-Token", ""), TOKEN):
            return self.json({"error": "请刷新本地页面后重试。"}, 403)
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
            return self.json({"error": "请求来源不受支持。"}, 403)
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= (65536 if self.path.startswith('/api/queue/') else 8192):
                raise ValueError("请求大小无效。")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求内容应为对象。")
            if self.path == '/api/reliability':
                return self.json({'ok':True,'reliability':save_settings(TASKS.base,body)})
            if self.path == '/api/storage/settings':
                return self.json({'ok':True,'storage':storage_service().configure(body)})
            if self.path == '/api/storage/action':
                if TASKS.job.get('running') or TASKS.stopping.is_set():
                    raise ValueError('请等待采集保存结束，或暂停任务后再管理空间。')
                if body.get('operation')=='cleanup' and body.get('confirm_zip_only') is not True:
                    raise ValueError('请核对 ZIP 清理清单后确认。')
                result=storage_service().submit(body.get('operation'),handle=body.get('username',''),
                    plan_id=body.get('plan_id',''),all_exports=body.get('all_exports') is True)
                return self.json({'ok':True,'storage':result})
            if self.path == '/api/cloud-backup':
                if CLOUD is None or TASKS.stopping.is_set():
                    raise ValueError('后台正在启动或退出，请稍后重试。')
                result = CLOUD.configure(body)
                if body['enabled']:
                    save_settings(TASKS.base, {'backup_after_task': True})
                return self.json({'ok': True, 'cloud_backup': result})
            if self.path == '/api/history/retry':
                result = TASKS.retry_history(body.get('id'), collect_options(body))
                return self.json({'ok':True, **result})
            if self.path.startswith('/api/queue/'):
                with TASKS.lock:
                    if TASKS.stopping.is_set():
                        raise ValueError('后台正在保存并退出，请重新启动后继续队列。')
                    queue = TASKS.task_queue()
                    result = {}
                    if self.path == '/api/queue/add':
                        handles = [canonical_handle(TASKS.base, h) for h in parse_handles(body.get('usernames'), body.get('platform','x'))]
                        options = collect_options({**body, 'mode':'full', 'auto_continue':True})
                        result = queue.add(handles, options, body.get('request_id',''))
                    elif self.path == '/api/queue/pause':
                        TASKS.pause_queue()
                    elif self.path == '/api/queue/resume':
                        if TASKS.job.get('running') and TASKS.job.get('pause_requested'):
                            raise ValueError('当前账号还在保存暂停进度，请稍候再继续队列。')
                        queue.resume()
                    elif self.path == '/api/queue/remove':
                        TASKS.task_history()
                        queue.remove(body.get('id'))
                    elif self.path == '/api/queue/clear-finished':
                        TASKS.task_history()
                        queue.clear_finished()
                    else:
                        return self.json({'error':'未找到队列操作。'}, 404)
                    TASKS.advance_queue()
                    return self.json({'ok':True, **result, 'queue':queue.public()})
            elif self.path == "/api/pause":
                if not TASKS.pause_current():
                    return self.json({"error": "已进入保存和打包阶段，请等待完成。"}, 409)
            elif self.path == '/api/identity':
                handle = username(str(body.get('username', '')))
                if is_instagram(handle):
                    raise ValueError('Instagram 暂不支持自动关联历史用户名；可按旧用户名建立单独合集。')
                alias = username(str(body.get('alias', '')))
                if not (TASKS.base / handle / 'manifest.json').is_file():
                    raise ValueError('请先创建该博主的合集。')
                with TASKS.lock:
                    if TASKS.job.get('running') or TASKS.collection_busy(handle):
                        return self.json({'error': '请等待当前任务保存完成后再修改历史用户名。'}, 409)
                    with collection_lock(TASKS.base / handle):
                        catalog = Catalog(TASKS.base)
                        try:
                            catalog.choice(handle, alias, body.get('state', 'candidate'))
                        finally:
                            catalog.close()
            elif self.path == '/api/delete':
                handle = username(str(body.get('username', '')))
                if body.get('confirm_username') != handle:
                    raise ValueError('请确认要删除的博主后再操作。')
                with TASKS.lock:
                    if any(i['username'] == handle and i['state'] in QUEUE_OPEN for i in TASKS.task_queue().data['items']):
                        return self.json({'error':'该博主仍在队列中，请先从队列移除，再删除合集。'}, 409)
                    if TASKS.job.get('running') and TASKS.job.get('username') == handle:
                        return self.json({'error': '该博主正在整理，请先暂停并等待保存完成，再删除。'}, 409)
                    history = TASKS.task_history()
                    try:
                        delete_collection(TASKS.base, handle)
                    except CollectionBusy as error:
                        return self.json({'error': str(error)}, 409)
                    except OSError:
                        return self.json({'error': '删除未完成，部分文件可能已移除。请关闭该博主的图片、视频或 ZIP 后重试。'}, 409)
                    history.forget(handle)
                    if TASKS.job.get('username') == handle:
                        TASKS.job.clear()
                        TASKS.job.update(running=False, pause_requested=False, username='', status='idle', stage='idle', logs=[], counts={})
                        TASKS.persist_job(force=True)
            elif self.path in {"/api/start", "/api/zip"}:
                handle = account_input(str(body.get("username", "")), body.get("platform"))
                if self.path == '/api/start':
                    handle = canonical_handle(TASKS.base, handle)
                options = collect_options(body)
                if is_instagram(handle) and options["mode"] == "identity":
                    raise ValueError("Instagram 暂不支持自动关联历史用户名。")
                if options["mode"] in {"repair","rescue"} and not (TASKS.base / handle / "manifest.json").exists():
                    raise ValueError("请先进行一次完整整理，再补全已有合集。")
                if self.path == "/api/zip" and not (TASKS.base / handle / "manifest.json").exists():
                    raise ValueError("请先整理该用户的存档。")
                with TASKS.lock:
                    queue = TASKS.task_queue()
                    if (self.path == '/api/start' and options['mode'] == 'full' and not TASKS.job.get('running')
                            and any(i['username'] == handle and i['state'] == 'paused' for i in queue.data['items'])):
                        queue.resume()
                        TASKS.advance_queue()
                    else:
                        TASKS.start(handle, options, self.path == "/api/zip")
            else:
                return self.json({"error": "未找到操作。"}, 404)
            return self.json({"ok": True})
        except StorageLow as e:
            return self.json({'error':str(e)}, 507)
        except CollectionBusy as e:
            return self.json({"error": str(e)}, 409)
        except OSError as e:
            if self.path.startswith('/api/queue/'):
                with TASKS.lock:
                    TASKS.task_queue().data.update(state='paused', reason='storage_error')
            return self.json({'error':'本地记录暂时无法保存，请检查磁盘和文件占用后重试：'+str(e)[:200]}, 503)
        except (ValueError, TypeError) as e:
            return self.json({"error": str(e)}, 400)


def running_instance(port):
    """Identify this app before reusing an occupied port; bypass system proxies for localhost."""
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=2) as response:
            result = json.loads(response.read(1024 * 1024))
        if isinstance(result, dict) and result.get("app") == "x-wayback-library":
            return result
    except (OSError, ValueError):
        pass
    return None


def open_browser(url):
    try:
        opened = webbrowser.open(url)
    except (OSError, webbrowser.Error):
        opened = False
    if not opened:
        print(f"Browser did not open automatically. Open this address manually: {url}")
    return opened


def main():
    global CLOUD
    parser = argparse.ArgumentParser(description="X / Twitter Wayback 本地合集工具")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="启动后打开浏览器")
    parser.add_argument('--data-dir', type=Path, help='使用指定资料库目录，例如检查恢复后的备份')
    parser.add_argument("--background-token", default="", help=argparse.SUPPRESS)
    parser.add_argument('--recover-queue-run', default='', help=argparse.SUPPRESS)
    parser.add_argument("--platform", choices=["x","instagram"], default="x")
    parser.add_argument("--collect", help="命令行整理一个用户名")
    parser.add_argument("--proxy", default="")
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument('--max-minutes', type=int, default=10, choices=range(1, 31), help='每批检索分钟数，默认 10，结束后保存打包')
    parser.add_argument('--max-requests', type=int, default=200, choices=range(1, 651), help='所有来源共享的每批请求上限，默认 200')
    parser.add_argument("--repair", action="store_true", help="仅补全 --collect 指定的已有合集，不重新扫描用户索引")
    parser.add_argument("--rescue", action="store_true", help="对已有合集执行多来源媒体恢复")
    parser.add_argument("--discover", action="store_true", help="重新检索历史帖子并解析正文，媒体可随后补全")
    parser.add_argument("--multi-source", action="store_true", help="常规采集后继续多来源恢复")
    parser.add_argument("--no-common-crawl", action="store_true", help="不查询 Common Crawl 存档")
    parser.add_argument("--no-public-mirrors", action="store_true", help="不查询第三方公开帖子镜像")
    parser.add_argument("--force-recheck", action="store_true", help="立即复查 24 小时内暂未找到存档的媒体")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.background_token and not re.fullmatch(r"[a-f0-9]{32}", args.background_token):
        parser.error("Invalid background session token")
    if args.data_dir:
        if args.background_token:parser.error('自定义资料库使用前台模式启动。')
        TASKS.base = args.data_dir.resolve()
    TASKS.base.mkdir(parents=True, exist_ok=True)
    if args.collect:
        handle = account_input(args.collect, args.platform)
        try:
            with prevent_idle_sleep(lambda state: print(SLEEP_MESSAGES[state]) if state in SLEEP_MESSAGES else None), collection_lock(TASKS.base / handle):
                status = collector_for(handle)(handle, {"proxy": args.proxy, "max_pages": args.max_pages, "mode": "discover" if args.discover else "rescue" if args.rescue else "repair" if args.repair else "full", "force_recheck": args.force_recheck,
                                           "max_minutes":args.max_minutes,"max_requests":args.max_requests,"multi_source":args.multi_source,"common_crawl":not args.no_common_crawl,"public_mirrors":not args.no_public_mirrors}, lambda **k: print(k.get("message", "")) if "message" in k else None, base=TASKS.base).run()
                make_zip(TASKS.base / handle)
        except CollectionBusy as error:
            print(error)
            return 1
        print(status, TASKS.base / (handle + "-合集.zip"))
        return 1 if status == "failed" else 0
    url = f"http://127.0.0.1:{args.port}"
    try:
        server = LocalServer(("127.0.0.1", args.port), Handler)
    except OSError as error:
        existing = running_instance(args.port)
        if existing:
            print(f"X Wayback Library is already running: {url}")
            if existing.get("data_dir"):
                print(f"Existing library folder: {existing['data_dir']}")
            if args.open:
                open_browser(url)
            return 0
        print(f"Could not start X Wayback Library: {error}")
        print(f"If port {args.port} belongs to another program, run: python app.py --port 8766 --open")
        return 1
    TASKS.restore_job()
    TASKS.task_queue()
    CLOUD = CloudBackup(TASKS.base)
    CLOUD.start()
    print(f"X Wayback Library: {url}  (Ctrl+C to stop)")
    print(f"Library folder: {TASKS.base}")
    if args.open:
        open_browser(url)
    if args.background_token:
        import sys
        from storage_guard import RotatingTextLog
        sys.stderr = RotatingTextLog(ROOT/'runtime'/f'http-{args.port}.log')
        from launcher import background_ready, monitor_stop
        background_ready(ROOT, args.port, args.background_token)
        threading.Thread(target=monitor_stop, args=(ROOT, args.port, args.background_token, server,
                         TASKS.stop, TASKS.job, TASKS.lock, TASKS.progress, TASKS.stopping), daemon=True).start()
        TASKS.recover_queue(args.recover_queue_run)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        TASKS.stop.set()
    finally:
        TASKS.stopping.set()
        if TASKS.retry_timer:
            TASKS.retry_timer.cancel()
        CLOUD.close()
        server.server_close()
        if args.background_token:
            from launcher import background_stopped
            background_stopped(ROOT, args.port, args.background_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
