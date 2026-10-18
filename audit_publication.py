"""Inspect a data-free source tree or release ZIP without printing secrets.

This is a bounded heuristic check, not proof that a publication is anonymous.
Run it on a clean export, not an installed library. Audit Git history separately.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import zipfile

TEXT = {'.py', '.pyw', '.cmd', '.js', '.html', '.css', '.md', '.txt', '.json'}
BLOCKED = {'collections', 'collections-backups', 'runtime', 'diagnostics', '.git',
           '.venv', 'venv', 'node_modules', '__pycache__', '.media-cache', 'tools'}
PRIVATE_FILES = {'.cloud-backup.json', '.reliability-settings.json', '.storage-settings.json',
                 '.storage-cleanup-plan.json', '.shiguangji-backup.json'}
RULES = {
    'github_token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})',
    'aws_access_key': r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'private_key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'api_key': r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}',
    'credential_url': r'https?://[^\s/@:]+:[^\s/@]+@',
}
SAMPLE_USERS = {'sample', 'example', 'public', 'default', 'test', '<user>', '<username>'}


def findings(name, data, private_terms=()):
    """Return locations and categories only; never echo a matched value."""
    result = []
    parts = PurePosixPath(name).parts
    p = PurePosixPath(name)
    if (p.is_absolute() or '..' in parts or '\\' in name or ':' in name
            or any(part.casefold() in BLOCKED for part in parts)
            or p.name in PRIVATE_FILES or p.name.startswith('private-terms')
            or p.name.startswith('.env') and p.name != '.env.example'
            or p.name.casefold().startswith('cookies')
            or p.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.log', '.pem', '.key', '.p12', '.pfx'}):
        result.append({'file': name, 'kind': 'private_or_generated_path', 'line': 0})
    # Official embedded Python is checked against its pinned upstream hash by
    # build_portable; its binary data, licenses and standard library are not ours.
    if name.startswith('python/') or p.suffix.lower() not in TEXT and p.name not in {'.gitignore', '.gitattributes', 'LICENSE'}:
        return result
    if len(data) > 4 * 1024**2:
        return result + [{'file': name, 'kind': 'unexpected_large_text', 'line': 0}]
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return result + [{'file': name, 'kind': 'unexpected_text_encoding', 'line': 0}]

    def add(kind, match):
        result.append({'file': name, 'kind': kind, 'line': text.count('\n', 0, match.start()) + 1})

    for kind, pattern in RULES.items():
        for match in re.finditer(pattern, text):
            # Synthetic proxy credentials exercise URL redaction/validation.
            if kind == 'credential_url' and name.startswith('tests/'):
                continue
            add(kind, match)
    for match in re.finditer(r'(?i)(?<![A-Za-z0-9_/:])(?:[a-z]:[/\\]+Users[/\\]+|/(?:Users|home)/)([^/\\\s\'"`]+)', text):
        if match.group(1).casefold() not in SAMPLE_USERS:
            add('personal_home_path', match)
    urls = [(m.start(), m.end()) for m in re.finditer(r'https?://[^\s\'"<>]+', text)]
    for match in re.finditer(r'(?<![\\\w.+-])[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})(?![\w.-])', text):
        if any(start <= match.start() < end for start, end in urls):
            continue
        domain = match.group(1).lower()
        if domain not in {'example.com', 'example.org', 'example.net', 'example.invalid', 'test.invalid'}:
            add('email_to_review', match)
    for term in private_terms:
        if not isinstance(term, str) or not term:
            continue
        for match in re.finditer(r'(?<![A-Za-z0-9_])' + re.escape(term) + r'(?![A-Za-z0-9_])', text, re.I):
            add('private_term', match)
    return result


def scan(target, private_terms=()):
    target = Path(target)
    issues, files = [], {}

    def inspect(name, data):
        issues.extend(findings(name, data, private_terms))
        files[name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

    if target.is_file():
        with zipfile.ZipFile(target) as archive:
            if len(archive.infolist()) > 2048 or sum(i.file_size for i in archive.infolist()) > 512 * 1024**2:
                raise ValueError('Unexpected publication ZIP size.')
            for item in archive.infolist():
                if not item.is_dir():
                    inspect(item.filename, archive.read(item))
    else:
        for path in sorted(target.rglob('*')):
            if path.is_symlink():
                issues.append({'file': path.relative_to(target).as_posix(), 'kind': 'linked_path', 'line': 0})
            elif path.is_file():
                inspect(path.relative_to(target).as_posix(), path.read_bytes())
    return {'files': files, 'findings': issues, 'notice': 'Heuristic scan only; inspect history, attachments and licenses separately.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('target', type=Path, help='Clean source export or release ZIP')
    parser.add_argument('--private-terms-file', type=Path, help='Local JSON list; never publish this file')
    parser.add_argument('--output', type=Path, help='Local audit report, outside the clean export')
    args = parser.parse_args()
    terms = json.loads(args.private_terms_file.read_text(encoding='utf-8')) if args.private_terms_file else []
    if not isinstance(terms, list):
        parser.error('Private terms must be a JSON list.')
    report = scan(args.target, terms)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'files': len(report['files']), 'findings': report['findings']}, ensure_ascii=False))
    return 1 if report['findings'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
