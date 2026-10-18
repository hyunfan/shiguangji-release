"""Durable source scope: preserve unfinished order, explain priority/deep work."""
import hashlib
import json
from archive_support import now


def indexes_plan(chosen, priority, previous=None):
    previous = previous if isinstance(previous,dict) else {}
    old = previous.get('indexes',[]) if previous.get('schema') == 1 else []
    available = set(chosen)
    order = list(dict.fromkeys([i for i in old if isinstance(i,str) and i in available] + chosen))
    signature = hashlib.sha256(json.dumps(chosen).encode()).hexdigest()
    return {'schema':1,'source':'common_crawl','indexes':order,'priority':priority,
            'catalog_revision':signature,'created_at':previous.get('created_at') or now(),
            'scope_note':'先查发帖/快照附近和近期集合，再检查更早范围；旧媒体重发可能早于帖子。'}


def update_progress(plan, completed, pending, errors):
    remaining = [i for i in plan['indexes'] if i not in completed]
    plan.update(remaining=len(remaining), retry_pending=len(pending),
                phase='priority' if any(i in plan['priority'] for i in remaining) else 'deep' if remaining else 'finished',
                reason='retry_pending' if pending else 'scope_checked' if not remaining else 'pending',
                last_errors={i:errors[i] for i in pending if i in errors})
