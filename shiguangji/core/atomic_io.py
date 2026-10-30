"""Atomic publication with bounded retries for Windows sharing/scan locks."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time


def temporary_lock(error):
    # Windows also uses access denied for a briefly held reader/sync handle.
    return isinstance(error, OSError) and getattr(error, 'winerror', None) in {5, 32, 33}


def replace_file(source, destination):
    for attempt in range(11):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if not temporary_lock(error) or attempt == 10:
                raise
            time.sleep(min(0.05 * 2 ** attempt, 0.5))


def atomic_write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # A scanner may still hold this uniquely named unpublished file.
            pass


def atomic_write_text(path, text):
    atomic_write_bytes(path, text.encode('utf-8'))


def read_json(path, default=None):
    """A JSON object from ``path``; ``default`` (or ``{}``) when missing, unreadable or not an object."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def write_json(path, value, *, indent=None):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent))


def publish_if_changed(path, data):
    path = Path(path)
    try:
        if path.read_bytes() == data:
            return
    except OSError as error:
        if not isinstance(error, FileNotFoundError) and not temporary_lock(error):
            raise
    atomic_write_bytes(path, data)
