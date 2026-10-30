"""Package layout guards: entry scripts at the root, application modules in shiguangji/."""
import ast
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

# Dependency direction: core -> platforms (pure parsing) -> sources/media/storage (one tier, may
# reference each other) -> collect -> tasks -> root entry scripts (app.py, ...). A package may only
# import packages of the same or a lower tier. system depends on core only and may be used by any
# tier. Package modules never import root scripts.
TIERS = {'core': 0, 'platforms': 1, 'sources': 2, 'media': 2, 'storage': 2, 'collect': 3, 'tasks': 4, 'web': 5}
# Known violations that are not yet safe to fix, as (module file, imported module).
# The list is empty and may only shrink: any new violation fails the layout test. Fix the
# dependency (move shared code down a tier, as core.stored_text, core.task_summary and
# system.process_reference did) instead of adding an entry here.
KNOWN_VIOLATIONS = frozenset()
MAX_KNOWN_VIOLATIONS = 0


def imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith('shiguangji.') and node.module.count('.') == 1:
                # from shiguangji.storage import compressed_files -> the submodule itself
                for alias in node.names:
                    yield node.module + '.' + alias.name
            else:
                yield node.module


def dependency_violations(root=None):
    root = root or ROOT
    scripts = {path.stem for path in root.glob('*.py')}
    found = set()
    for path in sorted((root / 'shiguangji').rglob('*.py')):
        parts = path.relative_to(root / 'shiguangji').parts
        if len(parts) < 2 or '__pycache__' in parts:
            continue
        source = parts[0]
        name = path.relative_to(root).as_posix()
        for module in imported_modules(ast.parse(path.read_text(encoding='utf-8'))):
            top = module.split('.')
            if top[0] in scripts:
                found.add((name, module))
                continue
            # Imports of the shiguangji package root itself (APP_ROOT) are allowed everywhere.
            if top[0] != 'shiguangji' or len(top) < 2 or top[1] == source or top[1] not in {*TIERS, 'system'}:
                continue
            target = top[1]
            if source == 'system':
                allowed = target == 'core'
            elif target == 'system':
                allowed = True
            else:
                allowed = TIERS.get(target, 99) <= TIERS[source]
            if not allowed:
                found.add((name, '.'.join(top[:3])))
    return found


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


    def test_every_package_has_a_dependency_tier(self):
        packages = {path.name for path in (ROOT / 'shiguangji').iterdir()
                    if path.is_dir() and (path / '__init__.py').is_file()}
        self.assertEqual(packages, set(TIERS) | {'system'})

    def test_package_dependencies_point_downwards(self):
        found = dependency_violations()
        self.assertEqual(found - KNOWN_VIOLATIONS, set(), 'new upward dependency; follow ARCHITECTURE.md')
        self.assertEqual(KNOWN_VIOLATIONS - found, set(), 'violation fixed; remove it from KNOWN_VIOLATIONS')
        self.assertLessEqual(len(KNOWN_VIOLATIONS), MAX_KNOWN_VIOLATIONS, 'the known-violation list may only shrink')

    def test_core_depends_on_nothing_above_it(self):
        self.assertFalse({item for item in dependency_violations() if item[0].startswith('shiguangji/core/')})

    def test_guard_detects_lazy_and_package_imports(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for package in (*TIERS, 'system'):
                (root / 'shiguangji' / package).mkdir(parents=True)
                (root / 'shiguangji' / package / '__init__.py').write_text('')
            (root / 'app.py').write_text('')
            (root / 'shiguangji' / 'core' / 'bad.py').write_text(
                'def later():\n    from shiguangji.platforms.extractor import original_url\n')
            (root / 'shiguangji' / 'platforms' / 'bad.py').write_text('from shiguangji.storage import compressed_files\n')
            (root / 'shiguangji' / 'system' / 'bad.py').write_text('import shiguangji.tasks.task_queue\n')
            (root / 'shiguangji' / 'media' / 'ok.py').write_text('from shiguangji.sources import source_http\nfrom shiguangji.system import reliability\n')
            (root / 'shiguangji' / 'tasks' / 'bad.py').write_text('import app\n')
            # The three shapes that used to be whitelisted: lazy imports of a higher tier or a root script.
            (root / 'supervisor.py').write_text('')
            (root / 'shiguangji' / 'platforms' / 'lazy.py').write_text(
                'def scan():\n    from shiguangji.storage.compressed_files import read_text\n')
            (root / 'shiguangji' / 'storage' / 'lazy.py').write_text(
                'def export():\n    from shiguangji.tasks.task_status import summary\n'
                'def watch():\n    from supervisor import ProcessReference\n')
            self.assertEqual(dependency_violations(root), {
                ('shiguangji/core/bad.py', 'shiguangji.platforms.extractor'),
                ('shiguangji/platforms/bad.py', 'shiguangji.storage.compressed_files'),
                ('shiguangji/system/bad.py', 'shiguangji.tasks.task_queue'),
                ('shiguangji/tasks/bad.py', 'app'),
                ('shiguangji/platforms/lazy.py', 'shiguangji.storage.compressed_files'),
                ('shiguangji/storage/lazy.py', 'shiguangji.tasks.task_status'),
                ('shiguangji/storage/lazy.py', 'supervisor')})

    def test_empty_whitelist_still_rejects_a_new_violation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for package in (*TIERS, 'system'):
                (root / 'shiguangji' / package).mkdir(parents=True)
            (root / 'shiguangji' / 'core' / 'clean.py').write_text('import json\n')
            self.assertEqual(dependency_violations(root) - KNOWN_VIOLATIONS, set())
            (root / 'shiguangji' / 'storage' / 'late.py').write_text(
                'def later():\n    from shiguangji.web import server\n')
            self.assertEqual(dependency_violations(root) - KNOWN_VIOLATIONS,
                             {('shiguangji/storage/late.py', 'shiguangji.web.server')})


if __name__ == '__main__':
    unittest.main()
