"""Release installation/rollback with only temporary fake installations."""
import hashlib
import json
from pathlib import Path
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
