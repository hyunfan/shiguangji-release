"""Offline generation and ZIP publication; independent of collectors."""
import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path
from shiguangji.core.atomic_io import atomic_write_bytes, atomic_write_text, publish_if_changed, replace_file
from shiguangji.storage.collection_lock import collection_lock
from shiguangji.core.archive_support import ROOT, now, write_json
from shiguangji.platforms.account_identity import snapshot as identity_snapshot
from shiguangji.media.media_inventory import inventory
from shiguangji.storage.storage_guard import require_space

def publish_manifest(folder, manifest, offline=True):
    """Publish metadata/assets without replacing content from a missing database."""
    manifest['media_inventory'] = inventory(manifest.get('posts', []))
    raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    atomic_write_bytes(folder / 'manifest.json', raw)
    from shiguangji.storage.collection_reader import publish_summary
    publish_summary(folder, manifest, raw)
    if not offline:
        return
    require_space(folder, len(raw)*3)
    atomic_write_text(folder / 'data.js', 'window.ARCHIVE_DATA=' + json.dumps(manifest, ensure_ascii=False).replace('<', '\\u003c').replace('\u2028', '\\u2028').replace('\u2029', '\\u2029') + ';')
    for name in ('gallery.js', 'style.css'):
        publish_if_changed(folder / name, (ROOT / 'static' / name).read_bytes())
    publish_if_changed(folder / 'index.html', (ROOT / 'static/offline.html').read_bytes())


def export_collection(store, status, errors, full=True, run_info=None):
    posts = store.posts()
    media = [m for p in posts for m in p["media"]]
    recoverable = [m for m in media if m['status'] != 'saved' and m['kind'] in {'image', 'video','hls'}]
    counts = {"posts": len(posts), "text": sum(bool(p["text"]) for p in posts),
              "media": len(media), "saved": sum(m["status"] == "saved" for m in media),
              "missing": sum(m["status"] != "saved" for m in media),
              "captures": store.db.execute("SELECT COUNT(*) FROM captures").fetchone()[0],
              "parsed": store.db.execute("SELECT COUNT(*) FROM captures WHERE status='done'").fetchone()[0],
              "text_missing": sum(not p["text"] for p in posts),
              "restricted_posts": sum(p.get("content_reason") == "restricted" for p in posts),
              "not_indexed": sum(m.get("error_kind") == "not_indexed" for m in media if m["status"] != "saved"),
              "retryable": sum(m.get("error_kind") in {"network", "not_found"} for m in media if m["status"] != "saved"),
              "live_recovered": sum(m.get('source')=='live_cdn' and m['status']=='saved' for m in media),
              "cc_recovered": sum(m.get('source')=='common_crawl' and m['status']=='saved' for m in media),
              "cc_pending": sum(bool(m.get('cc_pending_indexes') or (m.get('cc_more') and m.get('cc_remaining_indexes'))) for m in recoverable if m['kind'] != 'hls'),
              "media_retry_pending": sum(m.get('error_kind') == 'network' for m in recoverable),
              "source_retry_pending": sum(m.get('recovery_error_kind') == 'network' for m in recoverable)}
    from shiguangji.platforms.platform_accounts import account_info, is_instagram, empty_identity
    manifest = {"version": 2, **account_info(store.handle), "username": store.handle, "updated_at": now(), "status": status, "counts": counts,
                "errors": errors, "posts": posts, "last_run": dict(run_info or {}),
                "identity": empty_identity(store.handle) if is_instagram(store.handle) else identity_snapshot(store.folder.parent, store.handle),
                "scope_note": "历史页面与公开来源补全的媒体会分别标注来源；当前原始链接不等于历史快照。恢复范围取决于可访问资料，时间按发帖时间排列。"}
    if is_instagram(store.handle):
        manifest["scope_note"] = "Instagram 仅恢复公开历史快照中可核实归属的帖子和媒体。没有留下可访问存档的已删除内容无法恢复；不代表全部发帖，也不判断帖子当前是否已删除。未知发帖日期单独列出。"
    from shiguangji.tasks.task_status import summary
    last = manifest['last_run']
    counts['text_recovery_pending'] = int(last.get('post_recovery',{}).get('pending',0))
    from shiguangji.collect.recovery_report import report
    manifest['recovery_report'] = report(posts,store.db.execute('SELECT original,status FROM captures'))
    counts.update({k: last['scope'][k] for k in ('identity_pending', 'alias_search_pending', 'local_alias_pending')
                   if k in last.get('scope', {})})
    manifest['run_id'] = last.get('run_id', '')
    manifest['task_summary'] = summary(manifest, running=status == 'running')
    publish_manifest(store.folder, manifest, offline=full)
    if not full:
        return manifest
    captures = [dict(r) for r in store.db.execute("SELECT * FROM captures ORDER BY timestamp,original")]
    export_offline(store.folder, manifest, captures)
    return manifest


def export_offline(folder, manifest, captures=None):
    posts, errors = manifest['posts'], manifest.get('errors', [])
    post_files = []
    atomic_write_text(folder / "posts.jsonl", ''.join(json.dumps(p, ensure_ascii=False) + '\n' for p in posts))
    for p in posts:
        month = p["created_at"][:7].replace("-", "/") if p["created_at"] else "unknown-date"
        dest = folder / "posts" / month / (p["id"] + ".txt")
        content = f"@{p['username']}\n发帖时间（UTC）：{p['created_at'] or '未知'}\n时间来源：{p['date_source']}\n{p['url']}\n\n{p['text'] or '[未恢复正文]'}\n\n" + "\n".join(m.get("path") or m["urls"][0] for m in p["media"])
        publish_if_changed(dest, content.encode('utf-8'))
        post_files.append(dest.relative_to(folder).as_posix())
    csv_data = io.StringIO(newline='')
    writer = csv.writer(csv_data)
    writer.writerow(["id", "username", "created_at_utc", "date_source", "text", "url", "media_saved", "media_missing"])
    for p in posts:
        cells = [p["id"], p["username"], p["created_at"], p["date_source"], p["text"], p["url"], sum(m["status"] == "saved" for m in p["media"]), sum(m["status"] != "saved" for m in p["media"])]
        writer.writerow(["'" + c if isinstance(c, str) and c.startswith(("=", "+", "-", "@", "\t", "\r")) else c for c in cells])
    atomic_write_bytes(folder / "posts.csv", csv_data.getvalue().encode('utf-8-sig'))
    missing = [{"post_id": p["id"], **m} for p in posts for m in p["media"] if m["status"] != "saved"]
    write_json(folder / "missing-media.json", missing)
    write_json(folder / "errors.json", errors)
    if captures is not None:
        write_json(folder / "captures.json", captures)
    atomic_write_text(folder / "说明.txt", "解压全部文件后，双击 index.html 浏览。媒体按发帖年月存放，所有时间使用 UTC。\nmanifest.json / posts.jsonl 保留完整数据及正文版本；posts.csv 可导入表格。\nmissing-media.json 为未恢复媒体；errors.json 为本轮错误；captures.json 为快照读取状态。\nraw 中是原始页面文本，不执行其中脚本。数据库保留在本地用于继续任务。\n仅整理已发现的帖子与可访问媒体，不代表该用户发帖全量；视频分片（HLS）需完整合并并通过解码校验才计为已保存；无法恢复时保留入口。\n媒体卡片及清单保留实际来源、存档时间与逐来源检查结果；从原始CDN补回的当前文件不等同于历史快照。\n")


    # Commit marker written last; readers can identify the exact exported set.
    files = ['manifest.json','data.js','posts.jsonl','posts.csv','missing-media.json','errors.json','captures.json','index.html','gallery.js','style.css','说明.txt', *post_files]
    generation = {'schema':2,'files':{
        name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in files if (folder/name).is_file()}}
    write_json(folder / 'generation.json', generation)
    return generation


def make_zip(folder):
    with collection_lock(folder):
        return _make_zip_locked(folder)

def _make_zip_locked(folder):
    from shiguangji.storage.compressed_files import open_stored
    import shutil
    folder = Path(folder)
    require_space(folder, sum(p.stat().st_size for p in folder.rglob('*') if p.is_file()))
    manifest = json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
    publish_manifest(folder,manifest)
    generation = export_offline(folder,manifest)
    target = folder.parent / (folder.name + "-合集.zip")
    temp = target.with_suffix(".zip.part")
    with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=4, allowZip64=True) as z:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder).as_posix()
            if relative.startswith('.hls-cache/'):
                continue
            # posts/ is generated output. A corrected date can leave an old
            # path on disk; only this generation belongs in the new ZIP.
            # Preserve old files in place rather than guessing what to delete.
            if relative.startswith('posts/') and relative not in generation['files']:
                continue
            if path.name not in {'summary.json', '.network-retry.json'} and path.is_file() and path.suffix not in {".part", ".tmp", ".sqlite", ".bak", ".lock"}:
                if relative.startswith('raw/') and path.name.endswith('.txt.gz'):
                    logical = path.with_suffix('')
                    if logical.exists():
                        continue
                    # Export normal text for users and older versions of the tool.
                    info = zipfile.ZipInfo(folder.name + '/' + logical.relative_to(folder).as_posix())
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with open_stored(logical) as reader, z.open(info, 'w', force_zip64=True) as writer:
                        shutil.copyfileobj(reader, writer, 1024*1024)
                    continue
                compression = zipfile.ZIP_STORED if path.suffix in {".jpg", ".png", ".gif", ".mp4", ".webm", ".webp", ".avif"} else zipfile.ZIP_DEFLATED
                z.write(path, arcname=folder.name + "/" + path.relative_to(folder).as_posix(), compress_type=compression)
    replace_file(temp, target)
    write_json(target.with_suffix(".zip.json"), {
        "manifest_sha256": hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest(),
        "zip_bytes": target.stat().st_size, "zip_mtime_ns": target.stat().st_mtime_ns,
    })
    return target

def zip_is_current(folder, manifest_bytes=None, manifest_sha256=None):
    """A surviving older ZIP must never be offered as the latest export."""
    folder = Path(folder)
    target = folder.parent / (folder.name + "-合集.zip")
    try:
        state = json.loads(target.with_suffix(".zip.json").read_text(encoding="utf-8"))
        stat = target.stat()
        return (state["zip_bytes"] == stat.st_size and state["zip_mtime_ns"] == stat.st_mtime_ns
                and state["manifest_sha256"] == (manifest_sha256 or hashlib.sha256(manifest_bytes if manifest_bytes is not None else (folder / "manifest.json").read_bytes()).hexdigest()))
    except (OSError, ValueError, KeyError, TypeError):
        return False
