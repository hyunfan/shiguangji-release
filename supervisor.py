"""On-demand crash supervision. Never terminate a live server or retry forever."""
import argparse
import json
import os
from pathlib import Path
import time
from shiguangji.core.atomic_io import atomic_write_text
from launcher import launch_lock, read_state, runtime, start_background
from shiguangji.system.process_reference import ProcessReference
from shiguangji.system.reliability import settings


def may_restart(root, port, session, pid):
    state = read_state(root, port) or {}
    return (state.get('state') == 'ready' and state.get('session') == session and state.get('pid') == pid
            and not (runtime(root)/f'stop-{port}-{session}').exists())


def recovery_run(root):
    if not settings(Path(root)/'collections')['resume_queue_after_crash']:return ''
    try:
        job = json.loads((Path(root)/'collections/.job-state.json').read_text(encoding='utf-8'))['job']
        return job.get('run_id','') if job.get('running') and not job.get('pause_requested') else ''
    except (OSError, ValueError, KeyError, TypeError):return ''


def guard_status(root, port):
    try:
        saved = json.loads((Path(root)/'runtime'/f'guard-{port}.json').read_text(encoding='utf-8'))
        state = read_state(root, port) or {}
        if saved.get('session') != state.get('session'):return {'state':'off'}
        if saved.get('state') == 'watching':
            reference = ProcessReference(saved['pid'])
            try:
                if not reference.alive():return {'state':'off'}
            finally:reference.close()
        return {k:saved.get(k) for k in ('state','message','restarts')}
    except (OSError, ValueError, KeyError, TypeError):return {'state':'off'}


def supervise(root, port, session, pid, *, sleep=time.sleep, clock=time.time, process=ProcessReference, start=start_background):
    root = Path(root).resolve()
    path = runtime(root)/f'guard-{port}.json'
    restarts = []
    def report(state, message=''):
        atomic_write_text(path, json.dumps({'state':state, 'message':message, 'pid':os.getpid(),
            'server_pid':pid, 'session':session, 'restarts':len(restarts), 'updated_at':clock()}, ensure_ascii=False))
    try:
        with launch_lock(root, f'guard-{port}', timeout=5):
            while may_restart(root, port, session, pid):
                reference = process(pid)
                try:
                    report('watching')
                    while reference.alive():
                        if not may_restart(root, port, session, pid):
                            report('stopped');return
                        sleep(1)
                finally:reference.close()
                if not may_restart(root, port, session, pid):break
                restarts = [t for t in restarts if clock()-t < 3600]
                if len(restarts) >= 3:
                    report('attention','一小时内已尝试重启 3 次，已停止自动重启；请查看后台日志。');return
                report('restarting','检测到后台进程异常退出，正在恢复网页服务。')
                for _ in range(2**(len(restarts)+1)):
                    sleep(1)
                    if not may_restart(root, port, session, pid):
                        report('stopped');return
                resume = recovery_run(root)
                restarts.append(clock())
                start(root, port, supervised=False, recovery_run=resume)
                state = read_state(root, port) or {}
                if state.get('state') != 'ready' or state.get('session') == session:
                    report('attention','重启后无法确认新的后台会话，已停止自动操作。');return
                session, pid = state['session'], state['pid']
            report('stopped')
    except RuntimeError as error:
        # A second launcher can race this one. Its guard already owns the lock.
        if 'Another launcher' not in str(error):report('attention',str(error)[:300])
    except Exception as error:
        report('attention',str(error)[:300])


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--pid', type=int, required=True)
    args=parser.parse_args()
    supervise(Path(__file__).resolve().parent,args.port,args.session,args.pid)
