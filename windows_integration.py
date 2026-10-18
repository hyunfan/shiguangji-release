"""Per-user Windows entry points. Never register login startup."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import webbrowser

try:
    import winreg
except ImportError:
    winreg = None

ROOT = Path(__file__).resolve().parent
SCHEME = "x-wayback-library"
OPEN_URI = SCHEME + "://open"
REGISTRY_PATH = "Software\\Classes\\" + SCHEME
SHORTCUT_NAME = "时光集.lnk"
STOP_SHORTCUT_NAME = "停止时光集.lnk"
DESCRIPTION = "X Wayback Library managed launcher"


def _same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def pythonw_path(root=ROOT):
    root = Path(root).resolve()
    bundled = (root / 'portable.json').exists() or (root / 'python').exists()
    executable = root / 'python/pythonw.exe' if bundled else Path(sys.executable).resolve().with_name("pythonw.exe")
    if not executable.is_file():
        raise RuntimeError("未找到 pythonw.exe。便携版请重新完整解压；源码版请安装完整的 Python 后重新创建快捷方式。")
    return executable


def launcher_arguments(root, *arguments):
    return subprocess.list2cmdline(["-X", "utf8", str(Path(root).resolve() / "desktop_launcher.pyw"), *arguments])


def protocol_command(root):
    # The URI is one quoted argument; it is strictly checked before any action.
    return subprocess.list2cmdline([str(pythonw_path(root))]) + " " + launcher_arguments(root) + ' --protocol "%1"'


def _expected_protocol(root):
    return {"name": "URL:X Wayback Library", "url_protocol": "", "owner": str(Path(root).resolve()),
            "command": protocol_command(root), "delegate": None, "verb": "open"}


def _registry_value(key, name):
    try:
        value, kind = winreg.QueryValueEx(key, name)
        return value if kind == winreg.REG_SZ else None
    except FileNotFoundError:
        return None


def _read_protocol():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
            result = {"name": _registry_value(key, ""), "url_protocol": _registry_value(key, "URL Protocol"),
                      "owner": _registry_value(key, "XWaybackLibraryRoot")}
    except FileNotFoundError:
        return None
    for path, fields in (("shell", {"verb": ""}),
                         ("shell\\open\\command", {"command": "", "delegate": "DelegateExecute"})):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\" + path) as key:
                result.update({field: _registry_value(key, name) for field, name in fields.items()})
        except FileNotFoundError:
            result.update({field: None for field in fields})
    return result


def protocol_available(root=ROOT):
    """Only advertise a link when it launches this exact installation."""
    if os.name != "nt" or winreg is None:
        return False
    try:
        return _read_protocol() == _expected_protocol(root)
    except (OSError, RuntimeError, ValueError):
        return False


def _write_protocol(expected):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH, 0, winreg.KEY_WRITE) as key:
        for name, value in (("", expected["name"]), ("URL Protocol", ""),
                            ("XWaybackLibraryRoot", expected["owner"])):
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\shell", 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "open")
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + "\\shell\\open\\command", 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, expected["command"])


def special_folder(name):
    ids = {"desktop": 0x10, "startup": 0x07}
    buffer = ctypes.create_unicode_buffer(32768)
    shell = ctypes.WinDLL("shell32", use_last_error=True)
    function = shell.SHGetFolderPathW
    function.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p]
    function.restype = ctypes.c_long
    result = function(None, ids[name], None, 0, buffer)
    if result != 0 or not Path(buffer.value).is_dir():
        raise OSError(f"无法读取 Windows {name} 文件夹（{result}）。")
    return Path(buffer.value)


_SHORTCUT_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$payload = [Console]::In.ReadToEnd() | ConvertFrom-Json
$shell = New-Object -ComObject WScript.Shell
if ($payload.operation -eq 'save') {
    $link = $shell.CreateShortcut([string]$payload.path)
    $link.TargetPath = [string]$payload.values.target
    $link.Arguments = [string]$payload.values.arguments
    $link.WorkingDirectory = [string]$payload.values.cwd
    $link.Description = [string]$payload.values.description
    $link.WindowStyle = 7
    $link.Save()
}
if (-not (Test-Path -LiteralPath ([string]$payload.path) -PathType Leaf)) {
    throw 'Shortcut file was not created.'
}
$saved = $shell.CreateShortcut([string]$payload.path)
@{target=$saved.TargetPath; arguments=$saved.Arguments; cwd=$saved.WorkingDirectory;
  description=$saved.Description} | ConvertTo-Json -Compress
'''


def _shortcut(operation, path, values=None):
    # All variable data travels as JSON over stdin, never as PowerShell source.
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SHORTCUT_SCRIPT],
                            input=json.dumps({"operation": operation, "path": str(path), "values": values}, ensure_ascii=False),
                            capture_output=True, encoding="utf-8", errors="replace", timeout=25,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("创建或检查快捷方式失败：" + (result.stderr.strip() or result.stdout.strip()))
    try:
        result = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(result, dict):
            raise ValueError("not an object")
        return result
    except ValueError as error:
        raise RuntimeError("无法确认 Windows 快捷方式是否正确保存。") from error


def _shortcut_values(root, stop=False):
    root = Path(root).resolve()
    return {"target": str(pythonw_path(root)), "arguments": launcher_arguments(root, *(["--stop"] if stop else [])),
            "cwd": str(root), "description": DESCRIPTION + " | " + str(root)}


def _matching_shortcut(actual, expected):
    return (isinstance(actual, dict) and actual.get("arguments") == expected["arguments"]
            and actual.get("description") == expected["description"]
            and bool(actual.get("target")) and _same_path(actual["target"], expected["target"])
            and bool(actual.get("cwd")) and _same_path(actual["cwd"], expected["cwd"]))


def _check_shortcut(path, values):
    if path.exists() and not _matching_shortcut(_shortcut("read", path), values):
        raise RuntimeError(f"{path} 已存在且不属于本安装目录，没有覆盖它。")


def install(root=ROOT):
    """Register desktop start/stop entries for this user; leave login startup untouched."""
    if os.name != "nt" or winreg is None:
        raise RuntimeError("桌面入口仅支持 Windows。")
    root = Path(root).resolve()
    for name in ("desktop_launcher.pyw", "launcher.py", "app.py", "windows_integration.py"):
        if not (root / name).is_file():
            raise RuntimeError(f"缺少 {name}，请先完整解压工具。")
    expected = _expected_protocol(root)
    current = _read_protocol()
    if current is not None and current != expected:
        raise RuntimeError("启动协议已由另一个安装目录或程序注册，没有覆盖它。")
    desktop = special_folder("desktop")
    entries = [(desktop / SHORTCUT_NAME, _shortcut_values(root), "desktop"),
               (desktop / STOP_SHORTCUT_NAME, _shortcut_values(root, stop=True), "stop")]
    # Check ownership of every destination before modifying any of them.
    for path, values, _ in entries:
        _check_shortcut(path, values)
    output = {"autostart": "unchanged"}
    for path, values, kind in entries:
        saved = _shortcut("save", path, values)
        if not path.is_file() or not _matching_shortcut(saved, values):
            raise RuntimeError(f"快捷方式保存后校验失败：{path}")
        output[kind] = str(path)
    _write_protocol(expected)
    if _read_protocol() != expected:
        raise RuntimeError("启动协议写入后校验失败。")
    output["protocol"] = OPEN_URI
    return output


def _message_error(message):
    if os.name == "nt":
        function = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
        function.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        function(None, message, "时光集启动提示", 0x10)
    elif sys.stderr is not None:
        print(message, file=sys.stderr)


def _launch_action(argv):
    if not argv:
        return "open"
    if argv == ["--no-open"]:
        return "start"
    if argv == ["--stop"]:
        return "stop"
    if len(argv) == 2 and argv[0] == "--protocol" and argv[1] in (OPEN_URI, OPEN_URI + "/"):
        return "open"
    raise ValueError("启动参数无效。请使用桌面的“时光集”快捷方式；网页启动仅接受固定的 open 链接。")


def desktop_main(argv=None, root=ROOT):
    """A pythonw-safe entry point with a visible error and a persistent log."""
    root = Path(root).resolve()
    argv = list(sys.argv[1:] if argv is None else argv)
    log_path = root / "runtime" / "desktop-launcher.log"
    try:
        log_path.parent.mkdir(exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
            try:
                action = _launch_action(argv)
                print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {action} ---")
                import launcher
                if action == "stop":
                    with launcher.launch_lock(root, "auto"):
                        if not launcher.stop_background(root, launcher.active_port(root)):
                            raise RuntimeError("后台未能正常停止，请查看服务器日志。")
                    return 0
                port = launcher.start_automatic(root)
                health = launcher.ping(port)
                if not health or not health.get("data_dir") or not _same_path(health["data_dir"], root / "collections"):
                    raise RuntimeError("后台状态暂时无法确认，请稍后重试。")
                if action == "open":
                    url = f"http://127.0.0.1:{port}/"
                    if not webbrowser.open(url):
                        raise RuntimeError(f"后台已启动，但无法自动打开浏览器。请手动打开：{url}")
                return 0
            except Exception:
                traceback.print_exc()
                raise
    except Exception as error:
        _message_error(f"{error}\n\n日志：{log_path}")
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="创建时光集桌面启动/停止入口，不设置登录启动。")
    parser.add_argument("--install", action="store_true", help="创建桌面快捷方式和网页启动协议")
    args = parser.parse_args(argv)
    if not args.install:
        parser.print_help()
        return 2
    try:
        print(json.dumps(install(), ensure_ascii=False))
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
