"""Internet Archive transport, replay checks and CDX decoding."""
import gzip
import io
import re
import threading
import time
import zlib
from shiguangji.core.app_version import VERSION
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urljoin, unquote_plus, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from shiguangji.core.core_errors import ArchiveError, Cancelled
from shiguangji.core.archive_support import CDX, CDX_SUCCESS_FILTER
from shiguangji.platforms.extractor import media_item, original_url, post_id
from shiguangji.media.media_download import download_media
from shiguangji.sources.cancellation import CancellableClient

def archive_only(url):
    u = urlsplit(url)
    if u.scheme != "https" or u.hostname not in {"web.archive.org", "archive.org"} or u.port not in (None, 443) or u.username:
        raise ArchiveError("存档跳转到了非 Internet Archive 地址，已保留链接。")

class ArchiveRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith("http://web.archive.org/"):
            newurl = "https://" + newurl[7:]
        archive_only(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

class Client(CancellableClient):
    def __init__(self, stop=None, log=lambda x: None, proxy="", delay=1.2, attempts=3, budget=None):
        self.budget = budget
        self.stop = stop or threading.Event()
        self.log, self.delay, self.attempts = log, delay, attempts
        self.proxy = proxy
        handlers = [ArchiveRedirect()]
        if proxy:
            if not re.fullmatch(r"https?://[^\s/]+(?::\d+)?/?", proxy):
                raise ValueError("代理地址应为 http://127.0.0.1:端口 或 https://主机:端口。")
            handlers.append(ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = build_opener(*handlers)
        self.last = 0
        self.requests = 0
        self.retry_policy = None
    def wait(self, seconds):
        self.check()
        if self.budget:
            seconds = min(max(0, seconds), self.budget.timeout(max(0, seconds)))
        if self.stop.wait(max(0, seconds)):
            raise Cancelled()
        self.check()
    def open(self, url):
        archive_only(url)
        if self.retry_policy:self.retry_policy.check(url)
        for attempt in range(self.attempts):
            self.check()
            self.wait(self.delay - (time.monotonic() - self.last))
            self.last = time.monotonic()
            try:
                if self.budget:
                    self.budget.before_request()
                self.requests += 1
                response = self.opener.open(Request(url, headers={"User-Agent": f"X-Wayback-Library/{VERSION} (personal archive reader)", "Accept-Encoding": "identity"}), timeout=self.budget.timeout(35) if self.budget else 35)
                archive_only(response.url)
                try:
                    validate_replay_target(url, response.url)
                except ArchiveError:
                    response.close()
                    raise
                if self.retry_policy:
                    from shiguangji.sources.retry_policy import TrackedResponse
                    return TrackedResponse(response,self.retry_policy,url)
                return response
            except (HTTPError, URLError, TimeoutError, OSError, HTTPException) as e:
                retry_after = 0
                if isinstance(e, HTTPError):
                    try:retry_after = int(e.headers.get('Retry-After',0))
                    except (ValueError,TypeError):pass
                if self.retry_policy:self.retry_policy.failure(url,e.code if isinstance(e,HTTPError) else 0,retry_after)
                retry = not isinstance(e, HTTPError) or e.code in {429, 500, 502, 503, 504}
                if not retry or attempt + 1 == self.attempts:
                    if isinstance(e, HTTPError):
                        e.close()
                    raise ArchiveError(f"Internet Archive 请求失败：{e}") from e
                pause = min(60, 5 * 2 ** attempt)
                if isinstance(e, HTTPError):
                    try:
                        pause = min(300, max(pause, int(e.headers.get("Retry-After", pause))))
                    except ValueError:
                        pass
                    e.close()
                detail = f"HTTP {e.code}" if isinstance(e, HTTPError) else type(e).__name__
                self.log(f"请求暂未成功（{detail}），{pause} 秒后重试（{attempt + 1}/{self.attempts - 1}）")
                self.wait(pause)
    def text(self, url, limit=20 * 1024 * 1024):
        return self.text_with_meta(url,limit)[0]
    def text_with_meta(self, url, limit=20 * 1024 * 1024):
        """Keep the actual replay URL when a capture redirects to another date."""
        try:
            with self.open(url) as r:
                chunks, size = [], 0
                while size <= limit:
                    self.check()
                    chunk = getattr(r, 'read1', r.read)(min(256 * 1024, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                self.check()
                data = b''.join(chunks)
                if len(data) > limit:
                    raise ArchiveError("页面超过 20 MB，已记入失败清单。")
                return decode_page(data, r.headers, limit), {'url':r.url}
        except (TimeoutError, OSError, ValueError, HTTPException) as e:
            if self.retry_policy:self.retry_policy.failure(url)
            raise ArchiveError(f"读取存档失败：{e}") from e
    def cdx(self, url, match="exact"):
        """Stream sequential pages with a resumption key; never silently truncate at limit."""
        for records, _ in self.cdx_pages(url, match):
            yield records
    def identity_client(self, budget):
        client = Client(self.stop, self.log, self.proxy, attempts=1, budget=budget)
        client.retry_policy = self.retry_policy
        return client
    def identity_index(self, url, latest=False):
        # Sample both ends of a profile's history; no domain-wide or unbounded scan.
        params = {"url": url, "matchType": "exact", "fl": "timestamp,original,mimetype,statuscode,digest",
                  "filter": "statuscode:200", "collapse": "digest", "limit": "-3" if latest else "3", "gzip": "false"}
        return parse_cdx(self.text(CDX + "?" + urlencode(params), limit=128 * 1024))[0]
    def cdx_pages(self, url, match="exact", resume=""):
        seen = {resume} if resume else set()
        while True:
            params = {"url": url, "matchType": match, "fl": "timestamp,original,mimetype,statuscode,digest",
                      "filter": CDX_SUCCESS_FILTER, "collapse": "digest", "limit": "1000", "showResumeKey": "true", "gzip": "false"}
            if resume:
                params["resumeKey"] = resume
            body = self.text(CDX + "?" + urlencode(params))
            records, next_key = parse_cdx(body)
            if next_key and next_key in seen:
                raise ArchiveError("存档接口重复返回分页标记，已保留当前进度。")
            yield records, next_key
            if not next_key:
                return
            seen.add(next_key)
            resume = next_key
    def download(self, url, destination, max_bytes):
        return download_media(self, url, destination, max_bytes)


def decode_page(data, headers, limit=20 * 1024 * 1024):
    encoding = headers.get("Content-Encoding", "").lower().strip()
    try:
        if data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(limit + 1)
        elif encoding == "deflate":
            decoder = zlib.decompressobj()
            try:
                data = decoder.decompress(data, limit + 1)
            except zlib.error:
                decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                data = decoder.decompress(data, limit + 1)
            if len(data) <= limit and not decoder.eof:
                raise ArchiveError("压缩快照不完整，等待重试。")
        elif encoding not in {"", "identity", "gzip", "x-gzip"}:
            raise ArchiveError(f"快照使用尚不支持的压缩格式 {encoding}，已保留为待重试。")
        if len(data) > limit:
            raise ArchiveError("解压后的页面超过 20 MB，已记入失败清单。")
        charset = headers.get_content_charset() if hasattr(headers, "get_content_charset") else None
        result = data.decode(charset or "utf-8", errors="replace")
        if corrupt_page(result):
            raise ArchiveError("快照仍包含无法解码的二进制数据，未将其标为解析成功。")
        return result
    except (EOFError, OSError, zlib.error, LookupError) as error:
        raise ArchiveError(f"快照解压或解码失败：{error}") from error

def corrupt_page(raw):
    return raw.startswith("\x1f") or raw.count("\ufffd") > max(50, len(raw) // 50)

def media_search_prefix(item):
    u = urlsplit(item["urls"][0])
    path = re.sub(r":(?:orig|large|medium|small|thumb)$", "", u.path)
    if u.hostname == "pbs.twimg.com":
        path = re.sub(r"\.(jpg|jpeg|png|gif|webp|avif)$", "", path, flags=re.I)
    return f"{u.hostname}{path}"

def validate_replay_target(requested, received):
    if not re.match(r"https://web\.archive\.org/web/\d", requested):
        return
    original, final = original_url(requested), original_url(received)
    wanted_post = post_id(original)
    if wanted_post and post_id(final) != wanted_post:
        raise ArchiveError("快照跳转到了其他页面，未将其内容归入原帖子。")
    from shiguangji.platforms.instagram_extract import media_item as ig_media, shortcode as ig_code
    if ig_code(original) and ig_code(final) != ig_code(original):
        raise ArchiveError("Instagram 快照跳转到其他帖子或登录页，未使用该内容。")
    expected_media = media_item(original) or ig_media(original)
    if expected_media:
        actual_media = media_item(final) or ig_media(final)
        if not actual_media or actual_media["key"] != expected_media["key"]:
            raise ArchiveError("媒体快照跳转到了不同资源，保留为未恢复项。")

def parse_cdx(body):
    records, resume = [], ""
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) == 5 and re.fullmatch(r"\d{14}", fields[0]):
            records.append(dict(zip(("timestamp", "original", "mimetype", "statuscode", "digest"), fields)))
        elif len(fields) == 1 and not line.startswith("<"):
            resume = unquote_plus(line)
        else:
            raise ArchiveError("存档索引返回了无法识别的内容；可能是验证页或服务异常。")
    return records, resume
