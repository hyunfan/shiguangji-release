"""Bounded discovery of known video identities in historical public embeds.

All I/O is injected and all requested URLs are on web.archive.org. This module
never contacts current Nitter/X endpoints, executes JavaScript, or downloads
media. Historical players are queried only when their URLs were observed; the
syndication and Nitter lookups are scoped to the known post ID. ID-first CDX
prefixes intentionally do not enumerate an entire metadata endpoint: captures
with other leading query parameters need an observed full URL.

Format references:
https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server
https://github.com/vercel/react-tweet/blob/main/packages/react-tweet/src/api/fetch-tweet.ts
https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/twitter.py
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, parse_qsl, urlencode, unquote, urlsplit, urlunsplit

from extractor import Document, Node, X_HOSTS, original_url, post_id
from public_sources import _items, _restricted, safe_media, author_matches

CDX = "https://web.archive.org/cdx/search/cdx"
INDEX_LIMIT = 256 * 1024
PAGE_LIMIT = 2 * 1024 * 1024
SYNDICATION_HOSTS = {"cdn.syndication.twimg.com", "syndication.twitter.com", "syndication.twimg.com"}
TRACKING = {"embed_source", "ref_src", "ref_url", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
_PLAYER = re.compile(r"/i/(?:videos(?:/tweet)?|cards/tfw/v1)/([0-9]+)/?")
_BLOCKED_CLASSES = {"quote", "quote-big", "quote-card", "quoted-tweet", "tweet-quote", "QuoteTweet"}


def _url(value):
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value):
        raise ValueError("invalid_source_url")
    u = urlsplit(original_url(value))
    if (u.scheme not in {"http", "https"} or u.username is not None or u.password is not None
            or u.port not in (None, 80, 443) or u.fragment):
        raise ValueError("invalid_source_url")
    return u


def _without_tracking(url):
    u = _url(url)
    pairs = parse_qsl(u.query, keep_blank_values=True, max_num_fields=50)
    kept = [(key, value) for key, value in pairs if key.lower() not in TRACKING]
    if len(kept) == len(pairs):
        return url
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(kept), ""))


def endpoint_kind(url, pid, handle):
    """Recognize only explicit, single-post endpoint identities."""
    try:
        u = _url(url)
        if u.hostname in X_HOSTS:
            match = _PLAYER.fullmatch(u.path)
            return "player" if match and match[1] == pid else ""
        if u.hostname == "nitter.net":
            return "nitter" if re.fullmatch(rf"/{re.escape(handle)}/status/{re.escape(pid)}/?", u.path, re.I) else ""
        if u.hostname in SYNDICATION_HOSTS:
            query = parse_qs(u.query, keep_blank_values=True, max_num_fields=50)
            if u.path == "/tweet-result" and query.get("id") == [pid]:
                return "syndication"
            if u.path == "/tweets.json" and len(query.get("ids", [])) == 1:
                ids = query["ids"][0].split(",")
                if len(ids) <= 20 and pid in ids and all(re.fullmatch(r"[0-9]{1,25}", value) for value in ids):
                    return "tweets_json"
    except (ValueError, TypeError):
        pass
    return ""


def query_plans(post, observed_urls=()):
    pid, handle = str(post.get("id", "")), str(post.get("username", "")).lower()
    if not re.fullmatch(r"[0-9]{1,25}", pid) or not re.fullmatch(r"[a-z0-9_]{1,15}", handle):
        raise ValueError("An exact post ID and username are required")
    plans, seen = [], set()
    def add(url, match, observed_at=""):
        kind = endpoint_kind(url, pid, handle)
        key = (url, match)
        if kind and key not in seen:
            seen.add(key)
            plans.append({"url": url, "match": match, "kind": kind, "observed_at": observed_at})
    for observed in list(observed_urls)[:30]:
        if isinstance(observed, (tuple, list)):
            if not observed:continue
            url, evidence = observed[0], str(observed[1]) if len(observed) > 1 else ""
        else:
            url, evidence = observed, "observed-url"
        if not isinstance(url, str):continue
        try:
            url = original_url(url)
            kind = endpoint_kind(url, pid, handle)
            if not kind:continue
            canonical = _without_tracking(url)
            # Strip only listed tracking parameters; preserve unknown functional
            # parameters and every identity parameter.
            if canonical != url:add(canonical, "exact", evidence)
            add(url, "exact", evidence)
        except ValueError:
            continue
    add(f"https://cdn.syndication.twimg.com/tweet-result?id={pid}", "prefix")
    for host in ("cdn.syndication.twimg.com", "syndication.twitter.com", "syndication.twimg.com"):
        add(f"https://{host}/tweets.json?ids={pid}", "prefix")
    add(f"https://nitter.net/{handle}/status/{pid}", "exact")
    return plans


def _same_endpoint(plan, original, pid, handle):
    if endpoint_kind(original, pid, handle) != plan["kind"]:
        return False
    expected, actual = _url(plan["url"]), _url(original)
    if (expected.hostname, expected.path) != (actual.hostname, actual.path):
        return False
    if plan["match"] == "prefix":
        # ID-first index prefixes can match IDs with an extra digit; the parsed
        # endpoint identity above must match the complete requested ID.
        return True
    return sorted(parse_qsl(expected.query, keep_blank_values=True)) == sorted(parse_qsl(actual.query, keep_blank_values=True))


def _related(target, candidate):
    if not candidate or candidate["kind"] not in {"video", "hls"}:
        return False
    key = str(target.get("key", ""))
    if key == candidate["key"]:
        return True
    match = re.match(r"^(video\.twimg\.com/(?:ext_tw_video|amplify_video)/[0-9]+/)", key)
    return bool(match and candidate["key"].startswith(match[1]))


def decoded_media_urls(value):
    """Decode at most two URL-encoding layers, never evaluate embedded script."""
    if not isinstance(value, str):return []
    value = html.unescape(value).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/").replace("\\u0026", "&")
    for _ in range(2):
        decoded = unquote(value)
        if decoded == value:break
        value = decoded
    found = []
    pattern = r'''(?<![A-Za-z0-9_.-])(?:(?:https?:)?//)?video\.twimg\.com/[^\s"'<>\\]+'''
    for token in re.findall(pattern, value):
        url = token if token.startswith("https://") else "https://" + re.sub(r"^(?:http:)?//", "", token)
        item = safe_media(url)
        if item and item["kind"] in {"video", "hls"} and url not in found:found.append(url)
    return found


def _own_nodes(node):
    if node.tag in {"script", "style"} or any(node.has_class(name) for name in _BLOCKED_CLASSES):return
    yield node
    for child in node.children:
        if isinstance(child, Node):yield from _own_nodes(child)


def _nitter_media(raw, pid, handle):
    document = Document(raw)
    containers = [node for node in document.root.walk() if node.has_class("main-tweet")]
    if not containers:
        containers = [node for node in document.root.walk() if node.has_class("timeline-item")]
    for container in containers[:30]:
        nodes = list(_own_nodes(container))
        matched = False
        for node in nodes:
            href = node.attrs.get("href", "")
            if href.startswith("/"):href = "https://twitter.com" + href.split("#", 1)[0]
            else:href = href.replace("https://nitter.net/", "https://twitter.com/").split("#", 1)[0]
            if post_id(href, handle) == pid:
                matched = True;break
        if matched:
            return decoded_media_urls(" ".join(str(value) for node in nodes for value in node.attrs.values()))
    return []


def _parse_json(raw):
    raw = raw.strip().removeprefix("/**/").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Historical widgets used JSONP; accept a plain callback wrapper only,
        # and parse its argument as data. No JavaScript is ever executed.
        match = re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\s*\((.*)\)\s*;?", raw, re.S)
        if not match:raise
        return json.loads(match[1])


def _tweet_objects(data, pid):
    queue = [data]
    visited = 0
    while queue and visited < 100:
        value = queue.pop(0);visited += 1
        if isinstance(value, list):
            queue.extend(value[:30]);continue
        if not isinstance(value, dict):continue
        if str(value.get("id_str") or value.get("id") or "") == pid:
            yield value
            continue
        if isinstance(value.get(pid), dict):
            keyed = value[pid]
            if not (keyed.get("id_str") or keyed.get("id")):
                keyed = {**keyed, "id_str": pid}
            queue.append(keyed)
        for key in ("tweets", "body", "data", "results"):
            if isinstance(value.get(key), (dict, list)):queue.append(value[key])


def _html_restricted(raw):
    text = Document(raw).root.text().lower()
    return any(phrase in text for phrase in ("age-restricted", "age restricted", "log in to view", "sign in to view",
               "these tweets are protected", "this account is private", "this account’s posts are protected"))


def _extract(raw, kind, post, target):
    pid, handle = str(post["id"]), post["username"].lower()
    urls = []
    if kind in {"syndication", "tweets_json"}:
        try:data = _parse_json(raw)
        except (ValueError, RecursionError):return [], "invalid_metadata"
        if isinstance(data, dict) and _restricted(data):return [], "restricted"
        for tweet in _tweet_objects(data, pid):
            if _restricted(tweet):return [], "restricted"
            author = tweet.get("user") or tweet.get("author") or {}
            if not author_matches(post, author):continue
            normalized = dict(tweet)
            if not normalized.get("mediaDetails"):
                entities = normalized.get("extended_entities") or normalized.get("entities") or {}
                if isinstance(entities, dict):normalized["mediaDetails"] = entities.get("media", [])
            def normalize_media(value, depth=0):
                if depth > 8:return value
                if isinstance(value, list):return [normalize_media(item, depth + 1) for item in value[:50]]
                if isinstance(value, dict):
                    result = {}
                    for key, child in value.items():
                        decoded = decoded_media_urls(child) if key in {"url", "src", "media_url_https", "poster", "thumbnail_url"} and isinstance(child, str) else []
                        result[key] = decoded[0] if len(decoded) == 1 else normalize_media(child, depth + 1)
                    return result
                return value
            for key in ("mediaDetails", "video", "photos"):
                if key in normalized:normalized[key] = normalize_media(normalized[key])
            for item in _items(normalized, "syndication", pid, handle):
                urls.extend(item.get("urls", []))
        # No recursive quote/reply traversal or fallback to scanning an entire
        # JSON payload when its target post/author identity cannot be verified.
    else:
        if _html_restricted(raw):return [], "restricted"
        urls = _nitter_media(raw, pid, handle) if kind == "nitter" else decoded_media_urls(raw)
    matched = []
    for raw_url in urls:
        for url in decoded_media_urls(raw_url):
            candidate = safe_media(url)
            if _related(target, candidate) and url not in matched:matched.append(url)
    return matched, "found" if matched else "not_found"


class ArchivedSources:
    def __init__(self, read, check=lambda: None, log=lambda message: None, max_queries=8, max_captures=3, max_records=30):
        """read(url, limit) -> text, or (text, {'url': final_replay_url}).

        The caller supplies its Archive-only transport and cancellation. A final
        response URL, when supplied, is verified against the requested endpoint.
        Limits apply per discover() call; no files or collection state are changed.
        """
        self.read, self.check, self.log = read, check, log
        self.max_queries = max(1, min(int(max_queries), 12))
        self.max_captures = max(1, min(int(max_captures), 3))
        self.max_records = max(1, min(int(max_records), 100))
        self.last_report = []
        self.summary = {}

    def _read(self, url, limit):
        self.check()
        result = self.read(url, limit)
        self.check()
        raw, meta = result if isinstance(result, tuple) else (result, {})
        if isinstance(raw, bytes):raw = raw.decode("utf-8-sig")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > limit:raise ValueError("response_too_large")
        final = meta.get("url", url) if isinstance(meta, dict) else url
        received = urlsplit(final)
        if (received.scheme != "https" or received.hostname != "web.archive.org" or received.username is not None
                or received.password is not None or received.port not in (None,443) or received.fragment):
            raise ValueError("unexpected_archive_redirect")
        if url.startswith(CDX + "?"):
            if received.path != "/cdx/search/cdx":raise ValueError("unexpected_archive_redirect")
        elif not re.match(r"^/web/[0-9]{14}[a-z_]*/https?://", received.path):
            raise ValueError("unexpected_archive_redirect")
        return raw, final

    def discover(self, post, target, observed_urls=()):
        plans = query_plans(post, observed_urls)
        pid, handle = str(post["id"]), post["username"].lower()
        self.last_report = []
        report = self.summary = {"source": "wayback_public_embed", "status": "not_found", "queries": self.last_report,
            "captures_checked": 0, "requests": 0, "scope": "bounded_id_prefixes_and_observed_urls", "complete": len(plans) <= self.max_queries}
        if post.get("content_reason") == "restricted":
            report["status"] = "restricted";return []
        if target.get("kind") not in {"video", "hls"}:
            report["status"] = "unsupported";return []
        found, seen_captures = {}, set()
        for plan in plans[:self.max_queries]:
            self.check()
            params = {"url": plan["url"], "matchType": plan["match"], "fl": "timestamp,original,mimetype,statuscode,digest",
                      "filter": "statuscode:(200|-)", "collapse": "digest", "limit": str(self.max_records), "gzip": "false"}
            query_url = CDX + "?" + urlencode(params)
            entry = {**plan, "query_url": query_url, "status": "not_found", "captures_checked": 0, "evidence_urls": []}
            report["queries"].append(entry)
            try:
                report["requests"] += 1
                raw, _ = self._read(query_url, INDEX_LIMIT)
                rows = []
                for line in raw.splitlines()[:self.max_records + 1]:
                    parts = line.split()
                    if not parts:continue
                    if len(parts) != 5 or not re.fullmatch(r"[0-9]{14}", parts[0]):raise ValueError("invalid_index_response")
                    timestamp, original, mime, status, digest = parts
                    try:matches = status in {"200", "-"} and _same_endpoint(plan, original, pid, handle)
                    except (ValueError, TypeError):matches = False
                    if not matches:continue
                    rows.append((timestamp, original))
                if len(raw.splitlines()) >= self.max_records:report["complete"] = False
                for timestamp, original in rows[-self.max_captures:]:
                    self.check()
                    if (timestamp, original) in seen_captures:continue
                    seen_captures.add((timestamp, original))
                    evidence = f"https://web.archive.org/web/{timestamp}id_/{original}"
                    report["requests"] += 1
                    entry["captures_checked"] += 1;report["captures_checked"] += 1
                    entry["evidence_urls"].append(evidence)
                    body, final = self._read(evidence, PAGE_LIMIT)
                    final_original = original_url(final)
                    if not _same_endpoint(plan, final_original, pid, handle):raise ValueError("unexpected_capture_redirect")
                    final_timestamp = re.match(r"^/web/([0-9]{14})", urlsplit(final).path)[1]
                    urls, status = _extract(body, plan["kind"], post, target)
                    if status == "restricted":
                        entry["status"] = report["status"] = "restricted"
                        return list(found.values())
                    if status == "invalid_metadata":entry.update(status="failed",error="invalid_metadata")
                    for url in urls:
                        item = safe_media(url)
                        found.setdefault(url, {"url": url, "kind": item["kind"], "key": item["key"], "evidence_url": final,
                            "captured_at": final_timestamp, "source": "wayback_public_embed", "endpoint": final_original,
                            "discovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
                    if urls:entry["status"] = "found"
            except Exception as error:
                if type(error).__name__ in {"Cancelled", "CancelledError"}:raise
                self.check()
                entry.update(status="failed", error=type(error).__name__)
            finally:
                self.log(f"历史公开嵌入：帖子 {pid} · {plan['kind']} · {entry['status']}。")
        report["status"] = "found" if found else "failed" if any(x["status"] == "failed" for x in report["queries"]) else "not_found"
        return list(found.values())
