"""Windowless desktop/URI entry point; report even a damaged installation."""
from pathlib import Path
import sys

try:
    from windows_integration import desktop_main
except Exception as error:
    import ctypes
    import traceback
    root = Path(__file__).resolve().parent
    log = root / "runtime" / "desktop-launcher.log"
    try:
        log.parent.mkdir(exist_ok=True)
        with log.open("a", encoding="utf-8") as output:
            traceback.print_exc(file=output)
    except OSError:
        pass
    ctypes.windll.user32.MessageBoxW(None, f"启动组件无法加载：{error}\n请完整解压工具。\n\n日志：{log}", "时光集启动提示", 0x10)
    raise SystemExit(1)

raise SystemExit(desktop_main(sys.argv[1:], Path(__file__).resolve().parent))
