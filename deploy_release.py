"""Upgrade this desktop install while idle, with code-only backup and rollback.

Existing collections and registry/shortcut preferences are never copied, moved,
deleted or migrated. New runtime modules are the only new files in an upgrade.
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
    with zipfile.ZipFile(backup,'x',zipfile.ZIP_DEFLATED) as saved:
        for name,data in previous.items():
            if data is not None:saved.writestr(name,data)
        saved.writestr('rollback-files.json',json.dumps({'added':[n for n,d in previous.items() if d is None]},ensure_ascii=False))
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
    print(json.dumps({'version':manifest['version'],'target':str(target),'files':len(payload),'backup':str(backup),'restarted':bool(state)},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('package');parser.add_argument('--target',required=True);parser.add_argument('--backup-root',required=True);parser.add_argument('--port',type=int,default=8766)
    args=parser.parse_args();deploy(args.package,args.target,args.backup_root,args.port)
