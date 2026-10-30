"""Restore bounded, unencrypted VOD playlists for one explicit X video asset.

Only the Python transport accesses the network. FFmpeg receives rewritten local
playlists, with network protocols disabled. Partial segments are reusable but
never published as successfully recovered media.
"""
import hashlib
import json
import math
import os
from pathlib import Path
from http.client import HTTPException
import re
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urljoin, urlsplit
from shiguangji import APP_ROOT
from shiguangji.core.atomic_io import atomic_write_bytes, atomic_write_text, replace_file
from shiguangji.core.archive_support import replay, now
from shiguangji.core.core_errors import ArchiveError
from shiguangji.media.media_files import file_valid, VALIDATION_VERSION
from shiguangji.storage.storage_guard import require_space

ROOT=APP_ROOT


def asset(url):
    u=urlsplit(url)
    m=re.match(r'^/(ext_tw_video|amplify_video)/(\d+)/',u.path)
    if (u.scheme!='https' or u.hostname!='video.twimg.com' or u.port not in (None,443)
            or u.username or u.fragment or not m or '%' in u.path or '\\' in u.path
            or any(p in {'.','..'} for p in u.path.split('/'))):
        raise ArchiveError('HLS 链接缺少明确的视频身份。',reason='hls_unsupported')
    return m.groups()


def child_url(base, value):
    target=urljoin(base,value)
    if asset(target)!=asset(base):raise ArchiveError('HLS 分片属于另一视频，未采用。',reason='hls_unsupported')
    return target


def attributes(line):
    return {key:value.strip('"') for key,value in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)',line.partition(':')[2])}


def parse_playlist(raw, url):
    try:return _parse_playlist(raw,url)
    except (ValueError,KeyError,TypeError) as error:
        raise ArchiveError('HLS 播放列表字段无效或不完整。',reason='invalid_media') from error


def _parse_playlist(raw, url):
    if len(raw)>1024*1024:raise ArchiveError('HLS 播放列表过大。',reason='hls_unsupported')
    lines=[line.strip() for line in raw.decode('utf-8-sig').splitlines() if line.strip()]
    if not lines or lines[0]!='#EXTM3U':raise ArchiveError('返回的不是 HLS 播放列表。',reason='invalid_media')
    variants=[];audio={};pending=None;segments=[];initial=None;duration=None
    for line in lines[1:]:
        if line.startswith(('#EXT-X-KEY:','#EXT-X-SESSION-KEY:')) and attributes(line).get('METHOD')!='NONE':
            raise ArchiveError('该 HLS 含加密内容，保留入口。',reason='hls_unsupported')
        if line.startswith(('#EXT-X-BYTERANGE:','#EXT-X-DISCONTINUITY','#EXT-X-GAP')):
            raise ArchiveError('该 HLS 使用暂不支持的范围、缺片或时间断点，保留入口。',reason='hls_unsupported')
        if line.startswith('#EXT-X-STREAM-INF:'):pending=attributes(line)
        elif line.startswith('#EXT-X-MEDIA:'):
            a=attributes(line)
            if a.get('TYPE')=='AUDIO' and a.get('URI'):audio.setdefault(a.get('GROUP-ID',''),child_url(url,a['URI']))
        elif line.startswith('#EXT-X-MAP:'):
            a=attributes(line)
            if initial or 'BYTERANGE' in a:raise ArchiveError('HLS 初始化片段范围暂不支持。',reason='hls_unsupported')
            initial=child_url(url,a['URI'])
        elif line.startswith('#EXTINF:'):
            duration=float(line.partition(':')[2].split(',')[0])
            if not math.isfinite(duration) or not 0<duration<=600:raise ValueError('Invalid HLS segment duration')
        elif not line.startswith('#'):
            if pending is not None:
                variants.append({'url':child_url(url,line),'bandwidth':int(pending.get('BANDWIDTH','0')),'audio':pending.get('AUDIO','')});pending=None
            elif duration is not None:
                segments.append((child_url(url,line),duration));duration=None
            else:raise ArchiveError('HLS 列表结构不完整。',reason='invalid_media')
    if pending is not None or (variants and segments):
        raise ArchiveError('HLS 播放列表结构不完整。',reason='invalid_media')
    if variants:
        best=max(variants,key=lambda row:row['bandwidth'])
        return {'variants':[best['url']]+([audio[best['audio']]] if best['audio'] in audio else [])}
    if '#EXT-X-ENDLIST' not in lines or not segments or len(segments)>2000 or duration is not None:
        raise ArchiveError('HLS 不是完整点播列表或分片数超限。',reason='hls_unsupported')
    return {'segments':segments,'initial':initial,'duration':sum(t for _,t in segments)}


def binary(name):
    path=ROOT/'tools'/'ffmpeg'/(name+'.exe' if os.name=='nt' else name)
    candidate=str(path) if path.is_file() else shutil.which(name)
    if not candidate:raise ArchiveError('需要视频组件：运行“安装视频组件.cmd”后重试。',reason='hls_dependency')
    return candidate


def run_tool(args, check, timeout=180, cwd=None):
    with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
        child=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
                               cwd=cwd, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+timeout
        try:
            while child.poll() is None:
                check()
                if time.monotonic()>deadline:raise ArchiveError('视频合并或校验超时，已保留分片。',reason='hls_validation')
                time.sleep(.1)
            if child.returncode:raise ArchiveError('视频解码或合并失败，未计为恢复成功。',reason='hls_validation')
            out.seek(0);return out.read(1024*1024)
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=5)


def recover(collector, http, post, item, destination, max_bytes):
    ffmpeg,ffprobe=binary('ffmpeg'),binary('ffprobe')
    urls=list(dict.fromkeys(u for u in item['urls'] if '.m3u8' in urlsplit(u).path))
    if not urls:raise ArchiveError('没有 HLS 播放列表。',reason='hls_unsupported')
    anchor=urls[0];asset(anchor)
    cache=collector.folder/'.hls-cache'/hashlib.sha256(anchor.encode()).hexdigest()
    cache.mkdir(parents=True,exist_ok=True)
    cap=max_bytes or 512*1024**2
    ledger_path=cache/'segments.json'
    try:ledger=json.loads(ledger_path.read_text(encoding='utf-8'))
    except (OSError,ValueError):ledger={}
    # This is a disposable cache, not archive data. Invalid receipts are fetched
    # and checked again instead of aborting the account's whole recovery task.
    if not isinstance(ledger,dict):ledger={}
    ledger={key:value for key,value in ledger.items() if isinstance(value,dict)}
    total=0;sources=[];local_playlists=[];expected=0

    def fetch(url, limit, stamp=''):
        if limit <= 0:raise ArchiveError('视频总大小超过限制。',reason='too_large')
        if asset(url)!=asset(anchor):raise ArchiveError('分片身份不一致。',reason='hls_unsupported')
        endpoint=replay(stamp,url) if stamp else url
        key=hashlib.sha256(endpoint.encode()).hexdigest();path=cache/(key+'.bin')
        receipt=ledger.get(key,{})
        if path.is_file() and path.stat().st_size<=limit and receipt.get('sha256'):
            data=path.read_bytes()
            if len(data)<=limit and hashlib.sha256(data).hexdigest()==receipt['sha256']:
                return data,receipt.get('source_url',endpoint)
        require_space(cache,limit)
        client=collector.client if stamp else http
        try:
            with client.open(endpoint) as response:
                data=bytearray()
                while block:=getattr(response,'read1',response.read)(256*1024):
                    client.check();data.extend(block)
                    if len(data)>limit:raise ArchiveError('视频分片超过大小限制。',reason='too_large')
                length=response.headers.get('Content-Length')
                if length and int(length)!=len(data):raise OSError('Incomplete segment')
                actual=response.url
        except (OSError,HTTPException,EOFError) as error:
            policy=getattr(client,'retry_policy',None)
            if policy:policy.failure(endpoint)
            raise ArchiveError('视频分片读取中断，已保留完整分片供后续继续。',reason='network') from error
        if not data:raise ArchiveError('视频分片为空。',reason='invalid_media')
        if '.m3u8' in urlsplit(url).path:
            parse_playlist(bytes(data),url)
        elif not (data[0]==0x47 or bytes(data[4:8]) in {b'ftyp',b'styp',b'moof',b'moov',b'sidx'}):
            raise ArchiveError('返回的不是视频分片。',reason='invalid_media')
        atomic_write_bytes(path,bytes(data))
        ledger[key]={'sha256':hashlib.sha256(data).hexdigest(),'source_url':actual}
        atomic_write_text(ledger_path,json.dumps(ledger))
        count=int(collector.setting('hls_segments') or 0)+1
        collector.setting('hls_segments',str(count))
        if hasattr(collector,'budget'):
            collector.budget.last_saved=time.monotonic()
        return bytes(data),actual

    # Try the current public original first, then exact archived playlists.
    attempts=[(u,'') for u in urls[:3]]
    last=None;selected=None
    for url,stamp in attempts:
        try:selected=(url,stamp,parse_playlist(fetch(url,1024**2,stamp)[0],url));break
        except ArchiveError as error:last=error
    if selected is None:
        for records in collector.client.cdx(anchor):
            for row in records[:3]:
                try:selected=(anchor,row['timestamp'],parse_playlist(fetch(anchor,1024**2,row['timestamp'])[0],anchor));break
                except ArchiveError as error:last=error
            break
    if selected is None:raise last or ArchiveError('没有可读取的 HLS 存档。')
    url,stamp,playlist=selected
    tracks=playlist.get('variants',[url])
    for track_index,track in enumerate(tracks):
        plan=playlist if track==url and 'segments' in playlist else parse_playlist(fetch(track,1024**2,stamp)[0],track)
        if 'segments' not in plan:raise ArchiveError('HLS 嵌套层级超限。',reason='hls_unsupported')
        expected=max(expected,plan['duration'])
        lines=['#EXTM3U','#EXT-X-VERSION:7','#EXT-X-TARGETDURATION:600','#EXT-X-MEDIA-SEQUENCE:0']
        if plan['initial']:
            data,actual=fetch(plan['initial'],min(16*1024**2,cap-total),stamp);total+=len(data);sources.append(actual)
            name=f'init-{track_index}.mp4';atomic_write_bytes(cache/name,data);lines.append(f'#EXT-X-MAP:URI="{name}"')
        for index,(segment,duration) in enumerate(plan['segments']):
            collector.client.check()
            data,actual=fetch(segment,min(64*1024**2,cap-total),stamp)
            total+=len(data);sources.append(actual)
            if total>cap:raise ArchiveError('视频总大小超过限制。',reason='too_large')
            name=f'part-{track_index}-{index}.'+('m4s' if plan['initial'] else 'ts')
            atomic_write_bytes(cache/name,data)
            lines.extend([f'#EXTINF:{duration},',name])
            collector.log(f'HLS 已读取分片 {index+1}/{len(plan["segments"])}')
        lines.append('#EXT-X-ENDLIST')
        local=cache/f'track-{track_index}.m3u8';atomic_write_text(local,'\n'.join(lines)+'\n');local_playlists.append(local)
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.part.mp4')
    require_space(destination,total)
    try:
        args=[ffmpeg,'-nostdin','-v','error','-y']
        for local in local_playlists:args+=['-protocol_whitelist','file,crypto','-allowed_extensions','ALL','-i',str(local)]
        args+=['-map','0:v:0','-map','1:a:0' if len(local_playlists)>1 else '0:a?','-c','copy','-movflags','+faststart',str(temporary)]
        run_tool(args,collector.client.check)
        metadata=json.loads(run_tool([ffprobe,'-v','error','-protocol_whitelist','file','-show_format','-show_streams','-of','json',str(temporary)],collector.client.check))
        duration=float(metadata.get('format',{}).get('duration',0))
        if not math.isfinite(duration) or abs(duration-expected)>max(2,expected*.03) or not any(s.get('codec_type')=='video' for s in metadata.get('streams',[])):
            raise ArchiveError('视频时长或轨道不完整。',reason='hls_validation')
        run_tool([ffmpeg,'-nostdin','-v','error','-xerror','-protocol_whitelist','file','-i',str(temporary),'-map','0:v','-map','0:a?','-f','null','-'],collector.client.check,timeout=300)
        if not file_valid(temporary,'video'):raise ArchiveError('合并视频结构校验失败。',reason='hls_validation')
        sha=hashlib.sha256()
        with temporary.open('rb') as reader:
            while block:=reader.read(1024*1024):collector.client.check();sha.update(block)
        final=destination.with_name(destination.stem+'_'+sha.hexdigest()[:12]+'.mp4')
        replace_file(temporary,final)
        return {'file':final,'sha256':sha.hexdigest(),'bytes':final.stat().st_size,'validation_version':VALIDATION_VERSION,
                'hls':{'playlist':url,'scope':'wayback' if stamp else 'current_public','segments':len(sources),
                       'duration':duration,'checked_at':now(),'sources':sources,'decoded':True}}
    finally:temporary.unlink(missing_ok=True)
