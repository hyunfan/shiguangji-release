"""Durable FIFO queue. The app's single lock owns all queue mutations."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid
import time
import math

from atomic_io import atomic_write_text
from platform_accounts import collection_key as username, account_input, account_info
from task_status import retry_limit_summary, REASONS

OPEN = {'pending', 'running', 'paused'}
TERMINAL = {'finished', 'failed'}
MAX_RETRY_ROUNDS = 3
MAX_RETRY_AGE = 48 * 3600


def retry_limit_reached(item, due, current):
    return (item.get('retry_rounds', 0) >= MAX_RETRY_ROUNDS
            or max(current + 60, due) > item.get('retry_started', current) + MAX_RETRY_AGE)


def retry_epoch(value):
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def parse_handles(value, platform="x"):
    if not isinstance(value, str) or len(value) > 32768:
        raise ValueError('请粘贴用户名列表，一次最多 100 项。')
    parts = [p for p in re.split(r'[\s,，;；、]+', value.strip()) if p]
    if not 1 <= len(parts) <= 100:
        raise ValueError('一次请输入 1–100 个用户名，用换行、空格或逗号分隔。')
    result = []
    for index, part in enumerate(parts, 1):
        try:
            result.append(account_input(part,platform))
        except ValueError as error:
            raise ValueError(f'第 {index} 项格式有误（{part[:60]}）：{error}') from error
    return result


class TaskQueue:
    def __init__(self, path):
        self.path = Path(path)
        self.error = ''
        self.data = {'schema': 1, 'state': 'idle', 'reason': '', 'items': [], 'receipts': []}
        if not self.path.exists():
            return
        try:
            if self.path.stat().st_size > 2 * 1024 * 1024:
                raise ValueError('文件过大')
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data.get('schema') != 1 or data['state'] not in {'idle','running','paused','finished'}:
                raise ValueError('格式不支持')
            if not isinstance(data['items'], list) or len(data['items']) > 200:
                raise ValueError('项目列表无效')
            ids = set()
            for item in data['items']:
                if (not isinstance(item, dict) or item['state'] not in OPEN | TERMINAL
                        or not re.fullmatch(r'[a-f0-9]{32}', item['id']) or item['id'] in ids
                        or username(item['username']) != item['username']
                        or not isinstance(item['options'], dict)):
                    raise ValueError('项目格式无效')
                ids.add(item['id'])
                if (type(item.get('retry_rounds',0)) is not int or not 0 <= item.get('retry_rounds',0) <= MAX_RETRY_ROUNDS
                        or not isinstance(item.get('retry_started',0),(int,float))
                        or not math.isfinite(item.get('retry_started',0))
                        or ('next_retry_at' in item and not retry_epoch(item['next_retry_at']))):
                    raise ValueError('自动重试记录无效')
            if not isinstance(data.get('receipts', []), list):
                raise ValueError('提交记录无效')
            # A service restart never starts network work automatically.
            if any(i['state'] in OPEN for i in data['items']):
                data.update(state='paused', reason='restart')
            for item in data['items']:
                if item['state'] == 'running':
                    item.update(state='paused', message='上次运行已中断，继续队列时从已保存进度恢复。')
            self.data = data
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            self.error = '队列记录无法读取，原文件已保留：' + str(error)[:160]
            self.data.update(state='paused', reason='storage_error')

    def commit(self, data):
        if self.error:
            raise ValueError(self.error)
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False))
        self.data = data

    def add(self, handles, options, request_id=''):
        if request_id and not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', request_id):
            raise ValueError('提交编号无效，请刷新页面后重试。')
        data = deepcopy(self.data)
        for receipt in data.get('receipts', []):
            if receipt['id'] == request_id and request_id:
                return {**receipt['result'], 'replayed': True}
        active = {i['username'] for i in data['items'] if i['state'] in OPEN}
        added = duplicates = 0
        for handle in handles:
            if handle in active:
                duplicates += 1
                continue
            active.add(handle)
            data['items'].append({'id': uuid.uuid4().hex, 'username': handle,
                                  'state': 'pending', 'options': dict(options), 'added_at': now()})
            added += 1
        if len(active) > 100:
            raise ValueError('队列最多保留 100 个待处理账号，请等部分完成后再添加。')
        # Keep up to 100 recent results without removing pending work.
        keep = {i['id'] for i in data['items'] if i['state'] in TERMINAL}
        keep = set([i['id'] for i in data['items'] if i['id'] in keep][-100:])
        data['items'] = [i for i in data['items'] if i['state'] in OPEN or i['id'] in keep]
        if added and data['state'] in {'idle','finished'}:
            data.update(state='running', reason='')
        result = {'added': added, 'duplicates': duplicates}
        if request_id:
            data['receipts'] = (data.get('receipts', []) + [{'id':request_id, 'result':result}])[-100:]
        self.commit(data)
        return result

    def pause(self, reason='user_pause'):
        if not any(i['state'] in OPEN for i in self.data['items']):
            return
        data = deepcopy(self.data)
        data.update(state='paused', reason=reason)
        self.commit(data)

    def resume(self):
        data = deepcopy(self.data)
        for item in data['items']:
            if item['state'] == 'paused':
                item['state'] = 'pending'
        data.update(state='running' if any(i['state'] in OPEN for i in data['items']) else 'finished', reason='')
        self.commit(data)

    def remove(self, item_id):
        data = deepcopy(self.data)
        found = next((i for i in data['items'] if i['id'] == item_id), None)
        if not found:
            raise ValueError('该队列项目已不存在，请刷新。')
        if found['state'] == 'running':
            raise ValueError('请先暂停队列并等待当前账号保存完成，再移除。')
        data['items'].remove(found)
        if not any(i['state'] in OPEN for i in data['items']):
            data.update(state='finished' if data['items'] else 'idle', reason='')
        self.commit(data)

    def clear_finished(self):
        data = deepcopy(self.data)
        data['items'] = [i for i in data['items'] if i['state'] in OPEN]
        if not data['items']:
            data.update(state='idle', reason='')
        self.commit(data)

    def next_item(self):
        if self.data['state'] != 'running' or self.error:
            return None
        return next((i for i in self.data['items'] if i['state'] == 'pending'
                     and retry_epoch(i.get('next_retry_at')) <= time.time()), None)

    def next_retry(self):
        dates = [i['next_retry_at'] for i in self.data['items'] if i['state'] == 'pending'
                 and retry_epoch(i.get('next_retry_at')) > 0]
        return min(dates, key=retry_epoch) if dates else ''

    def retry_limit_for(self, item_id, run_id, due):
        item = next((i for i in self.data['items'] if i['id'] == item_id
                     and i['state'] == 'running' and run_id and i.get('run_id') == run_id), None)
        return bool(item and retry_limit_reached(item, retry_epoch(due), time.time()))

    def reconcile(self, outcome):
        # Old releases exported a retry promise before the queue enforced its
        # cap. Correct the read view only for the exact terminal run; keep all
        # saved files and newer runs untouched.
        if outcome.get('state') == 'finished' and outcome.get('reason') == 'retry_cooldown' and outcome.get('run_id'):
            if any(i.get('run_id') == outcome['run_id'] and i['state'] == 'finished'
                   and i.get('retry_exhausted') for i in self.data['items']):
                return retry_limit_summary(outcome)
        return outcome

    def defer_manual(self, handle, options, outcome):
        if any(i['username'] == handle and i['state'] in OPEN for i in self.data['items']):
            return False
        data = deepcopy(self.data)
        item = {'id':uuid.uuid4().hex, 'username':handle, 'options':{
            k:v for k,v in options.items() if not k.startswith('_')}, 'added_at':now()}
        if not self._defer(item, outcome):
            return False
        if sum(i['state'] in OPEN for i in data['items']) >= 100:
            return False
        data['items'].append(item)
        completed = [i['id'] for i in data['items'] if i['state'] in TERMINAL][-100:]
        data['items'] = [i for i in data['items'] if i['state'] in OPEN or i['id'] in completed]
        if data['state'] != 'paused':
            data.update(state='running', reason='')
        self.commit(data)
        return True

    @staticmethod
    def _defer(item, outcome):
        if outcome.get('state') == 'finished' and outcome.get('reason') == 'retry_limit':
            item.update(retry_exhausted=True, message=REASONS['retry_limit'])
            return False
        due = retry_epoch(outcome.get('next_retry_at'))
        current = time.time()
        start = item.get('retry_started', current)
        rounds = item.get('retry_rounds', 0)
        if (outcome.get('state') != 'finished' or outcome.get('reason') != 'retry_cooldown'
                or not outcome.get('automatic_retry')
                or not item.get('options', {}).get('auto_continue', True) or not due):
            return False
        due = max(current + 60, due)
        if retry_limit_reached(item, due, current):
            item['message'] = REASONS['retry_limit']
            item['retry_exhausted'] = True
            item['outcome'] = retry_limit_summary(outcome)
            item.pop('next_retry_at', None)
            return False
        item.update(state='pending', retry_started=start, retry_rounds=rounds+1,
                    next_retry_at=datetime.fromtimestamp(due,timezone.utc).isoformat().replace('+00:00','Z'),
                    message=f'等待来源恢复，自动重试 {rounds+1}/{MAX_RETRY_ROUNDS}；期间先处理其他账号。')
        return True

    def mark_running(self, item_id, handle, run_id):
        data = deepcopy(self.data)
        item = next(i for i in data['items'] if i['id'] == item_id)
        item.pop('outcome', None)
        item.pop('finished_at', None)
        item.pop('next_retry_at', None)
        item.update(state='running', username=handle, run_id=run_id, started_at=now(), message='')
        self.commit(data)

    def finish(self, item_id, run_id, outcome, stopping=False):
        data = deepcopy(self.data)
        item = next((i for i in data['items'] if i['id'] == item_id and i['state'] == 'running' and run_id and i.get('run_id') == run_id), None)
        if not item:
            return
        state = outcome['state']
        item.update(state='finished' if state == 'finished' else 'paused' if state in {'paused','interrupted'} else 'failed',
                    outcome={k:outcome.get(k) for k in ('state','reason','label','result_label','counts','finished_at','mode')},
                    message=outcome.get('reason_text',''), finished_at=now())
        if not stopping:
            self._defer(item, outcome)
        if item['state'] == 'paused' or stopping:
            data.update(state='paused', reason='shutdown' if stopping else outcome.get('reason','user_pause'))
        if not any(i['state'] in OPEN for i in data['items']):
            data.update(state='finished', reason='')
        self.commit(data)

    def fail_start(self, item_id, outcome):
        """Terminal transition for preparation failures, before a run exists."""
        data = deepcopy(self.data)
        item = next((i for i in data['items'] if i['id'] == item_id and i['state'] == 'pending'), None)
        if item is None:
            raise ValueError('队列启动状态已变化，已停止派发。')
        item.update(state='failed', outcome=deepcopy(outcome),
                    message=outcome.get('reason_text',''), finished_at=now())
        if not any(i['state'] in OPEN for i in data['items']):
            data.update(state='finished', reason='')
        self.commit(data)

    def public(self):
        items = [{**{k:v for k,v in item.items() if k != 'options'}, **account_info(item['username'])} for item in self.data['items']]
        for item in items:
            if item.get('retry_exhausted') and item['state'] == 'finished':
                item['outcome'] = retry_limit_summary(item.get('outcome', {}))
                item['message'] = REASONS['retry_limit']
        counts = {state:sum(i['state'] == state for i in items) for state in OPEN | TERMINAL}
        return {'state':self.data['state'], 'reason':self.data.get('reason',''),
                'items':items, 'counts':counts, 'error':self.error,
                'next_retry_at':self.next_retry() if self.data['state']=='running' else ''}
