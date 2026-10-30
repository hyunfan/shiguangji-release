"""Bounded, anonymous transport for explicitly supported public recovery sources."""
from __future__ import annotations
import gzip
import io
import re
import threading
import time
import zlib
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from shiguangji.core.core_errors import ArchiveError
from shiguangji.media.media_download import download_media
from shiguangji.sources.cancellation import CancellableClient
from shiguangji.sources.retry_policy import DeferredRequest
from shiguangji.platforms.extractor import media_item


class SourceError(ArchiveError):
    def __init__(self, message, status=0, *, request_sent=True):
        super().__init__(message)
        self.status = status
        self.code = status
        self.request_sent = request_sent


def validate_source_url(url):
    try:
        if not isinstance(url, str) or any(ord(character) < 33 or ord(character) == 127 for character in url):
            raise ValueError()
        u = urlsplit(url)
        if (u.scheme != 'https' or u.username is not None or u.password is not None
                or u.port not in (None, 443) or u.fragment):
            raise ValueError()
        if u.hostname in {'pbs.twimg.com','video.twimg.com','p.twimg.com','abs.twimg.com'}:
            valid = (media_item(url) is not None and re.fullmatch(r'/(media|tweet_video|ext_tw_video|amplify_video|tweet_video_thumb|ext_tw_video_thumb|amplify_video_thumb)/[A-Za-z0-9_./:-]+',u.path)
                     and not any(part in {'.','..'} for part in u.path.split('/')))
        elif u.hostname == 'cdn.syndication.twimg.com':
            valid = u.path == '/tweet-result'
        elif u.hostname in {'api.fxtwitter.com','api.vxtwitter.com'}:
            valid = re.fullmatch(r'/[A-Za-z0-9_]{1,15}/status/\d+', u.path) is not None
            if u.hostname == 'api.fxtwitter.com' and re.fullmatch(r'/2/profile/(?:[A-Za-z0-9_]{1,15}|id:[0-9]{1,20})', u.path):
                valid = not u.query
        elif u.hostname == 'index.commoncrawl.org':
            valid = u.path == '/collinfo.json' or re.fullmatch(r'/CC-MAIN-\d{4}-\d{2}-index', u.path) is not None
        elif u.hostname == 'data.commoncrawl.org':
            valid = (re.fullmatch(r'/crawl-data/[A-Za-z0-9_./-]+\.warc\.gz', u.path) is not None
                     and not any(p in {'.','..'} for p in u.path.split('/')))
        else:
            valid = False
        if not valid:
            raise ValueError()
        return u.hostname
    except ValueError as error:
        raise SourceError('已拒绝不属于已支持恢复来源的链接。',request_sent=False) from error


def validate_source_redirect(requested, received):
    """Validate every redirect before following it, plus the final resource."""
    validate_source_url(requested)
    validate_source_url(received)
    expected, actual = media_item(requested), media_item(received)
    if expected:
        if not actual or actual['key'] != expected['key']:
            raise SourceError('媒体跳转到了不同资源，未作为恢复结果。')
        return
    original, final = urlsplit(requested), urlsplit(received)
    if (original.hostname, original.path, original.query) != (final.hostname, final.path, final.query):
        raise SourceError('公开来源跳转到了不同资源，已停止请求。')


class SourceRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_source_redirect(req.full_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SourceClient(CancellableClient):
    def __init__(self, stop=None, log=lambda message:None, proxy='', delay=0.8, max_requests=650, budget=None):
        self.budget = budget
        self.stop = stop or threading.Event()
        self.log, self.delay, self.max_requests = log, delay, max_requests
        self.requests, self.last = 0, 0.0
        self.failures, self.blocked = {}, set()
        self.retry_policy = None
        handlers = [SourceRedirect()]
        if proxy:
            if not re.fullmatch(r'https?://[^\s/]+(?::\d+)?/?', proxy):
                raise ValueError('代理地址应为 HTTP 或 HTTPS 地址。')
            handlers.append(ProxyHandler({'http':proxy,'https':proxy}))
        self.opener = build_opener(*handlers)
    def wait(self, seconds):
        self.check()
        if self.budget:
            seconds = min(max(0, seconds), self.budget.timeout(max(0, seconds)))
        if self.stop.wait(max(0, seconds)):
            self.check()
        self.check()
    def open(self, url, headers=None):
        self.check()
        host = validate_source_url(url)
        if self.retry_policy:
            try:self.retry_policy.check(url)
            except DeferredRequest as error:
                raise SourceError(str(error),request_sent=False) from error
        supplied = dict(headers or {})
        if any(key.lower() not in {'range','accept','accept-encoding'} or (key.lower()=='accept-encoding' and value!='identity') for key,value in supplied.items()):
            raise SourceError('多源恢复不发送登录凭据或任意请求头。')
        if host in self.blocked:
            raise SourceError(f'{host} 本轮连续连接失败，暂时停止请求；下轮可重试。',request_sent=False)
        sent=False;last_status=0
        for attempt in range(2):
            self.check()
            if self.requests >= self.max_requests:
                if self.budget:
                    self.budget.finish('source_limit', '达到本批多源请求上限')
                detail=f' 上次返回 HTTP {last_status}。' if last_status else ''
                raise SourceError('达到本轮多源请求上限，剩余项目可继续恢复。'+detail,last_status,request_sent=sent)
            self.wait(max(self.delay, 1.5 if host == 'index.commoncrawl.org' else 0) - (time.monotonic()-self.last))
            if self.budget:
                self.budget.before_request()
            self.last = time.monotonic();self.requests += 1
            try:
                sent=True
                response = self.opener.open(Request(url, headers={'User-Agent':'X-Wayback-Library/1.9 (personal media recovery)','Accept-Encoding':'identity',**supplied}), timeout=self.budget.timeout(20) if self.budget else 20)
                try:
                    validate_source_redirect(url, response.url)
                except Exception:
                    response.close();raise
                self.failures[host] = 0
                if self.retry_policy:
                    from shiguangji.sources.retry_policy import TrackedResponse
                    return TrackedResponse(response,self.retry_policy,url)
                return response
            except (HTTPError, URLError, TimeoutError, OSError, HTTPException) as error:
                status = error.code if isinstance(error,HTTPError) else 0
                last_status=status
                transient = not status or status in {429,500,502,503,504}
                pause = 3 * (attempt+1)
                if isinstance(error,HTTPError):
                    try:pause=min(60,max(pause,int(error.headers.get('Retry-After',pause))))
                    except ValueError:pass
                    error.close()
                if self.retry_policy:self.retry_policy.failure(url,status,pause)
                if transient:
                    self.failures[host] = self.failures.get(host,0)+1
                    if self.failures[host] >= 3:self.blocked.add(host)
                else:
                    # A definite HTTP answer proves the host is reachable. In
                    # particular, CDX uses 404 for an ordinary empty result.
                    self.failures[host] = 0
                if not transient or attempt == 1 or host in self.blocked:
                    raise SourceError(f'{host} 请求失败：HTTP {status}' if status else f'{host} 连接失败：{type(error).__name__}',status) from error
                self.log(f'{host} 暂时不可用，{pause} 秒后重试。')
                self.wait(pause)
    def fetch(self, url, limit, headers=None):
        if not isinstance(limit, int) or limit <= 0:
            raise SourceError('来源响应必须设置有效的大小限制。')
        try:
            with self.open(url, headers) as response:
                chunks, size = [], 0
                while True:
                    self.check()
                    chunk = getattr(response, 'read1', response.read)(min(256 * 1024, limit + 1 - size))
                    self.check()
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise SourceError('来源响应超过本轮大小限制。')
                data = b''.join(chunks)
                meta={'url':response.url,'status':getattr(response,'status',200),'content_type':response.headers.get_content_type(),
                      'content_encoding':response.headers.get('Content-Encoding',''),'content_range':response.headers.get('Content-Range',''),
                      'headers':dict(response.headers)}
                if meta['content_encoding'].lower() in {'gzip', 'x-gzip'} and not any(k.lower()=='range' for k in (headers or {})):
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:data=stream.read(limit+1)
                    except (OSError,EOFError,zlib.error) as error:raise SourceError('来源压缩响应不完整。') from error
                    if len(data)>limit:raise SourceError('来源解压后超过大小限制。')
                    meta['content_encoding']=''
                self.check()
                return data,meta
        except (HTTPError, URLError, TimeoutError, OSError, HTTPException, EOFError) as error:
            if self.retry_policy:self.retry_policy.failure(url)
            self.check()
            status = error.code if isinstance(error, HTTPError) else 0
            if isinstance(error, HTTPError):
                error.close()
            raise SourceError(f'来源读取中断：{type(error).__name__}', status) from error
    def download(self, url, destination, max_bytes):
        if not media_item(url):raise SourceError('下载链接没有明确媒体身份。')
        try:
            return download_media(self,url,destination,max_bytes)
        except SourceError:
            raise
        except ArchiveError as error:
            self.check()
            cause = error.__cause__
            status = cause.code if isinstance(cause, HTTPError) else 0
            raise SourceError(str(error), status) from error
