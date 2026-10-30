"""Discover media from public, current post metadata; never a historical snapshot.

Only exact post/author matches are accepted, and quote/parent trees are not read.
No cookies, bearer tokens or authenticated endpoints are used. Transport, retry
limits and cancellation belong to the caller. Source results are kept on the post
even when nothing can be recovered.

Sources: github.com/vercel/react-tweet (syndication response schema),
github.com/FxEmbed/FxEmbed/wiki/Status-Fetch-API (optional public mirror).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from shiguangji.platforms.extractor import MEDIA_HOSTS, media_item, media_belongs_to_post, post_id

API_HOSTS = frozenset({"cdn.syndication.twimg.com", "api.fxtwitter.com", "api.vxtwitter.com"})
MAX_METADATA_BYTES = 2 * 1024 * 1024
_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"
_MEDIA_PATH = re.compile(r"^/(?:media|tweet_video|ext_tw_video|amplify_video|tweet_video_thumb|ext_tw_video_thumb|amplify_video_thumb)/[A-Za-z0-9_./:-]+$")


def syndication_token(pid: str) -> str:
    """Public embed cache token, equivalent to the embed's base-36 calculation.

    This token is derived only from the public post ID, not an access credential.
    Stop fractional conversion at the IEEE-754 rounding interval, as JavaScript
    Number.toString(36) does, without introducing a JavaScript dependency.
    """
    if not re.fullmatch(r"[0-9]{1,25}", str(pid)):
        raise ValueError("Invalid post ID")
    value = float(pid) / 1e15 * math.pi
    integer = int(value)
    fraction = value - integer
    delta = math.ulp(value) / 2
    digits = []
    while fraction > delta:
        fraction *= 36
        delta *= 36
        digit = int(fraction)
        fraction -= digit
        digits.append(digit)
        if fraction > 0.5 and fraction + delta > 1:
            for index in range(len(digits) - 1, -1, -1):
                if digits[index] < 35:
                    digits[index] += 1
                    break
                digits[index] = 0
            else:
                integer += 1
            break
    head = ""
    while integer:
        head = _DIGITS[integer % 36] + head
        integer //= 36
    return (head + "".join(_DIGITS[x] for x in digits)).replace("0", "") or "0"


def safe_media(value):
    """Reject arbitrary hosts, paths, credentials, ports and redirect gadgets."""
    if not isinstance(value, str) or any(ord(c) < 33 for c in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.hostname not in MEDIA_HOSTS
                or url.username is not None or url.password is not None
                or url.port not in (None, 443) or url.fragment
                or not _MEDIA_PATH.fullmatch(url.path)
                or any(segment in {".", ".."} for segment in url.path.split("/"))):
            return None
    except ValueError:
        return None
    return media_item(value)


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _own_media(entry, pid, handle):
    return media_belongs_to_post(entry, pid)


def _rank(variant):
    try:
        return float(variant.get("bitrate") or 0)
    except (ValueError, TypeError):
        return 0


def _items(tweet, source, pid, handle):
    entries = []
    if source == "syndication":
        details = _list(tweet.get("mediaDetails"))
        entries.extend(details)
        # Embeds can expose incomplete mediaDetails alongside an older video
        # representation. Read both; deduplicate their encodings below.
        entries.extend(_list(tweet.get("photos")))
        if isinstance(tweet.get("video"), dict):
            entries.append(tweet["video"])
    else:
        media = _dict(tweet.get("media"))
        entries.extend(_list(media.get("all")))
        entries.extend(_list(media.get("photos")))
        entries.extend(_list(media.get("videos")))
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not _own_media(entry, pid, handle):
            continue
        for field in ("media_url_https", "thumbnail_url", "poster", "url"):
            item = safe_media(entry.get(field))
            if item:
                result.append(item)
        variants = _list(_dict(entry.get("video_info")).get("variants")) or _list(entry.get("variants"))
        valid = [m for variant in sorted((v for v in variants if isinstance(v, dict)), key=_rank, reverse=True)
                 if (m := safe_media(variant.get("url") or variant.get("src")))]
        # One media entry per actual movie. Prefer an MP4, keeping lower bitrate
        # URLs as fallbacks instead of counting each encoding as a separate film.
        videos = [m for m in valid if m["kind"] == "video"]
        chosen = videos or [m for m in valid if m["kind"] == "hls"]
        if chosen:
            first = chosen[0]
            first["urls"] = list(dict.fromkeys(u for m in chosen for u in m["urls"]))
            result.append(first)
    deduplicated = {}
    for item in result:
        identity = item["key"]
        if item["kind"] == "video":
            match = re.match(r"^(video\.twimg\.com/(?:ext_tw_video|amplify_video)/[0-9]+/)", identity)
            if match:
                identity = match[1]
        if identity in deduplicated:
            prior = deduplicated[identity]
            prior["urls"] = list(dict.fromkeys(prior["urls"] + item["urls"]))
        else:
            deduplicated[identity] = item
    return list(deduplicated.values())


def _restricted(data):
    if any(data.get(key) is True for key in ("protected", "is_private", "age_restricted", "requires_login")):
        return True
    author = _dict(data.get("user") or data.get("author"))
    if author.get("protected") is True:
        return True
    message = str(data.get("message", "")).upper()
    if message in {"PRIVATE_TWEET", "PROTECTED_TWEET", "LOGIN_REQUIRED", "AGE_RESTRICTED"}:
        return True
    if data.get("__typename") == "TweetTombstone":
        # Tombstones can be deleted posts or an explicit login/age gate. Do not
        # try the optional mirror to get around a gate returned by the embed.
        tombstone = json.dumps(data.get("tombstone", {}), ensure_ascii=False).lower()
        return any(s in tombstone for s in ("age-restricted", "age restricted", "log in", "sign in", "protected", "private account"))
    return False


def author_matches(post, author):
    if not isinstance(author, dict):
        return False
    expected = set(post.get('author_ids', []))
    uid = str(author.get('id_str') or author.get('id') or author.get('rest_id') or '')
    if expected:
        return uid.isdigit() and expected == {uid}
    return str(author.get('screen_name', '')).lower() == post.get('username', '').lower()


def discover(post, fetch, check, log, enable_mirrors=False, recover_text=False):
    """Return validated media and update post['source_checks'] diagnostics.

    fetch(url, limit, headers=None) -> (decoded bytes, metadata dictionary);
    metadata supports url/content_type. check() must raise on cancellation.
    log(message) receives short status messages, never tweet bodies or media.
    Result items use extractor.media_item's schema plus provenance fields.
    """
    pid = str(post.get("id", ""))
    handle = str(post.get("username", "")).lower()
    if not re.fullmatch(r"[0-9]{1,25}", pid) or not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
        raise ValueError("Public media discovery requires an exact post ID and username")
    checks = post.get("source_checks")
    if not isinstance(checks, dict):
        checks = post["source_checks"] = {}
    sources = [("syndication", "https://cdn.syndication.twimg.com/tweet-result?" + urlencode(
        {"id": pid, "lang": "en", "token": syndication_token(pid)}))]
    if enable_mirrors:
        sources.append(("fxtwitter", f"https://api.fxtwitter.com/{handle}/status/{pid}"))
        sources.append(("vxtwitter", f"https://api.vxtwitter.com/{handle}/status/{pid}"))
    found = {}
    gated = post.get("content_reason") == "restricted"
    for source, url in sources:
        check()
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        report = {"source": source, "checked_at": now, "evidence_url": url,
                  "scope": "current_public", "status": "pending", "media": 0, "error": ""}
        checks[source] = report
        try:
            if gated:
                report.update(status="restricted", error="该帖子存在登录、隐私或年龄限制，已跳过公开来源查询。")
                continue
            body, meta = fetch(url, MAX_METADATA_BYTES, headers={"Accept": "application/json"})
            check()
            if not isinstance(body, bytes) or len(body) > MAX_METADATA_BYTES:
                raise ValueError("metadata_too_large")
            meta = _dict(meta)
            # The caller must restrict all redirect hops. Additionally ensure
            # the final API resource is still for this exact request/post ID.
            final = urlsplit(meta.get("url") or url)
            expected = urlsplit(url)
            if final.scheme != "https" or final.netloc != expected.netloc or final.path != expected.path or final.query != expected.query:
                raise ValueError("unexpected_metadata_redirect")
            content_type = str(meta.get("content_type", "")).lower()
            if content_type and "json" not in content_type:
                raise ValueError("metadata_not_json")
            data = json.loads(body.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("invalid_metadata_shape")
            if _restricted(data):
                gated = True
                report.update(status="restricted", error="公开接口返回访问限制。")
                continue
            if data.get("__typename") == "TweetTombstone" or not data or data.get("code") == 404:
                report.update(status="not_found", error="公开接口没有可用帖子。")
                continue
            tweet = _dict(data.get("tweet")) if source == "fxtwitter" else data
            if _restricted(tweet):
                gated = True
                report.update(status="restricted", error="公开接口返回访问限制。")
                continue
            if source == "fxtwitter" and data.get("code") != 200:
                raise ValueError("mirror_api_error")
            if source == 'vxtwitter':
                if data.get('error'):raise ValueError('mirror_api_error')
                entries=_list(data.get('media_extended'))+[{'url':u} for u in _list(data.get('mediaURLs'))]
                tweet={'id_str':data.get('tweetID'),'author':{'screen_name':data.get('user_screen_name')},
                       'url':data.get('tweetURL'),'text':data.get('text'),'media':{'all':entries}}
            author = _dict(tweet.get("user") if source == "syndication" else tweet.get("author"))
            if str(tweet.get("id_str") or tweet.get("id") or "") != pid or not author_matches(post, author):
                report.update(status="identity_mismatch", error="返回的帖子 ID 或作者与目标不一致，未采用。")
                continue
            if tweet.get("url") and post_id(tweet["url"]) != pid:
                report.update(status="identity_mismatch", error="返回的帖子链接与目标不一致，未采用。")
                continue
            if recover_text and not post.get('text'):
                text = tweet.get('full_text') or tweet.get('text')
                if isinstance(text,str) and text.strip() and len(text)<=200000:
                    from shiguangji.platforms.extractor import GENERIC_DESCRIPTIONS
                    if text.strip().lower() not in GENERIC_DESCRIPTIONS:
                        post.update(text=text.strip(),text_source='public_metadata',content_status='recovered',
                                    content_reason='',content_note='',text_provenance={
                                        'source':source,'url':url,'scope':'current_public','checked_at':now,
                                        'post_id':pid,'author_id':str(author.get('id_str') or author.get('id') or author.get('rest_id') or ''),
                                        'username':str(author.get('screen_name') or '')})
            items = _items(tweet, source, pid, str(author.get('screen_name') or handle).lower())
            own = set()
            for item in items:
                own.add(item["key"])
                if item["key"] in found:
                    found[item["key"]]["urls"] = list(dict.fromkeys(found[item["key"]]["urls"] + item["urls"]))
                    continue
                item.update(source=source, evidence_url=url, discovered_at=now, source_kind="current_public")
                found[item["key"]] = item
            report.update(status="found" if own else "no_media", media=len(own))
            if recover_text and post.get('text'):
                report['text_recovered']=True
                break
        except Exception as exc:
            if type(exc).__name__ in {"Cancelled", "CancelledError"}:
                raise
            check()
            code = getattr(exc, "code", None) or getattr(exc, "status", None)
            close = getattr(exc, "close", None)
            if callable(close):
                close()
            report.update(status="not_found" if code == 404 else "rate_limited" if code == 429 else "unavailable",
                          error=f"HTTP {code}" if isinstance(code, int) else type(exc).__name__)
            if isinstance(exc, ValueError) and str(exc) in {"metadata_too_large", "unexpected_metadata_redirect",
                    "metadata_not_json", "invalid_metadata_shape", "mirror_api_error"}:
                report["error"] = str(exc)
            # Keep diagnostics useful without leaking response bodies, headers,
            # proxy credentials or unrelated URLs from an exception message.
        finally:
            log(f"公开媒体来源 {source}：帖子 {pid}，{report['status']}，候选 {report['media']} 项。")
    return list(found.values())
