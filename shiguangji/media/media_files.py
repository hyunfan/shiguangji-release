"""Shared structural validation. This detects truncation, not codec decodability.

File validation maps bytes read-only; large videos are not copied into RAM.
Reuse decisions read the content again. File timestamps are not integrity proof,
especially on Windows where ctime may describe creation rather than modification.
"""
import hashlib
import mmap
import os
from pathlib import Path
import zlib

VALIDATION_VERSION = 1


def media_extension(data, mime=''):
    if data[:3] == b'\xff\xd8\xff': return 'jpg'
    if data[:8] == b'\x89PNG\r\n\x1a\n': return 'png'
    if data[:6] in {b'GIF87a', b'GIF89a'}: return 'gif'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP': return 'webp'
    if data[4:8] == b'ftyp':
        return 'avif' if data[8:12] in {b'avif', b'avis'} else 'mp4'
    if data[:4] == b'\x1aE\xdf\xa3': return 'webm'
    return ''


def boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    result = []
    while start < end:
        if end - start < 8: return []
        size = int.from_bytes(data[start:start+4], 'big')
        kind, header = data[start+4:start+8], 8
        if size == 1:
            if end - start < 16: return []
            size, header = int.from_bytes(data[start+8:start+16], 'big'), 16
        elif size == 0: size = end - start
        if size < header or start + size > end: return []
        result.append((kind, start+header, start+size))
        start += size
    return result


def _ebml(data, start, end):
    result = []
    while start < end:
        def number(position, identifier=False):
            if position >= end or not data[position]: raise ValueError()
            width = 9 - data[position].bit_length()
            if width > (4 if identifier else 8) or position + width > end: raise ValueError()
            value = int.from_bytes(data[position:position+width], 'big')
            if not identifier: value &= (1 << (7*width)) - 1
            return value, width
        kind, n = number(start, True)
        size, width = number(start+n)
        begin = start+n+width
        # Unknown sizes are allowed by EBML for Segment/Cluster streaming.
        unknown = size == (1 << (7*width)) - 1
        if unknown and kind not in {0x18538067, 0x1F43B675}: raise ValueError()
        stop = end if unknown else begin+size
        if stop > end: raise ValueError()
        result.append((kind, begin, stop))
        start = stop
    return result


def media_valid(data, kind=None):
    """Check complete container/termination markers before publishing a file."""
    try:
        ext = media_extension(data)
        if kind not in {None, 'image', 'video'}: return False
        if not ext or (kind and (ext in {'mp4','webm'}) != (kind == 'video')): return False
        if ext in {'mp4','avif'}:
            parts = boxes(data)
            names = {name for name, _, _ in parts}
            if not {b'ftyp', b'mdat', b'moov' if ext == 'mp4' else b'meta'} <= names: return False
            if not any(name == b'mdat' and end > start for name, start, end in parts): return False
            if ext == 'mp4':
                return any(name == b'moov' and any(k == b'trak' and b > a for k,a,b in boxes(data,start,end)) for name,start,end in parts)
            return True
        if ext == 'jpg':
            # End marker alone cannot distinguish a JPEG from an HTML/error tail.
            end = len(data)
            while end and data[end-1] in b'\0\r\n ': end -= 1
            return (data[end-2:end] == b'\xff\xd9' and data.find(b'\xff\xda') > 2
                    and any(data.find(bytes([255, marker])) > 2 for marker in (192,193,194,195,197,198,199,201,202,203,205,206,207)))
        if ext == 'png':
            pos, seen = 8, set()
            while pos < len(data):
                if pos+12 > len(data): return False
                size = int.from_bytes(data[pos:pos+4], 'big')
                name = data[pos+4:pos+8]
                end = pos+12+size
                if end > len(data): return False
                crc, offset = 0, pos+4
                while offset < end-4:
                    stop = min(end-4, offset+1024*1024)
                    crc = zlib.crc32(data[offset:stop], crc); offset = stop
                if crc != int.from_bytes(data[end-4:end], 'big'): return False
                if pos == 8 and (name != b'IHDR' or size != 13): return False
                seen.add(name); pos = end
                if name == b'IEND': return size == 0 and end == len(data) and {b'IHDR',b'IDAT'} <= seen
            return False
        if ext == 'gif':
            return len(data) >= 14 and data[-1:] == b';' and data.find(b',', 13) >= 13
        if ext == 'webp':
            if int.from_bytes(data[4:8], 'little')+8 != len(data): return False
            pos, picture = 12, False
            while pos < len(data):
                if pos+8 > len(data): return False
                size = int.from_bytes(data[pos+4:pos+8], 'little')
                picture |= data[pos:pos+4] in {b'VP8 ', b'VP8L', b'ANMF'} and size > 0
                pos += 8+size+(size % 2)
            return picture and pos == len(data)
        if ext == 'webm':
            top = _ebml(data, 0, len(data))
            if not top or top[0][0] != 0x1A45DFA3: return False
            for tag, start, end in top:
                if tag != 0x18538067: continue
                children = _ebml(data,start,end)
                tracks = any(k == 0x1654AE6B and any(t == 0xAE for t,_,_ in _ebml(data,a,b)) for k,a,b in children)
                blocks = any(k == 0x1F43B675 and any(t in {0xA3,0xA0} and y>x for t,x,y in _ebml(data,a,b)) for k,a,b in children)
                return tracks and blocks
    except (ValueError, IndexError, OverflowError):
        return False
    return False


def file_valid(path, kind=None):
    try:
        with Path(path).open('rb') as stream:
            if not stream.seek(0, 2): return False
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                return media_valid(data, kind)
    except (OSError, ValueError):
        return False


def saved_file_valid(folder, item, check=lambda: None):
    """Strong validation for collection/reuse, never called by UI polling."""
    if item.get('status') != 'saved' or not item.get('path'): return False
    try:
        folder = Path(folder).resolve()
        path = (folder / item['path']).resolve()
        if not path.is_relative_to(folder) or not path.is_file(): return False
        def identity(s):
            return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns
        before = path.stat()
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) != (before.st_dev, before.st_ino): return False
            if not stat.st_size or (item.get('bytes') and item['bytes'] != stat.st_size): return False
            check()
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if not media_valid(data, item.get('kind')): return False
            if item.get('sha256'):
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024*1024), b''):
                    check(); digest.update(block)
                if digest.hexdigest() != item['sha256']: return False
            if identity(os.fstat(stream.fileno())) != identity(stat): return False
        after = path.stat()
        # Compare each API to itself: on Windows fstat and path.stat can expose
        # different ctime meanings after an atomic rename (change vs creation).
        if identity(after) != identity(before): return False
        return True
    except (OSError, ValueError, TypeError):
        return False
