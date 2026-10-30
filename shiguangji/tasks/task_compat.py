"""Read-only reconciliation of old status records; new runs use run_id."""
from datetime import datetime

def mapping(value):
    return value if isinstance(value,dict) else {}

def same_run(manifest, job):
    """Legacy reconciliation needs timestamp AND counts, never handle alone."""
    if manifest.get('username') != job.get('username') or job.get('running'):
        return False
    run_id = manifest.get('run_id') or mapping(manifest.get('last_run')).get('run_id')
    if run_id:
        return run_id == job.get('run_id')
    if job.get('mode') == 'zip':
        return False
    try:
        def date(v):
            return datetime.fromisoformat(v.replace('Z', '+00:00'))
        if not date(job['started_at']) <= date(manifest['updated_at']) <= date(job['finished_at']):
            return False
        return all(manifest['counts'].get(k) == job['counts'].get(k)
                   for k in ('posts','text','media','saved','captures','parsed'))
    except (KeyError, TypeError, ValueError):
        return False
