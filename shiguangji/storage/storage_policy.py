"""Local storage preferences and reviewable cleanup of reproducible ZIP exports.

Never removes collection media, databases, cloud objects or current backups.
Plans bind file identities and the manifest; changed files require a new plan.
"""
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
import zipfile
import zlib

from shiguangji.core.atomic_io import atomic_write_text, read_json
from shiguangji.storage.collection_lock import collection_lock
from shiguangji.storage.collection_export import zip_is_current
from shiguangji.platforms.platform_accounts import collection_key

DEFAULTS = {'zip_on_demand': True, 'zip_keep_days': 7, 'backup_versions': 3,
            'cache_gb': 5}
_lock = threading.RLock()


def settings(base):
    saved = read_json(Path(base)/'.storage-settings.json')
    result = dict(DEFAULTS)
    for key, value in saved.items():
        if key == 'zip_on_demand' and type(value) is bool:
            result[key] = value
        elif key in {'zip_keep_days','backup_versions','cache_gb'} and type(value) is int:
            if value in {'zip_keep_days':(1,7,30,90), 'backup_versions':(1,3,5), 'cache_gb':(1,5,10,20)}[key]:
                result[key] = value
    return result


def save_settings(base, values):
    if not isinstance(values, dict) or set(values)-set(DEFAULTS):
        raise ValueError('空间设置包含未知选项。')
    for key,value in values.items():
        if key == 'zip_on_demand':
            if type(value) is not bool: raise ValueError('ZIP 选项应为开关值。')
        elif type(value) is not int or value not in {'zip_keep_days':(1,7,30,90), 'backup_versions':(1,3,5), 'cache_gb':(1,5,10,20)}[key]:
            raise ValueError('空间设置数值无效。')
    result = {**settings(base), **values}
    atomic_write_text(Path(base)/'.storage-settings.json',json.dumps(result))
    return result


def stamp(path):
    stat = path.stat()
    return [stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024), b''):result.update(chunk)
    return result.hexdigest()


def regular(path, root):
    return not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root.resolve())


def reproducible(package, folder, *, verify_content=False):
    """All archived bytes must still have a local source before releasing ZIP."""
    from shiguangji.storage.compressed_files import packed_path, logical_stat, open_stored
    try:
        with zipfile.ZipFile(package) as archive:
            for entry in archive.infolist():
                if entry.is_dir():continue
                prefix=folder.name+'/'
                if not entry.filename.startswith(prefix):return False
                source=folder/entry.filename[len(prefix):]
                is_raw=entry.filename[len(prefix):].startswith('raw/') and source.suffix=='.txt'
                physical=packed_path(source) if is_raw and not source.exists() else source
                if not regular(physical,folder):return False
                if (logical_stat(source) if is_raw else source.stat()).st_size!=entry.file_size:return False
                if verify_content:
                    before=stamp(physical);crc=0
                    with open_stored(source) as stream:
                        for block in iter(lambda:stream.read(1024*1024),b''):crc=zlib.crc32(block,crc)
                    if crc!=entry.CRC or stamp(physical)!=before:return False
        return True
    except (OSError,ValueError,zipfile.BadZipFile):return False


def plan_cleanup(base, *, all_exports=False, progress=None):
    """No media reads; ZIP fingerprints are rebound under a lock on execution."""
    base = Path(base)
    items, skipped = [], 0
    cutoff = time.time()-settings(base)['zip_keep_days']*86400
    packages=sorted(base.glob('*-合集.zip'))
    for number,package in enumerate(packages,1):
        if progress:progress(f'正在核对第 {number}/{len(packages)} 个 ZIP；已找到 {len(items)} 个可释放的文件。')
        try:
            handle = collection_key(package.name[:-len('-合集.zip')])
            folder = base/handle
            with collection_lock(folder):
                if not regular(package,base) or not regular(folder/'manifest.json',base):
                    skipped += 1; continue
                manifest = read_json(folder/'manifest.json')
                # Incomplete/crashed checkpoints should be resumed before cleanup.
                if manifest.get('status') == 'running': skipped += 1; continue
                if not all_exports and package.stat().st_mtime > cutoff:continue
                # A current sidecar proves this is a tool-created export. We never
                # infer ownership of an arbitrary similarly named user ZIP.
                sidecar = package.with_suffix('.zip.json')
                if not regular(sidecar,base) or not zip_is_current(folder):
                    skipped += 1; continue
                # Rebuilding must remain possible from the existing local media.
                media = {m['path'] for p in manifest.get('posts',[]) for m in p.get('media',[])
                         if m.get('status') == 'saved' and m.get('path')}
                if any(not regular(folder/name,folder) for name in media) or not reproducible(package,folder):
                    skipped += 1; continue
                items.append({'username':handle,'bytes':package.stat().st_size,
                              'zip_stat':stamp(package),'sidecar_sha256':digest(sidecar),
                              'manifest_sha256':digest(folder/'manifest.json')})
        except (OSError,ValueError):
            skipped += 1
    plan = {'id':secrets.token_hex(16),'created_at':time.time(),'items':items,
            'bytes':sum(i['bytes'] for i in items),'skipped':skipped,
            'kind':'generated_zip_only','all_exports':bool(all_exports)}
    with _lock:
        atomic_write_text(base/'.storage-cleanup-plan.json',json.dumps(plan,ensure_ascii=False))
    return plan


def execute_cleanup(base, plan_id, *, progress=None):
    base = Path(base)
    with _lock:
        plan = read_json(base/'.storage-cleanup-plan.json')
        if not isinstance(plan_id,str) or len(plan_id)!=32 or not secrets.compare_digest(plan.get('id',''),plan_id):
            raise ValueError('清理预览已失效，请重新预览。')
        if time.time()-plan.get('created_at',0)>1800:raise ValueError('清理预览已过期，请重新预览。')
        result = {'removed':0,'bytes':0,'skipped':0,'kind':'generated_zip_only'}
        for number,item in enumerate(plan['items'],1):
            if progress:progress(f"正在核对并释放第 {number}/{len(plan['items'])} 个 ZIP；已释放 {result['removed']} 个。大文件内容校验可能需要一些时间。")
            try:
                handle = collection_key(item['username']);folder=base/handle
                package=base/(handle+'-合集.zip');sidecar=package.with_suffix('.zip.json')
                with collection_lock(folder):
                    if (not regular(package,base) or not regular(sidecar,base)
                            or not regular(folder/'manifest.json',base)
                            or stamp(package)!=item['zip_stat']
                            or digest(sidecar)!=item['sidecar_sha256']
                            or digest(folder/'manifest.json')!=item['manifest_sha256']
                            or not zip_is_current(folder)):
                        result['skipped']+=1;continue
                    manifest=read_json(folder/'manifest.json')
                    media=[m['path'] for p in manifest.get('posts',[]) for m in p.get('media',[])
                           if m.get('status')=='saved' and m.get('path')]
                    if manifest.get('status')=='running' or any(not regular(folder/name,folder) for name in media):
                        result['skipped']+=1;continue
                    if not reproducible(package,folder,verify_content=True) or stamp(package)!=item['zip_stat']:
                        result['skipped']+=1;continue
                    # Only a regenerable export is deleted. Its source media stays.
                    package.unlink()
                    result['removed']+=1;result['bytes']+=item['bytes']
                    sidecar.unlink(missing_ok=True)
            except (OSError,ValueError):
                result['skipped']+=1
        atomic_write_text(base/'.storage-cleanup-plan.json',json.dumps({'consumed':plan_id,'result':result}))
        return result
