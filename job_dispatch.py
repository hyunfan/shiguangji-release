"""Queue dispatch independent of HTTP and thread implementation.

The caller owns its application lock. Every attempted item must leave pending.
Storage failure stops dispatch immediately and keeps the original queue intact.
"""
from task_status import summary


def dispatch(queue, prepare, launch, available):
    attempted = set()
    while available():
        item = queue.next_item()
        if not item: return
        if item['id'] in attempted:
            raise RuntimeError('队列项目状态没有推进，已停止派发。')
        attempted.add(item['id'])
        try:
            handle, options = prepare(item)
            launch(handle, options, queue_id=item['id'])
            return
        except Exception as error:
            outcome = summary({'status':'failed', 'counts':{}})
            outcome['reason_text'] = '无法启动此账号：' + str(error)[:240]
            current = next(i for i in queue.data['items'] if i['id'] == item['id'])
            if current['state'] == 'running':
                queue.finish(item['id'], current['run_id'], outcome)
            else:
                queue.fail_start(item['id'], outcome)
