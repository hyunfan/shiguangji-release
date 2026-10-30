"""Resumable, verified lossless compression of raw pages and backup text objects."""
import argparse
import json
from pathlib import Path
import time
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import sys
if __package__ in (None, ''):
    # 以文件路径运行（自身子进程或命令行）时，从应用根目录导入 shiguangji 包。
    _here, _root = Path(__file__).resolve().parent, str(Path(__file__).resolve().parents[2])
    if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == _here:del sys.path[0]
    if _root not in sys.path:sys.path.insert(0, _root)

from shiguangji.core.atomic_io import atomic_write_text
from shiguangji.storage.cloud_backup import checked_snapshot
from shiguangji.storage.collection_lock import collection_lock
from shiguangji.storage.compressed_files import compress_file, expand_file, logical_files, safe
from shiguangji.system.power_guard import prevent_idle_sleep
from shiguangji.storage.storage_guard import StorageLow

WORKERS = 8


def compact_library(base, progress=lambda message: None, *, expand=False, stop=None):
    base = Path(base).resolve()
    backups = safe(base.parent / (base.name + '-backups'), base.parent)
    state_path = safe(base / ('.expansion-state.json' if expand else '.compression-state.json'), base)
    label = '还原未压缩网页' if expand else '无损压缩'
    transform = expand_file if expand else compress_file
    stop = stop if stop is not None else threading.Event()
    result = {'operation': 'expand' if expand else 'compress', 'running': True, 'started_at': time.time(),
              'checked': 0, 'compressed': 0, 'already': 0, 'unchanged': 0,
              'expanded': 0, 'saved_bytes': 0, 'errors': [], 'phase': 'raw'}

    def report(force=False):
        if force or result['checked'] % 100 == 0:
            message = f"正在{label}（{'内容库' if result['phase']=='raw' else '备份'}）：已检查 {result['checked']} 个文件，空间减少 {result['saved_bytes']/1024**3:.2f} GiB。"
            result['message'] = message
            atomic_write_text(state_path, json.dumps(result, ensure_ascii=False))
            progress(message)

    def batch(items, root):
        # Only eight independent files are in flight. The owning thread keeps
        # the account lock until every submitted file finishes, including pause.
        iterator=iter(items);pending={};exhausted=False
        with ThreadPoolExecutor(max_workers=WORKERS,thread_name_prefix='compress-file') as pool:
            while pending or (not exhausted and not stop.is_set()):
                while not exhausted and not stop.is_set() and len(pending)<WORKERS:
                    try:path,expected=next(iterator)
                    except StopIteration:exhausted=True;break
                    pending[pool.submit(transform,path,root,expected)]=path
                if not pending:break
                done,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in done:
                    path=pending.pop(future)
                    try:
                        item=future.result();status=item['status']
                        key=status if status in {'compressed','already','expanded'} else 'unchanged'
                        result[key]+=1;result['saved_bytes']+=item['saved']
                    except (OSError,ValueError,EOFError,StorageLow) as error:
                        result['errors'].append({'path':str(path),'error':str(error)[:250]})
                        if isinstance(error,StorageLow):stop.set()
                    result['checked']+=1
                    report()

    with prevent_idle_sleep():
        report(True)
        for manifest in sorted(base.glob('*/manifest.json')):
            if stop.is_set():break
            folder = safe(manifest.parent, base)
            safe(folder/'raw', folder)
            try:
                with collection_lock(folder):
                    batch(((page,None) for page in logical_files(folder/'raw') if page.suffix=='.txt'),folder/'raw')
            except (OSError, ValueError) as error:
                result['errors'].append({'path': str(folder), 'error': str(error)[:250]})
        result['phase'] = 'backups'
        report(True)
        # Hashes remain unchanged, including hashes referenced by older snapshots.
        # Only raw objects explicitly referenced by a valid index are eligible.
        raw_hashes, media_hashes = set(), set()
        for index in sorted(backups.glob('*.json')):
            if stop.is_set():break
            try:
                safe(index, backups)
                snapshot, _, _ = checked_snapshot(index)
                for relative, record in snapshot['files'].items():
                    (raw_hashes if '/raw/' in relative else media_hashes).add(record['sha256'])
            except (OSError, ValueError, KeyError, TypeError) as error:
                result['errors'].append({'path': str(index), 'error': str(error)[:250]})
        batch(((backups/'objects'/sha[:2]/sha,sha) for sha in sorted(raw_hashes-media_hashes)),backups)
        result.update(running=False, paused=stop.is_set(), finished_at=time.time(), phase='paused' if stop.is_set() else 'finished')
        outcome='已暂停' if stop.is_set() else '完成' if not result['errors'] else '部分完成'
        result['message'] = (f"{label}{outcome}：空间减少 {result['saved_bytes']/1024**3:.2f} GiB，"
                             f"{len(result['errors'])} 项未完成。图片、视频和恢复索引保持不变。")
        atomic_write_text(state_path, json.dumps(result, ensure_ascii=False))
        progress(result['message'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--expand', action='store_true', help='Reverse compression before rolling back to an older program')
    args = parser.parse_args()
    summary = compact_library(args.base, lambda message: print(message, flush=True), expand=args.expand)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    raise SystemExit(bool(summary['errors']))
