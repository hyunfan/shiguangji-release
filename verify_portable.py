"""Exercise a real portable ZIP without Python on PATH (Windows only).

Uses a fresh Chinese/space-containing temporary folder, local ephemeral port,
and temporary shortcut files. Does not change the user's desktop or registry.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile


def process_image(pid):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return Path(buffer.value).resolve()
    finally:
        kernel.CloseHandle(handle)


def terminate_fixture(pid, root):
    """Fault injection only for the embedded server in this test's isolated root."""
    assert process_image(pid) == root/'python/pythonw.exe'
    assert root.name.startswith('便携测试 中文 空格 ')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.OpenProcess(1,False,pid)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not kernel.TerminateProcess(handle,99):raise ctypes.WinError(ctypes.get_last_error())
    finally:kernel.CloseHandle(handle)


PROBE = r'''
import ctypes, importlib, json, pathlib, sqlite3, ssl, sys
root = pathlib.Path.cwd().resolve()
assert pathlib.Path(sys.executable).resolve() == root/'python/python.exe'
assert sys.flags.isolated and sys.flags.no_site
assert all(pathlib.Path(p).resolve().is_relative_to(root) for p in sys.path), sys.path
for source in root.glob('*.py'):
    importlib.import_module(source.stem)
assert ssl.create_default_context().cert_store_stats()['x509'] > 0
db = sqlite3.connect(root/'runtime/portable-probe.sqlite')
db.execute('create table if not exists probe(value text)')
db.execute('insert into probe values (?)', ('中文数据',)); db.commit(); db.close()
db = sqlite3.connect(root/'runtime/portable-probe.sqlite')
assert db.execute('select value from probe').fetchone()[0] == '中文数据'
db.close()
from shiguangji.tasks.task_history import TaskHistory
history = TaskHistory(root/'collections')
history.begin({'username':'portabletest', 'run_id':'portable-history', 'status':'running', 'running':True}, {})
history.finish({'username':'portabletest', 'run_id':'portable-history', 'status':'failed', 'running':False})
import windows_integration as entry
for stop in (False, True):
    values = entry._shortcut_values(root, stop=stop)
    assert pathlib.Path(values['target']) == root/'python/pythonw.exe'
    link = root/'runtime'/('stop.lnk' if stop else 'start.lnk')
    actual = entry._shortcut('save', link, values)
    assert entry._matching_shortcut(actual, values)
    assert entry._matching_shortcut(entry._shortcut('read', link), values)
print(json.dumps({'version': sys.version, 'executable': sys.executable, 'sys_path': sys.path,
                  'sqlite': sqlite3.sqlite_version, 'ssl': ssl.OPENSSL_VERSION}, ensure_ascii=True))
'''


def verify(package, workdir):
    if os.name != 'nt':
        raise RuntimeError('Run this smoke test on Windows x64.')
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    report = {'package': str(package), 'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
    with tempfile.TemporaryDirectory(prefix='便携测试 中文 空格 ', dir=workdir) as folder:
        root = Path(folder).resolve()
        with zipfile.ZipFile(package) as archive:
            manifest = json.loads(archive.read('release.json'))
            if manifest.get('edition') != 'windows-x64-portable':
                raise ValueError('Expected a Windows x64 portable package.')
            assert len(archive.namelist()) == len(manifest['files']) + 1
            for name, digest in manifest['files'].items():
                assert '\\' not in name and ':' not in name and not name.startswith('/')
                assert (root/name).resolve().is_relative_to(root)
                assert not name.startswith(('collections/', 'runtime/', 'diagnostics/', 'tests/'))
                data = archive.read(name)
                assert hashlib.sha256(data).hexdigest() == digest, name
                target = root/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        runtime = root/'runtime'
        runtime.mkdir()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        (runtime/'active-port.json').write_text(json.dumps({'root': str(root), 'port': port}))
        system = Path(os.environ['SystemRoot'])
        env = dict(os.environ, PATH=os.pathsep.join(map(str, [system/'System32', system, system/'System32/WindowsPowerShell/v1.0'])),
                   PYTHONHOME=str(root/'DO-NOT-USE-PYTHONHOME'), PYTHONPATH=str(root/'DO-NOT-USE-PYTHONPATH'))
        command = str(system/'System32/cmd.exe')
        options = dict(cwd=root, env=env, input='', capture_output=True, text=True, encoding='utf-8', errors='replace',
                       timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        def run(args):
            result = subprocess.run(args, **options)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout
        found = subprocess.run([str(system/'System32/where.exe'), 'python'], **options)
        assert found.returncode != 0, 'System Python remained on PATH'
        report['environment'] = json.loads(run([str(root/'python/python.exe'), '-X', 'utf8', '-c', PROBE]))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(path):
            with opener.open(f'http://127.0.0.1:{port}{path}', timeout=5) as response:
                return response.read()
        def state():
            return json.loads((runtime/f'background-{port}.json').read_text(encoding='utf-8'))
        def until(check, timeout=20):
            deadline=time.monotonic()+timeout
            while time.monotonic()<deadline:
                try:
                    value=check()
                    if value:return value
                except (OSError,ValueError):pass
                time.sleep(0.1)
            raise AssertionError('Timed out waiting for isolated lifecycle check')
        def guard_ready():return json.loads(get('/api/state')).get('guard',{}).get('state')=='watching'
        def guard_stopped():
            saved=json.loads((runtime/f'guard-{port}.json').read_text(encoding='utf-8'))
            try:process_image(saved['pid']);return False
            except OSError:return True
        try:
            run([command, '/d', '/c', '启动工具.cmd', '--no-open'])
            health = json.loads(get('/api/state'))
            assert health['app'] == 'x-wayback-library'
            assert Path(health['data_dir']).resolve() == root/'collections'
            report['health_version'] = health['version']
            first = state()
            assert first['state'] == 'ready'
            assert process_image(first['pid']) == root/'python/pythonw.exe'
            assert b'<html' in get('/').lower()
            assert len(get('/static/app.js')) > 1000
            assert len(get('/static/history.js')) > 1000
            historical = json.loads(get('/api/history?q=portabletest&state=failed'))
            assert historical['total'] == 1 and not historical['items'][0]['has_collection']
            # The embedded runtime must launch its isolated cloud-copy child and
            # remain responsive when the selected target's parent is unavailable.
            from urllib.request import Request, build_opener, ProxyHandler
            backups = root/'collections-backups'; backups.mkdir(exist_ok=True)
            (backups/'cloudtest.json').write_text(json.dumps({'schema':1,'handle':'cloudtest',
                'metadata':'cloudtest-1.zip','metadata_sha256':'0'*64,'files':{}}),encoding='utf-8')
            cloud_target = root/'unavailable-sync-root'/'cloud-test-target'
            def cloud_setting(enabled):
                request = Request(f'http://127.0.0.1:{port}/api/cloud-backup',
                    data=json.dumps({'enabled':enabled,'destination':str(cloud_target)}).encode(),
                    headers={'X-Local-Token':health['token'],'Content-Type':'application/json'})
                with build_opener(ProxyHandler({})).open(request,timeout=5) as response:
                    assert response.status == 200
            cloud_setting(True)
            until(lambda: json.loads(get('/api/state'))['cloud_backup']['state']=='retrying')
            assert not cloud_target.exists()
            assert json.loads(get('/api/state'))['job']['running'] is False
            cloud_setting(False)
            run([command, '/d', '/c', '启动工具.cmd', '--no-open'])
            assert state()['pid'] == first['pid'], 'Second click started a duplicate server'
            run([command, '/d', '/c', '停止后台.cmd'])
            assert state()['state'] == 'stopped'
            # Exercise the windowless shortcut target without opening a browser.
            run([str(root/'python/pythonw.exe'), '-X', 'utf8', str(root/'desktop_launcher.pyw'), '--no-open'])
            second = state()
            assert second['state'] == 'ready' and second['session'] != first['session']
            assert process_image(second['pid']) == root/'python/pythonw.exe'
            assert json.loads(get('/api/state'))['version'] == health['version']
            assert json.loads(get('/api/history?q=portabletest&state=failed'))['total'] == 1
            until(guard_ready)
            # Simulate a real process crash without touching the user's service.
            terminate_fixture(second['pid'],root)
            until(lambda: state()['state']=='ready' and state()['session']!=second['session'])
            until(guard_ready)
            assert json.loads(get('/api/state'))['job']['running'] is False
            assert json.loads(get('/api/history?q=portabletest&state=failed'))['total']==1
            report['crash_restarts']=json.loads((runtime/f'guard-{port}.json').read_text(encoding='utf-8'))['restarts']
            report['checks'] = ['file hashes and no private data', 'no system Python on PATH', 'ignored PYTHONHOME/PYTHONPATH',
                                'all application imports', 'SQLite persistence', 'Windows SSL trust store',
                                'native shortcuts in a Chinese path', 'real HTTP page and assets', 'embedded background process',
                                'repeat-click reuse', 'graceful stop', 'windowless restart', 'task history survives restart',
                                'real process crash restored by supervisor', 'crash does not auto-start paused work', 'guardian exits on normal stop',
                                'embedded cloud worker fails safely without blocking HTTP or starting collection']
        finally:
            run([command, '/d', '/c', '停止后台.cmd'])
            assert state()['state'] == 'stopped'
            until(guard_stopped)
    report['result'] = 'passed'
    (workdir/'portable-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    parser.add_argument('--workdir', default='diagnostics/portable-verification')
    args = parser.parse_args()
    verify(args.package.resolve(), args.workdir)
