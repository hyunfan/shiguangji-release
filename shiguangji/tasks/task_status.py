"""One interpretation of task lifecycle, recovery coverage and terminal reasons."""
from __future__ import annotations
from datetime import datetime

STATES = {'idle', 'running', 'pausing', 'finalizing', 'paused', 'finished', 'failed', 'interrupted', 'unknown'}
LIMITS = {'time_limit', 'request_limit', 'source_limit', 'session_limit', 'batch_limit', 'storage_low'}
REASONS = {
    'retry_limit': '已达到自动重试上限（3 轮或 48 小时），本轮已结束。结果已保留，需要时可手动继续。',
    'retry_cooldown': '部分失败请求已暂缓，冷却结束后可重试；本轮尚未查完。',
    'storage_low': '磁盘空间不足，已停止新增下载；请释放空间后继续。',
    'user_pause': '已暂停并保存进度，需要时可继续。',
    'time_limit': '达到本批检索时限，已保存进度，等待继续。',
    'request_limit': '达到本批请求上限，已保存进度，等待继续。',
    'source_limit': '达到本批来源请求上限，已保存进度，等待继续。',
    'session_limit': '达到设定的自动运行时限，已保存进度，等待继续。',
    'batch_limit': '达到设定的批次上限，已保存进度，等待继续。',
    'no_progress': '连续 3 批没有新增内容或解析进展，已自动结束。',
    'no_new_media': '本批持续没有新增媒体，已结束并保存；可稍后重新检查。',
    'scope_checked': '本轮计划范围已检查完。未恢复内容仍可能缺少可用存档。',
    'sources_unavailable': '部分查询失败或仍有待查项目，本轮已结束；可查看原因并稍后重试。',
    'identity_unverified': '旧用户名的数字身份尚未核实或有归属冲突，无法完成旧名查找；当前用户名的已保存资料仍可浏览。',
    'error': '任务发生异常，请查看处理详情；已保存内容仍可浏览。',
    'zip_failed': '采集结果已保存，ZIP 生成失败；请重新生成 ZIP。',
    'process_lost': '上次任务意外中断，已保留保存过的进度。',
    'unrecorded': '旧记录未保存明确的结束原因，可浏览已有内容或重新查找。',
}


def mapping(value):
    return value if isinstance(value, dict) else {}


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def result(counts, scope='partial'):
    posts, media, saved = (number(counts.get(k)) for k in ('posts', 'media', 'saved'))
    text = number(counts.get('text'))
    if not posts:
        return 'none', '本轮未发现帖子' if scope == 'complete' else '尚未发现帖子'
    if media and not saved:
        return 'no_media', '尚未恢复媒体'
    missing = max(0, media - saved)
    if missing or number(counts.get('text_missing')) or text < posts:
        return 'partial', '部分恢复'
    return 'all_known', '已发现内容已恢复'


def summary(value, *, running=None, alias=None):
    value = mapping(value)
    last = mapping(value.get('last_run'))
    stored = mapping(value.get('task_summary')) or mapping(last.get('task_summary'))
    counts = mapping(value.get('counts'))
    automatic = mapping(value.get('automatic')) or mapping(last.get('automatic'))
    batch = mapping(value.get('batch')) or mapping(last.get('batch'))
    alias = mapping(alias if alias is not None else value.get('alias_discovery',
                    mapping(value.get('identity')).get('search', stored.get('alias_discovery', {}))))
    options = mapping(last.get('options'))
    mode = value.get('mode') or options.get('mode') or stored.get('mode') or 'full'
    if mode == 'zip':
        mode = stored.get('mode') or options.get('mode') or 'full'
    active = bool(value.get('running', False)) if running is None else bool(running)
    scope = mapping(value.get('scope')) or mapping(last.get('scope'))
    pending = bool(value.get('work_pending', scope.get('pending', False)))
    if 'pending' not in scope and 'work_pending' not in value:
        pending = number(counts.get('parsed')) < number(counts.get('captures'))
        pending |= bool(counts.get('local_alias_pending'))
        if mode not in {'repair', 'rescue', 'identity'}:
            pending |= bool(counts.get('identity_pending') or counts.get('alias_search_pending')
                            or alias.get('pending_items'))
            if options.get('multi_source'):pending |= bool(counts.get('text_recovery_pending'))
        if mode not in {'discover', 'identity'}:
            pending |= bool(counts.get('media_retry_pending'))
            if options.get('multi_source') or mode == 'rescue':
                pending |= bool(counts.get('source_retry_pending'))
                if options.get('common_crawl', True):
                    pending |= bool(counts.get('cc_pending'))
    failed_checks = bool(value.get('errors') or scope.get('failed_sources'))
    if mode not in {'repair', 'rescue', 'identity'}:
        failed_checks |= bool(alias.get('failed_items', alias.get('errors', 0)))
        failed_checks |= alias.get('state') == 'blocked' or alias.get('status') in {'no_id', 'conflict'}
        pending |= alias.get('state') in {'running', 'pending'}
    code = (automatic.get('stop_code') if automatic.get('enabled') else batch.get('stop_code')) or ''
    state, reason = 'unknown', 'unrecorded'
    status = value.get('status', '')
    if active or (status == 'running' and value.get('stage') == 'external'):
        state = ('finalizing' if value.get('stage') in {'export', 'zip', 'backup', 'done'} else
                 'pausing' if value.get('pause_requested') else 'running')
        reason = ''
    elif value.get('interrupted') or status == 'running':
        state, reason = 'interrupted', 'process_lost'
    elif status == 'failed':
        state, reason = 'failed', 'zip_failed' if stored.get('reason') == 'zip_failed' else 'error'
    elif stored.get('schema_version') == 1 and stored.get('state') in STATES:
        state, reason = stored['state'], stored.get('reason', 'unrecorded')
    elif code == 'user_pause' or code in LIMITS:
        state, reason = 'paused', code
    elif code in {'no_progress', 'no_new_media'}:
        state, reason = 'finished', code
    elif code in {'retry_cooldown', 'retry_limit'}:
        state, reason = 'finished', code
    elif status in {'complete', 'partial'} or code == 'pass_finished':
        state = 'finished'
        reason = 'identity_unverified' if alias.get('state') == 'blocked' and mode in {'full','discover'} else 'sources_unavailable' if pending or failed_checks else 'scope_checked'
    elif status == 'idle':
        state, reason = 'idle', ''
    cooldown = mapping(value.get('retry_policy') or last.get('retry_policy'))
    if state == 'finished' and reason != 'retry_limit' and number(cooldown.get('waiting')):
        reason = 'retry_cooldown'
    scope_status = 'complete' if state == 'finished' and reason == 'scope_checked' else 'partial'
    if state == 'unknown':
        scope_status = 'unknown'
    result_status, result_label = result(counts, scope_status)
    labels = {'idle':'等待开始', 'running':'正在查找', 'pausing':'正在暂停并保存',
              'finalizing':'正在保存与打包', 'paused':'已暂停，等待继续',
              'finished':'本轮查找完成' if scope_status == 'complete' else '本轮已结束',
              'failed':'ZIP 生成失败' if reason == 'zip_failed' else '任务失败',
              'interrupted':'任务意外中断', 'unknown':'已停止，结束原因未记录'}
    if state == 'finished' and mode in {'identity', 'discover', 'repair', 'rescue'}:
        labels[state] += {'identity':'（仅核对用户名）', 'discover':'（仅查找帖子）',
                          'repair':'（仅补全已有媒体）', 'rescue':'（多来源恢复）'}[mode]
    tone = ('info' if state in {'running','pausing','finalizing'} else 'success' if scope_status == 'complete'
            else 'danger' if state in {'failed','interrupted'} else 'neutral' if state in {'unknown','idle'} else 'warning')
    reason_text = REASONS.get(reason, '')
    if state == 'running':
        reason_text = '准备自动继续下一批。' if value.get('stage') == 'between_batches' else '正在执行当前步骤；可随时暂停并保存。'
    elif state == 'pausing':
        reason_text = '正在结束当前请求并保存，请等待保存完成。'
    elif state == 'finalizing':
        reason_text = '正在发布结果和下载合集，请等待保存完成。'
    if state == 'finished' and scope_status != 'complete' and (pending or failed_checks):
        reason_text += ' 部分来源仍待检查或重试。'
    return {'schema_version':1, 'run_id':str(value.get('run_id') or last.get('run_id') or stored.get('run_id') or ''),
            'state':state, 'reason':reason, 'label':labels[state], 'tone':tone, 'reason_text':reason_text,
            'scope_status':scope_status, 'result_status':result_status, 'result_label':result_label,
            'counts':dict(counts), 'alias_discovery':dict(alias),
            'next_retry_at':'' if reason == 'retry_limit' else automatic.get('next_retry_at') or cooldown.get('next_retry_at', ''),
            'automatic_retry':bool(state=='finished' and reason=='retry_cooldown' and automatic.get('enabled') and code=='retry_cooldown'),
            'finished_at':(stored.get('finished_at') or value.get('finished_at') or '')
                          if state not in {'running', 'pausing', 'finalizing', 'idle'} else '',
            'can_continue':state in {'paused','finished','failed','interrupted','unknown'},
            'recommended_action':'rebuild_zip' if reason == 'zip_failed' else
                'continue' if reason == 'retry_limit' or state in {'paused','interrupted','failed'} else 'browse',
            'mode':mode}


from shiguangji.tasks.task_compat import same_run


def retry_limit_summary(outcome):
    """Keep counts and run identity when durable scheduling has reached its cap."""
    return {**outcome, 'reason':'retry_limit', 'reason_text':REASONS['retry_limit'],
            'automatic_retry':False, 'next_retry_at':'', 'scope_status':'partial',
            'recommended_action':'continue'}


def collection_summary(manifest, job=None, busy=False):
    job = mapping(job)
    if busy:
        if job.get('username') == manifest.get('username') and job.get('running'):
            return summary({**job, 'counts':manifest['counts']}, running=True,
                           alias=mapping(manifest.get('identity')).get('search'))
        return summary({**manifest, 'stage':'external'}, running=True)
    if same_run(manifest, job) and (job.get('status') == 'failed' or job.get('interrupted')):
        # A final export can fail after the last successful checkpoint. The
        # matching run's failure remains authoritative even if that file could
        # not be updated; never apply another run's error to this collection.
        return summary({**manifest, **job, 'counts':manifest['counts']}, running=False,
                       alias=mapping(manifest.get('identity')).get('search'))
    if not manifest.get('task_summary') and same_run(manifest, job):
        return summary({**manifest, 'automatic':job.get('automatic', {}), 'batch':job.get('batch', {}),
                        'finished_at':job.get('finished_at',''), 'interrupted':job.get('interrupted',False)})
    return summary(manifest, running=False)
