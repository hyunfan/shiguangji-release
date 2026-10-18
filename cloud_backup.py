"""Copy verified snapshots to a sync folder or mount in an isolated process.

The local backup indexes are the durable queue. A receipt means mount read-back
verified, NOT provider upload confirmed. No cloud files are pruned or mirrored.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from atomic_io import atomic_write_bytes, atomic_write_text
from collection_lock import collection_lock
from platform_accounts import collection_key
from power_guard import prevent_idle_sleep
from storage_guard import require_space

CHUNK = 1024 * 1024
STALL_SECONDS = 180
SCAN_SECONDS = 60
RETRY_SECONDS = 300
NOTICE = '已复制并校验表示目标目录读回校验通过；实际上传是否完成，请在所用网盘客户端或服务端确认。'
RESTORE_NOTE = '''时光集恢复备份

“恢复包”文件夹里的每个 ZIP 保存一个博主的完整恢复备份。
先下载并完整解压所需 ZIP，再使用时光集的 backup_library.py，
将 --restore 指向解压目录中的博主 JSON，--restore-to 指向新的空目录。
示例：python backup_library.py --restore "解压目录/alice.json" --restore-to "新的恢复目录"
请保留解压后的 objects 文件夹和数据库 ZIP；不要只取出博主 JSON。
便携版使用包内 python/python.exe，无需另行安装 Python。

早期版本直接保存的 objects、博主 JSON 和数据库 ZIP 仍保留，可按原方法恢复。
恢复包按快照保留，不联动删除。挂载盘读回通过不等于网盘已上传完毕，
请在所用网盘客户端或服务端核实，并保留本地原始资料。
'''


def read_json(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False))


def backup_root(base):
    base = Path(base)
    return base.parent / (base.name + '-backups')


def config(base):
    saved = read_json(Path(base) / '.cloud-backup.json')
    return {'enabled': saved.get('enabled') is True, 'destination': saved.get('destination', ''),
            'library_id': saved.get('library_id', '')}


def validate_destination(base, destination):
    # Lexical only: configuration/HTTP threads must never touch a network path.
    if not isinstance(destination, str) or not destination.strip():
        raise ValueError('请填写同步目录或挂载目录中的专用备份文件夹。')
    if not Path(destination.strip()).is_absolute():
        raise ValueError('请填写专用备份文件夹的绝对路径，可使用任意盘符或网络共享路径。')
    destination = Path(os.path.abspath(destination.strip()))
    if not Path(destination).is_absolute() or str(destination) == destination.anchor:
        raise ValueError('请选择网盘内的专用文件夹，不能直接使用整个盘符。')
    for local in (Path(base).absolute(), backup_root(base).absolute()):
        if destination == local or destination in local.parents or local in destination.parents:
            raise ValueError('网盘备份目录不能与本地资料或备份目录重叠。')
    return str(destination)


def save_config(base, values):
    if set(values) != {'enabled', 'destination'} or type(values['enabled']) is not bool:
        raise ValueError('网盘备份设置无效。')
    old = config(base)
    destination = validate_destination(base, values['destination']) if values['enabled'] or values['destination'] else ''
    result = {**values, 'destination': destination, 'library_id': old['library_id'] or uuid.uuid4().hex}
    write_json(Path(base) / '.cloud-backup.json', result)
    return result


def check_mount(destination):
    """Called only by the disposable copy process, never the HTTP service."""
    root = Path(destination.anchor)
    if not root.is_dir():
        raise OSError('目标盘或共享目录不可用；请连接对应客户端或设备，等待自动重试。')
    # Never create a missing mount/sync root as an ordinary local directory.
    # The user selects an existing parent; only its final backup folder is new.
    if not destination.parent.is_dir():
        raise OSError('目标的上级目录不可用，请先连接网盘并建立上级目录。')


def destination_binding(control, destination):
    """Local-only identity pins; malformed state must not reset protection."""
    path = Path(control) / 'destination-bindings.json'
    if not path.exists():
        return {}, path, hashlib.sha256(os.path.normcase(str(destination)).encode('utf-8')).hexdigest()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise ValueError()
    except (OSError, ValueError) as error:
        raise ValueError('目标目录核对记录无法读取；请保留记录并检查本地备份目录。') from error
    key = hashlib.sha256(os.path.normcase(str(destination)).encode('utf-8')).hexdigest()
    return data, path, key


def verify_destination(destination, library_id, destination_id=''):
    marker = read_json(Path(destination) / '.shiguangji-backup.json')
    if (marker.get('library_id') != library_id or marker.get('kind') != 'shiguangji-backup'
            or (destination_id and marker.get('destination_id') != destination_id)):
        raise ValueError('目标目录标识不匹配或已丢失；请检查挂载、同步状态和路径，未写入备份。')
    return marker


def prepare_destination(destination, library_id, control, *, require_existing=False):
    if not re.fullmatch('[a-f0-9]{32}', library_id):
        raise ValueError('资料库备份标识无效，请重新保存网盘备份设置。')
    check_mount(destination)
    bindings, binding_path, key = destination_binding(control, destination)
    pinned = bindings.get(key)
    if pinned is not None:
        token = pinned.get('destination_id', '')
        if (pinned.get('library_id') != library_id or not isinstance(token, str)
                or not re.fullmatch('[a-f0-9]{32}', token)):
            raise ValueError('目标目录核对记录无效，请检查本地备份目录。')
        verify_destination(destination, library_id, token)
        return token
    marker = destination / '.shiguangji-backup.json'
    saved = read_json(marker)
    if require_existing:
        verify_destination(destination, library_id)
    if destination.exists():
        if saved.get('library_id') != library_id:
            if any(destination.iterdir()):
                raise ValueError('目标文件夹已有其他资料或属于另一份资料库，请选择空的专用文件夹。')
        elif saved.get('kind') != 'shiguangji-backup':
            raise ValueError('目标备份标识无效，请检查目标文件夹。')
    token = saved.get('destination_id') or uuid.uuid4().hex
    if not isinstance(token, str) or not re.fullmatch('[a-f0-9]{32}', token):
        raise ValueError('目标备份标识无效，请检查目标文件夹。')
    destination.mkdir(exist_ok=True)
    if saved.get('destination_id') != token:
        write_json(marker, {'schema': 2, 'library_id': library_id, 'kind': 'shiguangji-backup',
                            'destination_id': token})
    verify_destination(destination, library_id, token)
    bindings[key] = {'library_id': library_id, 'destination_id': token}
    write_json(binding_path, bindings)
    note = destination / '时光集恢复说明.txt'
    if not note.exists():
        atomic_write_text(note, RESTORE_NOTE)
    return token


def checked_snapshot(path):
    raw = path.read_bytes()
    snapshot = json.loads(raw)
    handle = collection_key(snapshot['handle'])
    if handle != path.stem or snapshot.get('schema') != 1:
        raise ValueError('本地备份索引无效。')
    name = snapshot['metadata']
    if not re.fullmatch(re.escape(handle) + r'-[0-9]+\.zip', name):
        raise ValueError('数据库备份名称无效。')
    if not re.fullmatch('[a-f0-9]{64}', snapshot['metadata_sha256']):
        raise ValueError('数据库备份摘要无效。')
    for relative, record in snapshot['files'].items():
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != handle or '..' in parts or '\\' in relative or ':' in relative:
            raise ValueError('媒体备份路径无效。')
        if not re.fullmatch('[a-f0-9]{64}', record['sha256']):
            raise ValueError('媒体备份摘要无效。')
    return snapshot, raw, hashlib.sha256(raw).hexdigest()


def file_digest(path, pulse):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(CHUNK), b''):
            value.update(block)
            pulse()
    return value.hexdigest()


def copy_verified(source, target, sha, pulse):
    # Incomplete immutable objects can be retried; an index is published LAST.
    # Avoid renaming each media file: a sync client may still be uploading it.
    if target.is_file() and file_digest(target, pulse) == sha:
        return False
    if file_digest(source, pulse) != sha:
        raise ValueError('本地备份文件校验失败，未发布网盘索引：' + source.name)
    # Sync/mount clients may stage writes locally. Conservatively retain enough
    # space on the source disk for another file; never fill it just to enqueue.
    require_space(source, source.stat().st_size)
    target.parent.mkdir(exist_ok=True)
    with source.open('rb') as reader, target.open('wb') as writer:
        for block in iter(lambda: reader.read(CHUNK), b''):
            writer.write(block)
            pulse()
        writer.flush()
    if file_digest(target, pulse) != sha:
        raise ValueError('网盘挂载读回校验失败，将保留任务重试：' + source.name)
    return True


def bundle_name(snapshot, revision):
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime(snapshot['created_at']))
    return snapshot['handle'] + '-' + stamp + 'UTC-' + revision[:16] + '.zip'


def make_bundle(index, pulse):
    """One self-contained ZIP avoids tens of thousands of cloud file requests."""
    from compressed_files import open_stored
    snapshot, raw, revision = checked_snapshot(index)
    root = index.parent
    unique = {record['sha256']: record for record in snapshot['files'].values()}
    media = {record['sha256'] for path, record in snapshot['files'].items() if '/media/' in path}
    name = bundle_name(snapshot, revision)
    cache = root / '.cloud' / 'packages'
    cache.mkdir(parents=True, exist_ok=True)
    bundle = cache / name
    receipt_path = bundle.with_suffix('.sha256')
    if bundle.exists() and receipt_path.exists():
        sha = receipt_path.read_text(encoding='ascii').strip()
        if re.fullmatch('[a-f0-9]{64}', sha) and file_digest(bundle, pulse) == sha:
            return snapshot, revision, bundle, sha
    size = sum(record['fingerprint'][0] for record in unique.values()) + (root/snapshot['metadata']).stat().st_size
    require_space(root, size * 2, reserve=1024**3)
    temporary = bundle.with_suffix('.part')
    zip_time = time.gmtime(max(315532800, snapshot['created_at']))[:6]
    try:
        with zipfile.ZipFile(temporary, 'w', allowZip64=True) as archive:
            sources = [(('objects/'+sha[:2]+'/'+sha), sha, sha not in media) for sha in sorted(unique)]
            sources.append((snapshot['metadata'], snapshot['metadata_sha256'], False))
            for number, (relative, expected, compress) in enumerate(sources, 1):
                pulse(message='正在打包 @'+snapshot['handle'], file=number,total_files=len(sources),force=True)
                info = zipfile.ZipInfo(relative, date_time=zip_time)
                info.compress_type = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
                info._compresslevel = 1  # ZipInfo's compatible spelling on Python 3.10+.
                value = hashlib.sha256()
                with open_stored(root/relative) as reader, archive.open(info,'w',force_zip64=True) as writer:
                    for block in iter(lambda:reader.read(CHUNK), b''):
                        writer.write(block); value.update(block); pulse()
                if value.hexdigest() != expected:
                    raise ValueError('本地备份文件校验失败，未发布网盘恢复包：'+relative)
            archive.writestr(zipfile.ZipInfo(index.name, date_time=zip_time), raw)
            archive.writestr(zipfile.ZipInfo('恢复说明.txt', date_time=zip_time), RESTORE_NOTE)
        os.replace(temporary, bundle)
        with zipfile.ZipFile(bundle) as check:
            for item in check.infolist():
                with check.open(item) as reader:
                    for block in iter(lambda:reader.read(CHUNK), b''):
                        pulse()  # Reading to EOF checks each entry's CRC.
        sha = file_digest(bundle, pulse)
        atomic_write_text(receipt_path, sha)
        return snapshot, revision, bundle, sha
    finally:
        temporary.unlink(missing_ok=True)


def copy_snapshot(index, destination, pulse, *, library_id='', destination_id=''):
    snapshot, revision, bundle, sha = make_bundle(index, pulse)
    if library_id:
        verify_destination(destination, library_id, destination_id)
    relative = Path('恢复包')/bundle.name
    pulse(message='正在复制恢复包 @'+snapshot['handle'],file=1,total_files=1,force=True)
    copied = copy_verified(bundle, destination/relative, sha, pulse)
    if library_id:
        verify_destination(destination, library_id, destination_id)
    receipt = {'revision': revision,'files': len(snapshot['files']),'copied':int(copied),
               'submitted_at':time.time(),'destination':str(destination),
               'format':'collection_zip','bundle':relative.as_posix(),'sha256':sha,'bytes':bundle.stat().st_size}
    # A small pointer is published only after the complete package read-back.
    pointer = destination/(snapshot['handle']+'.bundle.json')
    raw = json.dumps(receipt,ensure_ascii=False).encode('utf-8')
    atomic_write_bytes(pointer,raw)
    if file_digest(pointer,pulse) != hashlib.sha256(raw).hexdigest():
        raise ValueError('恢复包索引读回校验失败。')
    # Only the regenerable local transport cache is removed. The verified local
    # backup and every cloud snapshot remain intact.
    bundle.unlink(missing_ok=True)
    bundle.with_suffix('.sha256').unlink(missing_ok=True)
    return receipt


def copy_round(base, destination, library_id, control, username=None):
    root, destination, control = backup_root(base), Path(destination), Path(control)
    receipts = read_json(control / 'receipts.json')
    indexes = [root / (collection_key(username) + '.json')] if username else sorted(root.glob('*.json'))
    state = {'state': 'copying', 'total': len(indexes), 'submitted': 0, 'pending': len(indexes),
             'destination': str(destination), 'message': '正在检查本地备份。'}
    last_report = 0.0

    def pulse(force=False, **values):
        nonlocal last_report
        state.update(values)
        if force or time.monotonic() - last_report >= 1:
            state['updated_at'] = time.time()
            write_json(control / 'progress.json', state)
            last_report = time.monotonic()

    pulse(force=True)
    pending, errors = [], []
    for index in indexes:
        try:
            snapshot, raw, revision = checked_snapshot(index)
        except Exception as error:
            errors.append(index.stem + '：' + str(error)[:120])
            continue
        previous = receipts.get(index.stem, {})
        if previous.get('revision') == revision and previous.get('destination') == str(destination):
            state['submitted'] += 1
        else:
            pending.append(index)
    state['pending'] = len(pending) + len(errors)
    if pending:
        pulse(message='正在核对网盘备份目录。', force=True)
        known = any(r.get('destination') == str(destination) for r in receipts.values() if isinstance(r, dict))
        token = prepare_destination(destination, library_id, control, require_existing=known)
        with prevent_idle_sleep():
            for index in pending:
                pulse(handle=index.stem, message='正在复制 @' + index.stem, force=True)
                try:
                    receipt = copy_snapshot(index, destination, pulse, library_id=library_id, destination_id=token)
                except Exception as error:
                    errors.append(index.stem + '：' + str(error)[:120])
                    continue
                receipts[index.stem] = receipt
                # Receipt goes to local disk only after every mount read-back passed.
                write_json(control / 'receipts.json', receipts)
                state['submitted'] += 1
                state['pending'] -= 1
    pulse(state='retrying' if errors else 'submitted' if indexes else 'waiting', handle='', file=0, total_files=0,
          message=('部分备份未复制，将重试：' + '；'.join(errors[:3])) if errors else NOTICE if indexes else '等待归档任务生成本地备份。', force=True)
    return state


class CloudBackup:
    """Owns one cancellable child. Polling and collection never access the mount."""
    def __init__(self, base):
        self.base = Path(base).absolute()
        self.control = backup_root(self.base) / '.cloud'
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.current = {'state': 'disabled', 'message': '网盘备份未启用。'}
        self.generation = 0
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self.run, daemon=True, name='cloud-backup')
        self.thread.start()

    def configure(self, values):
        with self.lock:
            result = save_config(self.base, values)
            self.generation += 1
            self.current = {'state': 'waiting' if result['enabled'] else 'disabled',
                            'message': '设置已保存，正在等待备份检查。' if result['enabled'] else '网盘备份已关闭，已有副本保留。'}
            self.wake.set()
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            saved = config(self.base)
            return {**self.current, 'enabled': saved['enabled'], 'destination': saved['destination'], 'notice': NOTICE}

    def update(self, **values):
        with self.lock:
            self.current.update(values)

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=5)

    def execute(self, saved, generation):
        self.control.mkdir(parents=True, exist_ok=True)
        progress = self.control / 'progress.json'
        progress.unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).absolute()), '--base', str(self.base),
                   '--destination', saved['destination'], '--library-id', saved['library_id'],
                   '--control', str(self.control), '--parent-pid', str(os.getpid())]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        last_change, last_stamp = time.monotonic(), None
        try:
            while child.poll() is None:
                if self.stopped.wait(0.5) or generation != self.generation:
                    return False
                stamp = progress.stat().st_mtime_ns if progress.exists() else None
                if stamp != last_stamp:
                    last_stamp, last_change = stamp, time.monotonic()
                    self.update(**read_json(progress))
                if time.monotonic() - last_change > STALL_SECONDS:
                    raise TimeoutError('网盘连续 3 分钟没有读写进展，已暂停复制，稍后自动重试。')
            state = read_json(progress)
            self.update(**state)
            if child.returncode:
                raise OSError(state.get('message', '网盘复制进程意外退出，稍后重试。'))
            return True
        finally:
            if child.poll() is None:
                # Only terminate our child, never a sync client or archive worker.
                child.terminate()
                child.wait(timeout=3)

    def run(self):
        failures = 0
        while not self.stopped.is_set():
            with self.lock:
                self.wake.clear()
                saved, generation = config(self.base), self.generation
            delay = SCAN_SECONDS
            if saved['enabled']:
                try:
                    self.execute(saved, generation)
                    failures = 0
                except Exception as error:
                    failures += 1
                    delay = min(3600, RETRY_SECONDS * 2 ** min(failures - 1, 4))
                    self.update(state='retrying', message=str(error)[:300], next_retry_at=time.time() + delay)
            if generation != self.generation:
                continue
            self.wake.wait(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--library-id', required=True)
    parser.add_argument('--control', type=Path, required=True)
    parser.add_argument('--parent-pid', type=int)
    parser.add_argument('--username', help='Optional one-collection verification run')
    args = parser.parse_args()
    if args.parent_pid:
        from supervisor import ProcessReference
        parent = ProcessReference(args.parent_pid)
        def watch_parent():
            while parent.alive():
                time.sleep(1)
            os._exit(2)
        threading.Thread(target=watch_parent, daemon=True).start()
    try:
        with collection_lock(args.control):
            result = copy_round(args.base, args.destination, args.library_id, args.control, args.username)
        return 1 if result['state'] == 'retrying' else 0
    except Exception as error:
        state = read_json(args.control / 'progress.json')
        write_json(args.control / 'progress.json', {**state, 'state': 'retrying', 'message': str(error)[:300]})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
