"""Bounded text/attachment fallback for exact, publicly accessible X posts."""
import time
from datetime import datetime
from shiguangji.sources.public_sources import discover
from shiguangji.sources.source_http import SourceClient
from shiguangji.platforms.extractor import merge_post, blank_post
from shiguangji.core.archive_support import now


def recent_check(post, force=False):
    if force or not post.get('public_text_checked_at'):return False
    reports=list(post.get('source_checks',{}).values())
    if not reports:return False
    # Failed/deferred sources remain retryable; transport applies the cooldown.
    if any(r.get('status') in {'unavailable','rate_limited','pending'} for r in reports):return False
    try:
        # Media discovery shares source_checks and can refresh it independently.
        # It must not extend the lifetime of an older text-only lookup.
        dates=[post['public_text_checked_at'], *(r['checked_at'] for r in reports)]
        current=time.time()
        return all(0 <= current-datetime.fromisoformat(date.replace('Z','+00:00')).timestamp() < 86400 for date in dates)
    except (KeyError,TypeError,ValueError):return False


def recover_posts(collector):
    if not collector.options.get('multi_source') or collector.options.get('mode','full') not in {'full','discover'}:
        return
    http=SourceClient(collector.stop,collector.log,collector.options.get('proxy',''),budget=collector.budget)
    http.retry_policy=collector.retry_policy
    candidates=[p for p in collector.ordered_posts('content') if not p.get('text')
                and p.get('content_reason')!='restricted' and not recent_check(p,collector.options.get('force_recheck'))]
    report={'checked':0,'text_recovered':0,'media_discovered':0,'requests':0,'pending':len(candidates)}
    collector.run_info['post_recovery']=report
    start=time.monotonic()
    total=min(12,len(candidates))
    collector.progress(stage='content',completed=0,total=total)
    for post in candidates:
        if report['checked']>=12 or time.monotonic()-start>=90:break
        http.check();collector.visit_post('content',post)
        before=len(post['media'])
        try:
            items=discover(post,http.fetch,http.check,collector.log,
                           enable_mirrors=collector.options.get('public_mirrors',True),recover_text=True)
            fresh=blank_post(post['id'],post['username']);fresh['media']=items
            post=merge_post(post,fresh)
            report['checked']+=1
            report['text_recovered']+=int(bool(post.get('text')))
            report['media_discovered']+=max(0,len(post['media'])-before)
            post['public_text_checked_at']=now()
        finally:
            report['requests']=http.requests
            if post.get('text') or recent_check(post):report['pending']=max(0,report['pending']-1)
            collector.store.save_post(post)
            collector.checkpoint('running')
            collector.progress(stage='content',completed=report['checked'],total=total)
    collector.log(f"正文补取：检查 {report['checked']} 条，补回 {report['text_recovered']} 条正文，发现 {report['media_discovered']} 项媒体。")
