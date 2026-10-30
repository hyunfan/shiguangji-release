#!/usr/bin/env python3
"""Local-only web interface for X Wayback Library. Requires Python 3.10+, no pip packages.

Entry point and composition root: .cmd scripts, launcher.py, deploy_release.py and the
desktop shortcut start this file by path. Request handling lives in shiguangji/web/;
route handlers read this module's singletons (TASKS, CLOUD, STORAGE) and collaborators
at request time, so they remain the patch points used by tests.
"""
from __future__ import annotations
import argparse
import json
import logging
import re
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.request import build_opener, ProxyHandler
from shiguangji.collect.archive import Collector
from shiguangji.core.archive_support import ROOT, DATA
from shiguangji.storage.collection_export import make_zip
from shiguangji.platforms.platform_registry import platform_for
from shiguangji.storage.collection_lock import CollectionBusy, collection_lock
from shiguangji.platforms.platform_accounts import account_input
from shiguangji.collect.instagram_archive import InstagramCollector
from shiguangji.storage.collection_delete import delete_collection
from shiguangji.system.power_guard import prevent_idle_sleep
from windows_integration import protocol_available
from shiguangji.tasks.task_status import summary as task_summary

from shiguangji.tasks.task_service import TaskService, SLEEP_MESSAGES
from shiguangji.system.reliability import save_settings
from shiguangji.storage.cloud_backup import CloudBackup
from shiguangji.storage.storage_service import StorageService
from shiguangji.storage.cloud_media import open_media
from shiguangji.web.handler import LocalHandler, LocalServer
from shiguangji.web.options import collect_options
from shiguangji.web.security import tokens_equal

TOKEN = secrets.token_urlsafe(32)
LOG = logging.getLogger("shiguangji.app")


def token_matches(supplied, expected=None):
    """Constant-time token check that rejects (instead of raising on) non-ASCII input."""
    return tokens_equal(supplied, TOKEN if expected is None else expected)
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


def guard_status(root, port):
    # Imported lazily, as before the web split: supervisor is a root script, not a package module.
    from supervisor import guard_status as status
    return status(root, port)


def collector_for(handle):
    return {'x':Collector,'instagram':InstagramCollector}[platform_for(handle).name]


TASKS = TaskService(DATA, collector_for=lambda handle: collector_for(handle), options_for=collect_options)


class Handler(LocalHandler):
    env = sys.modules[__name__]


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
        from shiguangji.storage.storage_guard import RotatingTextLog
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
