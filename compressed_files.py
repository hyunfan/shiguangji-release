"""Lossless local text storage with stable logical names, hashes and timestamps.

Plain files remain readable. A .gz sibling is a storage detail; backup indexes
and cloud bundles always describe the original uncompressed bytes.
"""
from contextlib import contextmanager
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace

from atomic_io import replace_file
from storage_guard import require_space

CHUNK = 1024 * 1024
MAX_TEXT = 256 * CHUNK


def packed_path(path):
    return Path(str(path) + '.gz')


def safe(path, root):
    path, root = Path(path), Path(root).resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError('压缩文件路径超出资料目录')
    return path


@contextmanager
def open_stored(path):
    """Prefer a plain copy after interruption; tolerate atomic migration races."""
    path = Path(path)
    safe(path, path.parent)
    try:
        reader = path.open('rb')
    except FileNotFoundError:
        compressed = safe(packed_path(path), path.parent)
        reader = gzip.open(compressed, 'rb')
    with reader:
        yield reader


def exists(path):
    return Path(path).is_file() or packed_path(path).is_file()


def logical_stat(path):
    path = Path(path)
    safe(path, path.parent)
    try:
        return path.stat()
    except FileNotFoundError:
        compressed = safe(packed_path(path), path.parent)
        with compressed.open('rb') as reader:
            st = os.fstat(reader.fileno())
            if reader.read(2) != b'\x1f\x8b':
                raise ValueError('压缩网页格式无效')
            reader.seek(-4, 2)
            size = struct.unpack('<I', reader.read(4))[0]
        return SimpleNamespace(st_size=size, st_mtime_ns=st.st_mtime_ns)


def read_bytes(path):
    with open_stored(path) as reader:
        data = reader.read(MAX_TEXT + 1)
        if len(data) > MAX_TEXT:
            raise ValueError('历史网页超过安全读取大小')
        return data


def read_text(path):
    # Match Path.read_text's universal newline handling on existing Windows files.
    return read_bytes(path).decode('utf-8', errors='replace').replace('\r\n','\n').replace('\r','\n')


def digest_stored(path):
    value = hashlib.sha256()
    with open_stored(path) as reader:
        for block in iter(lambda: reader.read(CHUNK), b''):
            value.update(block)
    return value.hexdigest()


def matches(path, expected):
    try:
        return digest_stored(path) == expected
    except (OSError, ValueError, EOFError):
        return False


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
    from atomic_io import atomic_write_bytes
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


def copy_stored(source, target):
    with open_stored(source) as reader, Path(target).open('wb') as writer:
        shutil.copyfileobj(reader, writer, CHUNK)


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
