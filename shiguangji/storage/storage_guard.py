"""Check headroom before large writes; never delete user data to make space."""
from pathlib import Path
import shutil

RESERVE_BYTES = 256*1024*1024


class StorageLow(Exception):
    """Stop the whole task, rather than being treated as one bad media URL."""
    reason = 'storage_low'


def require_space(path, needed=0, reserve=RESERVE_BYTES):
    path = Path(path).resolve()
    while not path.exists():path=path.parent
    free = shutil.disk_usage(path).free
    if free < max(0,needed)+reserve:
        raise StorageLow(f'磁盘剩余 {free//(1024*1024)} MB，需至少 {(max(0,needed)+reserve)//(1024*1024)} MB；已停止新增写入，请释放空间后继续。')
    return free


def rotate_log(path, limit=5*1024*1024, copies=3):
    """Bound application-generated logs; only rotate closed handles."""
    path = Path(path)
    try:
        if path.stat().st_size < limit:return
        for number in range(copies,0,-1):
            source = path if number==1 else path.with_name(path.name+f'.{number-1}')
            if source.exists():source.replace(path.with_name(path.name+f'.{number}'))
    except OSError:
        pass  # A log lock must not stop collection or hide the existing log.


class RotatingTextLog:
    """HTTP diagnostics use short-lived file handles so Windows can rotate them."""
    encoding = 'utf-8'
    def __init__(self, path):
        import threading
        self.path, self.lock = Path(path), threading.Lock()

    def write(self, text):
        with self.lock:
            rotate_log(self.path)
            with self.path.open('a', encoding='utf-8') as stream:return stream.write(text)

    def flush(self):pass

    def isatty(self):return False
