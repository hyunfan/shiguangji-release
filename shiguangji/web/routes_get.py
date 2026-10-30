"""GET routes. Each handler is ``handler(h, env, path)`` and writes its own response.

``env`` is the application module; see ``shiguangji.web``.
"""
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from shiguangji.core.app_version import VERSION
from shiguangji.platforms.platform_accounts import account_info, collection_key as username
from shiguangji.storage.collection_export import zip_is_current
from shiguangji.storage.collection_reader import post_page, read_document, read_summary
from shiguangji.storage.storage_policy import settings as storage_settings
from shiguangji.system.reliability import settings as reliability_settings
from shiguangji.tasks.task_status import collection_summary
from shiguangji.web.files import STATIC_FILES, collection_file

CAPABILITIES = ["reliability", "repair", "force_recheck", "restart_recovery", "multi_source", "post_discovery",
                "bounded_batches", "auto_batches", "delete_collection", "prevent_sleep", "account_identity", "task_history"]
LATER_CAPABILITIES = ['auto_alias_search', 'task_status', 'task_queue', 'instagram', 'post_pagination']


def query(h):
    return {k: v[-1] for k, v in parse_qs(urlsplit(h.path).query).items()}


def state(h, env, path):
    tasks = env.TASKS
    with tasks.lock:
        tasks.refresh_external_job()
        capabilities = list(CAPABILITIES)
        if env.protocol_available(env.ROOT):
            capabilities.append("desktop_launcher")
        identity = env.identity_snapshot(tasks.base, tasks.job['username']) if tasks.job.get('username') else {}
        alias = tasks.job.get('alias_discovery', {}) if tasks.job.get('running') and tasks.job.get('stage') == 'identity_search' else identity.get('search') or tasks.job.get('alias_discovery', {})
        job = {**tasks.job, **account_info(tasks.job.get('username','')), 'identity': identity, 'task_summary': tasks.reconcile_summary(env.task_summary(tasks.job, alias=alias))}
        return h.json({"app": "x-wayback-library", "version": VERSION, "capabilities": capabilities + LATER_CAPABILITIES, "token": env.TOKEN, "job": job, "queue": tasks.task_queue().public(), "stages": {**env.STAGES,'backup':env.STABILITY_STAGE}, "data_dir": str(tasks.base),
                       'health':tasks.health_snapshot(), 'guard':env.guard_status(env.ROOT,h.server.server_address[1]), 'reliability':reliability_settings(tasks.base),
                       'cloud_backup':env.CLOUD.snapshot() if env.CLOUD else None, 'storage':env.storage_service().snapshot()})


def cloud_media(h, env, path):
    parts = path[len('/api/cloud-media/'):].split('/',1)
    if len(parts) != 2:
        return h.json({'error':'媒体地址无效。'},400)
    try:
        with env.open_media(env.TASKS.base,username(parts[0]),parts[1]) as (cached,record,_):
            return h.file(cached,mime=record['mime'])
    except (OSError,ValueError,KeyError) as error:
        return h.json({'error':str(error)},503)


def history(h, env, path):
    try:
        return h.json(env.TASKS.history_result(query(h)))
    except ValueError as error:
        return h.json({'error':str(error)}, 400)


def identity(h, env, path):
    return h.json(env.identity_snapshot(env.TASKS.base, username(path.rsplit("/", 1)[1])))


def collection_entry(env, folder):
    tasks = env.TASKS
    m = read_summary(folder)
    busy = tasks.collection_active(m['username'], username(m['username']))
    m['identity'] = env.identity_snapshot(tasks.base, username(m['username']))
    entry_summary = tasks.reconcile_summary(collection_summary(m, tasks.job, busy))
    if m["status"] == "running" and not tasks.collection_active(m['username'], username(m['username'])):
        m["status"] = "paused"
    entry = {k: m[k] for k in ("username", "status", "updated_at", "counts")}
    identity = m['identity']
    entry['identity'] = {k: identity[k] for k in ('status', 'user_id', 'aliases', 'search')}
    entry['task_summary'] = entry_summary
    entry.update(account_info(m['username']))
    return entry


def collections(h, env, path):
    result = []
    for p in sorted(env.TASKS.base.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            result.append(collection_entry(env, p.parent))
        except (OSError, ValueError, KeyError):
            continue
    return h.json(result)


def posts(h, env, path):
    handle = username(path.rsplit('/',1)[1])
    if not (env.TASKS.base / handle / 'manifest.json').is_file():
        return h.json({'error':'尚未生成合集。'},404)
    try:
        return h.json(post_page(env.TASKS.base / handle, query(h)))
    except ValueError as error:
        return h.json({'error':str(error)},400)


def collection_etag(m, raw, paginated, flags, identity, outcome):
    return '"' + hashlib.sha256((m['content_revision'].encode() if paginated else raw) + str(flags).encode()
                                + json.dumps([identity, outcome], sort_keys=True).encode()).hexdigest() + '"'


def collection(h, env, path):
    tasks = env.TASKS
    handle = username(path.rsplit("/", 1)[1])
    target = tasks.base / handle / "manifest.json"
    if not target.exists():
        return h.json({"error": "尚未生成合集。"}, 404)
    paginated = parse_qs(urlsplit(h.path).query).get('view') == ['summary']
    if paginated:
        m = read_summary(target.parent)
        raw = None
    else:
        document, raw = read_document(target.parent)
        m = dict(document)
    # Evidence can arrive through another collection after this manifest
    # was exported. Serve the current catalog and invalidate its ETag.
    identity = env.identity_snapshot(tasks.base, handle)
    m['identity'] = identity
    running = tasks.collection_active(handle)
    outcome = tasks.reconcile_summary(collection_summary(m, tasks.job, running))
    interrupted = m.get("status") == "running" and not running
    ready = not running and not interrupted and zip_is_current(tasks.base / handle, raw, manifest_sha256=m.get('content_revision') if paginated else None)
    offline_ready = all((target.parent / name).is_file() for name in ('index.html', 'data.js'))
    zip_on_demand = storage_settings(tasks.base)['zip_on_demand']
    etag = collection_etag(m, raw, paginated, (ready, offline_ready, running, interrupted, zip_on_demand), identity, outcome)
    if h.headers.get("If-None-Match") == etag:
        return h.not_modified(etag)
    if interrupted:
        m["status"] = "paused"
    m.update(zip_ready=ready, zip_on_demand=storage_settings(tasks.base)['zip_on_demand'], offline_ready=offline_ready, collecting=running, interrupted=interrupted, identity=identity, task_summary=outcome)
    return h.json(m, etag=etag)


def download(h, env, path):
    tasks = env.TASKS
    handle = username(path.rsplit("/", 1)[1])
    if tasks.collection_active(handle):
        return h.json({"error": "合集正在更新，请稍后下载。"}, 409)
    manifest_path = tasks.base / handle / "manifest.json"
    manifest_running = manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")).get("status") == "running"
    if manifest_running or not zip_is_current(tasks.base / handle):
        return h.json({"error": "ZIP 尚未生成或未包含最新结果，请点击“重新生成 ZIP”。"}, 409)
    return h.file(tasks.base / (handle + "-合集.zip"), attachment=f"{handle}-wayback.zip")


def collection_files(h, env, path):
    target = collection_file(env.TASKS.base, path[len("/collections/"):])
    if target is None:
        return h.json({"error": "文件不可访问。"}, 403)
    return h.file(target)


def static(h, env, path):
    filename = "index.html" if path == "/" else path.removeprefix("/static/")
    if filename not in STATIC_FILES:
        return h.json({"error": "未找到页面。"}, 404)
    return h.file(env.ROOT / "static" / filename)


EXACT = {
    "/api/state": state,
    "/api/history": history,
    "/api/collections": collections,
}
# Checked in order after the exact routes; the first matching prefix wins.
PREFIXES = (
    ("/api/cloud-media/", cloud_media),
    ("/api/identity/", identity),
    ("/api/posts/", posts),
    ("/api/collection/", collection),
    ("/download/", download),
    ("/collections/", collection_files),
)


def route(path):
    if path in EXACT:
        return EXACT[path]
    for prefix, handler in PREFIXES:
        if path.startswith(prefix):
            return handler
    return static
