"""Read side of lossless text storage: a plain file or its verified .gz sibling.

Kept in core so parsing tiers can read stored pages without depending on the
storage tier. Writing, compression and space checks stay in
shiguangji.storage.compressed_files, which re-exports these helpers.
"""
from contextlib import contextmanager
import gzip
import hashlib
import os
from pathlib import Path
import shutil
import struct
from types import SimpleNamespace

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


def copy_stored(source, target):
    with open_stored(source) as reader, Path(target).open('wb') as writer:
        shutil.copyfileobj(reader, writer, CHUNK)
