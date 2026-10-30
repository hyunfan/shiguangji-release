"""Small, atomic restart checkpoint. Never stores network settings or local API tokens."""
from __future__ import annotations

import json
from pathlib import Path
from shiguangji.core.atomic_io import atomic_write_text


FIELDS = {"mode", "running", "pause_requested", "username", "stage", "status", "logs", "counts",
          "captures", "completed", "total", "started_at", "updated_at", "finished_at", "interrupted", "batch", "batch_started_at", "automatic",
          "run_id", "task_summary", "scope", "work_pending", "alias_discovery", "page_progress", "stage_started_at", "stage_seconds", "retry_policy", "backup"}


def read_job(path):
    try:
        path = Path(path)
        if path.stat().st_size > 512 * 1024:
            return None
        saved = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(saved, dict) or saved.get("schema") != 1:
            return None
        job = saved.get("job")
        if not isinstance(job, dict):
            return None
        if not all(isinstance(job.get(key), str) for key in ("username", "stage", "status")):
            return None
        if not isinstance(job.get("running"), bool) or not isinstance(job.get("counts"), dict):
            return None
        if not isinstance(job.get("logs"), list):
            return None
        job = {key: value for key, value in job.items() if key in FIELDS}
        for key in ('task_summary', 'scope', 'alias_discovery', 'page_progress', 'stage_seconds', 'retry_policy', 'backup'):
            if key in job and not isinstance(job[key], dict):
                job.pop(key)
        if 'run_id' in job and not isinstance(job['run_id'], str):
            job.pop('run_id')
        if 'stage_started_at' in job and not isinstance(job['stage_started_at'], str):
            job.pop('stage_started_at')
        job["logs"] = [{"time": item["time"], "text": item["text"]}
                       for item in job["logs"][-100:]
                       if isinstance(item, dict) and isinstance(item.get("time"), str)
                       and isinstance(item.get("text"), str)]
        return job
    except (OSError, ValueError, TypeError):
        return None


def write_job(path, job):
    payload = {"schema": 1, "job": {key: value for key, value in job.items() if key in FIELDS}}
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
