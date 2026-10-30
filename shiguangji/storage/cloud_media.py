"""Bounded, verified read-through of media in existing cloud backup ZIPs.

Source files stay local until a separate migration is approved. The dedicated
pilot route reads verified copies. A mount receipt is never upload proof.
Network filesystem access occurs only in disposable child processes.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import threading
import time
import uuid
import zipfile

if __package__ in (None, ''):
    # 以文件路径运行（自身子进程或命令行）时，从应用根目录导入 shiguangji 包。
    _here, _root = Path(__file__).resolve().parent, str(Path(__file__).resolve().parents[2])
    if sys.path and sys.path[0] and Path(sys.path[0]).resolve() == _here:del sys.path[0]
    if _root not in sys.path:sys.path.insert(0, _root)

from shiguangji.core.atomic_io import atomic_write_text
from shiguangji.platforms.platform_accounts import collection_key
from shiguangji.storage.storage_guard import require_space
from shiguangji.storage.storage_policy import read_json, settings

_slots=threading.BoundedSemaphore(2)
_cache_lock=threading.RLock()
_pinned={}
_reserved=0
MAX_FILE_BYTES=2*1024**3
READ_TIMEOUT=120


def safe_relative(value):
    if not isinstance(value,str) or not value or '\\' in value or ':' in value:
        raise ValueError('媒体路径无效。')
    parts=PurePosixPath(value).parts
    if PurePosixPath(value).is_absolute() or '..' in parts or '.' in parts:
        raise ValueError('媒体路径无效。')
    return value


def descriptor(base, handle, relative):
    """Read only local manifests/receipts; reject stale backup revisions."""
    base=Path(base);handle=collection_key(handle);safe_relative(relative)
    if not relative.startswith('media/'):raise ValueError('只能读取合集中的媒体文件。')
    manifest=read_json(base/handle/'manifest.json')
    items=[m for p in manifest.get('posts',[]) for m in p.get('media',[])
           if m.get('status')=='saved' and m.get('path')==relative]
    if not items:raise ValueError('清单中没有这项已保存媒体。')
    root=base.parent/(base.name+'-backups')
    index=root/(handle+'.json')
    raw=index.read_bytes();snapshot=json.loads(raw)
    receipt=read_json(root/'.cloud/receipts.json').get(handle,{})
    if receipt.get('format')!='collection_zip' or receipt.get('revision')!=hashlib.sha256(raw).hexdigest():
        raise ValueError('此合集还没有与当前本地备份一致的网盘恢复包。')
    record=snapshot.get('files',{}).get(handle+'/'+relative,{})
    sha=record.get('sha256','');fingerprint=record.get('fingerprint')
    size=fingerprint[0] if isinstance(fingerprint,list) and fingerprint else 0
    if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha) or type(size) is not int or not 0<size<=MAX_FILE_BYTES:
        raise ValueError('媒体摘要或大小无效，暂不能云端试读。')
    if any(m.get('sha256')!=sha or m.get('bytes')!=size for m in items):
        raise ValueError('媒体已经变化，需先更新备份。')
    config=read_json(base/'.cloud-backup.json')
    destination=receipt.get('destination','')
    bundle=safe_relative(receipt.get('bundle',''))
    if (not Path(destination).is_absolute() or destination!=config.get('destination')
            or not bundle.startswith('恢复包/') or not bundle.endswith('.zip')
            or not re.fullmatch('[a-f0-9]{32}',config.get('library_id',''))):
        raise ValueError('网盘位置与当前备份设置不一致。')
    from shiguangji.storage.cloud_backup import destination_binding
    bindings, _, key = destination_binding(root/'.cloud', Path(destination))
    token = bindings.get(key, {}).get('destination_id', '')
    return {'destination':destination,'bundle':bundle,'library_id':config['library_id'],
            'destination_id':token,
            'member':'objects/'+sha[:2]+'/'+sha,'sha256':sha,'bytes':size,
            'bundle_bytes':receipt.get('bytes'), 'relative':relative,
            'mime':mimetypes.guess_type(relative)[0] or 'application/octet-stream'}


def cache_root(base):return Path(base)/'.media-cache'


def checked_cache_root(base):
    root=cache_root(base)
    if root.is_symlink() or root.resolve().parent!=Path(base).resolve():
        raise ValueError('媒体缓存目录不能指向资料库之外。')
    return root


def cached_valid(path, expected):
    if path.is_symlink() or not path.is_file() or path.stat().st_size!=expected['bytes']:return False
    h=hashlib.sha256()
    with path.open('rb') as reader:
        for block in iter(lambda:reader.read(1024*1024),b''):h.update(block)
    return h.hexdigest()==expected['sha256']


def trim_cache(base, limit=None, reserve=0):
    root=checked_cache_root(base)
    limit=settings(base)['cache_gb']*1024**3 if limit is None else limit
    with _cache_lock:
        files=[p for p in root.glob('*.blob') if not p.is_symlink() and p.is_file()]
        total=sum(p.stat().st_size for p in files)
        for path in sorted(files,key=lambda p:p.stat().st_mtime_ns):
            if total+reserve<=limit:break
            if _pinned.get(str(path)):continue
            size=path.stat().st_size;path.unlink();total-=size
        return total


def cache_status(base):
    root=checked_cache_root(base)
    with _cache_lock:
        files=[p for p in root.glob('*.blob') if p.is_file() and not p.is_symlink()]
        return {'bytes':sum(p.stat().st_size for p in files),'files':len(files),
                'limit_bytes':settings(base)['cache_gb']*1024**3}


def extract_worker(request, target):
    """All potentially blocking mount operations belong to this child."""
    from shiguangji.storage.cloud_backup import check_mount, verify_destination
    destination=Path(request['destination']);check_mount(destination)
    verify_destination(destination, request['library_id'], request.get('destination_id', ''))
    package=destination/safe_relative(request['bundle'])
    if not package.resolve().is_relative_to(destination.resolve()):raise ValueError('恢复包路径越界。')
    if package.stat().st_size!=request['bundle_bytes']:raise ValueError('恢复包大小变化，需重新核验。')
    expected=request['bytes'];h=hashlib.sha256();total=0
    with zipfile.ZipFile(package) as archive:
        info=archive.getinfo(request['member'])
        if info.file_size!=expected or info.compress_type!=zipfile.ZIP_STORED or info.flag_bits&1:
            raise ValueError('恢复包媒体格式或大小不符合预期。')
        with archive.open(info) as reader,Path(target).open('xb') as writer:
            while True:
                chunk=reader.read(min(1024*1024,expected-total+1))
                if not chunk:break
                total+=len(chunk)
                if total>expected:raise ValueError('媒体长度超出清单。')
                writer.write(chunk);h.update(chunk)
    if total!=expected or h.hexdigest()!=request['sha256']:raise ValueError('云端媒体校验失败。')


@contextmanager
def open_media(base, handle, relative, *, timeout=READ_TIMEOUT):
    global _reserved
    request=descriptor(base,handle,relative)
    root=checked_cache_root(base);root.mkdir(parents=True,exist_ok=True)
    if root.is_symlink():raise ValueError('媒体缓存目录不能是链接。')
    path=root/(request['sha256']+'.blob');hit=False;leased=False;reserved=0
    # Bound both waiting callers and network filesystem readers.
    if not _slots.acquire(timeout=2):raise TimeoutError('已有两个云端文件正在读取，请稍后重试。')
    try:
        with _cache_lock:
            hit=cached_valid(path,request)
            if hit:
                _pinned[str(path)]=_pinned.get(str(path),0)+1
                leased=True
                os.utime(path,None)
        if not hit:
            with _cache_lock:
                budget=settings(base)['cache_gb']*1024**3
                if request['bytes']>budget:raise ValueError('该文件大于缓存上限，请提高缓存容量。')
                if trim_cache(base,reserve=request['bytes']+_reserved)+request['bytes']+_reserved>budget:
                    raise ValueError('缓存正在使用，请等待播放结束后重试。')
                require_space(root,request['bytes'])
                reserved=request['bytes'];_reserved+=reserved
            tag=uuid.uuid4().hex
            control=root/(tag+'.json');temporary=root/(tag+'.part')
            atomic_write_text(control,json.dumps(request,ensure_ascii=False))
            try:
                result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--request',str(control),
                                       '--target',str(temporary)],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,timeout=timeout,
                                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                if result.returncode:
                    raise OSError('网盘媒体暂不可读或校验未通过；本地资料未改变。')
                if not cached_valid(temporary,request):raise ValueError('媒体缓存校验失败。')
                with _cache_lock:
                    if _pinned.get(str(path)):
                        # Another reader published the same immutable file.
                        temporary.unlink()
                    else:os.replace(temporary,path)
                    _pinned[str(path)]=_pinned.get(str(path),0)+1
                    leased=True
            except subprocess.TimeoutExpired as error:
                raise TimeoutError(f'网盘读取超过 {timeout:g} 秒，已停止本次读取，可稍后重试。') from error
            finally:
                control.unlink(missing_ok=True);temporary.unlink(missing_ok=True)
        yield path,request,hit
    finally:
        with _cache_lock:
            _reserved-=reserved
            if leased:
                _pinned[str(path)]-=1
                if not _pinned[str(path)]:del _pinned[str(path)]
        _slots.release()
        trim_cache(base)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--request',required=True,type=Path);parser.add_argument('--target',required=True,type=Path)
    args=parser.parse_args()
    extract_worker(read_json(args.request),args.target)
