"""Stable archive constants and small serialization helpers."""
import hashlib
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit
from shiguangji import APP_ROOT
from shiguangji.core.atomic_io import atomic_write_text
from shiguangji.platforms.extractor import original_url
ROOT = APP_ROOT
DATA = ROOT / "collections"
CDX = "https://web.archive.org/cdx/search/cdx"
HOSTS = ("twitter.com", "mobile.twitter.com", "m.twitter.com", "x.com", "mobile.x.com", "m.x.com")
MISSING_CACHE_SECONDS = 24 * 60 * 60
PARSER_CACHE_VERSION = 5
INDEX_POLICY_VERSION = "2-resource-records"
CDX_SUCCESS_FILTER = "statuscode:(200|-)"

def now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def neighboring_handle(url, handle):
    """Suggest a CDX prefix neighbor without ever treating it as this owner."""
    try:
        parsed = urlsplit(original_url(url))
        candidate = parsed.path.strip('/').split('/', 1)[0].lower()
        if (parsed.hostname in HOSTS and re.fullmatch(r'[a-z0-9_]{1,15}', candidate)
                and candidate != handle and candidate.startswith(handle)):
            return candidate
    except (TypeError, ValueError):
        pass
    return ''

def media_context(p, m):
    """Invalidate an empty lookup when the post supplies new URLs or captures."""
    sources = [sorted(m.get("urls", [])), sorted((c["timestamp"], c["url"]) for c in p.get("captures", []))]
    if p.get("author_ids"):
        sources.append(sorted(p["author_ids"]))
    return hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()

def missing_cache_valid(p, m, force=False):
    if force or m.get("status") == "saved" or m.get("error_kind") != "not_indexed":
        return False
    if m.get("checked_context") != media_context(p, m):
        return False
    try:
        checked = datetime.fromisoformat(m["checked_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return 0 <= age < MISSING_CACHE_SECONDS
    except (KeyError, TypeError, ValueError):
        return False

def write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))

def replay(timestamp, original, raw=True):
    return f"https://web.archive.org/web/{timestamp}{'id_' if raw else ''}/{original}"
