"""Build a data-free release and verify the exact fixed file manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from shiguangji.core.app_version import VERSION

ROOT=Path(__file__).resolve().parent


def application_files():
    files=json.loads((ROOT/'release_files.json').read_text(encoding='utf-8'))
    result={}
    for name in files:
        path=ROOT/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):raise ValueError(name)
        if name.startswith(('collections/','diagnostics/','runtime/','tests/')):raise ValueError('Data/test file in release: '+name)
        result[name]=path.read_bytes()
    return result


def write_package(destination, filename, result, **metadata):
    manifest={'version':VERSION, **metadata, 'files':{name:hashlib.sha256(data).hexdigest() for name,data in result.items()}}
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    target=destination/filename
    temporary=target.with_suffix('.zip.part')
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as output:
        for name,data in result.items():output.writestr(name,data)
        output.writestr('release.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(temporary) as check:
        assert check.testzip() is None
        for name,digest in manifest['files'].items():assert hashlib.sha256(check.read(name)).hexdigest()==digest
    temporary.replace(target)
    print(json.dumps({'package':str(target),'version':VERSION,'files':len(result),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()},ensure_ascii=False))
    return target


def build(destination):
    return write_package(destination, f'shiguangji-{VERSION}.zip', application_files(), edition='source')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',default=str(ROOT/'diagnostics/releases'))
    build(parser.parse_args().output)
