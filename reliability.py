"""Small installation-local preferences and read-only worker health indicators."""
import json
from pathlib import Path
from atomic_io import atomic_write_text

DEFAULTS = {'backup_after_task': True, 'resume_queue_after_crash': False}


def settings(base):
    try:
        saved = json.loads((Path(base)/'.reliability-settings.json').read_text(encoding='utf-8'))
        return {k:saved[k] if type(saved.get(k)) is bool else v for k,v in DEFAULTS.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(DEFAULTS)


def save_settings(base, values):
    if set(values)-set(DEFAULTS) or any(type(v) is not bool for v in values.values()):
        raise ValueError('稳定性设置只接受指定的开关值。')
    result = {**settings(base), **values}
    atomic_write_text(Path(base)/'.reliability-settings.json', json.dumps(result))
    return result


def worker_health(job, current, activity, progress):
    active = bool(job.get('running'))
    quiet, unchanged = max(0, int(current-activity)), max(0, int(current-progress))
    saving = job.get('stage') in {'export','zip','backup'}
    state = 'idle' if not active else 'saving' if saving else 'slow' if unchanged >= 600 else 'active'
    message = ('后台在线，当前没有采集任务。' if not active else
               '正在保存、打包或校验备份，请等待；守护不会因耗时而重启。' if saving else
               '已超过 10 分钟没有新增内容或解析进展，请查看记录；这不等于进程卡死。' if state == 'slow' else
               '后台在线，任务正在处理。')
    return {'state':state, 'activity_seconds_ago':quiet, 'progress_seconds_ago':unchanged, 'message':message}
