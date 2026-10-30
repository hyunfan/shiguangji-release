"""File responses with single byte-range support, and the static/collection whitelists."""
import mimetypes
import re

STATIC_FILES = {"index.html", "app-core.js", "app-job.js", "app-collection.js", "app.js", "gallery.js", "history.js", "storage.js", "style.css"}
COLLECTION_SUFFIXES = {".html", ".css", ".js", ".json", ".jsonl", ".csv", ".txt", ".jpg", ".png",
                       ".gif", ".webp", ".avif", ".mp4", ".webm"}
CHUNK = 256 * 1024


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header, size):
    """Return (begin, end, partial) for a ``bytes=`` header; ValueError if malformed."""
    begin, end = 0, size - 1
    if not header:
        return begin, end, False
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
    if not m or not any(m.groups()):
        raise ValueError("无效的文件范围。")
    if m[1]:
        begin, end = int(m[1]), min(int(m[2]), size - 1) if m[2] else size - 1
    else:
        begin = max(0, size - int(m[2]))
    if begin >= size or begin > end:
        raise RangeNotSatisfiable()
    return begin, end, True


def content_type(path, mime=None):
    mime = mime or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    return mime + ("; charset=utf-8" if mime.startswith("text/") or mime in {"application/json", "application/javascript"} else "")


def send_file(h, path, attachment="", mime=None):
    if not path.is_file():
        return h.json({"error": "文件尚未生成。"}, 404)
    size = path.stat().st_size
    try:
        begin, end, partial = parse_range(h.headers.get("Range", ""), size)
    except RangeNotSatisfiable:
        h.send_response(416)
        h.send_header("Content-Range", f"bytes */{size}")
        h.end_headers()
        return
    except ValueError as error:
        return h.json({"error": str(error)}, 416)
    h.send_response(206 if partial else 200)
    h.headers_common()
    h.send_header("Content-Type", content_type(path, mime))
    h.send_header("Content-Length", str(max(0, end - begin + 1)))
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Cache-Control", "no-cache")
    if partial:
        h.send_header("Content-Range", f"bytes {begin}-{end}/{size}")
    if attachment:
        h.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
    h.end_headers()
    with path.open("rb") as f:
        f.seek(begin)
        remaining = end - begin + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK, remaining))
            if not chunk:
                break
            h.wfile.write(chunk)
            remaining -= len(chunk)


def collection_file(base, relative):
    """Resolve a file below the library; None when it escapes or is not a served type."""
    target = (base / relative).resolve()
    if not target.is_relative_to(base.resolve()) or target.suffix.lower() not in COLLECTION_SUFFIXES:
        return None
    return target
