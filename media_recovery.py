"""Recover known missing media using independent, explicitly attributed sources."""
from __future__ import annotations
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, unquote
from core_errors import ArchiveError
from archive_support import media_context, now, replay
from archive_http import media_search_prefix
from media_files import media_extension
from extractor import X_HOSTS, media_item, original_url
from source_http import SourceClient
from core_errors import failure_kind
from atomic_io import atomic_write_bytes


def image_variants(item):
    urls = list(item.get('urls', []))
    if urls and item['kind'] == 'image':
        u = urlsplit(urls[0])
        stem = re.sub(r'\.(?:jpg|jpeg|png|webp)(?::\w+)?$','',u.path)
        if u.hostname == 'pbs.twimg.com':
            for size in ('orig','large','medium','small'):
                for fmt in ('jpg','png'):
                    urls.append(f'https://pbs.twimg.com{stem}?format={fmt}&name={size}')
    return list(dict.fromkeys(url.replace('http://','https://',1) for url in urls))


def video_identity(item):
    match=re.match(r'^(video\.twimg\.com/(?:ext_tw_video|amplify_video)/[0-9]+/)',item.get('key',''))
    return match[1] if match else ''


def related(item, candidate):
    if item.get('reference_only'):
        return candidate['kind'] == 'video' and (candidate['key'].startswith(item['key']) if item['key'].endswith('/') else candidate['key']==item['key'])
    if item['key'] == candidate['key']:
        return True
    if item['kind'] == candidate['kind'] == 'video' and video_identity(item):
        return video_identity(item)==video_identity(candidate)
    if item['kind'] == candidate['kind'] == 'image':
        return re.sub(r'\.(?:jpg|jpeg|png|webp)$','',item['key']) == re.sub(r'\.(?:jpg|jpeg|png|webp)$','',candidate['key'])
    return False


def definite_http_absence(error):
    """Recognize transport status, never infer absence from an error message."""
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        status=getattr(error,'status',None) or getattr(error,'code',None)
        if isinstance(status,int) and status:
            return status in {404,410}
        # Client wraps urllib.HTTPError with an explicit cause. A contextual
        # exception may be unrelated, so do not follow __context__ here.
        error=getattr(error,'__cause__',None)
    return False


class RecoveryEngine:
    def __init__(self, collector, http=None, discover=None, common_crawl=None, archived_sources=None):
        self.c = collector
        self.http = http or SourceClient(collector.stop, collector.log, collector.options.get('proxy',''), budget=getattr(collector, 'budget', None))
        if http is None:self.http.retry_policy = getattr(collector,'retry_policy',None)
        if discover is None:
            from public_sources import discover
        self.discover = discover
        if common_crawl is None:
            from commoncrawl_source import CommonCrawlSource
            common_crawl = CommonCrawlSource(self.http.fetch,self.http.check,collector.log,max_indexes=collector.options.get('cc_indexes',6))
        self.cc = common_crawl
        self.max_bytes = int(collector.options.get('max_media_mb',512))*1024*1024
        self.stats = {'recovered':0,'cached':0,'checked':0,'requests':0}
        self.policy = json.dumps([3,bool(collector.options.get('common_crawl',True)),bool(collector.options.get('public_mirrors',True)),collector.options.get('cc_indexes',6)])
        self.local_candidates = {}
        self.player_entries = {}
        self.video_entry_cache = {}
        self.hls_candidates = {}
        self.archived_requests=0
        self.archived_failures=0
        if archived_sources is None:
            from archived_sources import ArchivedSources
            archived_sources=ArchivedSources(self.archived_read,self.http.check,collector.log)
        self.archived_sources=archived_sources
    def check_cached(self,p,m):
        if self.c.options.get('force_recheck') or m.get('recovery_error_kind')=='network':return False
        if self.c.options.get('common_crawl',True) and (m.get('cc_more') or m.get('cc_pending_indexes')):return False
        if m.get('recovery_context') != media_context(p,m)+self.policy:return False
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(m['recovery_checked_at'].replace('Z','+00:00'))).total_seconds()
            return 0<=age<86400
        except (KeyError,TypeError,ValueError):return False
    def record(self,m,source,status,error='',**extra):
        check={'source':source,'status':status,'checked_at':now(),**extra}
        if error:check['error']=str(error)[:400]
        checks=[x for x in m.get('recovery_checks',[]) if x.get('source')!=source]
        m['recovery_checks']=checks+[check]
        if status=='failed':m['recovery_error_kind']='network'
    def source_cached(self,m,source,context):
        if self.c.options.get('force_recheck'):return False
        for report in m.get('recovery_checks',[]):
            if report.get('source')!=source or report.get('status')!='not_found' or report.get('context')!=context:continue
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(report['checked_at'].replace('Z','+00:00'))).total_seconds()
                if 0<=age<86400:return True
            except (KeyError,TypeError,ValueError):pass
        return False
    def destination(self,p,m):
        month=p['created_at'][:7].replace('-','/') if p['created_at'] else 'unknown-date'
        return self.c.folder/'media'/month/(p['id']+'_'+hashlib.sha256(m['key'].encode()).hexdigest()[:12]+'.bin')
    def saved(self,p,m,result,source,origin,evidence='',captured_at=''):
        extension=result['file'].suffix.lower()
        if (m['kind']=='video') != (extension in {'.mp4','.webm'}):raise ArchiveError('恢复文件类型与目标不匹配。')
        old_reason=m.get('error','')
        m.update(status='saved',path=result['file'].relative_to(self.c.folder).as_posix(),bytes=result['bytes'],sha256=result['sha256'],
                 error='',error_kind='',source=source,source_url=origin,source_evidence=evidence,recovered_at=now(),source_captured_at=captured_at)
        m['urls']=list(dict.fromkeys([origin]+m.get('urls',[])))
        if old_reason:m['previous_recovery_error']=old_reason
        if m.get('reference_only'):
            m['recovered_video_url']=origin
        self.record(m,source,'saved',url=origin,evidence_url=evidence,captured_at=captured_at)
        self.stats['recovered']+=1
        if hasattr(self.c, 'mark_saved'):
            self.c.mark_saved(m)
        self.c.log(f"已补回 {'视频' if m['kind']=='video' else '图片'} · #{p['id']} · {source}")
    def direct(self,p,m,candidates,attempted=None):
        # A URL can be mentioned by several sources with different evidence.
        # Try it once for this media item, including across fallback phases.
        unique={}
        for url,evidence in candidates:
            if url.startswith('http://'):url='https://'+url[len('http://'):]
            candidate=media_item(url)
            if not candidate or not related(m,candidate) or candidate['kind']=='hls':continue
            unique.setdefault(url,evidence)
        if not unique:return False
        if attempted is None:attempted={}
        context=hashlib.sha256(json.dumps(sorted(unique)).encode()).hexdigest()
        if self.source_cached(m,'live_cdn',context):
            for url in unique:attempted.setdefault(url,None)
            return False
        if all(url in attempted for url in unique):return False
        for url,evidence in unique.items():
            if url in attempted:continue
            try:
                result=self.http.download(url,self.destination(p,m),self.max_bytes)
                self.saved(p,m,result,'live_cdn',result['archive_url'],evidence)
                return True
            except ArchiveError as error:
                attempted[url]=error
                if '连续连接失败' in str(error) or '请求上限' in str(error):break
        errors=[attempted[url] for url in unique if attempted.get(url) is not None]
        checked=sum(url in attempted for url in unique)
        retryable=checked<len(unique) or any(getattr(error,'status',0) not in {404,410} for error in errors)
        self.record(m,'live_cdn','failed' if retryable else 'not_found',errors[-1] if errors else '',variants_checked=checked,context=context)
        return False
    def cached_links(self,posts):
        prefixes={m['key']:video_identity(m) or m['key'] for p in posts for m in p['media'] if m['kind']=='video' and m['status']!='saved'}
        if not prefixes:return
        pending_ids={p['id'] for p in posts if any(m['kind']=='video' and m['status']!='saved' for m in p['media'])}
        for path in (self.c.folder/'raw').glob('*.txt'):
            self.http.check()
            raw=html.unescape(path.read_text(encoding='utf-8',errors='replace')).replace('\\/','/').replace('\\u002F','/').replace('\\u002f','/').replace('\\u0026','&')
            raw=unquote(raw)
            # Only endpoints literally present in the cache are followed. The
            # post ID in /i/videos must be one of this run's missing videos;
            # quoted/other posts and arbitrary player hosts are not expanded.
            for url in re.findall(r'''https?://(?:[a-z0-9.-]+)/(?:i/videos)/[0-9]+[^\s"'<>\\]*''',raw,re.I):
                u=urlsplit(url)
                match=re.fullmatch(r'/i/videos/([0-9]+)/?',u.path)
                if u.hostname not in X_HOSTS or not match or match[1] not in pending_ids or u.fragment:
                    continue
                values=self.player_entries.setdefault(match[1],[])
                if not any(existing[0]==url for existing in values) and len(values)<3:
                    values.append((url,'local-cache:'+path.name))
            for url in re.findall(r'https?://video\.twimg\.com/[^\s\"<>\\]+',raw):
                item=media_item(url)
                if not item or item['kind']!='video':continue
                for key,prefix in prefixes.items():
                    if item['key'].startswith(prefix) if prefix.endswith('/') else item['key']==prefix:
                        values=self.local_candidates.setdefault(key,[])
                        pair=(url,'local-cache:'+path.name)
                        if pair not in values:values.append(pair)
    def archived_config(self,p,m):
        if m['kind']!='video' or p.get('content_reason')=='restricted':return []
        context=media_context(p,m)+json.dumps(self.player_entries.get(p['id'],[]))
        if self.source_cached(m,'wayback_video_config',context):return []
        candidates=[];reports=[]
        entries=[(f"https://api.twitter.com/1.1/videos/tweet/config/{p['id']}.json",'known-video-config')]
        entries+=self.player_entries.get(p['id'],[])[:3]
        for endpoint,observed_at in entries:
            self.http.check()
            cached=self.video_entry_cache.get(endpoint)
            if cached is None:
                report={'url':endpoint,'observed_at':observed_at,'status':'not_found','captures_checked':0,'evidence_urls':[]}
                links=[]
                try:
                    expected=urlsplit(endpoint)
                    query=endpoint if observed_at!='known-video-config' else endpoint.removeprefix('https://')
                    records=[]
                    for page in self.c.client.cdx(query,'exact'):
                        records.extend(page)
                        if len(records)>=3:break
                    report['captures_found']=len(records)
                    for record in records[-3:]:
                        self.http.check()
                        actual=urlsplit(record['original'])
                        if (actual.scheme not in {'http','https'} or actual.username is not None or actual.password is not None
                                or actual.port not in (None,80,443) or actual.fragment
                                or (actual.hostname,actual.path,actual.query)!=(expected.hostname,expected.path,expected.query)):
                            continue
                        evidence=replay(record['timestamp'],record['original'])
                        report['captures_checked']+=1
                        report['evidence_urls'].append(evidence)
                        try:
                            raw=html.unescape(self.c.client.text(evidence)).replace('\\/','/').replace('\\u002F','/').replace('\\u002f','/').replace('\\u0026','&')
                        except ArchiveError as error:
                            report.update(status='failed',error=str(error)[:400]);continue
                        for url in re.findall(r'''https?://video\.twimg\.com/[^\s"'<>\\]+''',raw):
                            item=media_item(url)
                            if item and item['kind']=='video':links.append((url,evidence))
                    if links:report['status']='found'
                except (ArchiveError,ValueError) as error:
                    report.update(status='failed',error=str(error)[:400])
                cached=(report,list(dict.fromkeys(links)))
                self.video_entry_cache[endpoint]=cached
            report,links=cached
            matched=[(url,evidence) for url,evidence in links if (item:=media_item(url)) and related(m,item)]
            # A page can mention several films; report found only for this
            # target's known media identity, never for an unrelated MP4.
            scoped=dict(report)
            if scoped['status']=='found' and not matched:scoped['status']='not_found'
            reports.append(scoped)
            candidates.extend(matched)
        errors=[r.get('error','') for r in reports if r['status']=='failed']
        status='found' if candidates else 'failed' if errors else 'not_found'
        evidence=next((u for r in reports for u in r.get('evidence_urls',[])),entries[-1][0])
        self.record(m,'wayback_video_config',status,errors[-1] if errors else '',
                    captures_checked=sum(r['captures_checked'] for r in reports),entry_points=reports,evidence_url=evidence,context=context)
        return list(dict.fromkeys(candidates))
    def archived_read(self,url,limit):
        self.http.check()
        if self.archived_failures>=3:raise ArchiveError('历史元数据连续连接失败，本轮暂缓；可稍后继续。')
        if self.archived_requests>=96:raise ArchiveError('达到本轮历史元数据请求上限；其余项目可继续恢复。')
        self.archived_requests+=1
        try:
            read=getattr(self.c.client,'text_with_meta',self.c.client.text)
            result=read(url,limit=limit)
        except ArchiveError:
            self.archived_failures+=1
            raise
        self.archived_failures=0
        return result
    def archived_metadata(self,p,m):
        if p.get('content_reason')=='restricted' or m['kind']!='video':return []
        observed=[url for url,_ in self.player_entries.get(p['id'],[])]
        context=media_context(p,m)+json.dumps(sorted(observed))+':metadata-v1'
        if self.source_cached(m,'archived_metadata',context):return []
        entries=self.archived_sources.discover(p,m,observed_urls=observed)
        candidates=[(entry['url'],entry.get('evidence_url','')) for entry in entries
                    if (candidate:=media_item(entry['url'])) and related(m,candidate) and candidate['kind']=='video']
        reports=self.archived_sources.last_report
        errors=[str(report.get('error') or report.get('status')) for report in reports if report.get('status') in {'failed','unavailable','invalid_metadata'}]
        self.record(m,'archived_metadata','found' if candidates else 'failed' if errors else 'not_found',
                    errors[-1] if errors else '',entry_points=reports,context=context)
        if candidates:
            m['urls']=list(dict.fromkeys(m.get('urls',[])+[url for url,_ in candidates]))
            m['discovered_sources']=entries
        return list(dict.fromkeys(candidates))
    def cc_recover(self,p,m):
        if not self.c.options.get('common_crawl',True):return False
        try:
            result=self.cc.recover(m,p,self.max_bytes)
            if not result:
                self.record(m,'common_crawl','not_found',indexes_limit=self.c.options.get('cc_indexes',6),scope=getattr(self.cc,'last_scope',{}));return False
            data=result['data'];extension=media_extension(data[:8192])
            if not extension or ((m['kind']=='video') != (extension in {'mp4','webm'})):raise ArchiveError('备用存档没有返回匹配的完整媒体。')
            digest=hashlib.sha256(data).hexdigest()
            dest=self.destination(p,m);dest=dest.with_name(dest.stem+'_'+digest[:12]+'.'+extension)
            dest.parent.mkdir(parents=True,exist_ok=True)
            self.http.check();atomic_write_bytes(dest,data)
            self.saved(p,m,{'file':dest,'bytes':len(data),'sha256':digest},'common_crawl',result['url'],result.get('evidence_url',''),result.get('captured_at',''))
            m['source_record_url']=result.get('retrieved_url','')
            m['source_scope']=getattr(self.cc,'last_scope',[])
            return True
        except (ArchiveError,ValueError) as error:
            self.record(m,'common_crawl','failed',error,scope=getattr(self.cc,'last_scope',[]));return False
    def archive_media(self,p,m):
        context=media_context(p,m)
        if self.source_cached(m,'wayback',context):return False
        try:
            query=video_identity(m) or (m['key'] if m.get('reference_only') else media_search_prefix(m))
            found=[]
            for page in self.c.client.cdx(query,'prefix'):
                for row in page:
                    candidate=media_item(row['original'])
                    if candidate and related(m,candidate) and candidate['kind']!='hls':found.append(row)
                if len(found)>=6:break
            download_errors=[]
            for row in sorted(found,key=lambda r:r['timestamp'],reverse=True)[:6]:
                self.http.check()
                try:
                    result=self.c.client.download(replay(row['timestamp'],row['original']),self.destination(p,m),self.max_bytes)
                    self.saved(p,m,result,'wayback',row['original'],result['archive_url'],row['timestamp'])
                    return True
                except ArchiveError as error:download_errors.append(error)
            absent=not found or (download_errors and all(definite_http_absence(error) for error in download_errors))
            self.record(m,'wayback','not_found' if absent else 'failed',download_errors[-1] if download_errors else '',
                        captures_found=len(found),captures_checked=len(download_errors),context=context)
        except ArchiveError as error:self.record(m,'wayback','failed',error)
        return False
    def checkpoint_media(self,p,m,finished=False):
        if finished or m['status']=='saved':
            m['recovery_checked_at']=now();m['recovery_context']=media_context(p,m)+self.policy
        else:
            m.pop('recovery_checked_at',None);m.pop('recovery_context',None)
        self.c.store.save_post(p)
        self.stats['requests']=self.http.requests
        self.stats['historical_requests']=self.archived_requests
        self.c.run_info['multi_source']=dict(self.stats)
        self.c.checkpoint('running')

    def hls(self,p,m):
        from hls_recovery import recover
        from media_inventory import video_identity
        identity,conflict=video_identity(m)
        for other in p['media']:
            if identity and not conflict and other is not m and other['kind']=='video' and video_identity(other)==(identity,False) and self.c.saved_file_valid(other):
                m.update({k:other[k] for k in ('path','bytes','sha256','source') if k in other})
                m.update(kind='video',status='saved',error='',error_kind='',reused_variant=True)
                self.record(m,'hls','saved',evidence_url=p['url'],reused=True)
                return
        # Prefer an explicit MP4 for this exact video identity when public
        # metadata still has one. A dead playlist need not mean a dead movie.
        if identity and not conflict and p.get('content_reason')!='restricted':
            if p['id'] not in self.hls_candidates:
                self.hls_candidates[p['id']]=self.discover(p,self.http.fetch,self.http.check,self.c.log,
                    enable_mirrors=self.c.options.get('public_mirrors',True))
            candidates=[(u,item.get('evidence_url','')) for item in self.hls_candidates[p['id']]
                        if item['kind']=='video' and video_identity(item)==(identity,False) for u in item['urls']]
            if candidates:
                target={**m,'kind':'video'}
                if self.direct(p,target,candidates):
                    m.update(target)
                    return
        try:
            result=recover(self.c,self.http,p,m,self.destination(p,m),self.max_bytes)
            m['kind']='video';m['hls_recovery']=result['hls']
            self.saved(p,m,result,'hls_wayback' if result['hls']['scope']=='wayback' else 'hls_live',result['hls']['playlist'],p['url'])
        except (ArchiveError,ValueError,OSError) as error:
            reason=getattr(error,'reason','') or failure_kind(error)
            m.update(status='link_only',error=str(error),error_kind=reason)
            self.record(m,'hls','failed' if reason=='network' else 'unresolved',str(error))
    def run(self):
        ordered = self.c.ordered_posts('sources') if hasattr(self.c, 'ordered_posts') else self.c.store.posts()
        posts=[p for p in ordered if any(not self.c.saved_file_valid(m) for m in p['media'])]
        self.c.progress(stage='sources',completed=0,total=len(posts))
        self.cached_links(posts)
        for number,p in enumerate(posts,1):
            self.http.check();self.c.progress(completed=number-1,total=len(posts))
            if hasattr(self.c, 'visit_post'):
                self.c.visit_post('sources', p)
            pending=[m for m in p['media'] if not self.c.saved_file_valid(m)]
            for m in pending:
                if m.get('status')=='saved':
                    m['status']='failed'
                    m['error']='本地文件缺失或不完整，重新查找来源。'
                    m.pop('recovery_checked_at',None)
                    m.pop('recovery_context',None)
            active=[m for m in pending if not self.check_cached(p,m)]
            self.stats['cached']+=len(pending)-len(active)
            if not active:continue
            self.c.log(f"多源恢复 {number}/{len(posts)} · #{p['id']}")
            unresolved=[];attempted={id(m):{} for m in active}
            # Save all known files in this post before spending the budget on
            # metadata lookups for resources that have no working direct URL.
            for m in active:
                self.http.check();self.stats['checked']+=1;m['recovery_error_kind']=''
                finished=False
                try:
                    if m['kind']=='hls':
                        self.hls(p,m);finished=True;continue
                    candidates=[(u,p['url']) for u in image_variants(m) if not m.get('reference_only') or (media_item(u) and related(m,media_item(u)))]
                    if m.get('recovered_video_url'):candidates.insert(0,(m['recovered_video_url'],m.get('source_evidence','')))
                    candidates+=self.local_candidates.get(m['key'],[])
                    if not self.direct(p,m,candidates,attempted[id(m)]):unresolved.append(m)
                finally:
                    self.checkpoint_media(p,m,finished)
            discovered=None;current_reports={}
            for m in unresolved:
                finished=False
                try:
                    self.http.check()
                    # Resolve metadata once per post, only after its known
                    # URLs have failed. A saved direct file needs no lookup.
                    if (m['kind']=='video' or m.get('reference_only')) and p.get('content_reason')!='restricted':
                        if discovered is None:
                            previous_reports=dict(p.get('source_checks',{}))
                            discovered=self.discover(p,self.http.fetch,self.http.check,self.c.log,enable_mirrors=self.c.options.get('public_mirrors',True))
                            current_reports={source:report for source,report in p.get('source_checks',{}).items() if report is not previous_reports.get(source)}
                        for source,report in current_reports.items():
                            self.record(m,source,'failed' if report.get('status') in {'unavailable','rate_limited'} else report.get('status','unknown'),report.get('error',''),evidence_url=report.get('evidence_url',''),checked_at=report.get('checked_at') or now())
                        candidates=[(u,item.get('evidence_url','')) for item in discovered if related(m,item) for u in item['urls']]
                        if self.direct(p,m,candidates,attempted[id(m)]):continue
                    archived=self.archived_config(p,m)
                    if archived and self.direct(p,m,archived,attempted[id(m)]):continue
                    historical=self.archived_metadata(p,m)
                    if historical and self.direct(p,m,historical,attempted[id(m)]):continue
                    if self.archive_media(p,m):continue
                    if self.cc_recover(p,m):continue
                    m['error']='多源检查后仍未恢复；展开来源记录查看各来源结果。'
                    m['error_kind']='network' if m.get('recovery_error_kind')=='network' else 'unresolved_sources'
                    finished=True
                finally:
                    self.checkpoint_media(p,m,finished)
            self.c.store.save_post(p)
            self.c.progress(completed=number,total=len(posts))
        self.stats['requests']=self.http.requests
        self.c.run_info['multi_source']=dict(self.stats)
        self.c.progress(completed=len(posts),total=len(posts))
        self.c.log(f"多源恢复结束：补回 {self.stats['recovered']} 项，复用 {self.stats['cached']} 项近期检查。")
