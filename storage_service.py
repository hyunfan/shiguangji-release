"""Background storage inspection and cloud-media pilot; polling stays local."""
from copy import deepcopy
from pathlib import Path
import threading
import time

from cloud_media import descriptor, open_media, cache_status, trim_cache
from platform_accounts import collection_key
from storage_policy import settings, save_settings, plan_cleanup, execute_cleanup, read_json


class StorageService:
    def __init__(self, base):
        self.base=Path(base);self.lock=threading.RLock()
        self.state={'running':False,'operation':'','message':'ZIP 按需生成；原始资料与当前恢复备份保留。'}
        prior=read_json(self.base/'.compression-state.json')
        if prior:
            self.state.update(operation='compress',result=prior,finished_at=prior.get('finished_at',time.time()),
                message=('上次压缩中断，已完成的文件保留；点击“无损压缩历史网页”可继续。' if prior.get('running') else prior.get('message','')))

    def snapshot(self):
        with self.lock:result=deepcopy(self.state)
        result.update(settings=settings(self.base),cache=cache_status(self.base))
        return result

    def configure(self, values):
        save_settings(self.base,values)
        trim_cache(self.base)
        return self.snapshot()

    def report(self, message):
        with self.lock:self.state['message']=message

    def submit(self, operation, *, handle='', plan_id='', all_exports=False):
        if operation=='pause_compression':
            with self.lock:
                if self.state.get('running') and self.state.get('operation')=='compress':
                    self.compression_stop.set()
                    self.state['message']='正在暂停压缩，等待当前文件完成校验。已完成的结果会保留。'
            return self.snapshot()
        if operation not in {'preview','cleanup','probe','clear_cache','compress'}:raise ValueError('未知空间操作。')
        if operation=='probe':handle=collection_key(handle)
        with self.lock:
            if self.state['running']:raise ValueError('空间任务正在处理，请等待当前操作结束。')
            if operation=='cleanup' and not plan_id:raise ValueError('请先预览要释放的 ZIP。')
            self.state={'running':True,'operation':operation,'message':'正在处理，请稍候。','started_at':time.time()}
            if operation=='compress':self.compression_stop=threading.Event()
            def run():
                try:
                    if operation=='preview':
                        result=plan_cleanup(self.base,all_exports=all_exports,progress=self.report)
                        message=f"可释放 {len(result['items'])} 个生成的 ZIP；不会删除媒体、数据库或备份。"
                    elif operation=='cleanup':
                        result=execute_cleanup(self.base,plan_id,progress=self.report)
                        message=f"已释放 {result['removed']} 个 ZIP，跳过 {result['skipped']} 个已变化或使用中的文件。"
                    elif operation=='compress':
                        from compact_library import compact_library
                        result=compact_library(self.base,progress=self.report,stop=self.compression_stop)
                        message=result['message']
                    elif operation=='clear_cache':
                        remaining=trim_cache(self.base,limit=0)
                        result={'remaining_bytes':remaining};message='已清理未使用的媒体缓存；正在播放的缓存保留。'
                    else:
                        result=self.probe(handle)
                        verified=sum(item['verified'] for item in result['files'])
                        message=f"网盘试读：{verified}/{len(result['files'])} 项校验通过。"
                        message+=('挂载盘读回不代表服务端上传完成；本地原件仍保留。' if verified
                                  else '本次未能验证可用的云端媒体，请查看下方原因；本地原件仍保留。')
                    with self.lock:self.state.update(result=result,message=message)
                except Exception as error:
                    with self.lock:self.state.update(error=str(error)[:300],message='本次操作未完成：'+str(error)[:300])
                finally:
                    with self.lock:self.state.update(running=False,finished_at=time.time())
            thread=threading.Thread(target=run,daemon=True,name='library-storage')
            try:thread.start()
            except Exception:
                self.state.update(running=False,message='空间任务未能启动，请重试。');raise
        return self.snapshot()

    def probe(self, handle):
        manifest=read_json(self.base/handle/'manifest.json')
        candidates={m['path']:m for p in manifest.get('posts',[]) for m in p.get('media',[])
                    if m.get('status')=='saved' and m.get('path') and 0<m.get('bytes',0)<=200*1024**2}
        chosen=[]
        for kind in ('image','video'):
            values=sorted((m for m in candidates.values() if m.get('kind')==kind),key=lambda m:m['bytes'])
            # Prefer a representative file over a tiny thumbnail/empty clip.
            minimum=50*1024 if kind=='image' else 1024**2
            typical=[m for m in values if m['bytes']>=minimum]
            if values:chosen.append((typical or values)[0])
        if not chosen:raise ValueError('当前合集没有适合试读的媒体（单文件不超过 200 MB）。')
        results=[]
        for item in chosen:
            with self.lock:self.state['message']='正在从网盘恢复包校验读取：'+item['path']
            started=time.monotonic()
            try:
                descriptor(self.base,handle,item['path'])
                with open_media(self.base,handle,item['path']) as (_,record,hit):
                    results.append({'path':item['path'],'kind':item['kind'],'bytes':record['bytes'],
                                    'seconds':round(time.monotonic()-started,2),'cache_hit':hit,'verified':True})
            except (OSError,ValueError,KeyError) as error:
                results.append({'path':item['path'],'kind':item['kind'],'verified':False,'error':str(error)[:200]})
        return {'username':handle,'files':results,'source':'mount_readback','provider_upload_confirmed':False}
