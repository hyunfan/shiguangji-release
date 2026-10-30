"""Conservative extraction from archived Twitter HTML and embedded JSON (stdlib only)."""
from __future__ import annotations
import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, parse_qs
from shiguangji.core.archive_urls import original_url  # re-exported for platform/source callers

X_HOSTS = {"twitter.com", "www.twitter.com", "mobile.twitter.com", "m.twitter.com",
           "x.com", "www.x.com", "mobile.x.com", "m.x.com"}
MEDIA_HOSTS = {"pbs.twimg.com", "video.twimg.com", "abs.twimg.com", "p.twimg.com"}
TEXT_PRIORITY = {"public_metadata": 0, "metadata": 1, "html": 2, "json": 2, "json_ld": 2,
                 "extended_tweet": 3, "note_tweet": 4}
GENERIC_DESCRIPTIONS = {
    "from breaking news and entertainment to sports and politics, get the full story with all the live commentary.",
    "this post is unavailable.", "this tweet is unavailable.",
    "this post was deleted.", "this tweet was deleted.",
    "age-restricted adult content", "sign in to x", "log in to twitter",
    "javascript is not available.",
}

def username(value: str) -> str:
    value = original_url(value.strip())
    if re.match(r"^(?:www\.)?(twitter|x)\.com/", value, re.I):
        value = "https://" + value
    if "://" in value:
        u = urlsplit(value)
        if u.hostname not in X_HOSTS:
            raise ValueError("请输入 X/Twitter 用户名或该用户的主页链接。")
        value = u.path.strip("/").split("/")[0]
    value = value.lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,15}", value) or value in {"home", "search", "i", "intent", "settings"}:
        raise ValueError("用户名应为 1–15 位字母、数字或下划线，例如 @jack；不是用户数字 UID。")
    return value

def post_id(url: str, handle: str = "") -> str:
    try:
        u = urlsplit(original_url(url))
    except ValueError:
        return ""
    if u.hostname not in X_HOSTS:
        return ""
    m = re.match(r"^/([\w]+)/status(?:es)?/(\d+)(?:/|$)", u.path, re.I)
    return m.group(2) if m and (not handle or m.group(1).lower() == handle.lower()) else ""

def belongs(url: str, handle: str) -> bool:
    try:
        u = urlsplit(original_url(url))
    except ValueError:
        return False
    return u.hostname in X_HOSTS and u.path.strip("/").split("/")[0].lower() == handle.lower()

def iso_date(value) -> str:
    if not value:
        return ""
    try:
        if str(value).replace(".", "", 1).isdigit():
            n = float(value)
            dt = datetime.fromtimestamp(n / 1000 if n > 1e11 else n, timezone.utc)
        else:
            try:
                dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                dt = parsedate_to_datetime(str(value))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError, OSError):
        return ""

def snowflake_date(pid: str) -> str:
    # Older sequential IDs have no recoverable timestamp. Never use capture time as post time.
    if not pid.isdigit() or int(pid) < 29700859247:
        return ""
    date = iso_date((int(pid) >> 22) + 1288834974657)
    return date if date and "2010-11-04" <= date[:10] <= datetime.now(timezone.utc).date().isoformat() else ""

def media_item(url: str, kind: str = "") -> dict | None:
    url = original_url(url)
    try:
        u = urlsplit(url)
        valid = u.scheme in {"http", "https"} and u.hostname in MEDIA_HOSTS and not u.username and u.port in (None, 80, 443)
    except ValueError:
        return None
    if not valid:
        return None
    if not any(x in u.path for x in ("/media/", "/tweet_video/", "/ext_tw_video/", "/amplify_video/", "/tweet_video_thumb/", "/ext_tw_video_thumb/", "/amplify_video_thumb/")):
        return None
    if not kind:
        kind = "hls" if ".m3u8" in u.path else "video" if ".mp4" in u.path else "image"
    path = re.sub(r":(?:orig|large|medium|small|thumb)$", "", u.path)
    key = (u.hostname or "") + path
    variants = [url]
    if u.hostname == "pbs.twimg.com":
        extension = re.search(r"\.(jpg|jpeg|png|gif|webp|avif)$", path, re.I)
        fmt = parse_qs(u.query).get("format", [""])[0].lower() or (extension.group(1).lower() if extension else "")
        if fmt in {"jpg", "jpeg", "png", "gif", "webp", "avif"}:
            stem = path[:extension.start()] if extension else path
            key = u.hostname + stem + "." + fmt
            variants += [f"https://pbs.twimg.com{stem}.{fmt}:orig", f"https://pbs.twimg.com{stem}?format={fmt}&name=orig",
                         f"https://pbs.twimg.com{stem}?format={fmt}&name=large", f"https://pbs.twimg.com{stem}.{fmt}:large", f"https://pbs.twimg.com{stem}.{fmt}"]
    return {"key": key, "kind": kind, "urls": list(dict.fromkeys(variants)), "status": "pending", "path": "", "error": ""}

def blank_post(pid: str, handle: str) -> dict:
    date = snowflake_date(pid)
    return {"id": pid, "username": handle, "text": "", "text_source": "", "created_at": date,
            "date_source": "snowflake" if date else "unknown", "url": f"https://twitter.com/{handle}/status/{pid}",
            "media": [], "captures": [], "text_versions": [], "content_status": "link_only", "content_note": "", "content_reason": ""}

def merge_post(old: dict, new: dict) -> dict:
    for key in ("author_ids", "observed_usernames"):
        values = old.get(key, []) + new.get(key, [])
        if values:
            old[key] = sorted(set(values))
    for item in new.get("captures", []):
        if item not in old["captures"]:
            old["captures"].append(item)
    # Preserve versions accumulated within a page when merging it into the database.
    # Descriptions often include author names/quotes: length alone cannot establish
    # that they are more complete than text taken from the actual tweet container.
    versions = old.get("text_versions", []) + new.get("text_versions", []) + [old.get("text", ""), new.get("text", "")]
    if new.get("text"):
        old_priority = TEXT_PRIORITY.get(old.get("text_source", ""), 2)
        new_priority = TEXT_PRIORITY.get(new.get("text_source", ""), 2)
        # Older libraries did not record the text source and often kept quoted
        # page descriptions. Migrate only an exact outer quote pair surrounding
        # the complete new body, never a substring or an inferred truncation.
        prior = old.get("text", "").strip()
        body = new["text"].strip()
        quoted_legacy = (not old.get("text_source") and new.get("text_source") in {"html", "json"}
                         and any(prior == opening + body + closing for opening, closing in (("\"", "\""), ("“", "”"))))
        if quoted_legacy or not old.get("text") or new_priority > old_priority or (new_priority == old_priority and len(new["text"]) >= len(old["text"])):
            old["text"] = new["text"]
            old["text_source"] = new.get("text_source", "")
            if new.get('text_provenance'):
                old['text_provenance']=new['text_provenance']
            elif new.get('text_source')!='public_metadata':
                old.pop('text_provenance',None)
    old["text_versions"] = list(dict.fromkeys(v for v in versions if isinstance(v, str) and v and v != old.get("text")))
    if new.get("date_source") == "page" or not old.get("created_at"):
        old["created_at"], old["date_source"] = new.get("created_at", ""), new.get("date_source", "unknown")
    existing = {m["key"]: m for m in old["media"]}
    for m in new.get("media", []):
        if m["key"] in existing:
            existing[m["key"]]["urls"] = list(dict.fromkeys(existing[m["key"]]["urls"] + m["urls"]))
        else:
            old["media"].append(m)
            existing[m["key"]] = m
    old["content_status"] = "recovered" if old["text"] or old["media"] else "link_only"
    if old["text"]:
        old["content_note"], old["content_reason"] = "", ""
    elif new.get("content_note"):
        old["content_note"], old["content_reason"] = new["content_note"], new.get("content_reason", "")
    return old

class Node:
    def __init__(self, tag="", attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or []), parent, []
    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()
    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        if self.tag == "br":
            return "\n"
        if self.tag == "img":
            return self.attrs.get("alt", "")
        return "".join(c.text() if isinstance(c, Node) else c for c in self.children)
    def has_class(self, value):
        return value in self.attrs.get("class", "").split()

class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(source)
    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.current = node
    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)
    def handle_endtag(self, tag):
        n = self.current
        while n.parent:
            if n.tag == tag:
                self.current = n.parent
                return
            n = n.parent
    def handle_data(self, data):
        self.current.children.append(data)

def walk_json(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk_json(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from walk_json(value)

def mapping(value):
    return value if isinstance(value, dict) else {}

def media_belongs_to_post(entry, pid):
    """Reject explicit foreign ownership after the enclosing author is verified.

    Missing ownership fields are not proof on their own: callers must already
    have scoped this entry to the matching post's attachment collection.
    """
    if not isinstance(entry, dict):
        return False
    for key in ('source_status_id_str', 'source_status_id', 'source_tweet_id', 'tweet_id', 'post_id'):
        if entry.get(key) is not None and str(entry[key]) != str(pid):
            return False
    candidate = post_id(entry.get('expanded_url', ''))
    # A renamed author can leave an old handle in this link. Match the post ID.
    if candidate and candidate != str(pid):
        return False
    availability = mapping(entry.get('ext_media_availability') or entry.get('mediaAvailability'))
    status = availability.get('status')
    return not status or (isinstance(status, str) and status.lower() == 'available')

def api_v2_posts(payload, handle):
    """Join each v2 envelope's authors and media by their explicit API IDs.

    WARC resource records can contain API v2 JSON at an ordinary status URL.
    Its includes arrays also contain quoted/replied-to authors and media, so
    neither array order nor the enclosing capture URL establishes ownership.
    """
    for envelope in walk_json(payload):
        includes = mapping(envelope.get("includes"))
        data = envelope.get("data")
        users = includes.get("users")
        if not isinstance(users, list) or not isinstance(data, (dict, list)):
            continue
        authors, ambiguous = {}, set()
        for user in users:
            if not isinstance(user, dict):
                continue
            uid, screen = str(user.get("id") or ""), str(user.get("username") or "").lower()
            if not uid.isdigit() or not re.fullmatch(r"[a-z0-9_]{1,15}", screen):
                continue
            identity = (screen, bool(user.get("protected") or user.get("withheld")))
            if uid in authors and authors[uid] != identity:
                ambiguous.add(uid)
            authors[uid] = identity
        media_by_key, ambiguous_media = {}, set()
        for entry in includes.get("media", []) if isinstance(includes.get("media"), list) else []:
            if not isinstance(entry, dict) or not isinstance(entry.get("media_key"), str):
                continue
            key = entry["media_key"]
            if key in media_by_key and media_by_key[key] != entry:
                ambiguous_media.add(key)
            media_by_key[key] = entry
        tweets = list(data) if isinstance(data, list) else [data]
        if isinstance(includes.get("tweets"), list):
            tweets.extend(includes["tweets"])
        for tweet in tweets:
            if not isinstance(tweet, dict):
                continue
            pid, uid = str(tweet.get("id") or ""), str(tweet.get("author_id") or "")
            if not pid.isdigit() or uid in ambiguous or authors.get(uid) != (handle.lower(), False) or tweet.get("withheld"):
                continue
            p = blank_post(pid, handle)
            p["author_ids"] = [uid]
            p["observed_usernames"] = [handle]
            text, note = tweet.get("text"), mapping(tweet.get("note_tweet")).get("text")
            if isinstance(text, str) and text:
                p["text"], p["text_source"] = html.unescape(text), "json"
            if isinstance(note, str) and note:
                if p["text"] and p["text"] != html.unescape(note):
                    p["text_versions"] = [p["text"]]
                p["text"], p["text_source"] = html.unescape(note), "note_tweet"
            date = iso_date(tweet.get("created_at"))
            if date:
                p["created_at"], p["date_source"] = date, "page"
            refs = tweet.get("referenced_tweets")
            retweet = isinstance(refs, list) and any(isinstance(ref, dict) and ref.get("type") == "retweeted" for ref in refs)
            attachments = mapping(tweet.get("attachments"))
            keys = attachments.get("media_keys")
            # A repost may repeat the source's attachment keys. Its body is a
            # target-authored record, but the source media belongs to that post.
            borrowed = attachments.get("media_source_tweet")
            if not retweet and not borrowed and isinstance(keys, list):
                for key in dict.fromkeys(k for k in keys if isinstance(k, str)):
                    entry = media_by_key.get(key)
                    if not entry or key in ambiguous_media or not media_belongs_to_post(entry, pid):
                        continue
                    kind = entry.get("type")
                    urls = []
                    if kind == "photo":
                        urls.append(entry.get("url"))
                    elif kind in {"video", "animated_gif"}:
                        urls.append(entry.get("preview_image_url"))
                        variants = entry.get("variants")
                        if isinstance(variants, list):
                            def bitrate(variant):
                                try:
                                    return max(0, int(variant.get("bit_rate") or variant.get("bitrate") or 0))
                                except (TypeError, ValueError, OverflowError):
                                    return 0
                            variants = sorted((v for v in variants if isinstance(v, dict)), key=bitrate, reverse=True)
                            urls.extend(v.get("url") for v in variants)
                    for url in urls:
                        if isinstance(url, str):
                            media = media_item(url)
                            if media:
                                p["media"].append(media)
            if p["text"] or p["media"]:
                yield p

class _Page:
    """Posts found on one captured page; every post carries this capture as evidence."""
    def __init__(self, page_url: str, timestamp: str, handle: str):
        self.page_url, self.handle = page_url, handle
        self.capture = {"timestamp": timestamp, "url": page_url, "archive_url": f"https://web.archive.org/web/{timestamp}/{page_url}"}
        self.found = {}

    def add(self, p):
        p["captures"] = [self.capture]
        self.found[p["id"]] = merge_post(self.found.get(p["id"], blank_post(p["id"], self.handle)), p)

    def link(self, value):
        try:
            return urljoin(self.page_url, original_url(value))
        except ValueError:
            return ""


def _dated(p, value):
    date = iso_date(value)
    if date:
        p["created_at"], p["date_source"] = date, "page"


def _attach(p, urls):
    for url in urls:
        m = media_item(url)
        if m:
            p["media"].append(m)


def _permalink_posts(page, nodes, target_pid):
    """A URL in a target user's timeline is useful evidence even when the HTML is a JS shell."""
    if target_pid:
        page.add(blank_post(target_pid, page.handle))
    for n in nodes:
        if "href" not in n.attrs:
            continue
        href = page.link(n.attrs.get("href", ""))
        pid = post_id(href, page.handle)
        if pid:
            page.add(blank_post(pid, page.handle))


def _in_container(x, container):
    """False for nodes inside a quoted post or a nested post container."""
    ancestor = x
    while ancestor is not container and ancestor:
        if ancestor.has_class("QuoteTweet") or ancestor.has_class("quoted-tweet") or ancestor.attrs.get("data-testid") == "quoteTweet" or (ancestor.tag == "div" and ancestor.attrs.get("role") == "link"):
            return False
        if ancestor.attrs.get("data-tweet-id") or ancestor.tag == "article":
            return False
        ancestor = ancestor.parent
    return True


def _html_container_posts(page, nodes):
    """Legacy Twitter cards, mobile tables, and server-rendered modern articles."""
    handle, link = page.handle, page.link
    for n in nodes:
        is_container = n.attrs.get("data-tweet-id") or n.has_class("tweet") or n.tag == "article"
        if not is_container:
            continue
        descendants = [x for x in n.walk() if _in_container(x, n)]
        screen = n.attrs.get("data-screen-name", "").lower()
        pid = str(n.attrs.get("data-tweet-id", ""))
        if screen and screen != handle:
            continue
        if not (pid.isdigit() and screen == handle):
            # The first status permalink identifies the article; quoted posts/replies must not own it.
            links = [(post_id(link(x.attrs.get("href", ""))),
                      post_id(link(x.attrs.get("href", "")), handle)) for x in descendants if x.tag == "a"]
            first = next((pair for pair in links if pair[0]), ("", ""))
            pid = first[1]
        if not pid:
            continue
        p = blank_post(pid, handle)
        uid = n.attrs.get("data-user-id", "")
        if uid.isdigit():
            p["author_ids"] = [uid]
            p["observed_usernames"] = [handle]
        textnode = next((x for x in descendants if x.has_class("tweet-text") or x.attrs.get("data-testid") == "tweetText" or x.has_class("js-tweet-text")), None)
        if textnode:
            p["text"] = textnode.text().strip()
            p["text_source"] = "html"
        for x in descendants:
            if x.tag == "time" or "data-time" in x.attrs or "data-time-ms" in x.attrs:
                _dated(p, x.attrs.get("datetime") or x.attrs.get("data-time-ms") or x.attrs.get("data-time"))
        _attach(p, [link(x.attrs[k]) for x in descendants for k in ("src", "data-image-url", "poster", "data-src", "data-video-url", "data-full-url", "href") if k in x.attrs])
        for x in descendants:
            for srcset in (x.attrs.get("srcset", ""), x.attrs.get("data-srcset", "")):
                _attach(p, [link(candidate.strip().split()[0]) for candidate in srcset.split(",") if candidate.strip()])
        page.add(p)


def _json_payloads(source, nodes):
    """Raw API JSON and JSON script blocks (including GraphQL legacy objects)."""
    payloads = []
    try:
        payloads.append(json.loads(source))
    except (ValueError, RecursionError):
        pass
    for n in nodes:
        if n.tag != "script":
            continue
        raw = "".join(x for x in n.children if isinstance(x, str)).strip()
        candidates = [raw]
        m = re.match(r"(?:window\.)?__[A-Z_]+__\s*=\s*", raw)
        if m:
            candidates.append(raw[m.end():])
        for candidate in candidates:
            try:
                payloads.append(json.JSONDecoder().raw_decode(candidate)[0])
                break
            except (ValueError, RecursionError):
                continue
    return payloads


def _known_users(objects):
    users = {}
    for obj in objects:
        details = mapping(obj.get("legacy")) or obj
        screen = details.get("screen_name")
        uid = str(obj.get("rest_id") or details.get("id_str") or details.get("id") or "")
        if screen and uid:
            users[uid] = str(screen).lower()
    return users


def _json_ld_post(page, obj):
    handle = page.handle
    author = mapping(obj.get("author"))
    post_url = obj.get("url") or mapping(obj.get("mainEntityOfPage")).get("@id") or page.page_url
    pid = post_id(str(post_url), handle)
    if pid and belongs(str(author.get("url", "")), handle):
        p = blank_post(pid, handle)
        p["text"] = str(obj.get("articleBody") or obj.get("text") or "")
        p["text_source"] = "json_ld"
        _dated(p, obj.get("datePublished") or obj.get("dateCreated"))
        for media in (obj.get("image"), obj.get("video"), obj.get("associatedMedia")):
            for entry in media if isinstance(media, list) else [media]:
                _attach(p, [entry if isinstance(entry, str) else mapping(entry).get("contentUrl", "")])
        page.add(p)


def _variant_bitrate(variant):
    try:
        return max(0, int(variant.get("bitrate") or 0))
    except (ValueError, TypeError, OverflowError):
        return 0


def _legacy_tweet_post(page, obj, users):
    """A REST/GraphQL tweet object by the target handle, or None."""
    handle = page.handle
    legacy = obj.get("legacy") if isinstance(obj.get("legacy"), dict) else obj
    pid = str(legacy.get("id_str") or obj.get("rest_id") or legacy.get("id") or "")
    user = mapping(obj.get("user")) or mapping(mapping(mapping(obj.get("core")).get("user_results")).get("result"))
    screen = str(user.get("screen_name") or mapping(user.get("legacy")).get("screen_name") or mapping(user.get("core")).get("screen_name") or users.get(str(legacy.get("user_id_str") or legacy.get("user_id")), "")).lower()
    extended = mapping(legacy.get("extended_tweet"))
    note = mapping(mapping(mapping(obj.get("note_tweet")).get("note_tweet_results")).get("result"))
    texts = [(legacy.get("text"), "json"), (legacy.get("full_text"), "json"),
             (extended.get("full_text"), "extended_tweet"), (note.get("text"), "note_tweet")]
    texts = [(html.unescape(value), origin) for value, origin in texts if isinstance(value, str) and value]
    if not pid.isdigit() or screen != handle or not texts:
        return None
    p = blank_post(pid, handle)
    uid = str(user.get("rest_id") or user.get("id_str") or user.get("id") or mapping(user.get("legacy")).get("id_str") or legacy.get("user_id_str") or legacy.get("user_id") or "")
    if uid.isdigit():
        p["author_ids"] = [uid]
        p["observed_usernames"] = [handle]
    p["text"], p["text_source"] = max(texts, key=lambda item: (TEXT_PRIORITY[item[1]], len(item[0])))
    p["text_versions"] = list(dict.fromkeys(value for value, origin in texts if value != p["text"]))
    _dated(p, legacy.get("created_at"))
    entries = []
    for container in (legacy, extended, note):
        for key in ("entities", "extended_entities", "entity_set"):
            media = mapping(container.get(key)).get("media")
            if isinstance(media, list):
                entries.extend(media)
    # Legacy reposts can repeat the original author's attachments at
    # the outer level, even without source_status_id on each entry.
    if any(container.get(key) for container in (obj, legacy)
           for key in ('retweeted_status', 'retweeted_status_result',
                       'retweeted_status_id', 'retweeted_status_id_str')):
        entries = []
    for entry in entries:
        if not media_belongs_to_post(entry, pid):
            continue
        _attach(p, [entry.get("media_url_https") or entry.get("media_url", "")])
        variants = [v for v in mapping(entry.get("video_info")).get("variants", []) or [] if isinstance(v, dict)]
        # Preserve every MP4 rendition, so a missing high-quality capture can fall back.
        for v in sorted(variants, key=_variant_bitrate, reverse=True):
            _attach(p, [v.get("url", "")])
    return p


def _json_posts(page, payload):
    for p in api_v2_posts(payload, page.handle):
        page.add(p)
    objects = list(walk_json(payload))
    users = _known_users(objects)
    for obj in objects:
        if obj.get("@type") == "SocialMediaPosting":
            _json_ld_post(page, obj)
        p = _legacy_tweet_post(page, obj, users)
        if p is not None:
            page.add(p)


def _metadata_post(page, nodes, target_pid):
    """Metadata is scoped to the page's own post, never to a profile or a reply."""
    meta_entries = [(n.attrs.get("property") or n.attrs.get("name"), n.attrs.get("content", "")) for n in nodes if n.tag == "meta"]
    meta = dict(meta_entries)
    # A captured status URL may contain the metadata of a different post.
    # Keep its permalink evidence, but never attach that other author's content.
    metadata_pid = post_id(page.link(meta.get("og:url", "")))
    if metadata_pid and metadata_pid != target_pid:
        meta, meta_entries = {}, []
    p = blank_post(target_pid, page.handle)
    description = meta.get("og:description") or meta.get("twitter:description") or ""
    if description and description.strip().lower() not in GENERIC_DESCRIPTIONS:
        p["text"] = description
        p["text_source"] = "metadata"
    _dated(p, meta.get("article:published_time"))
    _attach(p, [page.link(v) for k, v in meta_entries if k in {"og:image", "og:video", "og:video:url", "og:video:secure_url", "twitter:image", "twitter:image:src", "twitter:player:stream"}])
    page.add(p)


def _explain_missing_text(p, source):
    lower = source.lower()
    if "age-restricted adult content" in lower or "you’ll need to log in" in lower or "you'll need to log in" in lower:
        p.update(content_note="这份快照保存的是登录或年龄限制提示，没有公开可提取的正文。", content_reason="restricted")
    elif any(s in lower for s in ("this tweet was deleted", "this post was deleted", "this tweet is unavailable", "this post is unavailable")):
        p.update(content_note="这份快照提示帖子已删除或不可用。", content_reason="unavailable")
    elif "javascript" in lower and "tweettext" not in lower:
        p.update(content_note="这份快照只有页面框架，未包含帖子正文。", content_reason="shell")
    else:
        p.update(content_note="已读取快照，但未找到可可靠归属的正文。", content_reason="unparsed")


def extract(source: str, page_url: str, timestamp: str, handle: str) -> list[dict]:
    """Only attach media to a known target post container / structured tweet / single-post metadata."""
    nodes = list(Document(source).root.walk())
    page = _Page(page_url, timestamp, handle)
    target_pid = post_id(page_url, handle)
    _permalink_posts(page, nodes, target_pid)
    _html_container_posts(page, nodes)
    for payload in _json_payloads(source, nodes):
        _json_posts(page, payload)
    if target_pid:
        _metadata_post(page, nodes, target_pid)
    if target_pid and not page.found[target_pid]["text"]:
        _explain_missing_text(page.found[target_pid], source)
    return list(page.found.values())
