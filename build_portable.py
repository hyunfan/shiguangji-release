"""Build the Windows x64 portable edition from a pinned official Python ZIP.

No installed Python files or personal collections are copied into the package.
Run with --runtime-zip for an offline, hash-verified build.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import urllib.request
import zipfile

from app_version import VERSION
from build_release import ROOT, application_files, write_package

MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def read_runtime(data, pin):
    if hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('Python runtime SHA256 does not match the pinned official release.')
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > 256 or sum(info.file_size for info in members) > MAX_EXPANDED:
            raise ValueError('Python runtime ZIP exceeds the size limit.')
        seen = set()
        for info in members:
            name = info.filename
            # Official Windows embedded distributions have a flat layout.
            if not name or any(char in name for char in '/\\:') or name in ('.', '..') or name.endswith(('.', ' ')):
                raise ValueError('Unexpected runtime path: ' + name)
            if name.lower() in seen or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Duplicate or linked runtime member: ' + name)
            seen.add(name.lower())
            result['python/' + name] = archive.read(info)
    required = ('python.exe', 'pythonw.exe', 'python3.dll', 'python314.dll', 'LICENSE.txt',
                '_ssl.pyd', '_sqlite3.pyd', '_ctypes.pyd', pin['stdlib'], pin['path_file'])
    if any('python/' + name not in result for name in required):
        raise ValueError('The official embedded runtime is incomplete.')
    # Stay isolated: ignore PYTHONHOME, PYTHONPATH, registry and user site-packages.
    # The parent is the application root; runtime/ remains writable logs/state only.
    result['python/' + pin['path_file']] = (pin['stdlib'] + '\n.\n..\n').encode('ascii')
    return result


def runtime_data(pin, path=None):
    if path:
        path = Path(path)
        if path.stat().st_size > MAX_DOWNLOAD:
            raise ValueError('Python runtime ZIP exceeds the download limit.')
        return path.read_bytes()
    path = ROOT / 'diagnostics' / 'runtime-cache' / pin['url'].rsplit('/', 1)[1]
    if path.is_file():
        return runtime_data(pin, path)
    with urllib.request.urlopen(pin['url'], timeout=60) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD or hashlib.sha256(data).hexdigest() != pin['sha256']:
        raise ValueError('Downloaded runtime failed size or SHA256 verification.')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def build(destination, runtime_zip=None):
    pin = json.loads((ROOT / 'python-runtime.json').read_text(encoding='utf-8'))
    result = application_files()
    result.update(read_runtime(runtime_data(pin, runtime_zip), pin))
    # Marker makes launchers fail visibly if someone omits the embedded runtime.
    result['portable.json'] = json.dumps(pin, indent=2).encode('utf-8')
    return write_package(destination, f'shiguangji-{VERSION}-windows-x64-portable.zip', result,
                         edition='windows-x64-portable', python=pin)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=str(ROOT / 'diagnostics/releases'))
    parser.add_argument('--runtime-zip', help='Previously downloaded official embedded ZIP')
    args = parser.parse_args()
    build(args.output, args.runtime_zip)
