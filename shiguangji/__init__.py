"""时光集应用代码包；入口脚本保留在仓库根目录。"""
from pathlib import Path

# 应用根目录（含 app.py、static/、collections/、tools/），供包内模块定位数据与组件。
APP_ROOT = Path(__file__).resolve().parent.parent
