"""Bounded, resumable Instagram recovery from public Wayback snapshots."""
import hashlib
import json
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from shiguangji.sources.archive_http import Client
from shiguangji.storage.collection_store import Store
from shiguangji.core.core_errors import ArchiveError, Cancelled, failure_kind
from shiguangji.core.archive_support import replay, now
from shiguangji.storage.collection_export import export_collection
from shiguangji.media.media_files import saved_file_valid
from shiguangji.storage import compressed_files as raw_files
from shiguangji.storage.collection_lock import CollectionBusy, collection_lock
from shiguangji.collect.run_budget import RunBudget, RunLimit
from shiguangji.platforms.platform_accounts import account_info, IG_HOSTS
from shiguangji.platforms.instagram_extract import extract, shortcode, media_item


class InstagramCollector:
    def __init__(self, handle, options=None, progress=lambda **k:None, stop=None, base=None, client=None):
        from shiguangji.core.archive_support import DATA
        self.handle,self.options = handle,options or {}
        self.name = account_info(handle)['display_username']
        self.folder = Path(base if base is not None else DATA)/handle
        self.store = None
        self.stop,self.callback = stop or threading.Event(),progress
        self.budget = RunBudget(self.options.get('max_minutes',10),self.options.get('max_requests',200))
        self.client = client or Client(self.stop,self.log,self.options.get('proxy',''),budget=self.budget)
        self.errors,self.index_ok,self.index_expected = [],0,0
        self.last_checkpoint = 0
        self.run_info = {}

    def initialize_store(self):
        """Called only while run() owns the collection lock, including migrations."""
        self.store = Store(self.folder, self.handle)
        from shiguangji.sources.retry_policy import RetryPolicy
        self.retry_policy = RetryPolicy(self.folder/'.network-retry.json',
            force_run=(self.options.get('_run_id') or now()) if self.options.get('force_recheck') else '')
        self.client.retry_policy = self.retry_policy
        self.before = self.store.posts()
        self.run_info={'run_id':self.options.get('_run_id',''), 'platform':'instagram',
                       'options':{'mode':self.options.get('mode','full'),'multi_source':False},
                       'instagram':{'source':'Wayback','ownership_checked':True,'unverified_pages':0},
                       'discovery':{'posts_before':len(self.before),'text_before':sum(bool(p['text']) for p in self.before),'resource_captures':0,'queried_hosts':0,'failed_hosts':0}}
        self.store.db.execute("CREATE TABLE IF NOT EXISTS instagram_queries(url TEXT PRIMARY KEY, kind TEXT, status TEXT DEFAULT 'pending', resume TEXT DEFAULT '', error TEXT DEFAULT '')")
        for host in ('www.instagram.com','instagram.com','instagr.am'):
            for suffix in ('','/'):
                self.add_query('https://'+host+'/'+self.name+suffix,'profile')
        for link in self.options.get('instagram_links',[]):self.add_link(link)
        self.store.db.commit()
        marker = self.store.db.execute("SELECT value FROM settings WHERE key='instagram_run'").fetchone()
        run_id = self.options.get('_run_id') or now()
        if self.options.get('mode','full') in {'full','discover'} and (not marker or marker[0]!=run_id):
            with self.store.db:
                self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('instagram_run',?)",(run_id,))
                self.store.db.execute("UPDATE instagram_queries SET status='pending',error='' WHERE status='failed'")
                self.store.db.execute("UPDATE captures SET status='pending',error='' WHERE status='failed'")
                # New manual runs refresh profiles; automatic batches keep their cursors.
                if self.options.get('mode','full') in {'full','discover'}:
                    self.store.db.execute("UPDATE instagram_queries SET status='pending' WHERE kind='profile' AND status='done'")

    def progress(self,**updates):
        self.callback(**updates,batch=self.budget.snapshot())

    def log(self,message):
        self.progress(message=message)

    def issue(self,kind,url,error):
        self.errors.append({'stage':kind,'source':url,'error':str(error)[:500]})
        self.log(kind+'：'+str(error)[:240])

    def add_query(self,url,kind):
        self.store.db.execute('INSERT OR IGNORE INTO instagram_queries(url,kind) VALUES(?,?)',(url,kind))

    def add_link(self,url):
        code=shortcode(url)
        if not code:return
        for path in ('p','reel','tv'):
            self.add_query(f'https://www.instagram.com/{path}/{code}/','post')
        self.add_query(f'https://instagr.am/p/{code}/','post')

    def valid_capture(self,record,query):
        try:
            u=urlsplit(record['original']); q=urlsplit(query['url'])
            if u.hostname not in IG_HOSTS or u.scheme not in {'http','https'} or u.username or u.port not in {None,80,443}:return False
            if query['kind']=='post':return shortcode(record['original'])==shortcode(query['url']) and bool(shortcode(query['url']))
            parts=u.path.strip('/').split('/')
            return parts[0].lower()==self.name and (len(parts)==1 or bool(shortcode(record['original'])))
        except (ValueError,KeyError):return False

    def query(self,row):
        self.progress(stage='index',message='Instagram：查询历史索引 '+row['url'])
        try:
            match='prefix' if row['url'].endswith('/') else 'exact'
            for records,resume in self.client.cdx_pages(row['url'],match,row['resume']):
                with self.store.db:
                    for r in records:
                        if self.valid_capture(r,row):
                            self.store.db.execute('INSERT OR IGNORE INTO captures(timestamp,original,mimetype,digest) VALUES(?,?,?,?)',tuple(r[k] for k in ('timestamp','original','mimetype','digest')))
                    self.store.db.execute("UPDATE instagram_queries SET resume=?,status=?,error='' WHERE url=?",(resume,'pending' if resume else 'done',row['url']))
                self.checkpoint('running',False)
            self.index_ok+=1
            self.run_info['discovery']['queried_hosts']+=1
        except ArchiveError as e:
            self.issue('Instagram 索引暂不可用',row['url'],e)
            with self.store.db:self.store.db.execute("UPDATE instagram_queries SET status='failed',error=? WHERE url=?",(str(e),row['url']))
            self.run_info['discovery']['failed_hosts']+=1

    def pages(self):
        rows=self.store.db.execute("SELECT * FROM captures WHERE status='pending' ORDER BY timestamp DESC,original").fetchall()
        rows=[row for row in rows if self.retry_policy.allowed(replay(row['timestamp'],row['original']))]
        limit=self.options.get('max_pages',0)
        if limit:rows=rows[:max(0,limit-self.processed)]
        for number,row in enumerate(rows,1):
            self.client.check(); self.budget.before_work()
            self.progress(stage='pages',completed=number-1,total=len(rows),message=f'Instagram：读取历史快照 {number}/{len(rows)}')
            filename=hashlib.sha256((row['timestamp']+row['original']).encode()).hexdigest()[:24]+'.txt'
            raw=self.folder/'raw'/filename
            try:
                text=None
                if raw_files.exists(raw):
                    try:text=raw_files.read_text(raw)
                    except (OSError,ValueError,EOFError):
                        self.log('Instagram：本地网页缓存损坏，重新读取历史快照。')
                if text is None:
                    text=self.client.text(replay(row['timestamp'],row['original']))
                    from shiguangji.storage.storage_guard import require_space
                    require_space(self.folder,len(text.encode('utf-8')))
                    raw_files.write_text(raw,text)
                posts,links=extract(text,row['original'],row['timestamp'],self.name)
                self.store.put_posts(posts)
                page_error = ''
                if not posts:
                    self.run_info['instagram']['unverified_pages']+=1
                    page_error = '快照缺少可核实的作者和内容，可能只有登录提示；未将其归入合集。' if shortcode(row['original']) else '主页快照未提供可解析的帖子清单，可能只有登录页面或需要动态加载；可补充已知帖子链接。'
                    self.issue('快照内容未恢复',row['original'],page_error)
                with self.store.db:
                    for link in links:self.add_link(link)
                    self.store.db.execute("UPDATE captures SET status='done',error=? WHERE timestamp=? AND original=?",(page_error,row['timestamp'],row['original']))
                self.processed+=1
            except (ArchiveError,ValueError,RecursionError) as e:
                self.issue('Instagram 快照读取失败',row['original'],e)
                with self.store.db:self.store.db.execute("UPDATE captures SET status='failed',error=? WHERE timestamp=? AND original=?",(str(e),row['timestamp'],row['original']))
            if number%5==0:self.checkpoint('running',False)

    def downloads(self):
        self.budget.media_phase()
        posts=self.store.posts()
        cursor=self.store.db.execute("SELECT value FROM settings WHERE key='instagram_media_cursor'").fetchone()
        if cursor:
            for i,p in enumerate(posts):
                if p['id']==cursor[0]:
                    posts=posts[i+1:]+posts[:i+1];break
        for index,p in enumerate(posts,1):
            self.progress(stage='media',completed=index-1,total=len(posts))
            for m in p['media']:
                self.client.check()
                if m['status']=='saved':
                    if saved_file_valid(self.folder,m,self.client.check):continue
                    m.update(status='pending',path='')
                if m['kind']=='hls':
                    m.update(status='link_only',error_kind='unsupported_hls',error='保留视频分片入口，尚不拼接分片。')
                    continue
                if not self.options.get('force_recheck') and m.get('error_kind')=='not_indexed':
                    try:
                        from datetime import datetime,timezone
                        if (datetime.now(timezone.utc)-datetime.fromisoformat(m.get('checked_at','').replace('Z','+00:00'))).total_seconds()<86400:continue
                    except ValueError:pass
                self.budget.before_work()
                with self.store.db:self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('instagram_media_cursor',?)",(p['id'],))
                candidates=[]; failures=[]; queried=False; truncated=False
                # Query exact signed URLs and then the same CDN path without query parameters.
                try:
                    for url in m['urls'][:4]:
                        if not media_item(url,m['kind']):continue
                        u=urlsplit(url)
                        for target,match in ((url,'exact'),(urlunsplit((u.scheme,u.netloc,u.path,'','')),'prefix')):
                            count=0
                            for records,_ in self.client.cdx_pages(target,match):
                                queried=True
                                for r in records:
                                    other=media_item(r['original'],m['kind'])
                                    if other and other['key']==m['key']:candidates.append(replay(r['timestamp'],r['original']))
                                count+=len(records)
                                if count>=1000:
                                    truncated=True;break
                            if candidates:break
                        if candidates:break
                except ArchiveError as e:failures.append(failure_kind(e))
                for url in m['urls'][:2]:
                    for cap in p['captures'][:2]:candidates.append(replay(cap['timestamp'],url))
                try:
                    for url in list(dict.fromkeys(candidates))[:8]:
                        try:
                            month=p['created_at'][:7].replace('-','/') if p['created_at'] else 'unknown-date'
                            path=self.folder/'media'/month/(p['id']+'_'+hashlib.sha256(m['key'].encode()).hexdigest()[:12]+'.bin')
                            result=self.client.download(url,path,int(self.options.get('max_media_mb',512))*1024*1024)
                            m.update(status='saved',path=result['file'].relative_to(self.folder).as_posix(),bytes=result['bytes'],sha256=result['sha256'],archive_url=result['archive_url'],source='wayback',checked_at=now(),error='',error_kind='')
                            self.budget.saved(m['path']);break
                        except ArchiveError as e:failures.append(failure_kind(e))
                    if m['status']!='saved':
                        network=any(x != 'not_found' for x in failures)
                        m.update(status='missing',error_kind='network' if network or truncated else 'not_indexed',checked_at=now(),error='来源暂不可用，稍后可重试。' if network else '已查范围未找到可下载的历史媒体。')
                    m['recovery_checks']=[{'source':'wayback','status':'saved' if m['status']=='saved' else 'failed' if m['error_kind']=='network' else 'not_found','checked_at':now(),'message':m.get('error','')}]
                finally:
                    self.store.save_post(p)
                self.checkpoint('running',False)

    def checkpoint(self,status,full=True):
        if status=='running' and not full and time.monotonic()-self.last_checkpoint<3:
            return
        self.last_checkpoint=time.monotonic()
        # A later batch must not erase unreadable-page or failed-index evidence.
        known = {(e['source'],e['error']) for e in self.errors}
        for query in ("SELECT original,error FROM captures WHERE error!=''", "SELECT url,error FROM instagram_queries WHERE error!=''"):
            for url,error in self.store.db.execute(query):
                if (url,error[:500]) not in known:
                    self.errors.append({'stage':'Instagram 来源未完成','source':url,'error':error[:500]});known.add((url,error[:500]))
        pending_queries=self.store.db.execute("SELECT COUNT(*) FROM instagram_queries WHERE status!='done'").fetchone()[0]
        pending_pages=self.store.db.execute("SELECT COUNT(*) FROM captures WHERE status!='done'").fetchone()[0]
        if self.options.get('mode') in {'repair','rescue'}:pending_queries=pending_pages=0
        self.run_info['scope']={'pending':bool(pending_queries or pending_pages),'failed_sources':len(self.errors)}
        self.run_info['batch']=self.budget.snapshot()
        self.run_info['retry_policy']=self.retry_policy.snapshot()
        self.progress(retry_policy=self.run_info['retry_policy'])
        self.run_info['instagram'].update(pending_queries=pending_queries,pending_pages=pending_pages)
        posts=self.store.posts()
        self.run_info['discovery'].update(new_posts=len(posts)-len(self.before),new_text=sum(bool(p['text']) for p in posts)-sum(bool(p['text']) for p in self.before))
        manifest=export_collection(self.store,status,self.errors,
                                   full=full and self.budget.code != 'storage_low' and not self.options.get('_defer_offline_export'),run_info=self.run_info)
        self.progress(counts=manifest['counts'],work_pending=bool(pending_queries or pending_pages))
        return manifest

    def run(self):
        status='partial';self.processed=0
        try:
            with collection_lock(self.folder):
                try:
                    self.initialize_store()
                    status = self.collect()
                finally:
                    if self.store is not None:
                        self.store.close()
                        self.store = None
        except CollectionBusy as e:
            # No database initialization or final checkpoint without ownership.
            self.issue('Instagram 合集正在使用','',e)
            status = 'failed'
        return status

    def collect(self):
        """Collection and every checkpoint share the same lock lifetime."""
        from shiguangji.storage.storage_guard import StorageLow
        status = 'partial'
        try:
            self.log('Instagram：仅检查公开历史快照及其中的媒体；没有存档的已删除内容无法凭空恢复。')
            self.checkpoint('running',False)
            if self.options.get('mode') not in {'repair','rescue'}:
                while True:
                    self.pages()
                    if self.options.get('max_pages') and self.processed>=self.options['max_pages']:break
                    row=self.store.db.execute("SELECT * FROM instagram_queries WHERE status='pending' ORDER BY CASE kind WHEN 'profile' THEN 0 ELSE 1 END,url LIMIT 1").fetchone()
                    if row is None:break
                    self.budget.before_work();self.query(row)
            if self.options.get('mode')!='discover':self.downloads()
            status='partial' if self.errors else 'complete'
        except StorageLow as error:
            self.budget.code='storage_low';self.budget.reason=str(error);status='paused'
            self.progress(message=str(error))
        except Cancelled:
            self.budget.code='user_pause';self.budget.reason='已暂停并保存进度';status='paused'
        except RunLimit:
            status='paused'
        except Exception as e:
            self.issue('Instagram 归档失败','',e);status='failed'
        finally:
            manifest=self.checkpoint(status)
            if status=='complete' and (manifest['counts']['missing'] or manifest['counts']['text_missing'] or self.run_info['scope']['pending']):
                status='partial';self.checkpoint(status)
        return status
