(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  let token='',selected='',lastUpdate='',wasRunning=false,tick=0,loadSequence=0,librarySequence=0,connectionError=false,supportsMultiSource=false,supportsDiscovery=false,supportsRepair=false,jobRunning=false,selectedPosts=null;
  let pollTask=null,pollAgain=false,needsCollections=true,initialized=false,stateGeneration=0,cacheGeneration=0,actionPending=false,preferred='';
  let supportsAuto=false,supportsDelete=false,supportsDesktopLauncher=false,jobHandle='',selectedCollecting=false,deleteTarget='',deleting=false;
  let supportsIdentity=false,identityTarget='',identityBusy=false;
  let inputEdited=false,closeAddOnLoad=false;
  let selectedSummary=null;
  let supportsPagination=false;
  let supportsQueue=false,queueSubmission=null;
  let cloudFormReady=false;
  const isInstagram=name=>String(name||'').startsWith('instagram--');
  const displayName=name=>isInstagram(name)?'Instagram @'+String(name).slice(11):'@'+name;
  const inputPlatform=()=>$('platform').value==='instagram'?'instagram':'x';
  function platformControls(){
    const ig=inputPlatform()==='instagram';
    $('username-label').textContent=ig?'Instagram 用户名':'X / Twitter 用户名';
    $('platform-hint').textContent=ig?'恢复公开历史快照中可核实的帖子、图片与视频。已删除内容只有留下可访问存档才可能找回；暂不自动关联旧用户名。':'填写 @ 后的用户名。自动搜寻并核对旧用户名，再查找帖子与媒体；核实的旧名会一起整理。';
    $('instagram-seeds').hidden=!ig;
    $('queue-input-label').textContent=ig?'多个 Instagram 用户名':'多个 X / Twitter 用户名';
    $('queue-input').placeholder=ig?'@alice\n@bob.name\nhttps://www.instagram.com/charlie/':'@alice\n@bob\nhttps://x.com/charlie';
  }
  const actionControls=['start-button','continue-button','pause-button','repair-button','discover-button','discover-input-button','rebuild-zip','delete-button','job-resume','job-rebuild','queue-add','queue-pause','queue-resume','queue-clear'];
  const responses=new Map();
  let storageRevision='';
  const storagePanel=typeof StoragePanel==='undefined'?null:StoragePanel.create({api,getSelected:()=>selected,isBusy:()=>jobRunning||actionPending,refresh:()=>poll(true)});
  const historyPanel=typeof HistoryPanel==='undefined'?null:HistoryPanel.create({api,select,options:()=>fullOptions(''),retry:async(id,options)=>{
    if(actionPending||jobRunning||connectionError)throw new Error('请先等待当前任务完成或暂停保存。');
    actionPending=true;discoveryControl();
    try{const result=await api('/api/history/retry',{...options,id});choose(result.username);}
    finally{actionPending=false;await poll(true);}
  }});
  const statusLabels={complete:'已整理',partial:'部分恢复',paused:'已暂停',failed:'未完成',running:'整理中'};
  function resultStatus(value) {
    if(value.task_summary?.schema_version===1)return value.task_summary.label;
    if(value.mode==='identity'&&!value.running)return value.status==='paused'?'检查已暂停':value.status==='failed'?'检查未完成':'检查已结束';
    if(value.interrupted)return '已暂停';
    if(!value.running&&value.automatic?.enabled&&value.automatic?.stop_reason)return value.automatic.stop_code==='user_pause'?'已暂停':'自动处理已结束';
    if(value.status==='paused'&&value.batch?.stop_reason)return '本批已结束';
    if(!value.running&&value.status!=='running'&&value.counts?.posts===0)return value.status==='failed'?'查询失败':'未发现帖子';
    return statusLabels[value.status]||value.status||'本轮结束';
  }
  function discoveryControl() {
    $('identity-button').hidden=!supportsIdentity||isInstagram(selected);
    $('identity-button').disabled=!selected||actionPending||connectionError;
    $('identity-scan').disabled=jobRunning||actionPending||identityBusy||connectionError;
    $('identity-add').disabled=jobRunning||actionPending||identityBusy||connectionError;
    $('discover-input-button').hidden=!supportsDiscovery;
    $('discover-input-note').hidden=!supportsDiscovery;
    $('discover-input-button').disabled=jobRunning||actionPending||!supportsDiscovery||!$('username').value.trim();
    $('discover-button').hidden=!supportsDiscovery;
    $('discover-button').disabled=jobRunning||actionPending||!selected||!supportsDiscovery;
    $('continue-button').disabled=jobRunning||actionPending||!selected||selectedCollecting||selectedPosts===null;
    $('continue-button').textContent=jobRunning?(jobHandle===selected?'正在处理此博主…':`正在处理 ${displayName(jobHandle||'其他博主')}`):selectedCollecting?'该博主正在处理中…':'继续查找与下载';
    if(!jobRunning&&!selectedCollecting&&selectedSummary){
      $('continue-button').textContent=['paused','interrupted'].includes(selectedSummary.state)?'继续任务':'重新查找与下载';
      $('continue-button').classList.toggle('primary',selectedSummary.recommended_action==='continue');
      $('continue-button').classList.toggle('secondary',selectedSummary.recommended_action!=='continue');
    }
    $('repair-button').disabled=jobRunning||actionPending||!selected||selectedPosts===0||!supportsRepair;
    $('repair-button').textContent='仅下载已知媒体';
    $('delete-button').hidden=!supportsDelete;
    const busy=selectedCollecting||(jobRunning&&jobHandle===selected);
    $('delete-button').disabled=actionPending||!selected||busy;
    $('delete-note').hidden=!supportsDelete||!busy;
    if(connectionError)for(const id of actionControls)$(id).disabled=true;
  }
  function sleepStatus(job) {
    const messages={
      active:'已启用任务防休眠 · 屏幕可以熄灭；请勿手动睡眠或合盖。',
      pending:'正在申请任务防休眠，请稍候确认开启状态。',
      failed:'未能启用防休眠，任务仍在继续。请接通电源，并在 Windows 设置中临时关闭自动睡眠。',
      unsupported:'当前系统不支持自动防休眠，请保持电脑唤醒。',
      off:'当前任务未启用防休眠，请保持电脑唤醒。'
    };
    const message=job.running?messages[job.sleep_prevention]||'':'';
    $('job-sleep').textContent=message;$('job-sleep').hidden=!message;
  }
  function taskOutcome(job) {
    const summary=job.task_summary;
    for(const id of ['job-resume','job-rebuild'])$(id).disabled=actionPending||connectionError||jobRunning||!jobHandle;
    $('job-status').hidden=!summary;
    $('job-result').hidden=!summary;
    $('job-aliases').hidden=!summary||isInstagram(job.username);
    $('job-ended').hidden=!summary||!summary.finished_at||job.running;
    $('job-actions').hidden=!summary||job.running;
    if(summary){
      statusBadge($('job-status'),summary);
      const c=job.counts||{};
      $('job-result').textContent=`${summary.result_label} · 已保存 ${c.posts||0} 条帖子 · 已存 ${c.saved||0} 条文件记录 · 未存 ${Math.max(0,(c.media||0)-(c.saved||0))} 条链接记录`;
      $('job-aliases').textContent=aliasText(summary.alias_discovery,job.identity);
      $('job-ended').textContent='结束于 '+localTime(summary.finished_at);
      $('job-resume').hidden=summary.recommended_action!=='continue';
      $('job-rebuild').hidden=summary.recommended_action!=='rebuild_zip';
      $('job-browse').textContent='浏览 '+displayName(job.username||'')+' 的合集';
      $('job-outcome').classList.toggle('compact-outcome',true);
    }else $('job-outcome').classList.toggle('compact-outcome',false);
    if(job.mode==='identity'){
      $('job-outcome').textContent=job.running?'正在核对本地快照中的账号归属…':'可在“更多 → 历史用户名”查看已核对的结果';
      $('job-reason').textContent=summary?.reason_text||'在“更多 → 历史用户名”查看结果；继续整理会检索核对通过的名字。';
      $('job-reason').hidden=false;return;
    }
    // The server's automatic total already includes the active batch. Never add
    // batch.new_files again, or use the lifetime collection count as a run delta.
    const count=job.automatic?.enabled?job.automatic.new_files:job.batch?.new_files;
    const reused=job.automatic?.enabled?job.automatic.reused_files:job.batch?.reused_files;
    $('job-outcome').textContent=Number.isFinite(count)?`本次${job.running?'已':''}新增 ${count} 个媒体文件`:
      job.running?(['export','zip'].includes(job.stage)?'正在更新离线合集…':'正在查找可恢复内容…'):'已保存的内容可在合集中浏览';
    if(Number.isFinite(count)&&Number.isFinite(reused)&&reused>0)$('job-outcome').textContent+=`（其中 ${Math.min(count,reused)} 个复用自本地旧合集）`;
    const stop=job.automatic?.enabled?job.automatic:job.batch,code=stop?.stop_code;
    let reason='';
    if(job.running){
      reason=job.pause_requested?'正在完成当前请求并保存进度。':
        ['export','zip'].includes(job.stage)?'正在保存结果并更新下载合集。':
        job.automatic?.enabled?'会自动继续；完成检查或连续没有进展后结束。':'本批结束后会保存结果。';
    }else if(job.status==='failed')reason='本次任务未能完成，请展开运行详情查看错误。';
    else if(job.interrupted)reason='上次运行已中断，可从已保存的进度继续。';
    else if(code==='user_pause')reason='已暂停并保存进度，需要时可继续。';
    else if(code==='no_progress')reason='连续 3 批没有新增内容或解析进展，已自动结束，无需反复点击。';
    else if(code==='pass_finished')reason='本轮可执行检查已完成。未恢复内容可在合集中查看原因。';
    else if(stop?.stop_reason)reason=stop.stop_reason+'；进度已保存，需要时可继续。';
    else if(job.status==='paused')reason='已暂停并保存进度，需要时可继续。';
    else if(job.counts?.posts===0)reason='未发现可恢复的历史帖子，请核对用户名；未找到存档不代表从未发帖。';
    else if(job.status==='partial')reason='本轮已结束，仍有内容未恢复，可在合集中查看原因。';
    else if(job.status==='complete')reason='本轮已完成，已保存的内容可浏览和下载。';
    if(summary)reason=summary.reason_text;
    $('job-reason').textContent=reason;$('job-reason').hidden=!reason;
  }
  function renderQueue(queue,job){
    $('batch-add').hidden=!supportsQueue;
    $('queue-add').disabled=!supportsQueue||actionPending||connectionError;
    const items=queue?.items||[],counts=queue?.counts||{},pending=(counts.pending||0)+(counts.paused||0),active=items.find(i=>i.state==='running');
    $('queue-panel').hidden=!supportsQueue||(!items.length&&!queue?.error);
    const labels={idle:'归档队列',running:active?'队列正在归档':'队列等待当前任务结束',paused:'队列已暂停',finished:'队列已结束'};
    $('queue-title').textContent=labels[queue?.state]||'归档队列';
    if(queue?.state==='running'&&!active&&queue.next_retry_at)$('queue-title').textContent='等待自动重试';
    $('queue-counts').textContent=`共 ${items.length} 个 · 本轮结束 ${counts.finished||0} · 失败 ${counts.failed||0} · 待处理 ${pending}`;
    $('queue-note').textContent=queue?.error||(queue?.state==='running'?(active?`正在处理 ${displayName(active.username)}；保存与打包结束后自动转到下一个。`:'当前任务结束后会自动开始队列。'):queue?.state==='paused'?(queue.reason==='restart'?'队列和设置已保留。点击“继续队列”恢复，不会在启动工具时自动采集。':'队列已暂停，剩余账号保留原顺序；可点击“继续队列”。'):'本轮排队已结束。每个账号的恢复结果可在合集中查看。');
    $('queue-pause').hidden=queue?.state!=='running';
    if(queue?.state==='running'&&!active&&queue.next_retry_at)$('queue-note').textContent=`下次重试：${localTime(queue.next_retry_at)}。工具运行时自动继续；每项最多延后重试 3 轮、最长 48 小时。可暂停或移除。`;
    $('queue-resume').hidden=queue?.state!=='paused'||!pending;
    $('queue-clear').hidden=!(counts.finished||counts.failed);
    $('queue-pause').disabled=actionPending||connectionError;
    $('queue-resume').disabled=actionPending||connectionError||Boolean(job?.pause_requested)||Boolean(queue?.error);
    $('queue-clear').disabled=actionPending||connectionError;
    const list=$('queue-items');list.replaceChildren();
    const states={pending:'等待中',running:'正在归档',paused:'已暂停',finished:'本轮已结束',failed:'失败'};
    items.forEach(item=>{
      const row=document.createElement('li'),text=document.createElement('div'),name=document.createElement('strong'),detail=document.createElement('p');
      name.textContent=displayName(item.username)+' · '+states[item.state];
      detail.className='hint';detail.textContent=[item.outcome?.label,item.outcome?.result_label,item.message].filter(Boolean).join(' · ');
      if(item.next_retry_at&&item.state==='pending')detail.textContent+=' · '+localTime(item.next_retry_at);
      text.append(name,detail);row.append(text);
      if(['pending','paused'].includes(item.state)){
        const remove=document.createElement('button');remove.className='secondary small';remove.type='button';remove.textContent='移除';remove.disabled=actionPending||connectionError;
        remove.setAttribute('aria-label','从队列移除 '+displayName(item.username));
        remove.addEventListener('click',()=>action('/api/queue/remove',{id:item.id}));row.append(remove);
      }
      list.append(row);
    });
  }
  function statusBadge(element,summary){
    const icons={info:'◷',success:'✓',warning:'●',danger:'!',neutral:'—'};
    element.className='task-badge task-'+summary.tone;
    element.textContent=(icons[summary.tone]||'—')+' '+summary.label;
    element.hidden=false;
  }
  function localTime(value){const date=new Date(value);return Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{hour12:false}):value;}
  function aliasText(search={},identity=null){
    const names=identity?linkedNames(identity).map(a=>a.username):Array.isArray(search.verified_aliases)?search.verified_aliases:[],n=names.length;
    const states={running:'正在自动查找旧用户名',pending:'旧用户名搜索进度已保存，仍有线索待查',partial:'旧用户名搜索部分完成',completed:n?'本轮旧用户名搜索完成':'本轮未找到可核实的旧用户名',blocked:search.reason==='uid_conflict'?'用户名归属有冲突，已停止关联':'尚未取得数字账号 ID，旧用户名暂无法核实'};
    let text=states[search.state];
    if(!text){
      if(search.status==='searching')text='旧用户名搜索进度已保存，仍有线索待查';
      else if(search.errors)text='旧用户名搜索部分完成';
      else text={checked:'本轮限定范围旧名搜索完成',not_found:'本轮未找到可核实的旧用户名',no_id:'尚未取得数字账号 ID，旧用户名暂无法核实',conflict:'用户名归属有冲突，已停止关联'}[search.status]||'尚无自动旧名查询记录；下次查找时会自动搜寻';
    }
    if(n)text+=' · 已核实 '+names.map(name=>'@'+name).join('、');
    if(Number.isInteger(search.checked_items))text+=` · 已检查 ${search.checked_items} 项`;
    if(search.pending_items)text+=` · 待查 ${search.pending_items} 项`;
    if(search.failed_items||search.errors)text+=` · 部分来源暂不可用（${search.failed_items||search.errors} 项查询失败）`;
    if(search.next_retry_at)text+=' · '+localTime(search.next_retry_at)+' 后可重试失败来源';
    return text;
  }
  try{preferred=localStorage.getItem('wayback-selected')||'';}catch{}

  async function request(path,body) {
    const cached=body===undefined?responses.get(path):null,generation=cacheGeneration,controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),12000);
    try {
      const r=await fetch(path,body!==undefined?{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(body),signal:controller.signal}:{headers:cached?{'If-None-Match':cached.etag}:{},signal:controller.signal});
      if(r.status===304&&cached)return cached.data;
      const data=await r.json();
      if(r.status===404&&path.startsWith('/api/collection/'))return null;
      if(!r.ok){const failure=new Error(data.error||`请求失败 ${r.status}`);failure.status=r.status;throw failure;}
      const etag=r.headers.get('ETag');if(etag&&body===undefined&&generation===cacheGeneration)responses.set(path,{etag,data});return data;
    } catch(e) {if(e.name==='AbortError')throw new Error('本地服务响应超时，正在等待重新连接');throw e;} finally {clearTimeout(timer);}
  }
  function installToken(value) {
    if(token&&token!==value){responses.clear();cacheGeneration++;loadSequence++;librarySequence++;needsCollections=true;lastUpdate='';}
    token=value;
  }
  async function api(path,body) {
    if(body===undefined)return request(path);
    // A state request started before this action must not overwrite its result.
    stateGeneration++;loadSequence++;librarySequence++;
    try {
      if(!token)installToken((await request('/api/state')).token);
      try{return await request(path,body);}catch(e){
        if(e.status!==403)throw e;
        installToken((await request('/api/state')).token);
        return await request(path,body); // Retry only a rejected token, never an uncertain network result.
      }
    } finally {stateGeneration++;loadSequence++;librarySequence++;needsCollections=true;historyPanel?.invalidate();}
  }
  function error(message) {$('form-error').textContent=message;$('form-error').hidden=!message;}
  function connectionStatus(offline) {
    $('connection-panel').hidden=!offline;
    if(offline){$('activity-track').hidden=true;$('job-live-detail').hidden=true;$('job-status').className='task-badge task-neutral';$('job-status').textContent='— 后台状态暂无法确认';$('queue-title').textContent='队列状态暂无法确认';}
    $('launch-tool').hidden=!supportsDesktopLauncher;
    $('connection-help').textContent=supportsDesktopLauncher?
      '点击“启动时光集”唤起本机工具；浏览器可能询问是否打开应用。页面会自动重连。':
      '请双击桌面的“时光集”快捷方式；尚未安装快捷方式时，可双击工具文件夹内的“启动工具”。页面会自动重连。';
    if(offline)for(const id of actionControls)$(id).disabled=true;
  }
  function choose(handle,automatic=false) {
    selected=handle;selectedPosts=null;selectedCollecting=false;selectedSummary=null;lastUpdate='';loadSequence++;
    if(!automatic||!inputEdited){$('username').value=isInstagram(handle)?handle.slice(11):handle;$('platform').value=isInstagram(handle)?'instagram':'x';platformControls();inputEdited=false;closeAddOnLoad=Boolean(handle);}
    $('more-actions').open=false;
    if(!handle)$('add-account').open=true;
    discoveryControl();
    $('collection').hidden=true;$('welcome').hidden=false;
    if(handle)try{localStorage.setItem('wayback-selected',handle);}catch{}
  }
  function linkedNames(identity) {
    if(identity?.status!=='verified'||!identity.user_id)return [];
    return (identity.aliases||[]).filter(a=>['verified','reused'].includes(a.state)&&/^[a-z0-9_]{1,15}$/.test(a.username));
  }
  function collectionIdentity(identity) {
    const names=linkedNames(identity),element=$('collection-identity');
    element.hidden=!names.length;
    element.textContent=names.length?'同一账号的其他用户名：'+names.map(a=>'@'+a.username+(a.state==='reused'?'（逐条核对作者）':'')).join('、')+'。继续整理会一起检索并复用本地资料。':'';
    const search=identity?.search,note=$('collection-alias-status');
    note.hidden=!identity||isInstagram(selected);
    note.textContent=identity?aliasText(search||{},identity):'';
  }
  async function library() {
    const sequence=++librarySequence,generation=cacheGeneration,items=await api('/api/collections');
    if(sequence!==librarySequence||generation!==cacheGeneration)return null;
    const list=$('library');list.replaceChildren();
    if(!items.length){const p=document.createElement('p');p.className='hint';p.textContent='整理后的合集会保存在这里。';list.append(p);}
    items.forEach(m=>{const b=document.createElement('button');b.className='library-item'+(m.username===selected?' active':'');const title=document.createElement('strong');title.textContent=(isInstagram(m.username)?'':'X ')+displayName(m.username);const count=document.createElement('span');count.textContent=`${m.counts.posts} 条帖子 · ${m.task_summary?m.task_summary.result_label:resultStatus(m)}`;b.append(title,count);if(m.task_summary){const badge=document.createElement('span');statusBadge(badge,m.task_summary);b.append(badge);}const names=linkedNames(m.identity);if(names.length){const note=document.createElement('span');note.className='library-identity';note.textContent='同一账号 · '+names.map(a=>'@'+a.username).join('、');b.append(note);}b.addEventListener('click',()=>select(m.username));list.append(b);});
    return items;
  }
  async function select(handle) {
    choose(handle);
    try{await load();if(selected===handle)await library();}catch(e){if(selected===handle){needsCollections=true;error(e.message);}}
  }
  async function load() {
    if(!selected)return;
    const handle=selected,sequence=++loadSequence,generation=cacheGeneration;
    const m=await api('/api/collection/'+encodeURIComponent(handle)+(supportsPagination?'?view=summary':''));
    if(handle!==selected||sequence!==loadSequence||generation!==cacheGeneration)return;
    if(!m){$('collection').hidden=true;$('welcome').hidden=false;return;}
    selectedPosts=m.counts?.posts;selectedCollecting=Boolean(m.collecting);selectedSummary=m.task_summary||null;const empty=selectedPosts===0;discoveryControl();
    if(closeAddOnLoad){$('add-account').open=false;closeAddOnLoad=false;}
    $('welcome').hidden=true;$('collection').hidden=false;
    $('offline-link').href='/collections/'+handle+'/index.html';
    $('offline-link').textContent=m.collecting?'打开上次保存的离线版':empty?'查看空清单（0 条内容）':'打开离线版';
    $('offline-link').classList.toggle('disabled',m.offline_ready===false);
    $('offline-link').setAttribute('aria-disabled',String(m.offline_ready===false));
    if(m.offline_ready===false){$('offline-link').removeAttribute('href');$('offline-link').textContent=m.collecting?'暂停或结束后生成离线版':'离线版待生成';}
    const zip=$('zip-link');zip.href='/download/'+handle;zip.classList.toggle('disabled',!m.zip_ready);zip.classList.toggle('primary',false);zip.classList.toggle('secondary',true);zip.setAttribute('aria-disabled',String(!m.zip_ready));zip.textContent=m.zip_ready?(empty?'导出空清单 ZIP（0 条内容）':'下载合集 ↓'):m.collecting?(empty?'尚无内容，正在查询':'整理结束后可下载'):(empty?'空清单 ZIP 需要更新':'合集需要更新');
    $('rebuild-zip').hidden=m.zip_ready||m.collecting;
    $('rebuild-zip').textContent=m.zip_on_demand?'生成下载包':empty?'更新空清单 ZIP':'重新生成 ZIP';
    if(m.zip_on_demand&&!m.zip_ready&&!m.collecting)zip.textContent='需要时生成 ZIP';
    if(selectedSummary){
      const preferBrowse=selectedSummary.recommended_action==='browse';
      zip.classList.toggle('primary',preferBrowse&&m.zip_ready);zip.classList.toggle('secondary',!preferBrowse||!m.zip_ready);
    }
    $('collection-subtitle').textContent=`最近整理 · ${m.updated_at.slice(0,16).replace('T',' ')} UTC`;
    collectionIdentity(m.identity);
    const revision=(m.content_revision||m.updated_at)+JSON.stringify(m.task_summary||null);
    if(lastUpdate!==revision){Gallery.show(m,'/collections/'+handle+'/',supportsPagination?async params=>{const page=await api('/api/posts/'+encodeURIComponent(handle)+'?'+new URLSearchParams(params));if(page.stale&&selected===handle){lastUpdate='';await load();}return page;}:null);lastUpdate=revision;}
  }
  async function pollOnce() {
    const generation=stateGeneration;
    try {
      const s=await api('/api/state');
      if(generation!==stateGeneration){pollAgain=true;return;}
      const reconnect=connectionError||token!==s.token;installToken(s.token);const j=s.job,logs=j.logs||[];
      connectionError=false;supportsDesktopLauncher=Boolean(s.capabilities?.includes('desktop_launcher'));
      $('runtime-version').textContent=typeof s.version==='string'&&s.version?`当前版本：${s.version}`:'当前版本：后台未提供';
      $('runtime-folder').textContent=typeof s.data_dir==='string'&&s.data_dir?s.data_dir:'当前后台未提供保存目录；请查看启动日志。';
      supportsMultiSource=Boolean(s.capabilities?.includes('multi_source'));
      supportsAuto=Boolean(s.capabilities?.includes('auto_batches'));supportsDelete=Boolean(s.capabilities?.includes('delete_collection'));jobHandle=j.username||'';
      supportsPagination=Boolean(s.capabilities?.includes('post_pagination'));
      supportsQueue=Boolean(s.capabilities?.includes('task_queue'));renderQueue(s.queue,j);
      if($('platform').querySelector?.('option[value=instagram]')){$('platform').querySelector('option[value=instagram]').disabled=!s.capabilities?.includes('instagram');$('platform').querySelector('option[value=instagram]').textContent='Instagram';}
      $('auto-continue').disabled=!supportsAuto||Boolean(j.running);
      supportsIdentity=Boolean(s.capabilities?.includes('account_identity'));
      supportsDiscovery=Boolean(s.capabilities?.includes('post_discovery'));supportsRepair=Boolean(supportsMultiSource||s.capabilities?.includes('repair')||s.version==='1.1');jobRunning=Boolean(j.running);discoveryControl();
      $('multi-source').disabled=!supportsMultiSource;
      $('start-button').disabled=j.running||actionPending;$('pause-button').hidden=!j.running;$('pause-button').disabled=actionPending||j.pause_requested||(!j.automatic?.enabled&&['zip','export','done'].includes(j.stage));
      $('pause-button').textContent=j.pause_requested?'正在暂停并保存…':'暂停并保存';
      $('repair-button').disabled=j.running||actionPending||!selected||selectedPosts===0||!supportsRepair;
      $('rebuild-zip').disabled=j.running||actionPending;
      $('job-panel').hidden=j.status==='idle';$('job-title').textContent=`${j.username?displayName(j.username)+' · ':''}${j.running?(s.stages[j.stage]||'正在整理'):resultStatus(j)}`;
      sleepStatus(j);taskOutcome(j);
      $('job-detail').textContent=logs.length?logs[logs.length-1].text:j.status==='paused'?'上次整理已暂停，选择对应合集后可继续补全。':'准备检索存档…';
      const live=$('job-live-detail'),page=j.page_progress;
      live.hidden=!j.running&&!j.retry_policy?.waiting&&!j.backup?.state;
      const elapsedStage=Math.max(0,Math.floor((Date.now()-Date.parse(j.stage_started_at))/1000)||0);
      const stageTime=j.running&&j.stage_started_at?`当前阶段 ${Math.floor(elapsedStage/60)}分${elapsedStage%60}秒` : '';
      const liveParts=[];
      if(j.stage==='pages'&&page&&Number.isInteger(page.total))liveParts.push(`累计已解析 ${page.parsed||0} / ${page.total} 份快照，本批已检查 ${page.batch_checked||0} 份`);
      if(stageTime)liveParts.push(stageTime);
      if(j.running&&logs.length)liveParts.push(logs[logs.length-1].text);
      if(j.running&&s.health?.message)liveParts.push(s.health.message);
      if(j.task_summary?.reason==='retry_limit')liveParts.push(j.task_summary.reason_text);
      else if(j.retry_policy?.waiting){
        const due=Date.parse(j.retry_policy.next_retry_at);
        liveParts.push(Number.isFinite(due)&&due<=Date.now()
          ?'已到达上次记录的最早可重试时间；是否正在执行请以任务和队列状态为准。'
          :`上次检查有 ${j.retry_policy.waiting} 项请求或来源处于冷却期，最早可重试：${Number.isFinite(due)?new Date(due).toLocaleString():'稍后'}`);
      }
      if(j.backup?.state==='verified')liveParts.push(`备份已校验：${j.backup.files} 个文件`);
      if(j.backup?.state==='failed')liveParts.push(`备份失败，后续队列已暂停：${j.backup.error}`);
      live.textContent=liveParts.join(' · ');
      $('reliability-options').hidden=!s.reliability;
      storagePanel?.update(s.storage);
      const nextStorageRevision=JSON.stringify([s.storage?.settings?.zip_on_demand,s.storage?.finished_at]);
      if(nextStorageRevision!==storageRevision){if(storageRevision)needsCollections=true;storageRevision=nextStorageRevision;}
      if(s.reliability&&!actionPending){$('backup-after-task').checked=s.reliability.backup_after_task;$('resume-after-crash').checked=s.reliability.resume_queue_after_crash;}
      $('guard-status').textContent=s.guard?.state==='watching'?'异常退出保护已启用；后台进程存活时不会强制重启。':s.guard?.state==='attention'?s.guard.message:'当前未确认守护运行；双击桌面快捷方式可启用。';
      const cloud=s.cloud_backup;
      $('cloud-backup-options').hidden=!cloud;
      if(cloud){
        if(!cloudFormReady){$('cloud-backup-enabled').checked=cloud.enabled;$('cloud-backup-path').value=cloud.destination||'';cloudFormReady=true;}
        const cloudState=!cloud.enabled?'已关闭':({copying:'正在复制',retrying:'复制受阻，等待重试',submitted:'已复制并校验',waiting:'等待本地备份'}[cloud.state]||'等待检查');
        $('cloud-backup-status').textContent=[cloudState,cloud.enabled&&Number.isFinite(cloud.total)?`本地备份 ${cloud.total} 份 · 已交付 ${cloud.submitted||0} 份 · 待复制 ${cloud.pending||0} 份`:'',cloud.handle?`当前 @${cloud.handle} · 文件 ${cloud.file||0}/${cloud.total_files||0}`:'',cloud.message||'',cloud.enabled&&cloud.state==='retrying'&&cloud.next_retry_at?`下次重试 ${new Date(cloud.next_retry_at*1000).toLocaleTimeString()}`:''].filter(Boolean).join(' · ');
      }
      const batch=j.batch,details=[];
      if(j.total&&['identity','local_aliases','pages','content','media','sources','done'].includes(j.stage))details.push(`当前步骤已处理：${Math.min(j.completed||0,j.total)} / ${j.total}`);
      if(batch?.max_seconds){
        const active=j.running&&!['export','zip','done','between_batches'].includes(j.stage);
        const elapsed=active?Math.max(batch.elapsed_seconds||0,(Date.now()-Date.parse(j.batch_started_at||j.started_at))/1000||0):batch.elapsed_seconds||0;
        details.push(`本批新增 ${batch.new_files||0} 个文件`, `请求 ${batch.requests||0} / ${batch.max_requests}`, `已检索 ${Math.floor(elapsed/60)}分${Math.floor(elapsed%60)}秒`);
        if(active)details.push(`检索剩余至多 ${Math.ceil(Math.max(0,batch.max_seconds-elapsed))} 秒`);
        if(batch.stop_reason)details.push(batch.stop_reason);
      }
      if(j.automatic?.enabled){
        details.unshift(`自动处理 · ${j.running&&j.automatic.current_batch?'第 '+j.automatic.current_batch+' 批':'已处理 '+j.automatic.batches+' 批'} · 累计新增 ${j.automatic.new_files||0} 个文件`);
        if(Number.isFinite(j.automatic.new_text))details.push(`累计补回 ${j.automatic.new_text} 条正文 · 新增 ${j.automatic.new_posts||0} 条帖子 · 已结束批次请求 ${j.automatic.requests||0} 次`);
        if(j.task_summary?.reason==='retry_limit')details.push(j.task_summary.reason_text);
        else if(j.automatic.stop_reason)details.push(j.automatic.stop_reason);
      }
      $('job-progress').textContent=details.join(' · ');
      const spent=Object.entries(j.stage_seconds||{}).filter(([,seconds])=>Number.isFinite(seconds)&&seconds>=1).sort((a,b)=>b[1]-a[1]);
      if(spent.length)$('job-progress').textContent+=' · 已结束阶段累计用时：'+spent.map(([stage,seconds])=>`${s.stages[stage]||stage} ${Math.round(seconds)}秒`).join('、');
      const total=Number(j.total),completed=Number(j.completed||0);
      const knownProgress=j.running&&['identity','local_aliases','pages','content','media','sources'].includes(j.stage)&&Number.isFinite(total)&&total>0&&Number.isFinite(completed);
      $('activity-track').hidden=!j.running;
      $('activity').setAttribute('max','100');
      $('activity').classList.toggle('moving',j.running&&!knownProgress);
      if(knownProgress)$('activity').setAttribute('value',String(Math.max(0,Math.min(100,completed/total*100))));
      else $('activity').removeAttribute('value');
      $('job-logs').replaceChildren(...logs.slice(-60).map(log=>{const li=document.createElement('li');li.textContent=log.time.slice(11,19)+' '+log.text;return li;}));
      if(j.running&&!selected)choose(j.username,true);
      if(needsCollections||!initialized||reconnect||(j.running&&tick%3===0)||(wasRunning&&!j.running)) {
        needsCollections=true;
        const items=await library();
        if(generation!==stateGeneration){pollAgain=true;return;}
        if(!items)return;
        if(items.length&&(!selected||(!initialized&&!j.running&&!items.some(m=>m.username===selected)))){
          choose(items.find(m=>m.username===(selected||(j.running?j.username:preferred||j.username)))?.username||items[0].username,true);
          await library();
        }
        if(selected)await load();
        if(generation!==stateGeneration){pollAgain=true;return;}
        needsCollections=false;initialized=true;
      }
      if(reconnect)error('');connectionStatus(false);
      await historyPanel?.update(s);wasRunning=j.running;tick++;
    } catch(e) {historyPanel?.update(null,true);needsCollections=true;connectionError=true;connectionStatus(true);if(!$('job-sleep').hidden)$('job-sleep').textContent='防休眠状态暂无法确认，请保持电脑唤醒。';error('连接本地工具失败：'+e.message+'。请使用页面上方的连接提示；页面会自动重连。');}
  }
  function poll(force=false) {
    if(pollTask){if(force)pollAgain=true;return pollTask;}
    pollTask=(async()=>{do{pollAgain=false;await pollOnce();}while(pollAgain);})().finally(()=>{pollTask=null;});
    return pollTask;
  }
  async function action(path,body,after) {
    if(actionPending)return;
    if(connectionError)return poll(true);
    $('more-actions').open=false;
    actionPending=true;error('');for(const id of actionControls)$(id).disabled=true;
    try{const result=await api(path,body);if(after)after(result);}catch(e){error(e.message);}finally{actionPending=false;await poll(true);}
  }
  function sourceOptions(enabled=$('multi-source').checked) {
    return {...(supportsAuto?{auto_continue:$('auto-continue').checked,auto_max_minutes:Number($('auto-max-minutes').value||0)}:{}),max_minutes:Number($('max-minutes').value||10),multi_source:supportsMultiSource&&enabled,common_crawl:$('common-crawl').checked,public_mirrors:$('public-mirrors').checked,cc_indexes:Number($('cc-indexes').value||6)};
  }
  function fullOptions(username){return {username,mode:'full',proxy:$('proxy').value.trim(),max_pages:Number($('max-pages').value),max_media_mb:Number($('max-media-mb').value),force_recheck:$('force-recheck').checked,...sourceOptions()};}
  $('queue-form').addEventListener('submit',e=>{
    e.preventDefault();if(!supportsQueue)return;
    const text=$('queue-input').value.trim();if(!text){error('请先填写要排队的用户名。');return;}
    const options={...fullOptions(''),platform:inputPlatform()},signature=JSON.stringify([text,options]);
    if(queueSubmission?.signature!==signature)queueSubmission={signature,id:Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
    return action('/api/queue/add',{...options,usernames:text,request_id:queueSubmission.id},result=>{
      if($('queue-input').value.trim()===text)$('queue-input').value='';
      queueSubmission=null;
      $('queue-message').textContent=`已加入 ${result.added||0} 个账号${result.duplicates?'，跳过 '+result.duplicates+' 个重复 ID':''}。`;
      $('queue-message').hidden=false;
    });
  });
  $('queue-pause').addEventListener('click',()=>action('/api/queue/pause',{}));
  $('cloud-backup-save').addEventListener('click',()=>action('/api/cloud-backup',{enabled:$('cloud-backup-enabled').checked,destination:$('cloud-backup-path').value},()=>{cloudFormReady=false;}));
  for(const [id,key] of [['backup-after-task','backup_after_task'],['resume-after-crash','resume_queue_after_crash']]){
    $(id).addEventListener('change',()=>action('/api/reliability',{[key]:$(id).checked}));
  }
  $('queue-resume').addEventListener('click',()=>action('/api/queue/resume',{}));
  $('queue-clear').addEventListener('click',()=>action('/api/queue/clear-finished',{}));
  $('collect-form').addEventListener('submit',e=>{e.preventDefault();if(jobRunning)return;return action('/api/start',{...fullOptions($('username').value),platform:inputPlatform(),instagram_links:inputPlatform()==='instagram'?$('instagram-links').value:''},()=>choose(''));});
  $('continue-button').addEventListener('click',()=>{if(jobRunning||!selected||selectedCollecting||selectedPosts===null)return;return action('/api/start',fullOptions(selected));});
  $('platform').addEventListener('change',()=>{inputEdited=true;closeAddOnLoad=false;platformControls();});
  platformControls();
  $('username').addEventListener('input',()=>{inputEdited=true;closeAddOnLoad=false;discoveryControl();});
  $('add-account').addEventListener('toggle',()=>{if(selected&&$('add-account').open&&!actionPending&&$('username').value===(isInstagram(selected)?selected.slice(11):selected)){$('username').value='';inputEdited=true;discoveryControl();}});
  $('auto-continue').addEventListener('change',discoveryControl);
  $('discover-input-button').addEventListener('click',()=>{const username=$('username').value.trim();if(!supportsDiscovery||jobRunning||!username)return;return action('/api/start',{username,platform:inputPlatform(),instagram_links:inputPlatform()==='instagram'?$('instagram-links').value:'',mode:'discover',proxy:$('proxy').value.trim(),max_pages:Number($('max-pages').value),max_media_mb:Number($('max-media-mb').value),force_recheck:$('force-recheck').checked,...sourceOptions(false)},()=>choose(''));});
  $('pause-button').addEventListener('click',()=>action('/api/pause',{}));
  $('repair-button').addEventListener('click',()=>{if(jobRunning||!selected||selectedPosts===0||!supportsRepair)return;return action('/api/start',{username:selected,mode:'repair',proxy:$('proxy').value.trim(),max_media_mb:Number($('max-media-mb').value),force_recheck:$('force-recheck').checked,...sourceOptions()});});
  $('discover-button').addEventListener('click',()=>{if(jobRunning||!supportsDiscovery||!selected)return;return action('/api/start',{username:selected,mode:'discover',proxy:$('proxy').value.trim(),max_pages:Number($('max-pages').value),max_media_mb:Number($('max-media-mb').value),force_recheck:$('force-recheck').checked,...sourceOptions(false)});});
  $('rebuild-zip').addEventListener('click',()=>action('/api/zip',{username:selected}));
  $('job-browse').addEventListener('click',async()=>{if(!jobHandle)return;await select(jobHandle);$('collection').scrollIntoView?.({behavior:'smooth',block:'start'});});
  $('job-resume').addEventListener('click',()=>{if(jobRunning||!jobHandle)return;return action('/api/start',fullOptions(jobHandle),()=>choose(jobHandle));});
  $('job-rebuild').addEventListener('click',()=>{if(jobRunning||!jobHandle)return;return action('/api/zip',{username:jobHandle},()=>choose(jobHandle));});
  async function loadIdentity() {
    const target=identityTarget;
    const info=await api('/api/identity/'+encodeURIComponent(target));
    if(target!==identityTarget||!$('identity-dialog').open)return;
    $('identity-title').textContent='@'+target+' 的历史用户名';
    $('identity-summary').textContent=info.status==='verified'?`数字账号 ID：${info.user_id}`:
      info.status==='conflict'?'这个用户名出现过多个数字账号 ID，暂不自动关联其他名字。':'尚未找到这个账号的数字 ID；可先检查已保存快照，或继续整理新快照。';
    const search=info.search||{},searchNote=$('identity-search-status');
    const searchLabels={searching:'旧用户名搜索进度已保存，仍有线索待查；继续任务时接着检查。',checked:'已完成本轮限定范围搜索；核实的名字会一起整理。',not_found:'本轮来源未提供可核实的其他用户名，继续整理新快照时仍会核对线索。',no_id:'本轮尚未查到数字账号 ID，无法可靠关联旧名。',conflict:'存在账号归属冲突，已停止自动关联。'};
    searchNote.textContent=search.state||search.errors?aliasText(search,info):(searchLabels[search.status]||'开始或继续查找与下载时，会自动搜索并核对历史用户名，无需手动填写。');
    const list=$('identity-list');list.replaceChildren();
    const labels={verified:'已核对 · 随整理一起检索',reused:'曾被其他账号使用 · 逐条核对作者，只归并属于此账号的帖子',candidate:'待核对 · 查到同一账号的证据后才归并',conflict:'归属冲突 · 暂不归并',excluded:'已停止检索'};
    for(const alias of info.aliases||[]){
      const row=document.createElement('div');row.className='identity-row';
      const title=document.createElement('strong');title.textContent='@'+alias.username;
      const note=document.createElement('p');note.className='hint';note.textContent=labels[alias.state]||'待核对';
      row.append(title,note);
      for(const proof of (alias.evidence||[]).slice(0,2)){
        if(proof.kind==='current_profile'&&!/^https:\/\/api\.fxtwitter\.com\/2\/profile\/(?:[A-Za-z0-9_]{1,15}|id:[0-9]{1,20})$/.test(proof.source||''))continue;
        const link=document.createElement('a');link.target='_blank';link.rel='noopener';link.className='identity-proof';
        link.textContent=proof.kind==='current_profile'?`查看当前公开资料 · ID ${proof.uid}`:`查看 ${proof.timestamp||'未知日期'} 快照证据 · ID ${proof.uid}`;
        link.href=proof.kind==='current_profile'?proof.source:'https://web.archive.org/web/'+encodeURIComponent(proof.timestamp||'*')+'/'+proof.source;
        row.append(link);
      }
      const button=document.createElement('button');button.type='button';button.className='secondary small';
      button.textContent=alias.state==='excluded'?'重新核对':'停止检索此名';button.disabled=jobRunning||identityBusy||connectionError;
      button.addEventListener('click',()=>changeIdentity(alias.username,alias.state==='excluded'?'candidate':'excluded'));
      row.append(button);list.append(row);
    }
    if(!(info.aliases||[]).length){const p=document.createElement('p');p.textContent='暂未发现有证据关联的历史用户名。';list.append(p);}
    discoveryControl();
  }
  async function changeIdentity(alias,state) {
    if(identityBusy||jobRunning||connectionError)return;
    identityBusy=true;discoveryControl();
    const target=identityTarget;
    try{
      await api('/api/identity',{username:target,alias,state});
      if(identityTarget!==target)return;
      $('identity-input').value='';
      $('identity-message').textContent=state==='excluded'?'已停止检索此名，已保存内容仍保留。':'已加入待核对名字；下次继续整理时会查找归属证据。';
      await loadIdentity();
    }catch(e){$('identity-message').textContent=e.message;}finally{identityBusy=false;try{await loadIdentity();}catch(e){$('identity-message').textContent=e.message;}}
  }
  $('identity-button').addEventListener('click',async()=>{
    if(!supportsIdentity||!selected||actionPending)return;
    identityTarget=selected;$('more-actions').open=false;$('identity-message').textContent='';
    $('identity-list').replaceChildren();$('identity-summary').textContent='正在读取…';$('identity-dialog').showModal();
    try{await loadIdentity();}catch(e){$('identity-summary').textContent=e.message;}
  });
  $('identity-close').addEventListener('click',()=>{$('identity-dialog').close();identityTarget='';});
  $('identity-form').addEventListener('submit',e=>{e.preventDefault();return changeIdentity($('identity-input').value.trim(),'candidate');});
  $('identity-scan').addEventListener('click',()=>{
    if(jobRunning||actionPending||!identityTarget)return;
    const target=identityTarget;$('identity-dialog').close();identityTarget='';
    return action('/api/start',{username:target,mode:'identity',max_minutes:10,auto_continue:false});
  });
  $('delete-button').addEventListener('click',()=>{
    if(!supportsDelete||!selected||actionPending||selectedCollecting||(jobRunning&&jobHandle===selected))return;
    $('more-actions').open=false;
    deleteTarget=selected;
    $('delete-title').textContent='删除 '+displayName(deleteTarget)+' 的本地合集？';
    $('delete-description').textContent=`将删除 ${displayName(deleteTarget)} 的 ${selectedPosts||0} 条帖子及其本地文件。`;
    $('delete-confirm').textContent='确认删除 @'+deleteTarget;
    $('delete-error').hidden=true;$('delete-dialog').showModal();
  });
  $('delete-cancel').addEventListener('click',()=>{if(!deleting)$('delete-dialog').close();});
  $('delete-dialog').addEventListener('cancel',event=>{if(deleting)event.preventDefault();});
  $('delete-confirm').addEventListener('click',async()=>{
    if(deleting||!deleteTarget)return;
    const target=deleteTarget;deleting=true;actionPending=true;
    $('delete-confirm').disabled=true;$('delete-cancel').disabled=true;$('delete-error').hidden=true;
    try{
      await api('/api/delete',{username:target,confirm_username:target});
      responses.clear();cacheGeneration++;needsCollections=true;initialized=false;preferred='';
      try{localStorage.removeItem('wayback-selected');}catch{}
      if(selected===target)choose('');
      $('delete-dialog').close();deleteTarget='';
    }catch(e){$('delete-error').textContent=e.message;$('delete-error').hidden=false;}
    finally{deleting=false;actionPending=false;$('delete-confirm').disabled=false;$('delete-cancel').disabled=false;await poll(true);}
  });
  $('refresh-library').addEventListener('click',()=>{needsCollections=true;return poll(true);});
  $('reconnect-tool').addEventListener('click',async()=>{
    $('reconnect-tool').disabled=true;
    try{await poll(true);}finally{$('reconnect-tool').disabled=false;}
  });
  $('launch-tool').addEventListener('click',event=>{
    if(!supportsDesktopLauncher){event.preventDefault();return;}
    $('connection-help').textContent='请在浏览器提示中允许打开时光集。正在等待本机工具连接；若没有响应，可双击桌面的“时光集”快捷方式。';
  });
  $('zip-link').addEventListener('click',e=>{if(e.currentTarget.getAttribute('aria-disabled')==='true')e.preventDefault();});
  // Install the retry loop before the first network request; startup failure remains recoverable.
  setInterval(()=>poll(),1500);
  poll();
})();
