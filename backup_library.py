"""Incremental local backup with SQLite online snapshots and verified fresh restore."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import tempfile
import time
import zipfile
from contextlib import closing
from atomic_io import atomic_write_text, replace_file
from collection_lock import collection_lock
from platform_accounts import collection_key
from storage_guard import require_space
import compressed_files as stored


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def sqlite_copy(source,destination,timeout=30):
    with closing(sqlite3.connect(Path(source).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as reader, closing(sqlite3.connect(destination)) as writer:
        deadline=time.monotonic()+timeout
        def check(status,remaining,total):
            if time.monotonic()>deadline:raise TimeoutError('数据库备份等待超时，原数据库未修改，请稍后重试。')
        reader.backup(writer,pages=128,sleep=0.05,progress=check)
        if writer.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('数据库备份校验失败')


def backup_collection(base,handle,destination,progress=lambda text:None):
    base,destination=Path(base).resolve(),Path(destination).resolve()
    handle=collection_key(handle)
    folder=base/handle
    if destination.is_relative_to(base):raise ValueError('备份目录必须位于资料库之外')
    require_space(destination)
    destination.mkdir(parents=True,exist_ok=True)
    with collection_lock(folder):
        json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        files={}
        for subtree in ('media','raw'):
            sources = stored.logical_files(folder/subtree) if subtree == 'raw' else (folder/subtree).rglob('*')
            for source in sources:
                if stored.exists(source) and source.suffix not in {'.part','.tmp'}:
                    if source.is_symlink() or not source.resolve().is_relative_to(folder):raise ValueError('备份文件超出合集目录')
                    files[source.relative_to(base).as_posix()]=source
        state_path=destination/(handle+'.json')
        records={}
        added=0
        for number,(relative,source) in enumerate(files.items(),1):
            stat=stored.logical_stat(source)
            fingerprint=[stat.st_size,stat.st_mtime_ns]
            sha=stored.digest_stored(source)
            object_path=destination/'objects'/sha[:2]/sha
            # Existing immutable objects are rechecked before referencing them.
            if not stored.matches(object_path,sha):
                require_space(destination,stat.st_size)
                object_path.parent.mkdir(parents=True,exist_ok=True)
                # Separate accounts may contain identical files. Their staging
                # files must not collide when an external backup and the app run
                # together; only the verified, immutable final object is shared.
                descriptor, temporary = tempfile.mkstemp(prefix=sha+'.',suffix='.part',dir=object_path.parent)
                os.close(descriptor)
                temp=Path(temporary)
                try:
                    if source.is_file():shutil.copyfile(source,temp)
                    else:stored.copy_stored(source,temp)
                    if digest(temp)!=sha:raise ValueError('媒体备份内容发生变化，请重试')
                    # Compress privately, then publish immutable bytes in one
                    # rename. Two accounts may share the same raw object.
                    if '/raw/' in relative and not object_path.exists():
                        stored.compress_file(temp,destination,expected=sha)
                    if temp.exists():replace_file(temp,object_path)
                    else:replace_file(stored.packed_path(temp),stored.packed_path(object_path))
                    added+=1
                finally:
                    temp.unlink(missing_ok=True)
                    stored.packed_path(temp).unlink(missing_ok=True)
            records[relative]={'sha256':sha,'fingerprint':fingerprint}
            if number%100==0:progress(f'备份文件 {number}/{len(files)}，新增复制 {added} 个')
        with tempfile.TemporaryDirectory(prefix='snapshot-',dir=destination) as temporary:
            temp=Path(temporary)
            sources={handle+'/archive.sqlite':folder/'archive.sqlite'}
            sources.update({p.name:p for p in base.glob('*.sqlite') if p.is_file()})
            # Hidden SQLite catalogs are included by glob on Python/Windows.
            metadata={}
            for relative,source in sources.items():
                require_space(destination,source.stat().st_size*2)
                out=temp/relative;out.parent.mkdir(parents=True,exist_ok=True)
                sqlite_copy(source,out);metadata[relative]=out
            for relative in (handle+'/manifest.json',handle+'/.network-retry.json','.task-queue.json'):
                source=base/relative
                if source.is_file():
                    out=temp/relative;out.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source,out);metadata[relative]=out
            name=handle+'-'+str(time.time_ns())+'.zip'
            archive=destination/name
            require_space(destination,sum(p.stat().st_size for p in metadata.values())*2)
            hashes={relative:digest(path) for relative,path in metadata.items()}
            snapshot={'schema':1,'handle':handle,'metadata':name,'metadata_files':hashes,'files':records,
                      'created_at':time.time(),'coverage':'one_collection_and_library_catalogs'}
            with zipfile.ZipFile(archive.with_suffix('.part'),'w',zipfile.ZIP_DEFLATED) as output:
                for relative,path in metadata.items():output.write(path,relative)
                output.writestr('snapshot.json',json.dumps(snapshot,ensure_ascii=False))
            with zipfile.ZipFile(archive.with_suffix('.part')) as check:
                if check.testzip() is not None:raise ValueError('备份 ZIP 校验失败')
            replace_file(archive.with_suffix('.part'),archive)
            snapshot['metadata_sha256']=digest(archive)
            atomic_write_text(state_path,json.dumps(snapshot,ensure_ascii=False))
        # Keep the configured number of metadata snapshots. Immutable media objects are
        # retained; their deletion is never needed to publish a valid backup.
        pattern = re.compile(re.escape(handle)+r'-[0-9]+\.zip')
        from storage_policy import settings
        keep=settings(base)['backup_versions']
        older = sorted((p for p in destination.iterdir() if pattern.fullmatch(p.name)), key=lambda p:p.name, reverse=True)[keep:]
        for candidate in older:
            if candidate.resolve().parent == destination:
                try:candidate.unlink()
                except OSError:pass
        return {'path':str(state_path),'files':len(records),'copied':added,'verified':True}


def restore_backup(state_path,target):
    state_path,target=Path(state_path).resolve(),Path(target).resolve()
    if target.exists():raise ValueError('仅允许恢复到尚不存在的新目录，防止覆盖原资料')
    snapshot=json.loads(state_path.read_text(encoding='utf-8'))
    root=state_path.parent
    name=snapshot['metadata']
    if not isinstance(name,str) or Path(name).name!=name or ':' in name or not name.endswith('.zip'):raise ValueError('备份名称无效')
    archive=root/name
    if digest(archive)!=snapshot['metadata_sha256']:raise ValueError('数据库备份摘要不匹配')
    for relative in list(snapshot['files'])+list(snapshot['metadata_files']):
        parts=PurePosixPath(relative).parts
        if not parts or '..' in parts or '\\' in relative or ':' in relative or relative.startswith('/'):
            raise ValueError('备份路径无效')
    for record in snapshot['files'].values():
        sha=record['sha256']
        if not re.fullmatch('[a-f0-9]{64}',sha) or not stored.matches(root/'objects'/sha[:2]/sha,sha):
            raise ValueError('媒体备份缺失或损坏')
    require_space(target,sum(r['fingerprint'][0] for r in snapshot['files'].values())+archive.stat().st_size*4)
    # Validate every metadata entry before publishing any restored directory.
    with zipfile.ZipFile(archive) as zipped:
        for relative,sha in snapshot['metadata_files'].items():
            if hashlib.sha256(zipped.read(relative)).hexdigest()!=sha:raise ValueError('数据库备份内容校验失败')
        target.mkdir(parents=True)
        for relative in snapshot['metadata_files']:
            out=target/relative;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(zipped.read(relative))
    for relative,record in snapshot['files'].items():
        out=target/relative;out.parent.mkdir(parents=True,exist_ok=True)
        stored.copy_stored(root/'objects'/record['sha256'][:2]/record['sha256'],out)
    return target


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,default=Path(__file__).parent/'collections')
    parser.add_argument('--output',type=Path,default=Path(__file__).parent/'collections-backups')
    parser.add_argument('--username')
    parser.add_argument('--restore',type=Path)
    parser.add_argument('--restore-to',type=Path)
    args=parser.parse_args()
    if args.restore:
        if not args.restore_to:parser.error('--restore-to is required')
        print(restore_backup(args.restore,args.restore_to))
    else:
        handles=[args.username] if args.username else [p.parent.name for p in args.base.glob('*/manifest.json')]
        for handle in handles:print(json.dumps(backup_collection(args.base,handle,args.output,print),ensure_ascii=False))
