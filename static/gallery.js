/* Shared by the local app and file:// offline collections; no CDN or network dependencies. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const labels = {running:'正在整理', complete:'本轮已完成', partial:'部分恢复', paused:'已保存进度', failed:'未完成'};
  const sourceLabels={wayback:'Wayback 存档',live_cdn:'原始媒体链接（当前）',common_crawl:'Common Crawl 存档',syndication:'X 公开嵌入数据',fxtwitter:'FxTwitter 公开镜像',vxtwitter:'VxTwitter 公开镜像',archived_metadata:'历史帖子元数据',wayback_video_config:'Wayback 视频线索',hls:'视频分片',hls_live:'视频分片合并（当前原始来源）',hls_wayback:'视频分片合并（Wayback 存档）'};
  const checkLabels={saved:'已恢复',found:'找到媒体线索',not_found:'本次未找到',no_media:'未提供媒体',failed:'请求或读取失败',unavailable:'暂不可用',rate_limited:'请求受限',restricted:'存在访问限制',unsupported:'暂不支持',mismatch:'帖子身份不匹配',pending:'等待检查',unknown:'未确认'};
  let data = null, prefix = '', shown = 40, signature = '';
  let remote=null,remoteTotal=0,pageSequence=0,pageBusy=false;
  const cards=new Map(), headings=new Map(), filterIds=['search','type-filter','month-filter','sort'];
  let restore=null, images=[], imageKey='', imageVersion='';
  function remember() {
    if(!data)return;
    try{localStorage.setItem('wayback-filters:'+data.username,JSON.stringify({shown,...Object.fromEntries(filterIds.map(id=>[id,$(id).value]))}));}catch{}
  }
  function visualKey(p) {
    return JSON.stringify([p.username,p.text,p.text_source,p.text_provenance,p.created_at,p.date_source,p.content_note,p.text_versions,p.captures,
      p.media.map(m=>[m.kind,m.key,m.urls,isVideoCover(m),m.status,m.path,m.error,m.archive_url,m.sha256,m.bytes,m.source,m.source_url,m.source_evidence,m.evidence_url,m.source_captured_at,m.captured_at,m.source_scope,m.provenance,m.recovery_checks,m.cc_more,m.cc_remaining_indexes,m.cc_completed_indexes,m.cc_pending_indexes])]);
  }
  const node = (tag, className, text) => {const n=document.createElement(tag); if(className)n.className=className; if(text!==undefined)n.textContent=text; return n;};
  function link(text, href, className='') {
    const a=node('a',className,text);
    try {const url=new URL(href,location.href); if(!['http:','https:','file:'].includes(url.protocol))return a; a.href=url.href;} catch {return a;}
    a.target='_blank'; a.rel='noopener noreferrer'; return a;
  }
  const month = p => p.created_at ? p.created_at.slice(0,7) : 'unknown';
  function localPath(value) {
    if(typeof value!=='string'||!value.startsWith('media/')||value.includes('\\'))return '';
    const parts=value.split('/');
    if(parts.some(part=>!part||part==='.'||part==='..'))return '';
    return prefix+parts.map(encodeURIComponent).join('/');
  }
  const isSaved = m => m.status==='saved'&&Boolean(localPath(m.path));
  const mediaKey = (p,m) => p.id+'|'+m.path;
  const postLabel = p => p.platform==='instagram'&&p.shortcode?p.shortcode:p.id;
  function videoAddress(value,allowArchive=true) {
    if(typeof value!=='string')return null;
    try {
      const url=new URL(/^video\.twimg\.com\//i.test(value)?'https://'+value:value);
      if(!['http:','https:'].includes(url.protocol))return null;
      if(allowArchive&&url.hostname==='web.archive.org') {
        const replay=url.pathname.match(/^\/web\/(?:\d{1,14}|\*)[a-z_]*\/(https?:\/\/.+)$/i);
        return replay?videoAddress(replay[1],false):null;
      }
      if(url.hostname!=='video.twimg.com')return null;
      const identity=url.pathname.match(/^\/(ext_tw_video|amplify_video)\/(\d+)\//);
      if(!identity)return null;
      const size=url.pathname.match(/\/(\d{1,5})x(\d{1,5})\//);
      return {key:identity[1]+'/'+identity[2],width:size?Number(size[1]):0,height:size?Number(size[2]):0};
    }catch{return null;}
  }
  function videoInfo(m) {
    if(m.kind!=='video')return null;
    const candidates=[m.key,...(Array.isArray(m.urls)?m.urls:[])].map(value=>videoAddress(value)).filter(Boolean);
    const sources=[m.source_url,m.provenance?.source_url,m.archive_url].map(value=>videoAddress(value)).filter(Boolean);
    const identities=new Set([...candidates,...sources].map(info=>info.key));
    // Conflicting or unknown identities remain separate; filenames alone cannot identify a video.
    if(identities.size!==1)return null;
    // A fallback download may be smaller than its requested URL. Prefer the saved file's actual source.
    const size=sources.find(info=>info.width>0&&info.height>0)||candidates.sort((a,b)=>b.width*b.height-a.width*a.height)[0]||{width:0,height:0};
    return {key:[...identities][0],width:size.width,height:size.height};
  }
  function savedGroups(media) {
    const groups=[],videos=new Map();
    for(const m of media.filter(isSaved)) {
      const info=videoInfo(m);
      if(!info){groups.push([m]);continue;}
      let group=videos.get(info.key);
      if(!group){group=[];videos.set(info.key,group);groups.push(group);}
      const digest=typeof m.sha256==='string'&&/^[a-f0-9]{64}$/i.test(m.sha256)?m.sha256.toLowerCase():'';
      if(!group.some(other=>other.path===m.path||(digest&&typeof other.sha256==='string'&&other.sha256.toLowerCase()===digest&&Number.isInteger(m.bytes)&&m.bytes>0&&m.bytes===other.bytes)))group.push(m);
    }
    for(const group of groups)if(group.length>1)group.sort((a,b)=>{const x=videoInfo(a),y=videoInfo(b);return y.width*y.height-x.width*x.height;});
    return groups;
  }
  function videoVariantLabel(m,index) {
    const info=videoInfo(m),size=Number(m.bytes);
    return `${info?.width&&info?.height?`${info.width} × ${info.height}`:`版本 ${index+1}（清晰度未标注）`}${Number.isFinite(size)&&size>0?` · ${(size/1048576).toFixed(1)} MB`:''}`;
  }
  function isVideoCover(m) {
    if(m.kind!=='image')return false;
    return [m.key,...(Array.isArray(m.urls)?m.urls:[])].some(value=>typeof value==='string'&&/^(?:https?:\/\/)?pbs\.twimg\.com\/(?:ext_tw_video_thumb|amplify_video_thumb|tweet_video_thumb)\//i.test(value));
  }
  function downloadLink(m, className='media-download') {
    const dl=link('下载原文件 ↓',localPath(m.path),className);dl.removeAttribute('target');dl.download=m.path.split('/').pop();return dl;
  }
  function sourceLink(text,value) {
    if(typeof value!=='string')return null;
    try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)?link(text,url.href):null;}catch{return null;}
  }
  function timestamp(value) {
    const text=String(value||'');
    if(/^\d{14}$/.test(text))return `${text.slice(0,4)}-${text.slice(4,6)}-${text.slice(6,8)} ${text.slice(8,10)}:${text.slice(10,12)}:${text.slice(12,14)} UTC`;
    return text.replace('T',' ').replace(/Z$/,' UTC');
  }
  function scopeText(scope) {
    if(Array.isArray(scope))return scope.join('、');
    if(scope==='current_public')return '当前可公开访问的帖子信息';
    if(scope&&typeof scope==='object')return JSON.stringify(scope);
    return typeof scope==='string'?scope:'';
  }
  function sourceDetails(m) {
    const provenance=m.provenance&&typeof m.provenance==='object'?m.provenance:{};
    const source=m.source||provenance.source||'wayback',bar=node('div','media-provenance');
    if(isVideoCover(m))bar.append(node('span','tag','视频封面（图片）'));
    bar.append(node('span','source-tag',sourceLabels[source]||source));
    const details=node('details','source-details');details.append(node('summary','','来源详情'));
    if(source==='live_cdn')details.append(node('p','hint','此文件来自当前仍可访问的原始媒体链接，并非历史页面快照。'));
    const captured=m.source_captured_at||m.captured_at||provenance.captured_at;
    if(captured)details.append(node('p','hint','存档时间：'+timestamp(captured)));
    const origin=sourceLink('媒体来源 ↗',m.source_url||provenance.source_url||m.archive_url);
    const evidence=sourceLink('查找依据 ↗',m.source_evidence||m.evidence_url||provenance.evidence_url||provenance.evidence);
    if(origin)details.append(origin);if(evidence)details.append(evidence);
    const scope=scopeText(m.source_scope||provenance.scope);
    if(scope)details.append(node('p','hint','查询范围：'+scope));
    if(details.children.length>1)bar.append(details);
    return bar;
  }
  function recoveryChecks(m) {
    const checks=Array.isArray(m.recovery_checks)?m.recovery_checks.filter(check=>check&&typeof check==='object'):[];
    if(!checks.length)return null;
    const details=node('details','source-checks');details.append(node('summary','',`各来源检查（${checks.length}）`));
    for(const check of checks) {
      const entry=node('div','source-check');
      entry.append(node('strong','',sourceLabels[check.source]||check.source||'未知来源'),node('span','check-status',checkLabels[check.status]||check.status||'未确认'));
      if(check.status==='found')entry.append(node('p','hint','已发现候选地址；文件尚须成功下载并通过校验，才会计入已恢复媒体。'));
      if(check.checked_at)entry.append(node('p','hint','检查时间：'+timestamp(check.checked_at)));
      if(check.error)entry.append(node('p','check-error',String(check.error)));
      const scope=scopeText(check.scope);
      if(scope)entry.append(node('p','hint','查询范围：'+scope));
      if(check.source==='common_crawl')entry.append(node('p','hint',scope?'仅覆盖上述存档集合，不能据此判断全部历史存档。':check.indexes_limit?`本轮最多查询 ${check.indexes_limit} 个相关集合，未找到不代表全部历史存档均无记录。`:'本次结果仅代表已查询的范围。'));
      if(check.variants_checked)entry.append(node('p','hint',`检查了 ${check.variants_checked} 个媒体地址变体`));
      const evidence=sourceLink('检查依据 ↗',check.evidence_url||check.url);
      if(evidence)entry.append(evidence);
      details.append(entry);
    }
    return details;
  }
  function crawlProgress(m) {
    if(m.cc_more===undefined&&!Array.isArray(m.cc_remaining_indexes)&&!Array.isArray(m.cc_completed_indexes)&&!Array.isArray(m.cc_pending_indexes))return null;
    const unique=values=>[...new Set(Array.isArray(values)?values.filter(value=>typeof value==='string'&&/^CC-MAIN-\d{4}-\d{2}$/.test(value)):[])];
    const completed=unique(m.cc_completed_indexes),pending=unique(m.cc_pending_indexes).filter(value=>!completed.includes(value));
    const untouched=unique(m.cc_remaining_indexes).filter(value=>!completed.includes(value)&&!pending.includes(value));
    const unknown=m.cc_more!==false&&!untouched.length&&!pending.length;
    const details=node('details','crawl-progress');
    details.append(node('summary','',`Common Crawl：已查 ${completed.length} 个集合 · 待重试 ${pending.length} 个 · ${unknown?'未检索数待确认':`未检索 ${untouched.length} 个`}`));
    if(unknown||m.cc_more===true||pending.length||untouched.length)details.append(node('p','hint','还有范围未检查完。启用此来源时，“继续查找与下载”会接着检查并重试失败的集合，无需强制重查。'));
    else details.append(node('p','hint','当前目录内的集合已完成本轮限定范围查询，仍未得到可用文件；不代表该媒体在互联网中从未被保存。'));
    if(pending.length)details.append(node('p','hint','待重试：'+pending.slice(0,12).join('、')+(pending.length>12?` 等 ${pending.length} 个集合`:'')));
    details.append(node('p','hint','查询进度不等于文件恢复进度；只有成功保存并校验的媒体才计入已恢复数。'));
    return details;
  }
  function mediaError(media,container,kind,hide=media) {
    const error=node('p','media-error');error.hidden=true;error.setAttribute('role','status');container.append(error);
    media.addEventListener('error',()=>{
      const code=media.error?.code;
      error.textContent=kind==='video'&&[3,4].includes(code)
        ?'浏览器无法播放这个视频格式，或文件已损坏。可下载原文件，用本地播放器打开。'
        :`${kind==='video'?'视频':'图片'}无法读取，文件可能已移动、未完整解压或损坏。可尝试下载原文件；在时光集主页面继续查找与下载，可检查缺失文件。`;
      error.hidden=false;hide.hidden=true;
    });
    return error;
  }
  function updateImage(replace=true) {
    const index=images.findIndex(item=>item.key===imageKey);
    if(index<0){$('lightbox').close();imageKey='';return;}
    const item=images[index];
    $('image-count').textContent=`${index+1} / ${images.length}`;
    $('image-caption').textContent=`@${item.post.username} · ${item.post.created_at?item.post.created_at.slice(0,10):'日期未知'} · #${postLabel(item.post)}${isVideoCover(item.media)?' · 视频封面（图片）':''}`;
    $('previous-image').disabled=index===0;$('next-image').disabled=index===images.length-1;
    const version=JSON.stringify([item.key,item.media.sha256,item.media.bytes]);
    if(!replace&&version===imageVersion)return;
    imageVersion=version;
    const content=$('lightbox-content');content.replaceChildren();
    const img=node('img');img.alt=`@${item.post.username} 的${isVideoCover(item.media)?'视频封面':'存档图片'}`;
    mediaError(img,content,'image');content.append(img);img.src=localPath(item.media.path);
    $('image-download').replaceChildren(downloadLink(item.media,'secondary small'));
  }
  function openImage(key) {
    imageKey=key;updateImage();if(imageKey&&!$('lightbox').open)$('lightbox').showModal();
  }
  function stepImage(delta) {
    const index=images.findIndex(item=>item.key===imageKey),next=index+delta;
    if(index>=0&&next>=0&&next<images.length){imageKey=images[next].key;updateImage();}
  }
  function card(p) {
    const article=node('article','post');
    const head=node('div','post-header'); const who=node('span','post-author','@'+p.username);
    const date=p.created_at ? p.created_at.replace('T',' ').replace(/\.\d+Z$|Z$/,'').slice(0,19) : '发帖日期未知';
    head.append(who,node('time','post-date',date));
    if(p.date_source==='snowflake')head.append(node('span','tag','ID 推算'));
    article.append(head);
    const hasText=Boolean((p.text||'').trim());
    article.append(node('p',hasText?'post-text':'post-text muted',hasText?p.text:p.content_note||'未恢复正文，保留帖子与存档入口。'));
    if(p.text_source==='public_metadata')article.append(node('p','hint','正文来自当前公开帖子资料，非历史快照；来源：'+(p.text_provenance?.source||'公开来源')));
    if(p.text_versions?.length) {
      const details=node('details','versions');details.append(node('summary','',`其他正文版本（${p.text_versions.length}）`));
      p.text_versions.forEach(t=>details.append(node('p','post-text',t)));article.append(details);
    }
    const saved=savedGroups(p.media);
    if(saved.length) {
      const grid=node('div','media-grid'+(saved.length===1?' single':''));
      for(const variants of saved) {
        const m=variants[0];
        const frame=node('div','media-frame'), src=localPath(m.path);
        if(m.kind==='video') {
          const video=node('video');video.controls=true;video.preload='metadata';video.playsInline=true;
          const error=mediaError(video,frame,'video');frame.append(video);video.src=src;
          const download=node('div','video-download'),source=node('div','video-source');
          const details=current=>{download.replaceChildren(downloadLink(current));source.replaceChildren(sourceDetails(current));};
          if(variants.length>1) {
            const label=node('label','video-variants'),select=node('select');
            label.append(node('span','',`已保存 ${variants.length} 个版本`));
            select.setAttribute('aria-label','选择已保存的视频版本');
            variants.forEach((variant,index)=>select.add(new Option(videoVariantLabel(variant,index),String(index))));select.value='0';
            select.addEventListener('change',()=>{
              const selected=variants[Number(select.value)];if(!selected)return;
              video.pause?.();video.hidden=false;error.hidden=true;video.src=localPath(selected.path);details(selected);
            });
            label.append(select);frame.append(label);
          }
          details(m);frame.append(download,source);
        }
        else {const b=node('button','image-button');b.type='button';b.setAttribute('aria-label',isVideoCover(m)?'放大视频封面':'放大图片');const img=node('img');img.loading='lazy';img.alt=`@${p.username} 的${isVideoCover(m)?'视频封面':'存档图片'}`;b.append(img);b.addEventListener('click',()=>openImage(mediaKey(p,m)));mediaError(img,frame,'image',b);frame.append(b);img.src=src;frame.append(downloadLink(m),sourceDetails(m));}
        grid.append(frame);
      }article.append(grid);
    }
    const missing=p.media.filter(m=>!isSaved(m));
    if(missing.length) {
      const details=node('details','missing');details.append(node('summary','',`${missing.length} 条媒体链接未保存`));
      const savedVideos=new Set(saved.map(group=>videoInfo(group[0])?.key).filter(Boolean));
      if(missing.some(m=>savedVideos.has(videoInfo({...m,kind:'video'})?.key)))details.append(node('p','hint','部分链接属于已有保存版本的视频；缺少其他清晰度或播放列表不代表整段视频未恢复。'));
      for(const m of missing) {const row=node('div','missing-row');row.append(node('span','',m.kind==='hls'?'视频分片':m.kind==='video'?'视频':isVideoCover(m)?'视频封面':'图片'),node('span','hint',m.error||(m.status==='saved'?'本地文件路径缺失或无效，请在主页面继续查找与下载。':m.kind==='hls'?'等待恢复视频分片；可在多源恢复时重试。':'尚未保存')));if(m.archive_url)row.append(link('存档入口 ↗',m.archive_url));const checks=recoveryChecks(m);if(checks)row.append(checks);const crawl=crawlProgress(m);if(crawl)row.append(crawl);details.append(row);}article.append(details);
    }
    const foot=node('div','post-footer');foot.append(node('span','post-id','#'+postLabel(p)));
    const caps=[...(p.captures||[])].sort((a,b)=>b.timestamp.localeCompare(a.timestamp));
    if(caps.length) {
      foot.append(link('查看存档 ↗',caps[0].archive_url));
      const details=node('details','captures');details.append(node('summary','',`${caps.length} 份来源快照`));
      caps.forEach(c=>details.append(link(`${c.timestamp.slice(0,4)}-${c.timestamp.slice(4,6)}-${c.timestamp.slice(6,8)} ${c.timestamp.slice(8,10)}:${c.timestamp.slice(10,12)} UTC`,c.archive_url)));foot.append(details);
    }
    article.append(foot);return article;
  }
  function render() {
    if(!data)return;
    const q=$('search').value.trim().toLowerCase(),type=$('type-filter').value,mo=$('month-filter').value,asc=$('sort').value==='asc';
    let posts=data.posts.filter(p=>(!q||((p.text||'')+' '+p.id+' '+(p.shortcode||'')).toLowerCase().includes(q))&&(mo==='all'||month(p)===mo));
    posts=posts.filter(p=>type==='all'||(type==='saved'&&p.media.some(isSaved))||(type==='saved-video'&&p.media.some(m=>isSaved(m)&&m.kind==='video'))||(type==='text'&&!p.media.length)||(type==='missing'&&p.media.some(m=>!isSaved(m)))||(type==='missing-text'&&!(p.text||'').trim())||(type==='image'&&p.media.some(m=>m.kind==='image'))||(type==='video'&&p.media.some(m=>['video','hls'].includes(m.kind))));
    posts.sort((a,b)=> {if(!a.created_at&&b.created_at)return 1;if(a.created_at&&!b.created_at)return -1;const d=(a.created_at||'').localeCompare(b.created_at||'')||(BigInt(a.id)>BigInt(b.id)?1:BigInt(a.id)<BigInt(b.id)?-1:0);return asc?d:-d;});
    // Include all filtered images, even when their cards have not been loaded yet.
    images=posts.flatMap(p=>p.media.filter(m=>m.kind==='image'&&isSaved(m)).map(m=>({key:mediaKey(p,m),post:p,media:m})));
    if($('lightbox').open)updateImage(false);
    $('result-count').textContent=remote?`${remoteTotal.toLocaleString()} 条帖子 · 已载入 ${posts.length.toLocaleString()} 条`: `${posts.length.toLocaleString()} 条帖子`;
    const container=$('timeline'),desired=[],visible=new Set();let current='';
    for(const p of posts.slice(0,shown)) {
      const m=month(p);if(m!==current){if(!headings.has(m))headings.set(m,node('h2','month-heading',m==='unknown'?'日期未知':m.replace('-',' 年 ')+' 月'));desired.push(headings.get(m));current=m;}
      const key=visualKey(p),cached=cards.get(p.id);
      if(!cached||cached.key!==key)cards.set(p.id,{key,element:card(p)});
      desired.push(cards.get(p.id).element);visible.add(p.id);
    }
    for(const id of cards.keys())if(!visible.has(id))cards.delete(id);
    if(!posts.length) {
      const empty=node('div','empty-results',data.posts.length?'没有符合筛选条件的帖子。':`${data.status==='failed'?'查询失败，尚未发现帖子。':'未发现帖子。'}可在时光集主页面点击“继续查找与下载”；${data.platform==='instagram'?'也可选择 Instagram 后补充已知帖子链接。没有留下可访问存档的已删除内容无法恢复。':'也可核对该博主的历史用户名后重新查询。'}`);
      const nearby=data.last_run?.discovery?.nearby_handles;
      if(!data.posts.length&&Array.isArray(nearby)) {
        const seen=new Set([String(data.username).toLowerCase()]);
        const handles=nearby.filter(handle=>{if(typeof handle!=='string'||!/^[A-Za-z0-9_]{1,15}$/.test(handle)||seen.has(handle.toLowerCase()))return false;seen.add(handle.toLowerCase());return true;}).slice(0,3);
        if(handles.length)empty.append(node('p','nearby-handles',`索引出现相近用户名：${handles.map(handle=>'@'+handle).join('、')}；请核对后单独输入。相近账号的帖子未计入当前合集。`));
      }
      desired.push(empty);
    }
    // Keep unchanged cards attached: polling and "load more" must not reset video playback.
    const retained=new Set(desired);for(const child of [...container.children])if(!retained.has(child))child.remove();
    let cursor=container.firstChild;
    for(const child of desired){if(child===cursor)cursor=cursor.nextSibling;else container.insertBefore(child,cursor);}
    $('load-more').hidden=remote?posts.length>=remoteTotal:posts.length<=shown;
  }
  function show(manifest, pathPrefix='', fetchPage=null) {
    const changed=!data||data.username!==manifest.username||prefix!==pathPrefix;
    const sameContent=!changed&&remote&&fetchPage&&data.content_revision===manifest.content_revision;
    const previousPosts=!changed&&remote?data.posts:[];
    if(!sameContent){pageSequence++;remote=fetchPage;pageBusy=false;remoteTotal=manifest.counts?.posts||0;}
    if(changed){
      remember();cards.clear();headings.clear();signature='';shown=40;restore=null;imageKey='';if($('lightbox').open)$('lightbox').close();
      if(fetchPage){$('timeline').replaceChildren();images=[];$('load-more').hidden=true;}
      for(const id of filterIds)$(id).value=id==='search'?'':id==='sort'?'desc':'all';
      try{restore=JSON.parse(localStorage.getItem('wayback-filters:'+manifest.username));}catch{}
      if(restore&&typeof restore==='object'){
        for(const id of filterIds.filter(id=>id!=='month-filter'))if(typeof restore[id]==='string'){$(id).value=restore[id];if(id!=='search'&&!$(id).value)$(id).value=id==='sort'?'desc':'all';}
        if(Number.isInteger(restore.shown)&&restore.shown>0)shown=Math.min(restore.shown,Math.max(40,manifest.posts.length));
      }
    }
    data=sameContent?{...manifest,posts:previousPosts}:manifest;prefix=pathPrefix;
    $('collection-title').textContent=(data.platform==='instagram'?'Instagram ':'X ')+'@'+(data.display_username||data.username);
    const c=data.counts;
    $('stats').replaceChildren(...[['帖子',c.posts],['恢复正文',c.text],['已存文件记录',c.saved],['未存链接记录',c.missing]].map(([label,value])=>{const el=node('div','stat');el.append(node('strong','',Number(value||0).toLocaleString()),node('span','',label));return el;}));
    const inventory=$('media-inventory'),media=data.media_inventory;
    if(inventory) {
      inventory.replaceChildren();inventory.hidden=media?.schema!==1;
      if(!inventory.hidden) {
        const n=key=>Number(media[key]||0).toLocaleString();
        inventory.append(node('p','',`视频：已保存 ${n('video_saved')} 组 · 未保存 ${n('video_missing')} 组；图片：${n('image_saved')} / ${n('image_records')}；视频封面：${n('cover_saved')} / ${n('cover_records')}（已存 / 记录）`));
        const details=node('details','inventory-details');details.append(node('summary','','媒体统计说明'));
        details.append(node('p','hint',`同一帖子内按明确的视频 ID 合并清晰度和播放列表；封面单列。有 ${n('groups_with_missing_variants')} 组已有视频但仍缺其他版本。上方保留原始文件和链接记录数。`));
        if(media.duplicate_saved_records)details.append(node('p','hint',`${n('duplicate_saved_records')} 条已存记录的文件摘要和大小相同。原文件和帖子归属保留；同一视频的版本列表不重复展示相同文件。`));
        if(media.video_unidentified)details.append(node('p','hint',`${n('video_unidentified')} 条视频记录无法确认是否为同一视频，暂分别计数，其中 ${n('video_identity_conflicts')} 条存在 ID 冲突。`));
        if(media.invalid_saved_records)details.append(node('p','hint',`${n('invalid_saved_records')} 条已存记录的本地路径无效，未计入下方分类的已保存数量。`));
        details.append(node('p','hint','统计依据当前保存记录，不代表找回全部历史内容，也不替代本地文件重新校验。'));
        inventory.append(details);
      }
    }
    const task=data.task_summary;
    const outcome=task?.label||(c.posts===0&&data.status!=='running'?(data.status==='failed'?'查询失败':'未发现帖子'):(labels[data.status]||data.status));
    const badge=$('collection-status');
    if(badge){badge.hidden=!task;if(task){badge.className='task-badge task-'+task.tone;badge.textContent=({info:'◷',success:'✓',warning:'●',danger:'!',neutral:'—'}[task.tone]||'—')+' '+task.label;}}
    $('collection-note').textContent=`${outcome}${task?' · '+task.result_label:''} · 已读 ${c.parsed||0} / ${c.captures||0} 份快照`+(task?'。'+task.reason_text:'')+(data.platform==='instagram'?' '+(data.scope_note||''):'');
    const discoveryBox=$('discovery-records'),discovery=data.last_run?.discovery;
    if(discoveryBox) {
      discoveryBox.replaceChildren();
      const count=value=>Number.isInteger(value)&&value>=0?value.toLocaleString():'未提供';
      const valid=discovery&&typeof discovery==='object'&&!Array.isArray(discovery)&&['new_posts','new_text','resource_captures','queried_hosts','failed_hosts'].some(key=>Number.isInteger(discovery[key])&&discovery[key]>=0);
      discoveryBox.hidden=!valid;
      if(valid) {
        discoveryBox.append(node('p','',`本轮查找：新增 ${count(discovery.new_posts)} 条帖子 · 新增 ${count(discovery.new_text)} 条正文`));
        const details=node('details','discovery-diagnostics');details.append(node('summary','','查找诊断'));
        details.append(node('p','',`资源记录：${count(discovery.resource_captures)} 条 · 已查询域名：${count(discovery.queried_hosts)} 个 · 失败域名：${count(discovery.failed_hosts)} 个`));
        details.append(node('p','hint','资源记录和新增帖子不代表媒体已下载；文件成功保存并校验后才计入已下载媒体。'));
        discoveryBox.append(details);
      }
    }
    const summary=$('recovery-summary');
    if(summary) {
      summary.replaceChildren();
      const facts=[];
      if(c.text_missing)facts.push(`${c.text_missing} 条未恢复正文${c.restricted_posts?`，其中 ${c.restricted_posts} 条快照只有登录或年龄限制提示`:''}`);
      const textIssues=data.recovery_report?.text;
      if(textIssues){const names={page_not_read:'页面尚未成功读取',needs_parsing_or_other_snapshot:'页面已读，待解析或换快照',empty_snapshot:'快照为空壳页',no_capture:'尚无对应页面快照',restricted:'访问受限'};const parts=Object.entries(names).filter(([k])=>textIssues[k]).map(([k,v])=>`${v} ${textIssues[k]} 条`);if(parts.length)facts.push(parts.join('；')+'。');}
      if(c.not_indexed)facts.push(`${c.not_indexed} 项媒体：已查范围内未找到可下载存档，不代表其他来源一定没有保存。`);
      const missing=data.posts.flatMap(p=>p.media).filter(m=>!isSaved(m));
      const network=data.media_issues?.network ?? missing.filter(m=>m.error_kind==='network'||m.recovery_error_kind==='network').length;
      const unavailable=data.media_issues?.not_found ?? missing.filter(m=>m.error_kind==='not_found').length;
      const restricted=data.media_issues?.access_denied ?? missing.filter(m=>m.error_kind==='access_denied').length;
      const unsupported=data.media_issues?.unsupported ?? missing.filter(m=>m.kind==='hls'||m.error_kind==='unsupported_hls').length;
      if(network)facts.push(`${network} 项媒体：网络或读取失败，后续查找可重试。`);
      if(unavailable)facts.push(`${unavailable} 项媒体：已知文件地址暂不可用，后续查找会尝试其他线索。`);
      if(restricted)facts.push(`${restricted} 项媒体：来源拒绝访问。`);
      if(unsupported)facts.push(`${unsupported} 项视频分片记录尚未恢复；完整、无加密的受支持列表可在多源恢复时合并，具体限制见来源记录。`);
      if(c.live_recovered)facts.push(`${c.live_recovered} 项已从当前原始媒体链接补回`);
      if(c.cc_recovered)facts.push(`${c.cc_recovered} 项已从 Common Crawl 历史存档补回`);
      const continuing=missing.filter(m=>['image','video'].includes(m.kind)&&(m.cc_more===true||(Array.isArray(m.cc_pending_indexes)&&m.cc_pending_indexes.length)||(Array.isArray(m.cc_remaining_indexes)&&m.cc_remaining_indexes.length)));
      const continuingCount=data.media_issues?.continuing ?? continuing.length;
      if(continuingCount)facts.push(`${continuingCount} 项媒体还有 Common Crawl 存档范围未检查完；启用此来源时，后续查找会接着处理。`);
      if(continuingCount||(network&&c.not_indexed))facts.push('不同来源的检查结果可能涉及同一文件，未恢复总数以上方统计为准。');
      if(data.last_run?.multi_source){const result=data.last_run.multi_source;facts.push(`本轮多源恢复：检查 ${result.checked||0} 项，补回 ${result.recovered||0} 项${result.cached?`，复用 ${result.cached} 项近期检查`:''}。未恢复项可展开查看各来源结果。`);}
      if(data.last_run?.post_recovery){const r=data.last_run.post_recovery;facts.push(`最近一批正文补取：检查 ${r.checked||0} 条，补回 ${r.text_recovered||0} 条，发现 ${r.media_discovered||0} 项媒体。`);}
      if(data.last_run?.cached_missing)facts.push(`本轮复用了 ${data.last_run.cached_missing} 项近期未找到存档的检查结果；24 小时后再次查找会自动复查，也可在高级设置中立即重新检查。`);
      if(typeof data.scope_note==='string'&&data.scope_note.trim())facts.push(data.scope_note);
      facts.forEach(t=>summary.append(node('p','',t)));summary.hidden=!facts.length;
    }
    const errors=data.errors||[];$('error-details').hidden=!errors.length;$('error-summary').textContent=`本轮有 ${errors.length} 项记录需要留意`;$('error-list').replaceChildren(...errors.map(e=>node('li','',e.stage+'：'+e.error)));
    const months=data.months||[...new Set(data.posts.map(month))];
    const key=data.username+'|'+months.join(',');
    if(key!==signature) {const old=$('month-filter').value,select=$('month-filter');select.replaceChildren(new Option('全部月份','all'));[...months].sort().reverse().forEach(m=>select.add(new Option(m==='unknown'?'日期未知':m,m)));if([...select.options].some(o=>o.value===old))select.value=old;signature=key;}
    if(restore&&[...$('month-filter').options].some(o=>o.value===restore['month-filter']))$('month-filter').value=restore['month-filter'];restore=null;
    if(remote&&!sameContent){shown=Math.max(40,previousPosts.length);loadPage(false,shown);}else render();
  }
  async function loadPage(append, count=40) {
    if(!remote||(append&&pageBusy))return;
    const sequence=++pageSequence,loader=remote;
    pageBusy=true;$('load-more').disabled=true;
    if(!append){data={...data,posts:[]};$('result-count').textContent='正在载入帖子…';}
    try {
      const params={revision:data.content_revision||'',search:$('search').value,type:$('type-filter').value,month:$('month-filter').value,sort:$('sort').value};
      const received=[];let page;
      do {
        page=await loader({...params,offset:(append?data.posts.length:0)+received.length,limit:Math.min(200,count-received.length)});
        if(sequence!==pageSequence||loader!==remote||page.stale)return;
        received.push(...page.posts);
      } while(received.length<count&&page.has_more&&page.posts.length);
      data={...data,posts:append?[...data.posts,...received]:received};
      remoteTotal=page.total;shown=data.posts.length;render();remember();
    } catch(error) {
      if(sequence===pageSequence){$('result-count').textContent='帖子载入失败：'+error.message+'。点击下方按钮重试。';$('load-more').hidden=false;}
    } finally {if(sequence===pageSequence){pageBusy=false;$('load-more').disabled=false;}}
  }
  for(const id of filterIds)$(id).addEventListener(id==='search'?'input':'change',()=>{shown=40;if(remote)loadPage(false);else render();remember();});
  $('load-more').addEventListener('click',()=>{if(remote){loadPage(true);return;}shown+=40;render();remember();});
  $('close-lightbox').addEventListener('click',()=>$('lightbox').close());
  $('previous-image').addEventListener('click',()=>stepImage(-1));
  $('next-image').addEventListener('click',()=>stepImage(1));
  $('lightbox').addEventListener('keydown',e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();stepImage(e.key==='ArrowLeft'?-1:1);}});
  $('lightbox').addEventListener('click',e=>{if(e.target===$('lightbox'))$('lightbox').close();});
  window.Gallery={show};
  if(window.ARCHIVE_DATA)show(window.ARCHIVE_DATA);
})();
