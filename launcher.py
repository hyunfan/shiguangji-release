#!/usr/bin/env python3
"""Start the local app outside the launcher's console; Python 3.10+, stdlib only."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid
import webbrowser
from urllib.request import build_opener, ProxyHandler
from atomic_io import atomic_write_text

ROOT = Path(__file__).resolve().parent


class BackgroundExitError(RuntimeError):
    """The child exited, unlike a slow process that may still be starting."""


def runtime(root=ROOT):
    folder = Path(root) / "runtime"
    folder.mkdir(exist_ok=True)
    return folder


def state_path(root, port):
    return runtime(root) / f"background-{port}.json"


def read_state(root, port):
    try:
        data = json.loads(state_path(root, port).read_text(encoding="utf-8"))
        if (isinstance(data, dict) and data.get("port") == port
                and data.get("root") == str(Path(root).resolve())
                and re.fullmatch(r"[a-f0-9]{32}", str(data.get("session", "")))):
            return data
    except (OSError, ValueError):
        pass
    return None


def write_state(root, port, data):
    path = state_path(root, port)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False))


def active_port(root):
    try:
        data = json.loads((runtime(root) / "active-port.json").read_text(encoding="utf-8"))
        port = data.get("port")
        if (type(port) is int and 1 <= port <= 65535
                and data.get("root") == str(Path(root).resolve())):
            return port
    except (OSError, ValueError, AttributeError):
        pass
    return 8765


def remember_port(root, port):
    path = runtime(root) / "active-port.json"
    atomic_write_text(path, json.dumps({"root": str(Path(root).resolve()), "port": port}))


def bind_error(port):
    """Check availability without reuse options, matching the Windows server bind."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(("127.0.0.1", port))
    except OSError as error:
        return error
    return None


def start_automatic(root=ROOT):
    """Default launches follow the saved port; fallback only after a confirmed bind failure."""
    with launch_lock(root, "auto"):
        port = active_port(root)
        try:
            health = start_background(root, port)
        except BackgroundExitError:
            # A timeout may mean a live server is still initializing: never fallback on it.
            reason = bind_error(port) if port == 8765 else None
            if reason is None:
                raise
            print(f"Port 8765 cannot be used ({reason}). Trying http://127.0.0.1:8766")
            port = 8766
            health = start_background(root, port)
        if isinstance(health, dict):
            expected = os.path.normcase(str(Path(root).resolve() / "collections"))
            actual = health.get("data_dir")
            if actual and os.path.normcase(str(actual)) != expected:
                raise RuntimeError(f"Port {port} is serving another library folder: {actual}. No collection was moved or changed.")
            remember_port(root, port)
        return port


def ping(port):
    """Use loopback directly, never the system HTTP proxy."""
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=1) as response:
            value = json.loads(response.read(1024 * 1024))
        return value if isinstance(value, dict) and value.get("app") == "x-wayback-library" else None
    except (OSError, ValueError):
        return None


@contextmanager
def launch_lock(root, port, timeout=45):
    """A kernel lock survives stale lock files and serializes double clicks."""
    path = runtime(root) / f"launcher-{port}.lock"
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Another launcher is still starting. See runtime/launcher.log.")
                time.sleep(0.15)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_UN)


def spawn_server(root, port, session, recovery_run=''):
    executable = Path(sys.executable)
    if os.name == "nt":
        windowless = executable.with_name("pythonw.exe")
        if windowless.is_file():
            executable = windowless
    command = [str(executable), "-X", "utf8", "-u", str(Path(root) / "app.py"),
               "--port", str(port), "--background-token", session]
    if recovery_run:command += ['--recover-queue-run', recovery_run]
    options = {"cwd": str(root), "stdin": subprocess.DEVNULL, "close_fds": True,
               "env": dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        options["startupinfo"] = startup
    else:
        options["start_new_session"] = True
    log_path = runtime(root) / f"server-{port}.log"
    from storage_guard import rotate_log
    rotate_log(log_path)
    with log_path.open("ab", buffering=0) as log:
        log.write(f"\n--- Launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n".encode("utf-8"))
        return subprocess.Popen(command, stdout=log, stderr=log, **options)


def ensure_guard(root, port):
    state = read_state(root, port) or {}
    script = Path(root)/'supervisor.py'
    if state.get('state') != 'ready' or not script.is_file():return
    from supervisor import guard_status
    if guard_status(root,port).get('state') == 'watching':return
    executable = Path(sys.executable)
    options = {'cwd':str(root), 'stdin':subprocess.DEVNULL, 'stdout':subprocess.DEVNULL, 'stderr':subprocess.DEVNULL, 'close_fds':True}
    if os.name == 'nt':
        if executable.with_name('pythonw.exe').is_file():executable=executable.with_name('pythonw.exe')
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW;startup.wShowWindow=subprocess.SW_HIDE
        options['startupinfo']=startup
    else:options['start_new_session']=True
    subprocess.Popen([str(executable),'-X','utf8',str(script),'--port',str(port),'--session',state['session'],'--pid',str(state['pid'])],**options)


def start_background(root=ROOT, port=8765, timeout=35, *, supervised=True, recovery_run=''):
    root = Path(root).resolve()
    if not (root / "app.py").is_file():
        raise RuntimeError("app.py was not found. Extract the entire ZIP before starting.")
    with launch_lock(root, port):
        existing = ping(port)
        if existing:
            if supervised:ensure_guard(root, port)
            print(f"X Wayback Library is already running: http://127.0.0.1:{port}")
            if existing.get("data_dir"):
                print(f"Existing library folder: {existing['data_dir']}")
            return existing
        session = uuid.uuid4().hex
        child = spawn_server(root, port, session, recovery_run) if recovery_run else spawn_server(root, port, session)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = read_state(root, port)
            if (state and state.get("session") == session and state.get("pid") == child.pid
                    and state.get("state") == "ready"):
                health = ping(port)
                if health:
                    if supervised:ensure_guard(root, port)
                    print(f"Running in background: http://127.0.0.1:{port}")
                    print("You may close this window and the browser. Collection continues in the background.")
                    return health
            code = child.poll()
            if code is not None:
                # A separate copy may have won the port race. Reuse it only if it identifies itself.
                if code == 0:
                    existing = ping(port)
                    if existing:
                        return existing
                raise BackgroundExitError(f"The background server exited with code {code}. See {runtime(root) / f'server-{port}.log'}")
            time.sleep(0.15)
        # Do not kill the process: it may be healthy but temporarily slow to initialize.
        raise RuntimeError(f"Startup is taking longer than expected. Try the local address or inspect {runtime(root) / f'server-{port}.log'}")


def stop_background(root=ROOT, port=8765, timeout=40):
    """Ask only this installation's session to save and stop; never kill a PID."""
    with launch_lock(root, port):
        state = read_state(root, port)
        if not state or state.get("state") != "ready":
            print("No background session managed by this installation is running.")
            return True
        health = ping(port)
        if not health:
            print("The background server is not responding; inspect its log before trying again.")
            return False
        expected_data = str((Path(root).resolve() / "collections").resolve())
        if os.path.normcase(str(health.get("data_dir", ""))) != os.path.normcase(expected_data):
            raise RuntimeError("This port belongs to another library folder. No stop request was sent.")
        marker = runtime(root) / f"stop-{port}-{state['session']}"
        marker.touch()
        print("Stop requested. Any running collection will save its progress before the server exits.")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = read_state(root, port)
            if current and current.get("session") == state["session"] and current.get("state") == "stopped":
                print("Background server stopped. Saved collections are unchanged.")
                return True
            time.sleep(0.2)
        print("The stop request remains pending while the current request/export finishes. See the server log.")
        return True


def background_ready(root, port, session):
    if not re.fullmatch(r"[a-f0-9]{32}", session):
        raise ValueError("Invalid background session token")
    write_state(root, port, {"pid": os.getpid(), "port": port, "root": str(Path(root).resolve()),
                            "session": session, "state": "ready", "started_at": time.time()})


def monitor_stop(root, port, session, server, stop_event, job, lock, progress, stopping):
    """Control thread: wait for collection/export to finish before stopping the HTTP loop."""
    marker = runtime(root) / f"stop-{port}-{session}"
    while not marker.exists():
        time.sleep(0.25)
    with lock:
        stopping.set()
        if job.get("running"):
            stop_event.set()
            progress(pause_requested=True, message="后台退出请求已收到，正在保存进度；下次启动可继续。")
    while True:
        with lock:
            if not job.get("running"):
                break
        time.sleep(0.25)
    print("Background stop request completed.", flush=True)
    server.shutdown()


def background_stopped(root, port, session):
    state = read_state(root, port)
    if state and state.get("session") == session:
        state.update(state="stopped", stopped_at=time.time())
        write_state(root, port, state)
    (runtime(root) / f"stop-{port}-{session}").unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Launch X Wayback Library in the background")
    parser.add_argument("--port", type=int, default=None, help="Use exactly this port; omitted uses the saved port, with 8765/8766 fallback")
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser")
    parser.add_argument("--stop", action="store_true", help="Save active work and stop this background server")
    args = parser.parse_args(argv)
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        if args.stop:
            with launch_lock(ROOT, "auto"):
                return 0 if stop_background(ROOT, args.port if args.port is not None else active_port(ROOT)) else 1
        if args.port is None:
            port = start_automatic(ROOT)
        else:
            port = args.port
            health = start_background(ROOT, port)
            if isinstance(health, dict):
                actual = health.get("data_dir")
                if actual and os.path.normcase(str(actual)) == os.path.normcase(str(ROOT.resolve() / "collections")):
                    remember_port(ROOT, port)
        if not args.no_open:
            try:
                opened = webbrowser.open(f"http://127.0.0.1:{port}")
            except (OSError, webbrowser.Error):
                opened = False
            if not opened:
                print(f"Open this address manually: http://127.0.0.1:{port}")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        message = f"Startup/control failed: {error}"
        print(message)
        try:
            with (runtime(ROOT) / "launcher.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
