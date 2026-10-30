"""POST routes. Each handler is ``handler(env, body)`` and returns ``None`` for
``{"ok": true}`` or a ``(payload, status)`` pair. Errors are raised and mapped by
``shiguangji.web.handler``. ``env`` is the application module; see ``shiguangji.web``.
"""
from shiguangji.platforms.platform_accounts import account_input, collection_key as username, is_instagram
from shiguangji.storage.collection_lock import CollectionBusy, collection_lock
from shiguangji.platforms.account_identity import Catalog
from shiguangji.tasks.task_queue import OPEN as QUEUE_OPEN, parse_handles

QUEUE_PREFIX = '/api/queue/'
MAX_BODY = 8192
MAX_QUEUE_BODY = 65536


def body_limit(path):
    return MAX_QUEUE_BODY if path.startswith(QUEUE_PREFIX) else MAX_BODY


def reliability(env, body):
    return {'ok':True,'reliability':env.save_settings(env.TASKS.base,body)}, 200


def storage_settings(env, body):
    return {'ok':True,'storage':env.storage_service().configure(body)}, 200


def storage_action(env, body):
    if env.TASKS.job.get('running') or env.TASKS.stopping.is_set():
        raise ValueError('请等待采集保存结束，或暂停任务后再管理空间。')
    if body.get('operation')=='cleanup' and body.get('confirm_zip_only') is not True:
        raise ValueError('请核对 ZIP 清理清单后确认。')
    result = env.storage_service().submit(body.get('operation'),handle=body.get('username',''),
        plan_id=body.get('plan_id',''),all_exports=body.get('all_exports') is True)
    return {'ok':True,'storage':result}, 200


def cloud_backup(env, body):
    if env.CLOUD is None or env.TASKS.stopping.is_set():
        raise ValueError('后台正在启动或退出，请稍后重试。')
    result = env.CLOUD.configure(body)
    if body['enabled']:
        env.save_settings(env.TASKS.base, {'backup_after_task': True})
    return {'ok': True, 'cloud_backup': result}, 200


def history_retry(env, body):
    result = env.TASKS.retry_history(body.get('id'), env.collect_options(body))
    return {'ok':True, **result}, 200


def queue_add(env, queue, body):
    tasks = env.TASKS
    handles = [env.canonical_handle(tasks.base, h) for h in parse_handles(body.get('usernames'), body.get('platform','x'))]
    options = env.collect_options({**body, 'mode':'full', 'auto_continue':True})
    return queue.add(handles, options, body.get('request_id',''))


def queue_pause(env, queue, body):
    env.TASKS.pause_queue()


def queue_resume(env, queue, body):
    if env.TASKS.job.get('running') and env.TASKS.job.get('pause_requested'):
        raise ValueError('当前账号还在保存暂停进度，请稍候再继续队列。')
    queue.resume()


def queue_remove(env, queue, body):
    env.TASKS.task_history()
    queue.remove(body.get('id'))


def queue_clear_finished(env, queue, body):
    env.TASKS.task_history()
    queue.clear_finished()


QUEUE_ACTIONS = {
    'add': queue_add,
    'pause': queue_pause,
    'resume': queue_resume,
    'remove': queue_remove,
    'clear-finished': queue_clear_finished,
}


def queue_action(env, path, body):
    tasks = env.TASKS
    with tasks.lock:
        if tasks.stopping.is_set():
            raise ValueError('后台正在保存并退出，请重新启动后继续队列。')
        queue = tasks.task_queue()
        action = QUEUE_ACTIONS.get(path[len(QUEUE_PREFIX):])
        if action is None:
            return {'error':'未找到队列操作。'}, 404
        result = action(env, queue, body) or {}
        tasks.advance_queue()
        return {'ok':True, **result, 'queue':queue.public()}, 200


def pause(env, body):
    if not env.TASKS.pause_current():
        return {"error": "已进入保存和打包阶段，请等待完成。"}, 409


def identity(env, body):
    tasks = env.TASKS
    handle = username(str(body.get('username', '')))
    if is_instagram(handle):
        raise ValueError('Instagram 暂不支持自动关联历史用户名；可按旧用户名建立单独合集。')
    alias = username(str(body.get('alias', '')))
    if not (tasks.base / handle / 'manifest.json').is_file():
        raise ValueError('请先创建该博主的合集。')
    with tasks.lock:
        if tasks.job.get('running') or tasks.collection_busy(handle):
            return {'error': '请等待当前任务保存完成后再修改历史用户名。'}, 409
        with collection_lock(tasks.base / handle):
            catalog = Catalog(tasks.base)
            try:
                catalog.choice(handle, alias, body.get('state', 'candidate'))
            finally:
                catalog.close()


def delete(env, body):
    tasks = env.TASKS
    handle = username(str(body.get('username', '')))
    if body.get('confirm_username') != handle:
        raise ValueError('请确认要删除的博主后再操作。')
    with tasks.lock:
        if any(i['username'] == handle and i['state'] in QUEUE_OPEN for i in tasks.task_queue().data['items']):
            return {'error':'该博主仍在队列中，请先从队列移除，再删除合集。'}, 409
        if tasks.job.get('running') and tasks.job.get('username') == handle:
            return {'error': '该博主正在整理，请先暂停并等待保存完成，再删除。'}, 409
        history = tasks.task_history()
        try:
            env.delete_collection(tasks.base, handle)
        except CollectionBusy as error:
            return {'error': str(error)}, 409
        except OSError:
            return {'error': '删除未完成，部分文件可能已移除。请关闭该博主的图片、视频或 ZIP 后重试。'}, 409
        history.forget(handle)
        if tasks.job.get('username') == handle:
            tasks.job.clear()
            tasks.job.update(running=False, pause_requested=False, username='', status='idle', stage='idle', logs=[], counts={})
            tasks.persist_job(force=True)


def start_or_zip(env, body, zip_only):
    tasks = env.TASKS
    handle = account_input(str(body.get("username", "")), body.get("platform"))
    if not zip_only:
        handle = env.canonical_handle(tasks.base, handle)
    options = env.collect_options(body)
    if is_instagram(handle) and options["mode"] == "identity":
        raise ValueError("Instagram 暂不支持自动关联历史用户名。")
    if options["mode"] in {"repair","rescue"} and not (tasks.base / handle / "manifest.json").exists():
        raise ValueError("请先进行一次完整整理，再补全已有合集。")
    if zip_only and not (tasks.base / handle / "manifest.json").exists():
        raise ValueError("请先整理该用户的存档。")
    with tasks.lock:
        queue = tasks.task_queue()
        if (not zip_only and options['mode'] == 'full' and not tasks.job.get('running')
                and any(i['username'] == handle and i['state'] == 'paused' for i in queue.data['items'])):
            queue.resume()
            tasks.advance_queue()
        else:
            tasks.start(handle, options, zip_only)


def start(env, body):
    return start_or_zip(env, body, False)


def zip_collection(env, body):
    return start_or_zip(env, body, True)


ROUTES = {
    '/api/reliability': reliability,
    '/api/storage/settings': storage_settings,
    '/api/storage/action': storage_action,
    '/api/cloud-backup': cloud_backup,
    '/api/history/retry': history_retry,
    '/api/pause': pause,
    '/api/identity': identity,
    '/api/delete': delete,
    '/api/start': start,
    '/api/zip': zip_collection,
}


def dispatch(env, path, body):
    """Run the POST route for ``path`` (the raw request target, as before the split)."""
    if path in ROUTES:
        return ROUTES[path](env, body)
    if path.startswith(QUEUE_PREFIX):
        return queue_action(env, path, body)
    return {"error": "未找到操作。"}, 404
