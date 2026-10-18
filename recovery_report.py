"""Explain missing content using observed capture states, without guessing loss."""
from collections import Counter,defaultdict
from extractor import post_id
from instagram_extract import shortcode


def report(posts, captures=()):
    by_post=defaultdict(list)
    for row in captures:
        pid=post_id(row['original']) or shortcode(row['original'])
        if pid:by_post[pid].append(row['status'])
    text=Counter();media=Counter();sources=Counter()
    for post in posts:
        key=post.get('shortcode') or post['id']
        if post.get('text'):text['recovered']+=1
        elif post.get('content_reason')=='restricted':text['restricted']+=1
        elif post.get('content_reason')=='shell':text['empty_snapshot']+=1
        elif 'done' in by_post[key]:text['needs_parsing_or_other_snapshot']+=1
        elif by_post[key]:text['page_not_read']+=1
        else:text['no_capture']+=1
        for item in post.get('media',[]):
            if item.get('status')=='saved':
                media['saved']+=1;sources[item.get('source') or 'legacy_unspecified']+=1
            elif item.get('kind')=='hls':media['hls_pending']+=1
            elif item.get('error_kind')=='network' or item.get('recovery_error_kind')=='network':media['retryable']+=1
            elif item.get('error_kind')=='not_indexed':media['not_found_in_checked_scope']+=1
            else:media['unresolved']+=1
    return {'schema':1,'text':dict(text),'media_records':dict(media),'saved_sources':dict(sources),
            'note':'媒体为记录数，可能包含重复或清晰度变体；未找到仅指已检查范围。'}
