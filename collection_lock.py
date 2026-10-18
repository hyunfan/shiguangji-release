"""Cooperative account lock for CLI, worker and export; released by the OS on exit."""
from contextlib import contextmanager
import os
from pathlib import Path
import threading

if os.name == 'nt':
    import msvcrt
else:
    import fcntl

_guard = threading.RLock()
_held = {}


class CollectionBusy(ValueError):
    pass


@contextmanager
def collection_lock(folder):
    folder = Path(folder).resolve()
    key = os.path.normcase(str(folder))
    owner = (os.getpid(), threading.get_ident())
    with _guard:
        existing = _held.get(key)
        if existing:
            if existing['owner'] != owner:
                raise CollectionBusy('该合集正在由另一个任务处理，请等待或暂停原任务。')
            existing['depth'] += 1
        else:
            folder.mkdir(parents=True, exist_ok=True)
            stream = (folder / '.collection.lock').open('a+b')
            try:
                if stream.seek(0, 2) == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                stream.close()
                raise CollectionBusy('该合集正在由另一个进程处理，请先结束原任务。') from error
            _held[key] = {'owner': owner, 'depth': 1, 'stream': stream}
    try:
        yield
    finally:
        with _guard:
            existing = _held[key]
            existing['depth'] -= 1
            if not existing['depth']:
                stream = existing['stream']
                try:
                    stream.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
                    del _held[key]


def collection_is_busy(folder):
    if not Path(folder).is_dir():
        return False
    try:
        with collection_lock(folder):
            return False
    except CollectionBusy:
        return True
