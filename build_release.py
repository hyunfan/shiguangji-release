"""Build a data-free release and verify the exact fixed file manifest."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
from shiguangji.core.app_version import VERSION

ROOT=Path(__file__).resolve().parent
# DOS timestamps start in 1980 and store seconds in steps of two. A fixed value
# keeps the package bytes identical across consecutive builds of the same tree.
_ZIP_EPOCH=(1980,1,1,0,0,0)


def application_files():
    files=json.loads((ROOT/'release_files.json').read_text(encoding='utf-8'))
    result={}
    for name in files:
        path=ROOT/name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):raise ValueError(name)
        if name.startswith(('collections/','diagnostics/','runtime/','tests/')):raise ValueError('Data/test file in release: '+name)
        result[name]=path.read_bytes()
    return result


def _zip_entry(name):
    """Same compression and permissions as ZipFile.writestr, with a fixed timestamp."""
    info=zipfile.ZipInfo(filename=name,date_time=_ZIP_EPOCH)
    info.compress_type=zipfile.ZIP_DEFLATED
    if name.endswith('/'):
        info.external_attr=(0o40775<<16)|0x10
    else:
        info.external_attr=0o600<<16
    return info


def write_package(destination, filename, result, **metadata):
    manifest={'version':VERSION, **metadata, 'files':{name:hashlib.sha256(data).hexdigest() for name,data in result.items()}}
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    target=destination/filename
    temporary=target.with_suffix('.zip.part')
    members=dict(result)
    members['release.json']=json.dumps(manifest,ensure_ascii=False,indent=2)
    with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as output:
        for name in sorted(members):output.writestr(_zip_entry(name),members[name])
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
