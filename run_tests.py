"""Run Python integration/unit and frontend contract tests from one entry."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    python_tests = ROOT/'tests/python'
    frontend = ROOT/'tests/frontend'
    env = {**os.environ,'PYTHONPATH':str(ROOT),'PYTHONUTF8':'1'}
    commands = [[sys.executable,'-X','utf8','-m','unittest','discover','-s',str(python_tests),'-p','test_*.py','-v']]
    node = shutil.which('node')
    if not node:
        print('Frontend tests require Node.js; cannot report a complete test run.');return 2
    commands += [[node,str(frontend/name)] for name in ('test_app_ui.js','test_gallery.js')]
    for command in commands:
        result = subprocess.run(command,cwd=ROOT,env=env)
        if result.returncode:return result.returncode
    return 0


if __name__=='__main__':raise SystemExit(main())
