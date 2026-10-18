"""Read archived Instagram HTML/JSON without executing page scripts."""
import hashlib
import html
import json
import re
from urllib.parse import urlsplit, urljoin, urlunsplit
from extractor import Document, original_url, iso_date, merge_post
from platform_accounts import IG_HOSTS, instagram_username


def shortcode(url):
    try:
        u = urlsplit(original_url(url))
        if u.hostname not in IG_HOSTS or u.scheme not in {'http','https'} or u.username or u.port not in {None,80,443}:
            return ''
        m = re.fullmatch(r'/(?:[A-Za-z0-9_.]+/)?(?:p|reel|tv)/([A-Za-z0-9_-]{1,32})(?:/embed(?:/captioned)?)?/?', u.path)
        return m[1] if m else ''
    except ValueError:
        return ''


def post_url(url):
    code = shortcode(url)
    if not code:
        raise ValueError('Instagram 帖子链接应为 https://www.instagram.com/p/…/ 或 /reel/…/。')
    return 'https://www.instagram.com/p/' + code + '/'


def post_links(value):
    if not isinstance(value, str) or len(value) > 6000:
        raise ValueError('最多补充 20 个 Instagram 帖子链接。')
    parts = [x for x in re.split(r'[\s,，;；]+', value.strip()) if x]
    if len(parts) > 20:
        raise ValueError('最多补充 20 个 Instagram 帖子链接。')
    return list(dict.fromkeys(post_url(x) for x in parts))


def media_item(value, kind='image'):
    url = original_url(value)
    try:
        u = urlsplit(url)
        host = u.hostname or ''
        if (u.scheme not in {'http','https'} or u.username or u.port not in {None,80,443}
                or not any(host == h or host.endswith('.'+h) for h in ('cdninstagram.com','fbcdn.net','cdninstagram.net'))):
            return None
        if not u.path or u.path == '/':
            return None
    except ValueError:
        return None
    kind = 'hls' if '.m3u8' in u.path else kind
    # Keep path+host. Query strings are signed, expiring variants of the same file.
    return {'key':host+u.path, 'kind':kind, 'urls':[url], 'status':'pending', 'path':'', 'error':''}


def blank_post(code, handle):
    # A numeric stable storage ID supports the existing timeline/SQLite format.
    pid = str(int.from_bytes(hashlib.sha256(('instagram:'+code).encode()).digest()[:16], 'big'))
    return {'id':pid, 'username':handle, 'platform':'instagram', 'shortcode':code,
            'text':'', 'text_source':'', 'created_at':'', 'date_source':'unknown',
            'url':'https://www.instagram.com/p/'+code+'/', 'media':[], 'captures':[],
            'text_versions':[], 'content_status':'link_only', 'content_note':'', 'content_reason':''}


def objects(value):
    stack, count = [value], 0
    while stack and count < 100000:
        obj = stack.pop(); count += 1
        if isinstance(obj, dict):
            yield obj
            stack.extend(reversed(list(obj.values())))
        elif isinstance(obj, list):
            stack.extend(reversed(obj))


def payloads(source, nodes):
    decoder = json.JSONDecoder()
    try:
        yield json.loads(source)
    except (ValueError, RecursionError):
        pass
    for n in nodes:
        if n.tag != 'script':
            continue
        text = ''.join(c for c in n.children if isinstance(c,str)).strip()
        if not text:
            continue
        try:
            yield json.loads(text)
            continue
        except (ValueError, RecursionError):
            pass
        for pattern in (r'(?:window\.)?_sharedData\s*=\s*', r'__additionalDataLoaded\([^,]+,\s*'):
            for match in list(re.finditer(pattern,text))[:50]:
                try:
                    yield decoder.raw_decode(text[match.end():])[0]
                except (ValueError, RecursionError):
                    pass


def owner_name(obj):
    owner = obj.get('owner') or obj.get('user') or obj.get('author') or {}
    if isinstance(owner, list):
        owner = owner[0] if owner else {}
    if isinstance(owner,str):
        owner = {'url':owner} if '://' in owner else {'username':owner}
    if not isinstance(owner,dict):
        return ''
    value = owner.get('username') or owner.get('alternateName') or owner.get('url')
    try:
        return instagram_username(value) if value else ''
    except ValueError:
        return ''


def from_object(obj, handle):
    code = obj.get('shortcode') or obj.get('code') or shortcode(obj.get('url','')) or shortcode(obj.get('mainEntityOfPage','') if isinstance(obj.get('mainEntityOfPage'),str) else '')
    if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',code):
        return None
    p = blank_post(code,handle)
    caption = obj.get('caption')
    if isinstance(caption,dict):
        caption = caption.get('text')
    if not isinstance(caption,str):
        edges = (obj.get('edge_media_to_caption') or {}).get('edges',[])
        caption = next((e.get('node',{}).get('text') for e in edges if isinstance(e,dict) and isinstance(e.get('node'),dict)), '')
    p['text'] = caption or (obj.get('articleBody') if isinstance(obj.get('articleBody'),str) else '') or (obj.get('description') if isinstance(obj.get('description'),str) else '')
    p['text_source'] = 'json'
    p['created_at'] = iso_date(obj.get('taken_at_timestamp') or obj.get('taken_at') or obj.get('datePublished') or obj.get('uploadDate') or obj.get('date'))
    p['date_source'] = 'page' if p['created_at'] else 'unknown'
    children = obj.get('carousel_media') or [e.get('node',{}) for e in (obj.get('edge_sidecar_to_children') or {}).get('edges',[]) if isinstance(e,dict)] or [obj]
    for child in children:
        if not isinstance(child,dict):
            continue
        urls = [(child.get(k),kind) for k,kind in (('display_url','image'),('display_src','image'),('thumbnail_src','image'),('video_url','video'),('contentUrl','video' if child.get('@type')=='VideoObject' else 'image'))]
        images = child.get('image') or []
        for img in images if isinstance(images,list) else [images]:
            urls.append((img.get('url') if isinstance(img,dict) else img,'image'))
        for key,kind in (('display_resources','image'),('video_versions','video')):
            for item in child.get(key,[]) or []:
                if isinstance(item,dict):urls.append((item.get('src') or item.get('url'),kind))
        image_versions = child.get('image_versions2') or {}
        if isinstance(image_versions,dict):
            urls.extend((i.get('url'),'image') for i in image_versions.get('candidates',[]) if isinstance(i,dict))
        for value,kind in urls:
            if not isinstance(value,str):continue
            item = media_item(value,kind)
            if item:
                prior = next((m for m in p['media'] if m['key']==item['key']),None)
                if prior:prior['urls'] = list(dict.fromkeys(prior['urls']+item['urls']))
                else:p['media'].append(item)
    return p


def extract(source, page_url, timestamp, handle):
    """Return verified posts and post URLs to investigate; URLs alone prove no author."""
    doc = Document(source)
    nodes = list(doc.root.walk())
    parsed = list(payloads(source,nodes))
    posts, links = {}, set()
    current = shortcode(page_url)
    def accept(obj, inherited=''):
        author = owner_name(obj)
        if (author or inherited) != handle:
            return
        p = from_object(obj,handle)
        if not p or (current and p['shortcode'] != current):
            return
        p['captures'] = [{'timestamp':timestamp,'url':page_url,'archive_url':f'https://web.archive.org/web/{timestamp}/{page_url}'}]
        posts[p['id']] = merge_post(posts.get(p['id'],blank_post(p['shortcode'],handle)),p)
        links.add(p['url'])
    for payload in parsed:
        for obj in objects(payload):
            accept(obj)
            # Only a matching profile's OWNER timeline grants inherited authorship.
            if str(obj.get('username','')).lower() == handle:
                timeline = obj.get('edge_owner_to_timeline_media') or obj.get('media') or {}
                if isinstance(timeline,dict):
                    for entry in timeline.get('edges',timeline.get('nodes',[])):
                        node = entry.get('node',entry) if isinstance(entry,dict) else {}
                        if isinstance(node,dict):accept(node,handle)
    metas = {n.attrs.get('property',n.attrs.get('name','')):n.attrs.get('content','') for n in nodes if n.tag=='meta'}
    # Author metadata, never an arbitrary @mention in a caption, proves ownership.
    author = metas.get('instapp:owner_user_name','').lower()
    title = metas.get('og:title','')
    m = re.match(r'^(?:[^\n]{0,160}\(@([A-Za-z0-9_.]+)\)|([A-Za-z0-9_.]+))\s+on Instagram\s*:',title,re.I)
    if not author and m:author = (m[1] or m[2]).lower()
    canonical = metas.get('og:url') or next((n.attrs.get('href','') for n in nodes if n.tag=='link' and n.attrs.get('rel')=='canonical'),'')
    if current and author==handle and (not canonical or shortcode(canonical)==current):
        p = blank_post(current,handle)
        p['text'] = metas.get('og:description','')
        p['text_source']='metadata'
        p['created_at']=iso_date(metas.get('article:published_time') or metas.get('datePublished'))
        p['date_source']='page' if p['created_at'] else 'unknown'
        for k,kind in (('og:image','image'),('og:video','video'),('og:video:secure_url','video')):
            item=media_item(metas.get(k,''),kind)
            if item:p['media'].append(item)
        p['captures']=[{'timestamp':timestamp,'url':page_url,'archive_url':f'https://web.archive.org/web/{timestamp}/{page_url}'}]
        posts[p['id']]=merge_post(posts.get(p['id'],blank_post(current,handle)),p)
    # Link discovery is scoped to an account profile or verified post, never a login page.
    path = urlsplit(original_url(page_url)).path.strip('/').lower()
    profile = path == handle
    if profile or posts:
        for n in nodes:
            if n.tag=='a':
                url = original_url(urljoin('https://www.instagram.com/',n.attrs.get('href','')))
                if shortcode(url):links.add(post_url(url))
    return list(posts.values()), sorted(links)
