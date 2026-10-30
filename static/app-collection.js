/* Collection panel: library list, selected collection, identity and delete dialogs. */
(() => {
  'use strict';
  const App=window.ShiguangjiApp;
  const {state,$,isInstagram,linkedNames,aliasText,api,displayName,statusBadge,error,responses}=App;
  function choose(handle,automatic=false) {
    state.selected=handle;
    state.selectedPosts=null;
    state.selectedCollecting=false;state.selectedSummary=null;state.lastUpdate='';state.loadSequence++;
    if(!automatic||!state.inputEdited){
      $('username').value=isInstagram(handle)?handle.slice(11):handle;
      $('platform').value=isInstagram(handle)?'instagram':'x';
      App.platformControls();
      state.inputEdited=false;
      state.closeAddOnLoad=Boolean(handle);
    }
    $('more-actions').open=false;
    if(!handle)$('add-account').open=true;
    App.discoveryControl();
    $('collection').hidden=true;$('welcome').hidden=false;
    if(handle)try{localStorage.setItem('wayback-selected',handle);}catch{}
  }
  function collectionIdentity(identity) {
    const names=linkedNames(identity),element=$('collection-identity');
    element.hidden=!names.length;
    element.textContent=names.length?
      '同一账号的其他用户名：'+names.map(a=>'@'+a.username+(a.state==='reused'?'（逐条核对作者）':'')).join('、')+
      '。继续整理会一起检索并复用本地资料。':'';
    const search=identity?.search,note=$('collection-alias-status');
    note.hidden=!identity||isInstagram(state.selected);
    note.textContent=identity?aliasText(search||{},identity):'';
  }
  async function library() {
    const sequence=++state.librarySequence,generation=state.cacheGeneration,items=await api('/api/collections');
    if(sequence!==state.librarySequence||generation!==state.cacheGeneration)return null;
    const list=$('library');list.replaceChildren();
    if(!items.length){
      const p=document.createElement('p');
      p.className='hint';
      p.textContent='整理后的合集会保存在这里。';
      list.append(p);
    }
    items.forEach(m=>{
      const b=document.createElement('button');
      b.className='library-item'+(m.username===state.selected?' active':'');
      const title=document.createElement('strong');
      title.textContent=(isInstagram(m.username)?'':'X ')+displayName(m.username);
      const count=document.createElement('span');
      count.textContent=`${m.counts.posts} 条帖子 · ${m.task_summary?m.task_summary.result_label:App.resultStatus(m)}`;
      b.append(title,count);
      if(m.task_summary){const badge=document.createElement('span');statusBadge(badge,m.task_summary);b.append(badge);}
      const names=linkedNames(m.identity);
      if(names.length){
        const note=document.createElement('span');
        note.className='library-identity';
        note.textContent='同一账号 · '+names.map(a=>'@'+a.username).join('、');
        b.append(note);
      }
      b.addEventListener('click',()=>select(m.username));
      list.append(b);
    });
    return items;
  }
  async function select(handle) {
    choose(handle);
    try{
      await load();
      if(state.selected===handle)await library();
    }catch(e){if(state.selected===handle){state.needsCollections=true;error(e.message);}}
  }
  async function load() {
    if(!state.selected)return;
    const handle=state.selected,sequence=++state.loadSequence,generation=state.cacheGeneration;
    const m=await api('/api/collection/'+encodeURIComponent(handle)+(state.supportsPagination?'?view=summary':''));
    if(handle!==state.selected||sequence!==state.loadSequence||generation!==state.cacheGeneration)return;
    if(!m){$('collection').hidden=true;$('welcome').hidden=false;return;}
    state.selectedPosts=m.counts?.posts;
    state.selectedCollecting=Boolean(m.collecting);
    state.selectedSummary=m.task_summary||null;const empty=state.selectedPosts===0;App.discoveryControl();
    if(state.closeAddOnLoad){$('add-account').open=false;state.closeAddOnLoad=false;}
    $('welcome').hidden=true;$('collection').hidden=false;
    $('offline-link').href='/collections/'+handle+'/index.html';
    $('offline-link').textContent=m.collecting?'打开上次保存的离线版':empty?'查看空清单（0 条内容）':'打开离线版';
    $('offline-link').classList.toggle('disabled',m.offline_ready===false);
    $('offline-link').setAttribute('aria-disabled',String(m.offline_ready===false));
    if(m.offline_ready===false){
      $('offline-link').removeAttribute('href');
      $('offline-link').textContent=m.collecting?'暂停或结束后生成离线版':'离线版待生成';
    }
    const zip=$('zip-link');
    zip.href='/download/'+handle;
    zip.classList.toggle('disabled',!m.zip_ready);
    zip.classList.toggle('primary',false);
    zip.classList.toggle('secondary',true);
    zip.setAttribute('aria-disabled',String(!m.zip_ready));
    zip.textContent=m.zip_ready?(empty?'导出空清单 ZIP（0 条内容）':'下载合集 ↓'):
      m.collecting?(empty?'尚无内容，正在查询':'整理结束后可下载'):
      (empty?'空清单 ZIP 需要更新':'合集需要更新');
    $('rebuild-zip').hidden=m.zip_ready||m.collecting;
    $('rebuild-zip').textContent=m.zip_on_demand?'生成下载包':empty?'更新空清单 ZIP':'重新生成 ZIP';
    if(m.zip_on_demand&&!m.zip_ready&&!m.collecting)zip.textContent='需要时生成 ZIP';
    if(state.selectedSummary){
      const preferBrowse=state.selectedSummary.recommended_action==='browse';
      zip.classList.toggle('primary',preferBrowse&&m.zip_ready);
      zip.classList.toggle('secondary',!preferBrowse||!m.zip_ready);
    }
    $('collection-subtitle').textContent=`最近整理 · ${m.updated_at.slice(0,16).replace('T',' ')} UTC`;
    collectionIdentity(m.identity);
    const revision=(m.content_revision||m.updated_at)+JSON.stringify(m.task_summary||null);
    if(state.lastUpdate!==revision){
      Gallery.show(m,'/collections/'+handle+'/',state.supportsPagination?async params=>{
        const page=await api('/api/posts/'+encodeURIComponent(handle)+'?'+new URLSearchParams(params));
        if(page.stale&&state.selected===handle){state.lastUpdate='';await load();}
        return page;
      }:null);
      state.lastUpdate=revision;
    }
  }
  async function loadIdentity() {
    const target=state.identityTarget;
    const info=await api('/api/identity/'+encodeURIComponent(target));
    if(target!==state.identityTarget||!$('identity-dialog').open)return;
    $('identity-title').textContent='@'+target+' 的历史用户名';
    $('identity-summary').textContent=info.status==='verified'?`数字账号 ID：${info.user_id}`:
      info.status==='conflict'?'这个用户名出现过多个数字账号 ID，暂不自动关联其他名字。':
        '尚未找到这个账号的数字 ID；可先检查已保存快照，或继续整理新快照。';
    const search=info.search||{},searchNote=$('identity-search-status');
    const searchLabels={
      searching:'旧用户名搜索进度已保存，仍有线索待查；继续任务时接着检查。',
      checked:'已完成本轮限定范围搜索；核实的名字会一起整理。',
      not_found:'本轮来源未提供可核实的其他用户名，继续整理新快照时仍会核对线索。',
      no_id:'本轮尚未查到数字账号 ID，无法可靠关联旧名。',
      conflict:'存在账号归属冲突，已停止自动关联。',
    };
    searchNote.textContent=search.state||search.errors?aliasText(search,info):
      (searchLabels[search.status]||'开始或继续查找与下载时，会自动搜索并核对历史用户名，无需手动填写。');
    const list=$('identity-list');list.replaceChildren();
    const labels={
      verified:'已核对 · 随整理一起检索',
      reused:'曾被其他账号使用 · 逐条核对作者，只归并属于此账号的帖子',
      candidate:'待核对 · 查到同一账号的证据后才归并',
      conflict:'归属冲突 · 暂不归并',
      excluded:'已停止检索',
    };
    for(const alias of info.aliases||[]){
      const row=document.createElement('div');row.className='identity-row';
      const title=document.createElement('strong');title.textContent='@'+alias.username;
      const note=document.createElement('p');note.className='hint';note.textContent=labels[alias.state]||'待核对';
      row.append(title,note);
      for(const proof of (alias.evidence||[]).slice(0,2)){
        if(proof.kind==='current_profile'&&!
          /^https:\/\/api\.fxtwitter\.com\/2\/profile\/(?:[A-Za-z0-9_]{1,15}|id:[0-9]{1,20})$/.test(proof.source||''))continue;
        const link=document.createElement('a');link.target='_blank';link.rel='noopener';link.className='identity-proof';
        link.textContent=proof.kind==='current_profile'?`查看当前公开资料 · ID ${proof.uid}`:
          `查看 ${proof.timestamp||'未知日期'} 快照证据 · ID ${proof.uid}`;
        link.href=proof.kind==='current_profile'?proof.source:
          'https://web.archive.org/web/'+encodeURIComponent(proof.timestamp||'*')+'/'+proof.source;
        row.append(link);
      }
      const button=document.createElement('button');button.type='button';button.className='secondary small';
      button.textContent=alias.state==='excluded'?'重新核对':'停止检索此名';
      button.disabled=state.jobRunning||state.identityBusy||state.connectionError;
      button.addEventListener('click',()=>changeIdentity(alias.username,alias.state==='excluded'?'candidate':'excluded'));
      row.append(button);list.append(row);
    }
    if(!(info.aliases||[]).length){const p=document.createElement('p');p.textContent='暂未发现有证据关联的历史用户名。';list.append(p);}
    App.discoveryControl();
  }
  async function changeIdentity(alias,aliasState) {
    if(state.identityBusy||state.jobRunning||state.connectionError)return;
    state.identityBusy=true;App.discoveryControl();
    const target=state.identityTarget;
    try{
      await api('/api/identity',{username:target,alias,state:aliasState});
      if(state.identityTarget!==target)return;
      $('identity-input').value='';
      $('identity-message').textContent=aliasState==='excluded'?'已停止检索此名，已保存内容仍保留。':
        '已加入待核对名字；下次继续整理时会查找归属证据。';
      await loadIdentity();
    }catch(e){
      $('identity-message').textContent=e.message;
    }finally{state.identityBusy=false;try{await loadIdentity();}catch(e){$('identity-message').textContent=e.message;}}
  }
  $('identity-button').addEventListener('click',async()=>{
    if(!state.supportsIdentity||!state.selected||state.actionPending)return;
    state.identityTarget=state.selected;$('more-actions').open=false;$('identity-message').textContent='';
    $('identity-list').replaceChildren();$('identity-summary').textContent='正在读取…';$('identity-dialog').showModal();
    try{await loadIdentity();}catch(e){$('identity-summary').textContent=e.message;}
  });
  $('identity-close').addEventListener('click',()=>{$('identity-dialog').close();state.identityTarget='';});
  $('identity-form').addEventListener('submit',e=>{
    e.preventDefault();
    return changeIdentity($('identity-input').value.trim(),'candidate');
  });
  $('identity-scan').addEventListener('click',()=>{
    if(state.jobRunning||state.actionPending||!state.identityTarget)return;
    const target=state.identityTarget;$('identity-dialog').close();state.identityTarget='';
    return App.action('/api/start',{username:target,mode:'identity',max_minutes:10,auto_continue:false});
  });
  $('delete-button').addEventListener('click',()=>{
    if(!state.supportsDelete||!state.selected||state.actionPending||
      state.selectedCollecting||(state.jobRunning&&state.jobHandle===state.selected))return;
    $('more-actions').open=false;
    state.deleteTarget=state.selected;
    $('delete-title').textContent='删除 '+displayName(state.deleteTarget)+' 的本地合集？';
    $('delete-description').textContent=`将删除 ${displayName(state.deleteTarget)} 的 ${state.selectedPosts||0} 条帖子及其本地文件。`;
    $('delete-confirm').textContent='确认删除 @'+state.deleteTarget;
    $('delete-error').hidden=true;$('delete-dialog').showModal();
  });
  $('delete-cancel').addEventListener('click',()=>{if(!state.deleting)$('delete-dialog').close();});
  $('delete-dialog').addEventListener('cancel',event=>{if(state.deleting)event.preventDefault();});
  $('delete-confirm').addEventListener('click',async()=>{
    if(state.deleting||!state.deleteTarget)return;
    const target=state.deleteTarget;state.deleting=true;state.actionPending=true;
    $('delete-confirm').disabled=true;$('delete-cancel').disabled=true;$('delete-error').hidden=true;
    try{
      await api('/api/delete',{username:target,confirm_username:target});
      responses.clear();state.cacheGeneration++;state.needsCollections=true;state.initialized=false;state.preferred='';
      try{localStorage.removeItem('wayback-selected');}catch{}
      if(state.selected===target)choose('');
      $('delete-dialog').close();state.deleteTarget='';
    }catch(e){$('delete-error').textContent=e.message;$('delete-error').hidden=false;}
    finally{
      state.deleting=false;
      state.actionPending=false;
      $('delete-confirm').disabled=false;
      $('delete-cancel').disabled=false;
      await App.poll(true);
    }
  });
  Object.assign(App,{select,choose,library,load});
})();
