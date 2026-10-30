"""Versioned collection summaries and bounded, read-only online pagination.

Old collections need one full read on first use; subsequent polls use the small
summary cache. Cache invalidation uses the atomically replaced manifest's stat.
"""
from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import threading
from shiguangji.core.atomic_io import atomic_write_text
from shiguangji.media.media_inventory import inventory

_lock = threading.RLock()
_summaries, _documents = OrderedDict(), OrderedDict()
MAX_DOCUMENT_BYTES = 64*1024*1024


def fingerprint(path):
    s = path.stat()
    return [s.st_size, s.st_mtime_ns, s.st_ctime_ns, s.st_ino]


def summarize(manifest, revision):
    value = {k:v for k,v in manifest.items() if k != 'posts'}
    posts = manifest.get('posts', [])
    missing = [m for p in posts for m in p.get('media',[]) if m.get('status') != 'saved']
    value.update(content_revision=revision, posts=[], media_inventory=inventory(posts), months=sorted({p.get('created_at','')[:7] or 'unknown' for p in posts}, reverse=True),
                 media_issues={
                     'network':sum(m.get('error_kind') == 'network' or m.get('recovery_error_kind') == 'network' for m in missing),
                     'not_found':sum(m.get('error_kind') == 'not_found' for m in missing),
                     'access_denied':sum(m.get('error_kind') == 'access_denied' for m in missing),
                     'unsupported':sum(m.get('kind') == 'hls' or m.get('error_kind') == 'unsupported_hls' for m in missing),
                     'continuing':sum(bool(m.get('cc_more') or m.get('cc_pending_indexes') or m.get('cc_remaining_indexes')) for m in missing if m.get('kind') in {'image','video'})})
    return value


def publish_summary(folder, manifest, raw):
    path = Path(folder)/'manifest.json'
    stamp = fingerprint(path)
    value = summarize(manifest,hashlib.sha256(raw).hexdigest())
    atomic_write_text(Path(folder)/'summary.json', json.dumps({'schema':2,'manifest_stat':stamp,'summary':value},ensure_ascii=False))
    with _lock:
        _summaries[str(path)] = (stamp,value)
        _summaries.move_to_end(str(path))
        while len(_summaries)>512: _summaries.popitem(last=False)


def read_document(folder):
    path = Path(folder)/'manifest.json'
    key = str(path)
    with _lock:
        stamp = fingerprint(path)
        cached = _documents.get(key)
        if cached and cached[0] == stamp:
            _documents.move_to_end(key)
            return cached[1],cached[2]
    # Do disk work outside the shared cache lock; retry an atomic replacement.
    for _ in range(3):
        stamp = fingerprint(path)
        raw = path.read_bytes()
        document = json.loads(raw)
        if fingerprint(path) == stamp: break
    else:
        raise OSError('合集正在更新，请稍后重试。')
    with _lock:
        _documents[key] = (stamp,document,raw)
        _documents.move_to_end(key)
        while len(_documents)>1 and sum(len(v[2]) for v in _documents.values())>MAX_DOCUMENT_BYTES:
            _documents.popitem(last=False)
    return document,raw


def read_summary(folder):
    path = Path(folder)/'manifest.json'
    stamp = fingerprint(path)
    with _lock:
        cached = _summaries.get(str(path))
        if cached and cached[0] == stamp: return deepcopy(cached[1])
    try:
        saved = json.loads((Path(folder)/'summary.json').read_text(encoding='utf-8'))
        if saved.get('schema') != 2 or saved['manifest_stat'] != stamp: raise ValueError()
        value = saved['summary']
        if not isinstance(value,dict) or not value.get('content_revision'): raise ValueError()
    except (OSError,ValueError,KeyError,TypeError):
        document,raw = read_document(folder)
        value = summarize(document,hashlib.sha256(raw).hexdigest())
        # Legacy fallback is deliberately read-only. First write occurs on export.
    with _lock:
        _summaries[str(path)] = (stamp,value)
        _summaries.move_to_end(str(path))
        while len(_summaries)>512: _summaries.popitem(last=False)
    return deepcopy(value)


def post_page(folder, params):
    limit = max(1,min(200,int(params.get('limit','40'))))
    offset = max(0,int(params.get('offset','0')))
    q = params.get('search','').strip().lower()[:1000]
    month,kind = params.get('month','all'),params.get('type','all')
    if kind not in {'all','saved','saved-video','text','missing','missing-text','image','video'}: raise ValueError('筛选类型无效。')
    m,raw = read_document(folder)
    revision = hashlib.sha256(raw).hexdigest()
    if params.get('revision') and params['revision'] != revision:
        return {'stale':True,'revision':revision,'posts':[],'total':0,'offset':0,'has_more':False}
    def saved(media): return media.get('status') == 'saved' and bool(media.get('path'))
    def matches(p):
        media = p.get('media',[])
        if q and q not in ((p.get('text','') or '')+' '+p['id']+' '+p.get('shortcode','')).lower(): return False
        if month != 'all' and (p.get('created_at','')[:7] or 'unknown') != month: return False
        return (kind == 'all' or kind == 'saved' and any(map(saved,media))
                or kind == 'saved-video' and any(saved(a) and a['kind']=='video' for a in media)
                or kind == 'text' and not media or kind == 'missing' and any(not saved(a) for a in media)
                or kind == 'missing-text' and not p.get('text','').strip()
                or kind == 'image' and any(a['kind']=='image' for a in media)
                or kind == 'video' and any(a['kind'] in {'video','hls'} for a in media))
    posts = [p for p in m.get('posts',[]) if matches(p)]
    # Stable numeric-ID tiebreak; unknown dates always last, in either direction.
    posts.sort(key=lambda p:(p.get('created_at',''),int(p['id'])),reverse=params.get('sort','desc')!='asc')
    posts.sort(key=lambda p:not bool(p.get('created_at')))
    return {'revision':revision,'posts':posts[offset:offset+limit],'offset':offset,'total':len(posts),'has_more':offset+limit<len(posts)}
