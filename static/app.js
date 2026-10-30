/* Page controller: state polling, actions, forms and settings; loaded after the panel scripts. */
(() => {
  'use strict';
  const App=window.ShiguangjiApp;
  const {inputPlatform,$,api,state,isInstagram,displayName,actionControls,installToken,error}=App;
  function platformControls(){
    const ig=inputPlatform()==='instagram';
    $('username-label').textContent=ig?'Instagram 用户名':'X / Twitter 用户名';
    $('platform-hint').textContent=ig?
      '恢复公开历史快照中可核实的帖子、图片与视频。已删除内容只有留下可访问存档才可能找回；暂不自动关联旧用户名。':
      '填写 @ 后的用户名。自动搜寻并核对旧用户名，再查找帖子与媒体；核实的旧名会一起整理。';
    $('instagram-seeds').hidden=!ig;
    $('queue-input-label').textContent=ig?'多个 Instagram 用户名':'多个 X / Twitter 用户名';
    $('queue-input').placeholder=ig?'@alice\n@bob.name\nhttps://www.instagram.com/charlie/':'@alice\n@bob\nhttps://x.com/charlie';
  }
  const storagePanel=typeof StoragePanel==='undefined'?null:StoragePanel.create({
    api,
    getSelected:()=>state.selected,
    isBusy:()=>state.jobRunning||state.actionPending,
    refresh:()=>poll(true),
  });
  const historyPanel=typeof HistoryPanel==='undefined'?null:HistoryPanel.create({
    api,
    select:App.select,
    options:()=>fullOptions(''),
    retry:async(id,options)=>{
      if(state.actionPending||state.jobRunning||state.connectionError)throw new Error('请先等待当前任务完成或暂停保存。');
      state.actionPending=true;discoveryControl();
      try{const result=await api('/api/history/retry',{...options,id});App.choose(result.username);}
      finally{state.actionPending=false;await poll(true);}
    },
  });
  function discoveryControl() {
    $('identity-button').hidden=!state.supportsIdentity||isInstagram(state.selected);
    $('identity-button').disabled=!state.selected||state.actionPending||state.connectionError;
    $('identity-scan').disabled=state.jobRunning||state.actionPending||state.identityBusy||state.connectionError;
    $('identity-add').disabled=state.jobRunning||state.actionPending||state.identityBusy||state.connectionError;
    $('discover-input-button').hidden=!state.supportsDiscovery;
    $('discover-input-note').hidden=!state.supportsDiscovery;
    $('discover-input-button').disabled=state.jobRunning||state.actionPending||!state.supportsDiscovery||
      !$('username').value.trim();
    $('discover-button').hidden=!state.supportsDiscovery;
    $('discover-button').disabled=state.jobRunning||state.actionPending||!state.selected||!state.supportsDiscovery;
    $('continue-button').disabled=state.jobRunning||state.actionPending||!state.selected||
      state.selectedCollecting||state.selectedPosts===null;
    $('continue-button').textContent=state.jobRunning?
      (state.jobHandle===state.selected?'正在处理此博主…':`正在处理 ${displayName(state.jobHandle||'其他博主')}`):
      state.selectedCollecting?'该博主正在处理中…':'继续查找与下载';
    if(!state.jobRunning&&!state.selectedCollecting&&state.selectedSummary){
      $('continue-button').textContent=['paused','interrupted'].includes(state.selectedSummary.state)?
        '继续任务':'重新查找与下载';
      $('continue-button').classList.toggle('primary',state.selectedSummary.recommended_action==='continue');
      $('continue-button').classList.toggle('secondary',state.selectedSummary.recommended_action!=='continue');
    }
    $('repair-button').disabled=state.jobRunning||state.actionPending||!state.selected||
      state.selectedPosts===0||!state.supportsRepair;
    $('repair-button').textContent='仅下载已知媒体';
    $('delete-button').hidden=!state.supportsDelete;
    const busy=state.selectedCollecting||(state.jobRunning&&state.jobHandle===state.selected);
    $('delete-button').disabled=state.actionPending||!state.selected||busy;
    $('delete-note').hidden=!state.supportsDelete||!busy;
    if(state.connectionError)for(const id of actionControls)$(id).disabled=true;
  }
  function connectionStatus(offline) {
    $('connection-panel').hidden=!offline;
    if(offline){
      $('activity-track').hidden=true;
      $('job-live-detail').hidden=true;
      $('job-status').className='task-badge task-neutral';
      $('job-status').textContent='— 后台状态暂无法确认';
      $('queue-title').textContent='队列状态暂无法确认';
    }
    $('launch-tool').hidden=!state.supportsDesktopLauncher;
    $('connection-help').textContent=state.supportsDesktopLauncher?
      '点击“启动时光集”唤起本机工具；浏览器可能询问是否打开应用。页面会自动重连。':
      '请双击桌面的“时光集”快捷方式；尚未安装快捷方式时，可双击工具文件夹内的“启动工具”。页面会自动重连。';
    if(offline)for(const id of actionControls)$(id).disabled=true;
  }
  function renderSettings(s){
    $('reliability-options').hidden=!s.reliability;
    storagePanel?.update(s.storage);
    const nextStorageRevision=JSON.stringify([s.storage?.settings?.zip_on_demand,s.storage?.finished_at]);
    if(nextStorageRevision!==state.storageRevision){
      if(state.storageRevision)state.needsCollections=true;
      state.storageRevision=nextStorageRevision;
    }
    if(s.reliability&&!state.actionPending){
      $('backup-after-task').checked=s.reliability.backup_after_task;
      $('resume-after-crash').checked=s.reliability.resume_queue_after_crash;
    }
    $('guard-status').textContent=s.guard?.state==='watching'?'异常退出保护已启用；后台进程存活时不会强制重启。':
      s.guard?.state==='attention'?s.guard.message:
      '当前未确认守护运行；双击桌面快捷方式可启用。';
    const cloud=s.cloud_backup;
    $('cloud-backup-options').hidden=!cloud;
    if(cloud){
      if(!state.cloudFormReady){
        $('cloud-backup-enabled').checked=cloud.enabled;
        $('cloud-backup-path').value=cloud.destination||'';
        state.cloudFormReady=true;
      }
      const cloudState=!cloud.enabled?'已关闭':({
        copying:'正在复制',
        retrying:'复制受阻，等待重试',
        submitted:'已复制并校验',
        waiting:'等待本地备份',
      }[cloud.state]||'等待检查');
      $('cloud-backup-status').textContent=[cloudState,
        cloud.enabled&&Number.isFinite(cloud.total)?`本地备份 ${cloud.total} 份 · 已交付 ${cloud.submitted||0} 份 · 待复制 ${cloud.pending||0} 份`:'',
        cloud.handle?`当前 @${cloud.handle} · 文件 ${cloud.file||0}/${cloud.total_files||0}`:'',
        cloud.message||'',
        cloud.enabled&&cloud.state==='retrying'&&cloud.next_retry_at?
          `下次重试 ${new Date(cloud.next_retry_at*1000).toLocaleTimeString()}`:''
      ].filter(Boolean).join(' · ');
    }
  }
  async function pollOnce() {
    const generation=state.stateGeneration;
    try {
      const s=await api('/api/state');
      if(generation!==state.stateGeneration){state.pollAgain=true;return;}
      const reconnect=state.connectionError||state.token!==s.token;installToken(s.token);const j=s.job,logs=j.logs||[];
      state.connectionError=false;state.supportsDesktopLauncher=Boolean(s.capabilities?.includes('desktop_launcher'));
      $('runtime-version').textContent=typeof s.version==='string'&&s.version?`当前版本：${s.version}`:
        '当前版本：后台未提供';
      $('runtime-folder').textContent=typeof s.data_dir==='string'&&s.data_dir?s.data_dir:
        '当前后台未提供保存目录；请查看启动日志。';
      state.supportsMultiSource=Boolean(s.capabilities?.includes('multi_source'));
      state.supportsAuto=Boolean(s.capabilities?.includes('auto_batches'));
      state.supportsDelete=Boolean(s.capabilities?.includes('delete_collection'));state.jobHandle=j.username||'';
      state.supportsPagination=Boolean(s.capabilities?.includes('post_pagination'));
      state.supportsQueue=Boolean(s.capabilities?.includes('task_queue'));App.renderQueue(s.queue,j);
      if($('platform').querySelector?.('option[value=instagram]')){
        $('platform').querySelector('option[value=instagram]').disabled=!s.capabilities?.includes('instagram');
        $('platform').querySelector('option[value=instagram]').textContent='Instagram';
      }
      $('auto-continue').disabled=!state.supportsAuto||Boolean(j.running);
      state.supportsIdentity=Boolean(s.capabilities?.includes('account_identity'));
      state.supportsDiscovery=Boolean(s.capabilities?.includes('post_discovery'));
      state.supportsRepair=Boolean(state.supportsMultiSource||s.capabilities?.includes('repair')||s.version==='1.1');
      state.jobRunning=Boolean(j.running);discoveryControl();
      $('multi-source').disabled=!state.supportsMultiSource;
      App.renderJobStatus(s,j,logs);
      renderSettings(s);
      App.renderJobProgress(s,j,logs);
      if(j.running&&!state.selected)App.choose(j.username,true);
      if(state.needsCollections||!state.initialized||reconnect||(j.running&&state.tick%3===0)||(state.wasRunning&&!j.running)) {
        state.needsCollections=true;
        const items=await App.library();
        if(generation!==state.stateGeneration){state.pollAgain=true;return;}
        if(!items)return;
        if(items.length&&(!state.selected||(!state.initialized&&!j.running&&!items.some(m=>m.username===state.selected)))){
          App.choose(items.find(m=>m.username===(state.selected||(j.running?j.username:state.preferred||j.username)))?.username||
            items[0].username,true);
          await App.library();
        }
        if(state.selected)await App.load();
        if(generation!==state.stateGeneration){state.pollAgain=true;return;}
        state.needsCollections=false;state.initialized=true;
      }
      if(reconnect)error('');connectionStatus(false);
      await historyPanel?.update(s);state.wasRunning=j.running;state.tick++;
    } catch(e) {
      historyPanel?.update(null,true);
      state.needsCollections=true;
      state.connectionError=true;
      connectionStatus(true);
      if(!$('job-sleep').hidden)$('job-sleep').textContent='防休眠状态暂无法确认，请保持电脑唤醒。';
      error('连接本地工具失败：'+e.message+'。请使用页面上方的连接提示；页面会自动重连。');
    }
  }
  function poll(force=false) {
    if(state.pollTask){if(force)state.pollAgain=true;return state.pollTask;}
    state.pollTask=(async()=>{
      do{state.pollAgain=false;await pollOnce();}while(state.pollAgain);
    })().finally(()=>{state.pollTask=null;});
    return state.pollTask;
  }
  async function action(path,body,after) {
    if(state.actionPending)return;
    if(state.connectionError)return poll(true);
    $('more-actions').open=false;
    state.actionPending=true;error('');for(const id of actionControls)$(id).disabled=true;
    try{
      const result=await api(path,body);
      if(after)after(result);
    }catch(e){error(e.message);}finally{state.actionPending=false;await poll(true);}
  }
  function sourceOptions(enabled=$('multi-source').checked) {
    return {
      ...(state.supportsAuto?{
        auto_continue:$('auto-continue').checked,
        auto_max_minutes:Number($('auto-max-minutes').value||0),
      }:{}),
      max_minutes:Number($('max-minutes').value||10),
      multi_source:state.supportsMultiSource&&enabled,
      common_crawl:$('common-crawl').checked,
      public_mirrors:$('public-mirrors').checked,
      cc_indexes:Number($('cc-indexes').value||6),
    };
  }
  function fullOptions(username){
    return {
      username,
      mode:'full',
      proxy:$('proxy').value.trim(),
      max_pages:Number($('max-pages').value),
      max_media_mb:Number($('max-media-mb').value),
      force_recheck:$('force-recheck').checked,
      ...sourceOptions(),
    };
  }
  Object.assign(App,{action,historyPanel,platformControls,discoveryControl,poll});
  $('queue-form').addEventListener('submit',e=>{
    e.preventDefault();if(!state.supportsQueue)return;
    const text=$('queue-input').value.trim();if(!text){error('请先填写要排队的用户名。');return;}
    const options={...fullOptions(''),platform:inputPlatform()},signature=JSON.stringify([text,options]);
    if(state.queueSubmission?.signature!==signature)state.queueSubmission={
      signature,
      id:Date.now().toString(36)+'-'+Math.random().toString(36).slice(2),
    };
    return action('/api/queue/add',{...options,usernames:text,request_id:state.queueSubmission.id},result=>{
      if($('queue-input').value.trim()===text)$('queue-input').value='';
      state.queueSubmission=null;
      $('queue-message').textContent=
        `已加入 ${result.added||0} 个账号${result.duplicates?'，跳过 '+result.duplicates+' 个重复 ID':''}。`;
      $('queue-message').hidden=false;
    });
  });
  $('queue-pause').addEventListener('click',()=>action('/api/queue/pause',{}));
  $('cloud-backup-save').addEventListener('click',()=>action('/api/cloud-backup',{
    enabled:$('cloud-backup-enabled').checked,
    destination:$('cloud-backup-path').value,
  },()=>{
    state.cloudFormReady=false;
  }));
  for(const [id,key] of [['backup-after-task','backup_after_task'],['resume-after-crash','resume_queue_after_crash']]){
    $(id).addEventListener('change',()=>action('/api/reliability',{[key]:$(id).checked}));
  }
  $('queue-resume').addEventListener('click',()=>action('/api/queue/resume',{}));
  $('queue-clear').addEventListener('click',()=>action('/api/queue/clear-finished',{}));
  $('collect-form').addEventListener('submit',e=>{
    e.preventDefault();
    if(state.jobRunning)return;
    return action('/api/start',{
      ...fullOptions($('username').value),
      platform:inputPlatform(),
      instagram_links:inputPlatform()==='instagram'?$('instagram-links').value:'',
    },()=>App.choose(''));
  });
  $('continue-button').addEventListener('click',()=>{
    if(state.jobRunning||!state.selected||state.selectedCollecting||state.selectedPosts===null)return;
    return action('/api/start',fullOptions(state.selected));
  });
  $('platform').addEventListener('change',()=>{state.inputEdited=true;state.closeAddOnLoad=false;platformControls();});
  platformControls();
  $('username').addEventListener('input',()=>{state.inputEdited=true;state.closeAddOnLoad=false;discoveryControl();});
  $('add-account').addEventListener('toggle',()=>{
    if(state.selected&&$('add-account').open&&!state.actionPending&&
      $('username').value===(isInstagram(state.selected)?state.selected.slice(11):state.selected)){
      $('username').value='';
      state.inputEdited=true;
      discoveryControl();
    }
  });
  $('auto-continue').addEventListener('change',discoveryControl);
  $('discover-input-button').addEventListener('click',()=>{
    const username=$('username').value.trim();
    if(!state.supportsDiscovery||state.jobRunning||!username)return;
    return action('/api/start',{
      username,
      platform:inputPlatform(),
      instagram_links:inputPlatform()==='instagram'?$('instagram-links').value:'',
      mode:'discover',
      proxy:$('proxy').value.trim(),
      max_pages:Number($('max-pages').value),
      max_media_mb:Number($('max-media-mb').value),
      force_recheck:$('force-recheck').checked,
      ...sourceOptions(false),
    },()=>App.choose(''));
  });
  $('pause-button').addEventListener('click',()=>action('/api/pause',{}));
  $('repair-button').addEventListener('click',()=>{
    if(state.jobRunning||!state.selected||state.selectedPosts===0||!state.supportsRepair)return;
    return action('/api/start',{
      username:state.selected,
      mode:'repair',
      proxy:$('proxy').value.trim(),
      max_media_mb:Number($('max-media-mb').value),
      force_recheck:$('force-recheck').checked,
      ...sourceOptions(),
    });
  });
  $('discover-button').addEventListener('click',()=>{
    if(state.jobRunning||!state.supportsDiscovery||!state.selected)return;
    return action('/api/start',{
      username:state.selected,
      mode:'discover',
      proxy:$('proxy').value.trim(),
      max_pages:Number($('max-pages').value),
      max_media_mb:Number($('max-media-mb').value),
      force_recheck:$('force-recheck').checked,
      ...sourceOptions(false),
    });
  });
  $('rebuild-zip').addEventListener('click',()=>action('/api/zip',{username:state.selected}));
  $('job-browse').addEventListener('click',async()=>{
    if(!state.jobHandle)return;
    await App.select(state.jobHandle);
    $('collection').scrollIntoView?.({behavior:'smooth',block:'start'});
  });
  $('job-resume').addEventListener('click',()=>{
    if(state.jobRunning||!state.jobHandle)return;
    return action('/api/start',fullOptions(state.jobHandle),()=>App.choose(state.jobHandle));
  });
  $('job-rebuild').addEventListener('click',()=>{
    if(state.jobRunning||!state.jobHandle)return;
    return action('/api/zip',{username:state.jobHandle},()=>App.choose(state.jobHandle));
  });
  $('refresh-library').addEventListener('click',()=>{state.needsCollections=true;return poll(true);});
  $('reconnect-tool').addEventListener('click',async()=>{
    $('reconnect-tool').disabled=true;
    try{await poll(true);}finally{$('reconnect-tool').disabled=false;}
  });
  $('launch-tool').addEventListener('click',event=>{
    if(!state.supportsDesktopLauncher){event.preventDefault();return;}
    $('connection-help').textContent=
      '请在浏览器提示中允许打开时光集。正在等待本机工具连接；若没有响应，可双击桌面的“时光集”快捷方式。';
  });
  $('zip-link').addEventListener('click',e=>{
    if(e.currentTarget.getAttribute('aria-disabled')==='true')e.preventDefault();
  });
  // Install the retry loop before the first network request; startup failure remains recoverable.
  setInterval(()=>poll(),1500);
  poll();
})();
