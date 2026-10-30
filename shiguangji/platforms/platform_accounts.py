"""Platform-aware identifiers; existing X directory names remain unchanged."""
import re
from urllib.parse import urlsplit
from shiguangji.platforms.extractor import username as x_username, original_url

IG_PREFIX = 'instagram--'
IG_HOSTS = {'instagram.com', 'www.instagram.com', 'm.instagram.com', 'instagr.am', 'www.instagr.am'}


def instagram_username(value):
    value = original_url(str(value).strip())
    if re.match(r'^(?:www\.|m\.)?(?:instagram\.com|instagr\.am)/', value, re.I):
        value = 'https://' + value
    if '://' in value:
        u = urlsplit(value)
        parts = u.path.strip('/').split('/')
        if u.scheme not in {'http', 'https'} or u.hostname not in IG_HOSTS or u.username or u.port not in {None,80,443} or len(parts) != 1:
            raise ValueError('请输入 Instagram 用户名或主页链接；帖子链接请填入补充线索。')
        value = parts[0]
    value = value.lstrip('@').lower()
    if (not re.fullmatch(r'[a-z0-9_](?:[a-z0-9_.]{0,28}[a-z0-9_])?', value) or '..' in value
            or value in {'p','reel','reels','tv','stories','explore','accounts','direct','about','developer','legal'}):
        raise ValueError('Instagram 用户名应为 1–30 位字母、数字、下划线或点；不能以点开头或结尾。')
    return value


def collection_key(value):
    """Validate a persisted or routed key, never an arbitrary filesystem path."""
    if isinstance(value, str) and value.startswith(IG_PREFIX):
        return IG_PREFIX + instagram_username(value[len(IG_PREFIX):])
    return x_username(value)


def account_input(value, platform=None):
    if platform is None:
        return collection_key(value)
    if platform == 'instagram':
        return IG_PREFIX + instagram_username(value)
    if platform == 'x':
        return x_username(value)
    raise ValueError('请选择 X / Twitter 或 Instagram。')


def is_instagram(key):
    return str(key).startswith(IG_PREFIX)


def account_info(key):
    ig = is_instagram(key)
    return {'platform': 'instagram' if ig else 'x',
            'display_username': key[len(IG_PREFIX):] if ig else key}


def empty_identity(key):
    return {'username':key, 'status':'unsupported', 'user_id':'', 'user_ids':[], 'aliases':[], 'search':{}}
