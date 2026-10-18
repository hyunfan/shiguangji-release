"""Check tracked source and data-free packages; never read an installed library.

Requires Git. Stage new source files before running. Build/smoke outputs live in a
temporary directory, and reports contain finding locations rather than secrets.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import zipfile

from app_version import VERSION
from audit_publication import findings, scan
from build_portable import read_runtime

ROOT = Path(__file__).resolve().parent
MAX_BYTES = 512 * 1024**2


class PublicationError(ValueError):
    def __init__(self, code, issues=()):
        super().__init__(code)
        self.code, self.issues = code, list(issues)


def safe_name(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and str(path) == name
            and not any(part in ('', '.', '..') for part in path.parts)
            and not any(char in name for char in '\\:\x00\r\n'))


def export_tracked(root, destination):
    """Copy only tracked worktree files; Git history/ignored local data stay out."""
    root, destination = Path(root).resolve(), Path(destination).resolve()
    result = subprocess.run(['git', '-c', 'safe.directory=' + str(root), 'ls-files', '-z'],
                            cwd=root, capture_output=True, timeout=30, check=True)
    names = result.stdout.decode('utf-8').rstrip('\0').split('\0')
    if not names or len(names) > 2048 or names == ['']:
        raise PublicationError('unexpected_tracked_file_count')
    seen, total = set(), 0
    for name in names:
        if not safe_name(name) or name.casefold() in seen:
            raise PublicationError('unsafe_tracked_path')
        seen.add(name.casefold())
        issues = findings(name, b'')
        # Runtime files are exempt from text scanning only after verification
        # against the official ZIP. They must never enter the tracked export.
        if PurePosixPath(name).parts[0].casefold() == 'python' or name.casefold() == 'portable.json':
            issues.append({'file': name, 'kind': 'private_or_generated_path', 'line': 0})
        if issues:
            raise PublicationError('private_tracked_path', issues)
        source = root / name
        if (not source.is_file() or not source.resolve().is_relative_to(root)
                or any(path.is_symlink() for path in (source, *source.parents) if path != root)):
            raise PublicationError('missing_or_linked_tracked_file')
        size = source.stat().st_size
        total += size
        if size > 16 * 1024**2 or total > MAX_BYTES:
            raise PublicationError('unexpected_tracked_file_size')
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    return len(names)


def verify_package(package, expected, edition):
    """Verify against source/runtime bytes, not just a self-reported manifest."""
    with zipfile.ZipFile(package) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        if (len(items) > 2048 or sum(item.file_size for item in items) > MAX_BYTES
                or any(not safe_name(name) for name in names)
                or len({name.casefold() for name in names}) != len(names)
                or set(names) != set(expected) | {'release.json'}
                or any((item.external_attr >> 16) & 0o170000 == 0o120000 for item in items)):
            raise PublicationError('unexpected_package_members')
        manifest = json.loads(archive.read('release.json'))
        digests = {name: hashlib.sha256(data).hexdigest() for name, data in expected.items()}
        if (manifest.get('version') != VERSION or manifest.get('edition') != edition
                or manifest.get('files') != digests):
            raise PublicationError('package_manifest_mismatch')
        for name, digest in digests.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise PublicationError('package_content_mismatch')
    return {'files': len(expected), 'sha256': hashlib.sha256(Path(package).read_bytes()).hexdigest()}


def audit(target, terms):
    report = scan(target, terms)
    if report['findings']:
        raise PublicationError('privacy_review_required', report['findings'])


def run_script(root, script, arguments, timeout=180):
    # Do not echo subprocess output: a failed check can contain local paths/data.
    result = subprocess.run([sys.executable, '-X', 'utf8', str(root / script), *map(str, arguments)],
                            cwd=root, capture_output=True, timeout=timeout)
    if result.returncode:
        raise PublicationError(script.removesuffix('.py') + '_failed')


def check(root=ROOT, *, portable=False, smoke=False, runtime_zip=None, private_terms=()):
    if smoke and (not portable or os.name != 'nt'):
        raise PublicationError('smoke_requires_windows_and_portable')
    if runtime_zip:
        runtime_zip = Path(runtime_zip).resolve()
    with tempfile.TemporaryDirectory(prefix='shiguangji-publication-') as folder:
        temporary = Path(folder)
        source, output = temporary / 'source', temporary / 'packages'
        count = export_tracked(root, source)
        audit(source, private_terms)
        whitelist = json.loads((source / 'release_files.json').read_text(encoding='utf-8'))
        if (not isinstance(whitelist, list) or len(set(whitelist)) != len(whitelist)
                or any(not isinstance(name, str) or not safe_name(name) for name in whitelist)
                or any(not (source / name).is_file() for name in whitelist)):
            raise PublicationError('invalid_release_whitelist')
        expected = {name: (source / name).read_bytes() for name in whitelist}
        run_script(source, 'build_release.py', ['--output', output])
        package = output / f'shiguangji-{VERSION}.zip'
        audit(package, private_terms)
        report = {'result': 'passed', 'tracked_files': count,
                  'source_package': verify_package(package, expected, 'source')}
        if portable:
            arguments = ['--output', output]
            if runtime_zip:
                arguments += ['--runtime-zip', runtime_zip]
            run_script(source, 'build_portable.py', arguments)
            pin = json.loads((source / 'python-runtime.json').read_text(encoding='utf-8'))
            cached = runtime_zip or source / 'diagnostics/runtime-cache' / pin['url'].rsplit('/', 1)[1]
            runtime = read_runtime(cached.read_bytes(), pin)
            bundled = {**expected, **runtime, 'portable.json': json.dumps(pin, indent=2).encode('utf-8')}
            package = output / f'shiguangji-{VERSION}-windows-x64-portable.zip'
            audit(package, private_terms)
            report['portable_package'] = verify_package(package, bundled, 'windows-x64-portable')
            if smoke:
                workdir = temporary / 'smoke'
                run_script(source, 'verify_portable.py', [package, '--workdir', workdir], timeout=300)
                result = json.loads((workdir / 'portable-smoke.json').read_text(encoding='utf-8'))
                if result.get('result') != 'passed' or not result.get('checks'):
                    raise PublicationError('portable_smoke_incomplete')
                report['portable_smoke'] = {'result': 'passed', 'checks': len(result['checks'])}
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--portable', action='store_true', help='Also build and verify the pinned Windows runtime')
    parser.add_argument('--smoke', action='store_true', help='Run isolated Windows startup/crash/restart checks')
    parser.add_argument('--runtime-zip', type=Path, help='Optional local copy of the pinned official runtime')
    parser.add_argument('--private-terms-file', type=Path, help='Local JSON list; never commit it')
    args = parser.parse_args()
    try:
        terms = json.loads(args.private_terms_file.read_text(encoding='utf-8')) if args.private_terms_file else []
        if not isinstance(terms, list) or not all(isinstance(term, str) for term in terms):
            raise PublicationError('invalid_private_terms')
        report = check(portable=args.portable, smoke=args.smoke, runtime_zip=args.runtime_zip, private_terms=terms)
    except PublicationError as error:
        print(json.dumps({'result': 'failed', 'check': error.code, 'findings': error.issues}, ensure_ascii=False))
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile, KeyError, TypeError):
        print(json.dumps({'result': 'failed', 'check': 'publication_check_error'}))
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
