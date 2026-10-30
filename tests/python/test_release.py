"""Release installation/rollback with only temporary fake installations."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import deploy_release
from shiguangji.core.atomic_io import atomic_write_bytes


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.target=self.root/'installed';self.target.mkdir()
        for name in ('app.py','launcher.py'):(self.target/name).write_bytes(b'old')
        (self.target/'collections').mkdir();(self.target/'collections/keep.txt').write_bytes(b'untouched')
        self.package=self.root/'update.zip'

    def package_files(self,files):
        with zipfile.ZipFile(self.package,'w') as package:
            for name,data in files.items():package.writestr(name,data)
            package.writestr('release.json',json.dumps({'version':'fixture','files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}))

    def run_deploy(self):
        with patch.object(deploy_release,'health',return_value=None),patch.object(deploy_release.socket,'create_connection',side_effect=OSError('closed')):
            deploy_release.deploy(self.package,self.target,self.root/'backups',8766)

    def test_idle_upgrade_backs_up_code_and_does_not_start_service(self):
        self.package_files({'app.py':b'new','added.py':b'new module'})
        with patch.object(deploy_release.subprocess,'run') as run:
            self.run_deploy();run.assert_not_called()
        self.assertEqual((self.target/'app.py').read_bytes(),b'new')
        self.assertEqual((self.target/'collections/keep.txt').read_bytes(),b'untouched')
        with zipfile.ZipFile(next((self.root/'backups').glob('*.zip'))) as saved:
            self.assertEqual(saved.read('app.py'),b'old')
            self.assertIn('added.py',json.loads(saved.read('rollback-files.json'))['added'])

    def test_interrupted_copy_rolls_back_and_preserves_collections(self):
        self.package_files({'app.py':b'new','added.py':b'module','launcher.py':b'new launcher'})
        failed=False
        def write(path,data):
            nonlocal failed
            if path.name=='launcher.py' and not failed:
                failed=True;raise OSError('fixture write failure')
            atomic_write_bytes(path,data)
        with patch.object(deploy_release,'atomic_write_bytes',side_effect=write):
            with self.assertRaises(OSError):self.run_deploy()
        self.assertEqual((self.target/'app.py').read_bytes(),b'old')
        self.assertFalse((self.target/'added.py').exists())
        self.assertEqual((self.target/'collections/keep.txt').read_bytes(),b'untouched')

    def test_data_and_escape_paths_are_rejected_before_backup_or_stop(self):
        for name in ('collections/keep.txt','../app.py','C:/file','static/../../escape.py'):
            with self.subTest(name=name):
                self.package_files({name:b'bad'})
                with self.assertRaises(ValueError):self.run_deploy()
        self.assertFalse((self.root/'backups').exists())

    def test_unresponsive_service_or_running_job_prevents_upgrade(self):
        self.package_files({'app.py':b'new'})
        state={'app':'x-wayback-library','data_dir':str(self.target/'collections'),'job':{'running':True}}
        with patch.object(deploy_release,'health',return_value=state):
            with self.assertRaises(ValueError):deploy_release.deploy(self.package,self.target,self.root/'backups',8766)
        with patch.object(deploy_release,'health',return_value=None),patch.object(deploy_release.socket,'create_connection'):
            with self.assertRaises(RuntimeError):deploy_release.deploy(self.package,self.target,self.root/'backups',8766)
        self.assertFalse((self.root/'backups').exists())

    def test_running_storage_operation_prevents_upgrade_before_any_write(self):
        self.package_files({'app.py':b'new'})
        state={'app':'x-wayback-library','data_dir':str(self.target/'collections'),
               'job':{'running':False},'storage':{'running':True}}
        with patch.object(deploy_release,'health',return_value=state),patch.object(deploy_release.subprocess,'run') as stop:
            with self.assertRaisesRegex(ValueError,'Space management'):
                deploy_release.deploy(self.package,self.target,self.root/'backups',8766)
            stop.assert_not_called()
        self.assertFalse((self.root/'backups').exists())
        self.assertEqual((self.target/'app.py').read_bytes(),b'old')

    def install_previous_release(self,files):
        for name,data in files.items():
            path=self.target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        (self.target/'release.json').write_text(json.dumps({'version':'1.28.2','files':{name:hashlib.sha256(data).hexdigest() for name,data in files.items()}}))

    def protected_fixture(self):
        protected={'collections/alice/manifest.json':b'user data','runtime/http-8766.log':b'log',
                   'tools/ffmpeg/ffmpeg.exe':b'ffmpeg','python/python.exe':b'runtime','diagnostics/x.txt':b'diag',
                   'config.json':b'settings','notes.py':b'user script'}
        for name,data in protected.items():
            path=self.target/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
        return protected

    def test_upgrade_removes_files_dropped_from_installed_manifest_and_keeps_user_data(self):
        old={'app.py':b'old','launcher.py':b'old','archive.py':b'old archive','extractor.py':b'old extractor',
             'static/old.js':b'old js','python/python.exe':b'runtime','portable.json':b'{}','README.md':b'docs'}
        self.install_previous_release(old)
        protected=self.protected_fixture()
        self.package_files({'app.py':b'new','launcher.py':b'new','shiguangji/__init__.py':b'',
                            'shiguangji/collect/archive.py':b'new archive'})
        self.run_deploy()
        for name in ('archive.py','extractor.py','static/old.js'):
            self.assertFalse((self.target/name).exists(),name)
        # Listed but not program code: the embedded runtime, its marker and documentation stay.
        for name in ('python/python.exe','portable.json','README.md'):
            self.assertTrue((self.target/name).exists(),name)
        for name,data in protected.items():
            self.assertEqual((self.target/name).read_bytes(),data,name)
        self.assertEqual((self.target/'collections/keep.txt').read_bytes(),b'untouched')
        self.assertEqual((self.target/'shiguangji/collect/archive.py').read_bytes(),b'new archive')
        with zipfile.ZipFile(next((self.root/'backups').glob('*.zip'))) as saved:
            rollback=json.loads(saved.read('rollback-files.json'))
            self.assertEqual(sorted(rollback['removed']),['archive.py','extractor.py','static/old.js'])
            self.assertEqual(saved.read('archive.py'),b'old archive')
            self.assertEqual(saved.read('static/old.js'),b'old js')
        self.assertIn('shiguangji/__init__.py',json.loads((self.target/'release.json').read_text())['files'])

    def test_install_without_manifest_removes_only_legacy_root_modules(self):
        for name in ('archive.py','task_service.py','atomic_io.py'):(self.target/name).write_bytes(b'legacy '+name.encode())
        protected=self.protected_fixture()
        self.package_files({'app.py':b'new','launcher.py':b'new','shiguangji/__init__.py':b''})
        self.run_deploy()
        for name in ('archive.py','task_service.py','atomic_io.py'):
            self.assertFalse((self.target/name).exists(),name)
        for name,data in protected.items():
            self.assertEqual((self.target/name).read_bytes(),data,name)
        self.assertEqual((self.target/'app.py').read_bytes(),b'new')

    def test_legacy_cleanup_requires_package_layout_in_new_release(self):
        (self.target/'archive.py').write_bytes(b'legacy')
        self.package_files({'app.py':b'new'})
        self.run_deploy()
        self.assertEqual((self.target/'archive.py').read_bytes(),b'legacy')

    def test_failed_upgrade_restores_removed_files(self):
        old={'app.py':b'old','launcher.py':b'old','archive.py':b'old archive','static/old.js':b'old js'}
        self.install_previous_release(old)
        previous_manifest=(self.target/'release.json').read_bytes()
        protected=self.protected_fixture()
        self.package_files({'app.py':b'new','launcher.py':b'new','shiguangji/__init__.py':b''})
        state={'app':'x-wayback-library','data_dir':str(self.target/'collections'),'job':{'running':False}}
        healthy=[state,None]  # running before upgrade, stopped for replacement
        def health(port):
            return healthy.pop(0) if healthy else None
        with patch.object(deploy_release,'health',side_effect=health),patch.object(deploy_release.subprocess,'run'):
            with self.assertRaisesRegex(RuntimeError,'health check'):
                deploy_release.deploy(self.package,self.target,self.root/'backups',8766)
        self.assertEqual((self.target/'archive.py').read_bytes(),b'old archive')
        self.assertEqual((self.target/'static/old.js').read_bytes(),b'old js')
        self.assertEqual((self.target/'app.py').read_bytes(),b'old')
        self.assertEqual((self.target/'release.json').read_bytes(),previous_manifest)
        self.assertFalse((self.target/'shiguangji/__init__.py').exists())
        for name,data in protected.items():
            self.assertEqual((self.target/name).read_bytes(),data,name)

    def test_cleanup_ignores_unsafe_or_linked_manifest_entries(self):
        outside=self.root/'outside.py';outside.write_bytes(b'outside')
        (self.target/'release.json').write_text(json.dumps({'version':'old','files':{
            '../outside.py':'x','collections/keep.txt':'x','C:/x.py':'x','runtime/a.py':'x','tools/t.py':'x'}}))
        (self.target/'runtime').mkdir();(self.target/'runtime/a.py').write_bytes(b'keep')
        (self.target/'tools').mkdir();(self.target/'tools/t.py').write_bytes(b'keep')
        self.assertEqual(deploy_release.stale_program_files(self.target,{'app.py':b'','shiguangji/__init__.py':b''}),[])
        self.package_files({'app.py':b'new','shiguangji/__init__.py':b''})
        self.run_deploy()
        self.assertEqual(outside.read_bytes(),b'outside')
        self.assertEqual((self.target/'runtime/a.py').read_bytes(),b'keep')
        self.assertEqual((self.target/'tools/t.py').read_bytes(),b'keep')
        self.assertEqual((self.target/'collections/keep.txt').read_bytes(),b'untouched')

    def test_legacy_module_list_matches_package_layout(self):
        root=Path(deploy_release.__file__).resolve().parent
        shipped={PurePosixPath(name).name for name in json.loads((root/'release_files.json').read_text(encoding='utf-8'))
                 if name.startswith('shiguangji/')}
        self.assertEqual(len(deploy_release.LEGACY_ROOT_MODULES),52)
        self.assertEqual(set(deploy_release.LEGACY_ROOT_MODULES)-shipped,set())
        self.assertFalse({path.name for path in root.glob('*.py')}&deploy_release.LEGACY_ROOT_MODULES)
