"""Validation of collection options sent by the page or a queued task."""
from shiguangji.platforms.instagram_extract import post_links

MODES = {'full', 'repair', 'rescue', 'discover', 'identity'}


def collect_options(body):
    options = {'proxy':str(body.get('proxy','')), 'max_pages':int(body.get('max_pages',0)),
               'max_media_mb':int(body.get('max_media_mb',512)), 'mode':body.get('mode','full')}
    if len(options['proxy']) > 2048:
        raise ValueError('代理地址过长。')
    for key, default in (('force_recheck',False), ('multi_source',False), ('common_crawl',True),
                         ('public_mirrors',True), ('auto_continue',True)):
        options[key] = body.get(key, default)
        if not isinstance(options[key], bool):
            raise ValueError('来源和继续选项应为 true 或 false。')
    for key, default, low, high in (('cc_indexes',6,1,12), ('max_minutes',10,1,30),
                                    ('max_requests',200,1,650), ('auto_max_minutes',0,0,1440)):
        options[key] = int(body.get(key, default))
        if not low <= options[key] <= high:
            raise ValueError(f'{key} 超出范围：应为 {low}–{high}。')
    if options['mode'] not in MODES:
        raise ValueError('无法识别的整理方式。')
    if not 0 <= options['max_pages'] <= 1000000 or not 0 <= options['max_media_mb'] <= 100000:
        raise ValueError('数量限制超出范围。')
    if options['mode'] == 'identity':
        options['auto_continue'] = False
    options['instagram_links'] = post_links(body.get('instagram_links', '')) if isinstance(body.get('instagram_links',''),str) else post_links('\n'.join(body['instagram_links']))
    return options
