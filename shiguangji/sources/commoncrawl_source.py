"""Bounded Common Crawl fallback for media explicitly linked to a known post.

API/record formats: https://commoncrawl.org/cdxj-index and
https://commoncrawl.org/get-started . Network access is supplied by the caller;
this module never creates a connection or writes collection files itself.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zlib
from datetime import datetime
from urllib.parse import urlencode, urlsplit

from shiguangji.platforms.extractor import media_item

COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
INDEX_LIMIT = 512 * 1024
HEADER_LIMIT = 64 * 1024
RECORD_LIMIT = 64 * 1024 * 1024
RESULTS_PER_INDEX = 20
WARC_ATTEMPTS = 6
SCAN_VERSION = 2


class CommonCrawlError(ValueError):
    """A source response could not establish a complete, matching media file."""


def _inflate(data, limit, encoding="gzip"):
    if encoding == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                out = stream.read(limit + 1)
        except (OSError, EOFError, zlib.error) as error:
            raise CommonCrawlError("Common Crawl 压缩记录损坏或不完整。") from error
    else:
        out = None
        for window in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                inflater = zlib.decompressobj(window)
                candidate = inflater.decompress(data, limit + 1)
                if len(candidate) > limit or inflater.unconsumed_tail:
                    raise CommonCrawlError("Common Crawl 解压后的内容超过大小限制。")
                if not inflater.eof or inflater.unused_data:
                    raise CommonCrawlError("Common Crawl HTTP 压缩内容不完整。")
                out = candidate
                break
            except zlib.error:
                continue
        if out is None:
            raise CommonCrawlError("Common Crawl HTTP 压缩内容无法解码。")
    if len(out) > limit:
        raise CommonCrawlError("Common Crawl 解压后的内容超过大小限制。")
    return out


def _headers(data):
    split = data.find(b"\r\n\r\n", 0, HEADER_LIMIT + 4)
    delimiter = 4
    if split < 0:
        split = data.find(b"\n\n", 0, HEADER_LIMIT + 2)
        delimiter = 2
    if split < 0 or split > HEADER_LIMIT:
        raise CommonCrawlError("Common Crawl 记录头缺失或过长。")
    lines = data[:split].decode("latin-1").splitlines()
    if not lines:
        raise CommonCrawlError("Common Crawl 记录头为空。")
    headers = {}
    for line in lines[1:]:
        if ":" not in line or line.startswith((" ", "\t")):
            raise CommonCrawlError("Common Crawl 记录头格式无效。")
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key in headers and key in {"content-length", "transfer-encoding", "content-encoding", "warc-target-uri", "warc-type"}:
            raise CommonCrawlError("Common Crawl 记录包含冲突的关键头字段。")
        headers[key] = value
    return lines[0], headers, data[split + delimiter:]


def _number(value, maximum):
    if not re.fullmatch(r"[0-9]{1,20}", str(value)):
        raise CommonCrawlError("Common Crawl 记录的长度或偏移无效。")
    number = int(value)
    if number > maximum:
        raise CommonCrawlError("Common Crawl 记录超过大小限制。")
    return number


def _unchunk(data, limit):
    parts, position, total = [], 0, 0
    while position < len(data):
        end = data.find(b"\r\n", position, position + 1024)
        if end < 0:
            raise CommonCrawlError("Common Crawl 分块响应不完整。")
        token = data[position:end].split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9a-fA-F]{1,16}", token):
            raise CommonCrawlError("Common Crawl 分块长度无效。")
        size = int(token, 16)
        position = end + 2
        if size == 0:
            trailer = data[position:]
            if trailer == b"\r\n" or (len(trailer) <= HEADER_LIMIT and trailer.endswith(b"\r\n\r\n")):
                return b"".join(parts)
            raise CommonCrawlError("Common Crawl 分块响应结尾不完整。")
        total += size
        if total > limit or position + size + 2 > len(data) or data[position + size:position + size + 2] != b"\r\n":
            raise CommonCrawlError("Common Crawl 分块响应被截断或超过大小限制。")
        parts.append(data[position:position + size])
        position += size + 2
    raise CommonCrawlError("Common Crawl 分块响应缺少结束标记。")


from shiguangji.media.media_files import media_valid as _media_valid


def _reference_prefix(item, post):
    prefix = str(item.get("key", ""))
    match = re.fullmatch(r"video\.twimg\.com/(ext_tw_video|amplify_video)/(\d+)/", prefix)
    gif = re.fullmatch(r"video\.twimg\.com/tweet_video/([\w-]+)\.mp4", prefix)
    if not item.get("reference_only") or not (match or gif):
        return ""
    # A saved prefix alone is insufficient: require its thumbnail or video URL
    # in this post, preventing a broad CDN query from asserting ownership.
    for related in post.get("media", []):
        if related.get("reference_only"):
            continue
        for url in related.get("urls", []):
            associated = media_item(url)
            if not associated:
                continue
            key = associated["key"]
            if match and (key.startswith(f"pbs.twimg.com/{match[1]}_thumb/{match[2]}/") or key.startswith(prefix)):
                return prefix
            if gif and (re.fullmatch(r"pbs\.twimg\.com/tweet_video_thumb/" + re.escape(gif[1]) + r"\.(?:jpg|jpeg|png|gif|webp|avif)", key) or key == prefix):
                return prefix
    return ""


def _scope(item, post):
    if item.get("reference_only"):
        prefix = _reference_prefix(item, post)
        return (prefix, "prefix" if prefix.endswith("/") else "exact") if prefix else ("", "")
    if item.get("kind") not in {"image", "video"}:
        return "", ""
    for url in item.get("urls", []):
        normalized = media_item(url)
        if normalized and normalized["key"] == item.get("key"):
            if not any(p.get("key") == item["key"] for p in post.get("media", [])):
                return "", ""
            u = urlsplit(url)
            if u.hostname == "pbs.twimg.com":
                path = re.sub(r":(?:orig|large|medium|small|thumb)$", "", u.path)
                path = re.sub(r"\.(jpg|jpeg|png|gif|webp|avif)$", "", path, flags=re.I)
                return u.hostname + path, "prefix"
            movie = re.match(r"^/(ext_tw_video|amplify_video)/(\d+)/", u.path)
            if u.hostname == "video.twimg.com" and movie and item["kind"] == "video":
                return f"video.twimg.com/{movie[1]}/{movie[2]}/", "prefix"
            return normalized["key"], "exact"
    return "", ""


def _matches(url, item, prefix):
    candidate = media_item(url)
    if not candidate:
        return False
    if item.get("kind") == "video":
        return candidate["kind"] == "video" and (candidate["key"].startswith(prefix) if prefix.endswith("/") else candidate["key"] == prefix)
    if item.get("kind") == candidate["kind"] == "image" and prefix.startswith("pbs.twimg.com/"):
        return re.sub(r"\.(?:jpg|jpeg|png|gif|webp|avif)$", "", candidate["key"], flags=re.I) == prefix
    return candidate["key"] == item.get("key") and candidate["kind"] == item.get("kind")


def _warc_payload(raw, row, item, prefix, max_bytes):
    expanded_limit = min(max_bytes, RECORD_LIMIT) + 2 * HEADER_LIMIT
    content = _inflate(raw, expanded_limit) if raw.startswith(b"\x1f\x8b") else raw
    if len(content) > expanded_limit:
        raise CommonCrawlError("Common Crawl WARC 记录超过大小限制。")
    first, warc, block = _headers(content)
    if not re.fullmatch(r"WARC/1\.[01]", first) or warc.get("warc-type") != "response" or "warc-truncated" in warc:
        raise CommonCrawlError("Common Crawl 未提供完整的 HTTP 响应记录。")
    target = warc.get("warc-target-uri", "")
    if not _matches(target, item, prefix) or media_item(target)["key"] != media_item(row["url"])["key"]:
        raise CommonCrawlError("Common Crawl WARC 的媒体归属与索引不符。")
    length = _number(warc.get("content-length", ""), expanded_limit)
    if len(block) < length or block[length:].strip(b"\r\n"):
        raise CommonCrawlError("Common Crawl WARC 响应被截断或范围不匹配。")
    first, http, payload = _headers(block[:length])
    if not re.match(r"HTTP/\d(?:\.\d)? 200(?: |$)", first) or "content-range" in http:
        raise CommonCrawlError("Common Crawl 捕获的不是完整成功响应。")
    if "content-length" in http and len(payload) != _number(http["content-length"], expanded_limit):
        raise CommonCrawlError("Common Crawl 媒体长度与响应头不符。")
    transfer = http.get("transfer-encoding", "").lower()
    if transfer:
        if transfer != "chunked" or "content-length" in http:
            raise CommonCrawlError("Common Crawl HTTP 分块编码不受支持。")
        payload = _unchunk(payload, expanded_limit)
    encoding = http.get("content-encoding", "identity").lower()
    if encoding in {"gzip", "x-gzip", "deflate"}:
        payload = _inflate(payload, min(max_bytes, RECORD_LIMIT), "deflate" if encoding == "deflate" else "gzip")
    elif encoding != "identity":
        raise CommonCrawlError("Common Crawl HTTP 内容编码不受支持。")
    if len(payload) > min(max_bytes, RECORD_LIMIT) or not _media_valid(payload, item.get("kind")):
        raise CommonCrawlError("Common Crawl 内容不是可验证的完整媒体文件。")
    return payload


class CommonCrawlSource:
    def __init__(self, fetch, check=lambda: None, log=lambda message: None, max_indexes=6):
        self.fetch, self.check, self.log = fetch, check, log
        self.max_indexes = max(1, min(int(max_indexes), 12))
        self._collections = None
        self._queries = {}
        self.last_scope = []

    def _read(self, url, limit, headers=None):
        self.check()
        data, metadata = self.fetch(url, limit, headers=headers)
        self.check()
        if not isinstance(data, bytes) or len(data) > limit:
            raise CommonCrawlError("Common Crawl 响应超过大小限制。")
        if metadata.get("url", url) != url:
            raise CommonCrawlError("Common Crawl 请求跳转到了其他资源。")
        if headers and "Range" in headers and metadata.get("status") is not None and metadata["status"] != 206:
            raise CommonCrawlError("Common Crawl 未返回请求的局部文件范围。")
        return data, metadata

    def _indexes(self, post, item=None):
        if self._collections is None:
            raw, metadata = self._read(COLLECTIONS_URL, INDEX_LIMIT)
            if metadata.get("content_encoding", "").lower() == "gzip":
                raw = _inflate(raw, INDEX_LIMIT)
            try:
                listings = json.loads(raw)
            except (ValueError, UnicodeError) as error:
                raise CommonCrawlError("Common Crawl 集合列表格式无效。") from error
            if not isinstance(listings, list):
                raise CommonCrawlError("Common Crawl 集合列表格式无效。")
            self._collections = []
            seen = set()
            for collection in listings[:2048]:
                if not isinstance(collection, dict):
                    continue
                ident = collection.get("id", "")
                if isinstance(ident, str) and re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", ident) and ident not in seen:
                    seen.add(ident)
                    self._collections.append(ident)
            self._collections.sort(reverse=True)
            if not self._collections:
                self._collections = None
                raise CommonCrawlError("Common Crawl 未返回可用集合列表，不能据此判定已查完。")
        targets = []
        for value in [post.get("created_at", "")] + [c.get("timestamp", "") for c in post.get("captures", [])[:20]]:
            try:
                if re.fullmatch(r"\d{14}", str(value)):
                    date = datetime.strptime(value, "%Y%m%d%H%M%S")
                else:
                    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                targets.append((date.year, date.isocalendar().week))
            except (TypeError, ValueError, AttributeError):
                continue
        def distance(ident):
            year, week = (int(x) for x in ident.split("-")[-2:])
            return min((abs((year - y) * 53 + week - w) for y, w in targets), default=0)
        nearby = sorted(self._collections, key=distance)
        latest_count = min(2, self.max_indexes // 2) if targets else self.max_indexes
        chosen = nearby[:self.max_indexes - latest_count]
        chosen += [ident for ident in self._collections[:latest_count] if ident not in chosen]
        chosen += [ident for ident in nearby if ident not in chosen]
        if item is not None:
            from shiguangji.sources.search_plan import indexes_plan, update_progress
            plan = indexes_plan(chosen, chosen[:self.max_indexes], item.get('cc_plan'))
            item['cc_plan'] = plan
            chosen = plan['indexes']
            available = set(chosen)
            completed = [i for i in item.get("cc_completed_indexes", []) if i in available]
            pending = [i for i in item.get("cc_pending_indexes", []) if i in available and i not in completed]
            item["cc_completed_indexes"] = list(dict.fromkeys(completed))
            item["cc_pending_indexes"] = list(dict.fromkeys(pending))
            item["cc_index_errors"] = {index: error for index, error in item.get("cc_index_errors", {}).items() if index in available}
            item["cc_catalog_size"] = len(chosen)
            item["cc_remaining_indexes"] = [i for i in chosen if i not in completed]
            item["cc_more"] = bool(item["cc_remaining_indexes"])
            update_progress(plan, completed, pending, item["cc_index_errors"])
            chosen = pending + [i for i in chosen if i not in completed and i not in pending]
        return chosen[:self.max_indexes]

    def _prepare_progress(self, item, prefix, match):
        identity = hashlib.sha256(json.dumps([SCAN_VERSION, item.get("kind"), prefix, match]).encode()).hexdigest()
        if item.get("cc_scan_identity") != identity or item.get("cc_scan_version") != SCAN_VERSION:
            for key in ("cc_completed_indexes", "cc_pending_indexes", "cc_remaining_indexes"):
                item[key] = []
            item.pop("cc_plan", None)
            item["cc_index_errors"] = {}
            item["cc_more"] = True
            item["cc_catalog_size"] = 0
        item["cc_scan_identity"] = identity
        item["cc_scan_version"] = SCAN_VERSION
        # Do not trust malformed state imported from older manifests.
        for key in ("cc_completed_indexes", "cc_pending_indexes"):
            values = item.get(key, [])
            item[key] = list(dict.fromkeys(v for v in values if isinstance(v, str) and re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", v))) if isinstance(values, list) else []
        if not isinstance(item.get("cc_index_errors"), dict):
            item["cc_index_errors"] = {}

    def _complete_index(self, item, index):
        if index not in item["cc_completed_indexes"]:
            item["cc_completed_indexes"].append(index)
        item["cc_pending_indexes"] = [i for i in item["cc_pending_indexes"] if i != index]
        item["cc_remaining_indexes"] = [i for i in item["cc_remaining_indexes"] if i != index]
        item["cc_index_errors"].pop(index, None)
        item["cc_more"] = bool(item["cc_remaining_indexes"])
        self._update_plan(item)

    def _update_plan(self,item):
        if item.get("cc_plan"):
            from shiguangji.sources.search_plan import update_progress
            update_progress(item["cc_plan"],item["cc_completed_indexes"],item["cc_pending_indexes"],item["cc_index_errors"])

    def _pending_index(self, item, index, error, request_sent):
        if index not in item["cc_pending_indexes"]:
            item["cc_pending_indexes"].append(index)
        item["cc_index_errors"][index] = {"type": type(error).__name__, "error": str(error)[:400],
                                          "request_sent": bool(request_sent)}
        item["cc_more"] = True
        self._update_plan(item)

    def _query(self, index, pattern, match):
        url = f"https://index.commoncrawl.org/{index}-index?" + urlencode({
            "url": pattern, "matchType": match, "output": "json", "filter": "status:200",
            "fl": "url,timestamp,status,mime,filename,offset,length", "limit": RESULTS_PER_INDEX,
        })
        if url in self._queries:
            return url, self._queries[url]
        try:
            raw, metadata = self._read(url, INDEX_LIMIT)
        except Exception as error:
            # The CDX API uses 404 for an empty result; cancellation and all other
            # transport errors propagate so callers cannot cache a false absence.
            if getattr(error, "code", None) == 404 or re.search(r"HTTP(?: Error)?\s+404\b", str(error)):
                close = getattr(error, "close", None)
                if callable(close):
                    close()
                self._queries[url] = []
                if len(self._queries) > 256:
                    del self._queries[next(iter(self._queries))]
                return url, []
            raise
        if metadata.get("content_encoding", "").lower() == "gzip":
            raw = _inflate(raw, INDEX_LIMIT)
        rows = []
        try:
            for line in raw.decode("utf-8").splitlines()[:RESULTS_PER_INDEX]:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("url"), str) or not str(row.get("status", "")).isdigit():
                        raise ValueError("Unexpected CDX record schema")
                    rows.append(row)
        except (ValueError, UnicodeError) as error:
            raise CommonCrawlError("Common Crawl 索引响应无效。") from error
        self._queries[url] = rows
        if len(self._queries) > 256:
            del self._queries[next(iter(self._queries))]
        return url, rows

    def _recover_rows(self, rows, evidence, item, prefix, max_bytes, attempted):
        record_error = None
        for row in rows:
            self.check()
            if str(row.get("status", "")) != "200" or not _matches(row.get("url", ""), item, prefix):
                continue
            filename = row.get("filename", "")
            if not isinstance(filename, str) or not re.fullmatch(r"crawl-data/CC-MAIN-\d{4}-\d{2}/segments/[\w.-]+/warc/[\w.-]+\.warc\.gz", filename) or ".." in filename:
                record_error = CommonCrawlError("Common Crawl 匹配媒体的 WARC 路径无效。")
                continue
            try:
                offset = _number(row.get("offset", ""), 2 ** 63 - 1)
                length = _number(row.get("length", ""), min(max_bytes + 2 * HEADER_LIMIT, RECORD_LIMIT))
                if not length:
                    raise CommonCrawlError("Common Crawl 匹配媒体的 WARC 长度为零。")
            except CommonCrawlError as error:
                record_error = error
                continue
            identity = (filename, offset, length)
            if identity in attempted:
                record_error = CommonCrawlError("Common Crawl 匹配记录已在本轮尝试，但未恢复完整媒体。")
                continue
            if len(attempted) >= WARC_ATTEMPTS:
                raise CommonCrawlError("Common Crawl 达到本项最多 6 个存档记录的读取上限，集合仍待复查。")
            attempted.add(identity)
            retrieved_url = "https://data.commoncrawl.org/" + filename
            raw, metadata = self._read(retrieved_url, length, {"Range": f"bytes={offset}-{offset + length - 1}", "Accept-Encoding": "identity"})
            expected_range = f"bytes {offset}-{offset + length - 1}/"
            if len(raw) != length or (metadata.get("content_range") and not str(metadata["content_range"]).startswith(expected_range)):
                raise CommonCrawlError("Common Crawl 返回的文件范围与索引不符。")
            try:
                data = _warc_payload(raw, row, item, prefix, max_bytes)
            except CommonCrawlError as error:
                self.log(str(error))
                record_error = error
                continue
            return {"data": data, "source": "common_crawl", "url": row["url"],
                    "retrieved_url": retrieved_url, "captured_at": str(row.get("timestamp", "")),
                    "evidence_url": evidence}
        if record_error:
            raise record_error
        if len(rows) >= RESULTS_PER_INDEX:
            raise CommonCrawlError("Common Crawl 达到每集合 20 条结果的读取上限，不能判定该集合查询完整。")
        return None

    def recover(self, item, post, max_bytes):
        """Try one bounded round, updating resumable scan fields on ``item``.

        Persist the item in the caller's normal checkpoint even on failure.
        ``cc_completed_indexes`` contains only fully read empty/mismatched
        queries, never failed, cancelled, capped or unread media responses.
        ``cc_more`` permits the next round to bypass a whole-media empty cache.
        A fresh source instance refreshes the catalog for each collection run.
        """
        self.last_scope = []
        self.check()
        prefix, match = _scope(item, post)
        if not prefix or max_bytes < 0:
            return None
        if max_bytes == 0:
            max_bytes = RECORD_LIMIT
        self._prepare_progress(item, prefix, match)
        indexes = self._indexes(post, item)
        if not indexes:
            self.log("Common Crawl：已完成当前已知集合的有界查询；新集合发布后可继续检查。")
            return None
        self.log("Common Crawl：本轮续查 " + "、".join(indexes) + "；仅查询所列集合，不代表全部历史存档。")
        attempted = set()
        first_error = None
        for index in indexes:
            self.check()
            queried = False
            try:
                evidence, rows = self._query(index, prefix, match)
                queried = True
                self.last_scope.append(index)
                result = self._recover_rows(rows, evidence, item, prefix, max_bytes, attempted)
                if result:
                    item["cc_pending_indexes"] = [i for i in item["cc_pending_indexes"] if i != index]
                    item["cc_index_errors"].pop(index, None)
                    item["cc_recovered_index"] = index
                    item["cc_more"] = False
                    return result
                self._complete_index(item, index)
            except Exception as error:
                cancelled = type(error).__name__ in {"Cancelled", "CancelledError"} or getattr(error, 'batch_stop', False)
                if not queried and not cancelled and getattr(error, "request_sent", True):
                    self.last_scope.append(index)
                self._pending_index(item, index, error, queried or (not cancelled and getattr(error, "request_sent", True)))
                if cancelled:
                    raise
                first_error = first_error or error
                self.log(f"Common Crawl {index} 未完成：{type(error).__name__}。保留续查进度。")
                # A stopped source/budget cannot usefully query more indexes
                # this round. Real per-index errors may allow later indexes.
                if not getattr(error, "request_sent", True):
                    break
        if first_error:
            raise first_error
        return None
