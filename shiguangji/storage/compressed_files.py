"""Lossless local text storage with stable logical names, hashes and timestamps.

Plain files remain readable. A .gz sibling is a storage detail; backup indexes
and cloud bundles always describe the original uncompressed bytes.
"""
import gzip
import hashlib
import os
from pathlib import Path
import tempfile

from shiguangji.core.atomic_io import replace_file
# Read helpers live in core so lower tiers can use them; re-exported for existing callers.
from shiguangji.core.stored_text import (  # noqa: F401
    CHUNK, MAX_TEXT, copy_stored, digest_stored, exists, logical_stat, matches, open_stored,
    packed_path, read_bytes, read_text, safe)
from shiguangji.storage.storage_guard import require_space


def _check_gzip(path, expected, size):
    value, total = hashlib.sha256(), 0
    with gzip.open(path, 'rb') as reader:
        for block in iter(lambda: reader.read(CHUNK), b''):
            total += len(block)
            if total > size:
                raise ValueError('压缩副本大小校验失败，保留原件')
            value.update(block)
    if total != size or value.hexdigest() != expected:
        raise ValueError('压缩副本内容校验失败，保留原件')


def _stamp(path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def compress_file(path, root, expected=None):
    """Verify a durable gzip before removing its unchanged plain original.

    Callers hold the collection lock for mutable pages. Backup objects are
    immutable and named by their uncompressed hash. Interrupted runs are safe
    to repeat. Files with no useful compression stay plain.
    """
    path = safe(Path(path), root)
    compressed = safe(packed_path(path), root)
    if not path.exists():
        if compressed.exists():
            # Validate existing compressed files on resume as well.
            sha = digest_stored(path)
            if expected is not None and sha != expected:
                raise ValueError('已压缩文件摘要不匹配')
            return {'status': 'already', 'saved': 0}
        raise FileNotFoundError(path)
    before = _stamp(path)
    if before[0] > MAX_TEXT:
        return {'status': 'too_large', 'saved': 0}
    require_space(path, before[0] + CHUNK)
    descriptor, name = tempfile.mkstemp(prefix='.compress-', suffix='.tmp', dir=path.parent)
    temporary = safe(Path(name), root)
    try:
        value = hashlib.sha256()
        with os.fdopen(descriptor, 'wb') as output:
            with gzip.GzipFile(filename='', mode='wb', fileobj=output, compresslevel=6, mtime=0) as zipped:
                with path.open('rb') as reader:
                    for block in iter(lambda: reader.read(CHUNK), b''):
                        value.update(block)
                        zipped.write(block)
            output.flush()
            os.fsync(output.fileno())
        sha = value.hexdigest()
        if expected is not None and sha != expected:
            raise ValueError('原文件摘要不匹配，未替换')
        if before != _stamp(path):
            raise ValueError('原文件正在变化，未替换')
        if temporary.stat().st_size + 64 >= before[0]:
            return {'status': 'unchanged', 'saved': 0}
        _check_gzip(temporary, sha, before[0])
        os.utime(temporary, ns=(path.stat().st_atime_ns, before[1]))
        replace_file(temporary, compressed)
        # The final location is also read back, before releasing the original.
        _check_gzip(compressed, sha, before[0])
        if before != _stamp(path):
            raise ValueError('原文件正在变化，保留两份副本')
        size = compressed.stat().st_size
        safe(path, root).unlink()
        return {'status': 'compressed', 'saved': before[0] - size}
    finally:
        temporary.unlink(missing_ok=True)


def write_bytes(path, data):
    """Publish new raw text, then compress if it saves space, under caller lock."""
    from shiguangji.core.atomic_io import atomic_write_bytes
    path = Path(path)
    safe(path, path.parent)
    safe(packed_path(path), path.parent)
    atomic_write_bytes(path, data)
    result = compress_file(path, path.parent)
    if result['status'] in {'unchanged', 'too_large'}:
        packed_path(path).unlink(missing_ok=True)


def write_text(path, text):
    write_bytes(path, text.encode('utf-8'))


def logical_files(folder):
    """Enumerate each raw page once, including mixed old/new storage."""
    folder = Path(folder)
    seen = set()
    for physical in folder.rglob('*'):
        if physical.is_file() and physical.suffix not in {'.part', '.tmp'}:
            safe(physical, folder)
            logical = physical.with_suffix('') if physical.name.endswith('.txt.gz') else physical
            if logical not in seen:
                seen.add(logical)
                yield logical


def expand_file(path, root, expected=None):
    """Verified reverse migration, for rollback to pre-compression versions."""
    path = safe(Path(path), root)
    compressed = safe(packed_path(path), root)
    if not compressed.exists():return {'status':'unchanged','saved':0}
    before = _stamp(compressed)
    size = logical_stat(path).st_size
    require_space(path, size + CHUNK)
    fd, name = tempfile.mkstemp(prefix='.expand-',suffix='.tmp',dir=path.parent)
    temporary = safe(Path(name), root)
    try:
        value, total = hashlib.sha256(), 0
        with os.fdopen(fd,'wb') as writer, gzip.open(compressed,'rb') as reader:
            for block in iter(lambda:reader.read(CHUNK),b''):
                total += len(block)
                if total > MAX_TEXT:raise ValueError('压缩网页超过安全大小')
                value.update(block);writer.write(block)
            writer.flush();os.fsync(writer.fileno())
        sha = value.hexdigest()
        if expected is not None and sha != expected:raise ValueError('解压内容摘要不匹配')
        if path.exists():
            if not matches(path,sha):raise ValueError('原文件与压缩副本不一致，保留两份')
        else:
            os.utime(temporary,ns=(compressed.stat().st_atime_ns,before[1]))
            if before != _stamp(compressed):raise ValueError('压缩副本已变化')
            replace_file(temporary,path)
        if not matches(path,sha) or before != _stamp(compressed):raise ValueError('解压读回校验失败')
        compressed.unlink()
        return {'status':'expanded','saved':before[0]-total}
    finally:
        temporary.unlink(missing_ok=True)
