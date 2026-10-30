"""Publication gates operate only on temporary Git repositories and fake ZIPs."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from shiguangji.core.app_version import VERSION
from check_publication import PublicationError, export_tracked, verify_package, check


class PublicationChecksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')

    def git(self, *arguments):
        return subprocess.run(['git', '-c', 'safe.directory=' + str(self.root), *arguments],
                              cwd=self.root, capture_output=True, check=True)

    def track(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.git('add', '-f', '--', name)

    def test_export_excludes_git_history_ignored_data_and_untracked_files(self):
        self.track('.gitignore', b'collections/\n')
        self.track('app.py', b'print("sample")\n')
        (self.root / 'collections').mkdir()
        (self.root / 'collections/private.txt').write_text('local data')
        (self.root / 'untracked.txt').write_text('local data')
        destination = self.root.parent / 'export'
        self.assertEqual(export_tracked(self.root, destination), 2)
        names = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
        self.assertEqual(names, {'.gitignore', 'app.py'})

    def test_even_force_added_private_data_stops_export(self):
        self.track('collections/sample/archive.sqlite', b'private data')
        with self.assertRaises(PublicationError) as result:
            export_tracked(self.root, self.root.parent / 'export')
        self.assertEqual(result.exception.code, 'private_tracked_path')
        self.assertNotIn('private data', str(result.exception.issues))

    def test_credential_in_tracked_workflow_stops_before_build(self):
        token = 'ghp_' + 'v' * 36
        self.track('.github/workflows/checks.yml', ('value: ' + token).encode())
        with patch('check_publication.run_script') as build:
            with self.assertRaises(PublicationError) as result:
                check(self.root)
        build.assert_not_called()
        self.assertEqual(result.exception.code, 'privacy_review_required')
        self.assertNotIn(token, str(result.exception.issues))

    def test_tracked_runtime_cannot_use_the_verified_runtime_scan_exemption(self):
        for name in ('python/config.txt', 'portable.json'):
            self.track(name, b'not an official verified runtime')
        with self.assertRaisesRegex(PublicationError, 'private_tracked_path'):
            export_tracked(self.root, self.root.parent / 'export')

    def package(self, content=b'original', extra=None, manifest_content=b'original'):
        target = self.root.parent / 'release.zip'
        manifest = {'version': VERSION, 'edition': 'source',
                    'files': {'app.py': hashlib.sha256(manifest_content).hexdigest()}}
        with zipfile.ZipFile(target, 'w') as archive:
            archive.writestr('app.py', content)
            archive.writestr('release.json', json.dumps(manifest))
            if extra:
                archive.writestr(extra, 'unexpected')
        return target

    def test_matching_manifest_and_content_pass(self):
        result = verify_package(self.package(), {'app.py': b'original'}, 'source')
        self.assertEqual(result['files'], 1)
        self.assertEqual(len(result['sha256']), 64)

    def test_changed_bytes_are_detected_even_when_manifest_is_unchanged(self):
        with self.assertRaisesRegex(PublicationError, 'package_content_mismatch'):
            verify_package(self.package(content=b'changed'), {'app.py': b'original'}, 'source')

    def test_self_consistent_but_wrong_source_digest_is_rejected(self):
        package = self.package(content=b'changed', manifest_content=b'changed')
        with self.assertRaisesRegex(PublicationError, 'package_manifest_mismatch'):
            verify_package(package, {'app.py': b'original'}, 'source')

    def test_unexpected_duplicate_and_traversal_members_are_rejected(self):
        for name in ('extra.txt', '../escape.txt', 'APP.py'):
            with self.subTest(name=name), self.assertRaisesRegex(PublicationError, 'unexpected_package_members'):
                verify_package(self.package(extra=name), {'app.py': b'original'}, 'source')

    def test_smoke_cannot_silently_skip_portable_checks(self):
        with self.assertRaisesRegex(PublicationError, 'smoke_requires_windows_and_portable'):
            check(self.root, smoke=True)


if __name__ == '__main__':
    unittest.main()
