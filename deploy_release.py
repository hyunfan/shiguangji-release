"""Upgrade this desktop install while idle, with code-only backup and rollback.

Existing collections and registry/shortcut preferences are never copied, moved,
deleted or migrated. Program files that the installed release listed but the new
release no longer ships (for example the flat root modules of 1.28.2 and earlier)
are backed up and then removed; rollback restores them.
"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import socket
import sys
import time
from urllib.request import build_opener, ProxyHandler
import zipfile
from shiguangji.core.atomic_io import atomic_write_bytes


def health(port):
    try:
        with build_opener(ProxyHandler({})).open(f'http://127.0.0.1:{port}/api/state',timeout=3) as response:
            return json.load(response)
    except OSError:return None


# Root-level application modules of 1.28.2 and earlier, before they moved into shiguangji/.
# Used only when an installation has no readable release.json to identify its own files.
LEGACY_ROOT_MODULES=frozenset(name+'.py' for name in (
    'app_version','atomic_io','core_errors','archive_support',
    'platform_accounts','platform_registry','account_identity','identity_discovery','local_aliases',
    'extractor','instagram_extract',
    'archive','instagram_archive','post_recovery','recovery_report','run_budget',
    'archive_http','source_http','retry_policy','archived_sources','commoncrawl_source','public_sources','search_plan',
    'media_download','media_files','media_inventory','media_recovery','hls_recovery',
    'collection_store','collection_reader','collection_export','collection_lock','collection_delete',
    'compressed_files','storage_guard','storage_policy','storage_service','backup_library','compact_library',
    'cloud_backup','cloud_media',
    'task_service','task_queue','task_history','task_status','task_compat','job_dispatch','job_state','auto_batches',
    'reliability','power_guard','windows_shortcuts'))
# Only program code is ever removed. User data, logs, the embedded runtime, FFmpeg,
# documentation and launch scripts are never cleanup candidates.
PROTECTED_TOP={'collections','runtime','diagnostics','tools','python','ffmpeg'}


def removable_program_file(name):
    parts=PurePosixPath(name).parts
    if '\\' in name or ':' in name or not parts or '..' in parts or name.startswith('/'):return False
    if parts[0].lower() in PROTECTED_TOP or name=='release.json':return False
    if len(parts)==1:return name.endswith(('.py','.pyw'))
    return parts[0] in {'shiguangji','static'} and name.endswith(('.py','.js','.css','.html'))


def stale_program_files(target, new_files):
    """Files the installed release shipped that the new release does not; never user data."""
    installed=None
    try:
        recorded=json.loads((target/'release.json').read_text(encoding='utf-8'))
        if isinstance(recorded,dict) and isinstance(recorded.get('files'),dict):installed=set(recorded['files'])
    except (OSError,ValueError):installed=None
    if installed is None:
        # Pre-manifest installs: only the documented legacy root modules, and only when the
        # new release ships the package layout that replaces them.
        installed=set(LEGACY_ROOT_MODULES) if 'shiguangji/__init__.py' in new_files else set()
    stale=[]
    for name in sorted(installed-set(new_files)):
        if not isinstance(name,str) or not removable_program_file(name):continue
        path=target/name
        if path.is_symlink() or not path.is_file():continue
        if not path.resolve().is_relative_to(target):continue
        stale.append(name)
    return stale


def stale_bytecode(target):
    """Compiled files under shiguangji/ whose source module no longer exists.

    A sourceless .pyc beside a removed module stays importable, and orphaned
    __pycache__ entries outlive every upgrade. Only bytecode is a candidate:
    it is a cache that Python rebuilds from any restored source, so removing it
    never affects rollback, which restores sources from the backup zip.
    """
    package=Path(target)/'shiguangji'
    if package.is_symlink() or not package.is_dir():return []
    root=package.resolve();stale=[]
    for path in sorted(package.rglob('*.pyc')):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):continue
        if path.parent.name=='__pycache__':source=path.parent.parent/(path.name.split('.',1)[0]+'.py')
        else:source=path.with_suffix('.py')
        if not source.is_file():stale.append(path)
    return stale


def remove_stale_bytecode(target):
    """Best effort after a successful upgrade; a locked or vanished file is left alone."""
    removed,package=0,Path(target)/'shiguangji'
    for path in stale_bytecode(target):
        try:path.unlink();removed+=1
        except OSError:continue
        cache=path.parent
        if cache.name=='__pycache__':
            for folder in (cache,cache.parent):
                # Drop a cache folder (and a package folder left with nothing but it) once empty.
                if folder==package:break
                try:folder.rmdir()
                except OSError:break
    return removed


def deploy(package, target, backup_root, port):
    target=Path(target).resolve();backup_root=Path(backup_root).resolve()
    if not (target/'app.py').is_file() or not (target/'launcher.py').is_file():raise ValueError('Not an existing installation')
    with zipfile.ZipFile(package) as archive:
        manifest=json.loads(archive.read('release.json'))
        payload={}
        for name,digest in manifest['files'].items():
            parts=PurePosixPath(name).parts
            if '\\' in name or ':' in name or not parts or '..' in parts or name.startswith('/') or parts[0] in {'collections','runtime','diagnostics'}:raise ValueError('Unsafe release path')
            path=(target/name).resolve()
            if not path.is_relative_to(target):raise ValueError('Release escapes target')
            data=archive.read(name)
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Release checksum mismatch')
            payload[name]=data
        payload['release.json']=archive.read('release.json')
    state=health(port)
    if state is None:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=1):
                reachable=True
        except OSError:reachable=False
        if reachable:raise RuntimeError('Port is occupied but service identity cannot be verified; upgrade cancelled')
    if state:
        if state.get('app')!='x-wayback-library' or Path(state['data_dir']).resolve()!=target/'collections':raise ValueError('Port belongs to another installation')
        if state['job'].get('running') or state.get('queue',{}).get('state')=='running':raise ValueError('Active work; wait until the task is paused or finished before upgrading')
        if state.get('storage',{}).get('running'):raise ValueError('Space management is running; wait for preview, cleanup or cloud read to finish before upgrading')
    backup_root.mkdir(parents=True,exist_ok=True)
    backup=backup_root/('desktop-before-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.zip')
    previous={name:(target/name).read_bytes() if (target/name).is_file() else None for name in payload}
    stale=stale_program_files(target,payload)
    for name in stale:previous[name]=(target/name).read_bytes()
    with zipfile.ZipFile(backup,'x',zipfile.ZIP_DEFLATED) as saved:
        for name,data in previous.items():
            if data is not None:saved.writestr(name,data)
        saved.writestr('rollback-files.json',json.dumps({'added':[n for n,d in previous.items() if d is None],'removed':stale},ensure_ascii=False))
    hidden={'creationflags':subprocess.CREATE_NO_WINDOW} if sys.platform=='win32' else {}
    if state:
        subprocess.run([sys.executable,str(target/'launcher.py'),'--stop','--port',str(port)],check=True,timeout=60,**hidden)
        for _ in range(50):
            if health(port) is None:break
            time.sleep(.2)
        else:raise RuntimeError('Old service is still running; no files replaced')
    try:
        for name,data in payload.items():atomic_write_bytes(target/name,data)
        for name,data in payload.items():
            if (target/name).read_bytes()!=data:raise OSError('Installed checksum mismatch: '+name)
        # Old program files go only after every new file is verified; the backup holds them.
        for name in stale:(target/name).unlink(missing_ok=True)
        # An idle install remains on-demand. Restart only if it was running.
        if state:
            subprocess.run([sys.executable,str(target/'launcher.py'),'--no-open','--port',str(port)],check=True,timeout=60,**hidden)
            new=health(port)
            if not new or new.get('version')!=manifest['version']:raise RuntimeError('Updated health check failed')
    except Exception:
        current=health(port)
        if current and Path(current.get('data_dir','')).resolve()==target/'collections':
            subprocess.run([sys.executable,str(target/'launcher.py'),'--stop','--port',str(port)],timeout=60,**hidden)
            if health(port):raise RuntimeError('Rollback blocked: service did not stop; backup: '+str(backup))
        for name,data in previous.items():
            path=(target/name).resolve()
            if not path.is_relative_to(target):raise RuntimeError('Rollback path changed')
            if data is None:path.unlink(missing_ok=True)
            else:atomic_write_bytes(path,data)
        if state:subprocess.run([sys.executable,str(target/'launcher.py'),'--no-open','--port',str(port)],timeout=60,**hidden)
        raise
    # Only after the upgrade is final: any rollback above still finds the tree exactly as before.
    bytecode=remove_stale_bytecode(target)
    print(json.dumps({'version':manifest['version'],'target':str(target),'files':len(payload),'removed':len(stale),'bytecode_removed':bytecode,'backup':str(backup),'restarted':bool(state)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('package');parser.add_argument('--target',required=True);parser.add_argument('--backup-root',required=True);parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args();deploy(args.package,args.target,args.backup_root,args.port)
