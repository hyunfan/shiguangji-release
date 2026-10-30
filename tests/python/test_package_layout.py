"""Package layout guards: entry scripts at the root, application modules in shiguangji/."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from shiguangji import APP_ROOT
from shiguangji.core import archive_support
from shiguangji.media import hls_recovery

ROOT = Path(__file__).resolve().parents[2]
# Scripts started by path (.cmd launchers, launcher/supervisor, release tooling) stay at the root.
ROOT_SCRIPTS = {'app', 'audit_collection', 'audit_publication', 'build_portable', 'build_release',
                'check_publication', 'deploy_release', 'launcher', 'run_tests', 'setup_ffmpeg',
                'supervisor', 'verify_portable', 'windows_integration'}
# Modules that re-launch themselves (or are documented CLIs) by file path.
SELF_SPAWNED = ('cloud_backup', 'cloud_media', 'backup_library', 'compact_library')


class PackageLayoutTests(unittest.TestCase):
    def test_root_keeps_only_entry_scripts(self):
        self.assertEqual({path.stem for path in ROOT.glob('*.py')}, ROOT_SCRIPTS)

    def test_application_root_is_unchanged_for_moved_modules(self):
        self.assertEqual(APP_ROOT, ROOT)
        self.assertEqual(archive_support.ROOT, ROOT)
        self.assertEqual(archive_support.DATA, ROOT / 'collections')
        self.assertEqual(hls_recovery.ROOT, ROOT)

    def test_release_whitelist_covers_every_package_module(self):
        whitelist = set(json.loads((ROOT / 'release_files.json').read_text(encoding='utf-8')))
        package = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'shiguangji').rglob('*.py')
                   if '__pycache__' not in path.parts}
        self.assertTrue(package)
        self.assertEqual(package - whitelist, set())

    def test_self_spawned_modules_run_by_file_path_without_pythonpath(self):
        env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
        with tempfile.TemporaryDirectory() as folder:
            for name in SELF_SPAWNED:
                with self.subTest(module=name):
                    script = ROOT / 'shiguangji' / 'storage' / (name + '.py')
                    result = subprocess.run([sys.executable, '-X', 'utf8', str(script), '--help'], cwd=folder,
                                            env=env, capture_output=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
