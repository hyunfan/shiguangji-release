"""One interpretation of task lifecycle, recovery coverage and terminal reasons."""
from __future__ import annotations

# The single-manifest interpretation lives in core so storage can use it; re-exported here.
from shiguangji.core.task_summary import (  # noqa: F401
    LIMITS, REASONS, STATES, mapping, number, result, summary)
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
