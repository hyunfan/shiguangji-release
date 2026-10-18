"""Shared bounded stream download and atomic validated publication."""
import gzip
import hashlib
import zlib
from http.client import HTTPException
from atomic_io import replace_file
from core_errors import ArchiveError
from extractor import media_item, original_url
from media_files import file_valid, media_extension, VALIDATION_VERSION
from storage_guard import require_space

def download_media(client, url, destination, max_bytes):
    temp = destination.with_suffix(destination.suffix + ".part")
    require_space(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with client.open(url) as r:
            length = r.headers.get('Content-Length', '')
            require_space(destination, int(length) if str(length).isdigit() else 0)
            mime = r.headers.get_content_type()
            compressed = r.headers.get("Content-Encoding", "").lower() in {"gzip", "x-gzip"}
            stream = gzip.GzipFile(fileobj=r) if compressed else r
            first = b''
            # read(n) may wait indefinitely for n bytes on a trickling
            # connection. Use single-read chunks, checking between them.
            while len(first) < 32:
                client.check()
                chunk = getattr(stream, 'read1', stream.read)(8192 - len(first))
                client.check()
                if not chunk:
                    break
                first += chunk
            extension = media_extension(first, mime)
            if not extension:
                raise ArchiveError(f"返回的不是可识别图片或视频（{mime}），可能是存档错误页。", reason="invalid_media")
            from instagram_extract import media_item as ig_media
            expected_item = media_item(original_url(url)) or ig_media(original_url(url), "video" if ".mp4" in original_url(url).split("?",1)[0] else "image")
            if expected_item and ((expected_item['kind']=='image' and extension in {'mp4','webm'}) or (expected_item['kind']=='video' and extension not in {'mp4','webm'})):
                raise ArchiveError('返回文件的图片/视频类型与目标链接不一致。')
            destination = destination.with_suffix("." + extension)
            size, digest = 0, hashlib.sha256()
            with temp.open("wb") as f:
                chunk = first
                while chunk:
                    client.check()
                    if size // (8*1024*1024) != (size + len(chunk)) // (8*1024*1024):
                        require_space(destination, len(chunk))
                    size += len(chunk)
                    if max_bytes and size > max_bytes:
                        raise ArchiveError("超过单文件大小限制；调高限制后可重试。", reason="too_large")
                    digest.update(chunk)
                    f.write(chunk)
                    chunk = getattr(stream, 'read1', stream.read)(256 * 1024)
            expected = r.headers.get("Content-Length")
            if expected and not compressed and int(expected) != size:
                raise ArchiveError("媒体下载不完整，已丢弃临时文件。")
            destination = destination.with_name(destination.stem + "_" + digest.hexdigest()[:12] + destination.suffix)
            client.check()
            if not file_valid(temp, expected_item['kind'] if expected_item else None):
                raise ArchiveError('媒体结构不完整或损坏，已丢弃临时文件。', reason='invalid_media')
            client.check()
            replace_file(temp, destination)
            return {"file": destination, "bytes": size, "sha256": digest.hexdigest(), "archive_url": r.url, "validation_version": VALIDATION_VERSION}
    except (TimeoutError, OSError, ValueError, HTTPException, EOFError, zlib.error) as e:
        if getattr(client, 'retry_policy', None):client.retry_policy.failure(url)
        raise ArchiveError(f"下载媒体失败：{e}") from e
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
