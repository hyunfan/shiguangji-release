/* Shared page state, formatting helpers and the authenticated local API client. Loaded first. */
(() => {
  'use strict';
  const App=window.ShiguangjiApp={};
  // Shared mutable page state. Panels read and write it through App.state instead of closure variables.
  const state={
    // Connection, polling and response caches
    token:'',connectionError:false,pollTask:null,pollAgain:false,tick:0,initialized:false,wasRunning:false,
    stateGeneration:0,cacheGeneration:0,loadSequence:0,librarySequence:0,needsCollections:true,
    storageRevision:'',
    // Capabilities reported by the local server
    supportsMultiSource:false,supportsDiscovery:false,supportsRepair:false,supportsAuto:false,
    supportsDelete:false,supportsDesktopLauncher:false,supportsIdentity:false,supportsPagination:false,
    supportsQueue:false,
    // Current task and pending user actions
    jobRunning:false,jobHandle:'',actionPending:false,queueSubmission:null,cloudFormReady:false,
    // Selected collection and the add-account form
    selected:'',preferred:'',selectedPosts:null,selectedCollecting:false,selectedSummary:null,lastUpdate:'',
    inputEdited:false,closeAddOnLoad:false,
    // Identity and delete dialogs
    identityTarget:'',identityBusy:false,deleteTarget:'',deleting:false,
  };
  const $=id=>document.getElementById(id);
  const isInstagram=name=>String(name||'').startsWith('instagram--');
  const displayName=name=>isInstagram(name)?'Instagram @'+String(name).slice(11):'@'+name;
  const inputPlatform=()=>$('platform').value==='instagram'?'instagram':'x';
  const actionControls=[
    'start-button','continue-button','pause-button','repair-button','discover-button','discover-input-button',
    'rebuild-zip','delete-button','job-resume','job-rebuild','queue-add','queue-pause','queue-resume','queue-clear'];
  const responses=new Map();
  function statusBadge(element,summary){
    const icons={info:'◷',success:'✓',warning:'●',danger:'!',neutral:'—'};
    element.className='task-badge task-'+summary.tone;
    element.textContent=(icons[summary.tone]||'—')+' '+summary.label;
    element.hidden=false;
  }
  function localTime(value){
    const date=new Date(value);
    return Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{hour12:false}):value;
  }
  function aliasText(search={},identity=null){
    const names=identity?linkedNames(identity).map(a=>a.username):
      Array.isArray(search.verified_aliases)?search.verified_aliases:[],n=names.length;
    const states={
      running:'正在自动查找旧用户名',
      pending:'旧用户名搜索进度已保存，仍有线索待查',
      partial:'旧用户名搜索部分完成',
      completed:n?'本轮旧用户名搜索完成':'本轮未找到可核实的旧用户名',
      blocked:search.reason==='uid_conflict'?'用户名归属有冲突，已停止关联':'尚未取得数字账号 ID，旧用户名暂无法核实',
    };
    let text=states[search.state];
    if(!text){
      if(search.status==='searching')text='旧用户名搜索进度已保存，仍有线索待查';
      else if(search.errors)text='旧用户名搜索部分完成';
      else text={
        checked:'本轮限定范围旧名搜索完成',
        not_found:'本轮未找到可核实的旧用户名',
        no_id:'尚未取得数字账号 ID，旧用户名暂无法核实',
        conflict:'用户名归属有冲突，已停止关联',
      }[search.status]||'尚无自动旧名查询记录；下次查找时会自动搜寻';
    }
    if(n)text+=' · 已核实 '+names.map(name=>'@'+name).join('、');
    if(Number.isInteger(search.checked_items))text+=` · 已检查 ${search.checked_items} 项`;
    if(search.pending_items)text+=` · 待查 ${search.pending_items} 项`;
    if(search.failed_items||search.errors)text+=` · 部分来源暂不可用（${search.failed_items||search.errors} 项查询失败）`;
    if(search.next_retry_at)text+=' · '+localTime(search.next_retry_at)+' 后可重试失败来源';
    return text;
  }
  try{state.preferred=localStorage.getItem('wayback-selected')||'';}catch{}
  async function request(path,body) {
    const cached=body===undefined?responses.get(path):null,generation=state.cacheGeneration,controller=new AbortController();
    const timer=setTimeout(()=>controller.abort(),12000);
    try {
      const r=await fetch(path,body!==undefined?{
        method:'POST',
        headers:{'Content-Type':'application/json','X-Local-Token':state.token},
        body:JSON.stringify(body),
        signal:controller.signal,
      }:{headers:cached?{'If-None-Match':cached.etag}:{},signal:controller.signal});
      if(r.status===304&&cached)return cached.data;
      const data=await r.json();
      if(r.status===404&&path.startsWith('/api/collection/'))return null;
      if(!r.ok){const failure=new Error(data.error||`请求失败 ${r.status}`);failure.status=r.status;throw failure;}
      const etag=r.headers.get('ETag');
      if(etag&&body===undefined&&generation===state.cacheGeneration)responses.set(path,{etag,data});return data;
    } catch(e) {if(e.name==='AbortError')throw new Error('本地服务响应超时，正在等待重新连接');throw e;} finally {clearTimeout(timer);}
  }
  function installToken(value) {
    if(state.token&&state.token!==value){
      responses.clear();
      state.cacheGeneration++;
      state.loadSequence++;
      state.librarySequence++;
      state.needsCollections=true;
      state.lastUpdate='';
    }
    state.token=value;
  }
  async function api(path,body) {
    if(body===undefined)return request(path);
    // A state request started before this action must not overwrite its result.
    state.stateGeneration++;state.loadSequence++;state.librarySequence++;
    try {
      if(!state.token)installToken((await request('/api/state')).token);
      try{return await request(path,body);}catch(e){
        if(e.status!==403)throw e;
        installToken((await request('/api/state')).token);
        return await request(path,body); // Retry only a rejected token, never an uncertain network result.
      }
    } finally {
      state.stateGeneration++;
      state.loadSequence++;
      state.librarySequence++;
      state.needsCollections=true;
      App.historyPanel?.invalidate();
    }
  }
  function error(message) {$('form-error').textContent=message;$('form-error').hidden=!message;}
  function linkedNames(identity) {
    if(identity?.status!=='verified'||!identity.user_id)return [];
    return (identity.aliases||[]).filter(a=>['verified','reused'].includes(a.state)&&/^[a-z0-9_]{1,15}$/.test(a.username));
  }
  Object.assign(App,{
    state,inputPlatform,$,api,isInstagram,displayName,actionControls,statusBadge,aliasText,localTime,linkedNames,
    error,installToken,responses
  });
})();
