/* Async UI contract tests with Node's built-in runner; all HTTP and timers are simulated. */
'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Element {
  constructor(tag='div') {
    this.tagName=tag;this.children=[];this.listeners={};this.attributes={};
    this.value='';this.checked=false;this.hidden=false;this.disabled=false;this._text='';
    this.classes=new Set();this.classList={toggle:(name,enabled)=>{
      const add=enabled===undefined?!this.classes.has(name):enabled;
      if(add)this.classes.add(name);else this.classes.delete(name);return add;
    },contains:name=>this.classes.has(name)};
  }
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this._text='';this.children=[...nodes];}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  setAttribute(key,value){this.attributes[key]=String(value);}
  getAttribute(key){return this.attributes[key]??null;}
  removeAttribute(key){delete this.attributes[key];}
  querySelector(selector){if(selector==='option[value=instagram]')return this.instagramOption??=(new Element('option'));return null;}
  showModal(){this.open=true;}
  close(){this.open=false;}
  fire(type,values={}){
    const event={target:this,currentTarget:this,preventDefault(){this.defaultPrevented=true;},...values};
    return Promise.all((this.listeners[type]||[]).map(fn=>fn(event)));
  }
}
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
const response=(data,status=200,etag=null)=>({status,ok:status>=200&&status<300,headers:{get:name=>name.toLowerCase()==='etag'?etag:null},json:async()=>structuredClone(data)});
const manifest=(username,updated='2026-09-12T01:00:00Z')=>({username,updated_at:updated,zip_ready:true,collecting:false,status:'partial',counts:{posts:2},posts:[]});
const APP_SCRIPTS=['app-core.js','app-job.js','app-collection.js','app.js'];
const idleJob=()=>({running:false,status:'idle',stage:'done',username:'',logs:[]});

const storageState=()=>({running:false,operation:'',message:'原件保留',settings:{zip_on_demand:true,zip_keep_days:7,backup_versions:3,cache_gb:5},cache:{bytes:0,limit_bytes:5*1024**3}});
test('lossless compression submits only storage work and shows released bytes',async()=>{
  let value=storageState();
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state')return response({token:server.token,capabilities:[],stages:{},job:idleJob(),storage:value});
    if(call.path==='/api/storage/action'){
      value={...value,operation:'compress',finished_at:10,message:'无损压缩完成',result:{saved_bytes:1024**3,errors:[]}};
      return response({ok:true,storage:value});
    }
  }});await settle();
  await app.get('storage-compress').fire('click');
  assert.equal(app.posts().length,1);assert.equal(app.posts()[0].body.operation,'compress');
  assert.equal(app.posts()[0].path,'/api/storage/action');
  assert.match(app.get('storage-result').textContent,/1.00 GiB/);
  assert.equal(app.posts().some(call=>call.path==='/api/start'),false);
});
test('storage controls stay hidden on old servers and do not submit a task',async()=>{
  const app=createApp();await settle();assert.equal(app.get('storage-options').hidden,true);assert.equal(app.posts().length,0);
});
test('compression pause is available during compression and does not pause collection',async()=>{
  let value={...storageState(),running:true,operation:'compress'};
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state')return response({token:server.token,capabilities:[],stages:{},job:idleJob(),storage:value});
    if(call.path==='/api/storage/action'){
      value={...value,running:false,message:'压缩已暂停'};return response({ok:true,storage:value});
    }
  }});await settle();
  assert.equal(app.get('storage-compress-pause').disabled,false);
  await app.get('storage-compress-pause').fire('click');
  assert.equal(app.posts().length,1);assert.equal(app.posts()[0].body.operation,'pause_compression');
  assert.equal(app.get('storage-compress-pause').disabled,true);
});
test('new compression progress clears the previous run result',async()=>{
  let value={...storageState(),operation:'compress',finished_at:1,result:{saved_bytes:4096,errors:[]}};
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state')return response({token:server.token,capabilities:[],stages:{},job:idleJob(),storage:value});
    if(call.path==='/api/storage/action'){
      value={...storageState(),running:true,operation:'compress',message:'正在压缩'};
      return response({ok:true,storage:value});
    }
  }});await settle();assert.match(app.get('storage-result').textContent,/4\.0 KiB/);
  await app.get('storage-compress').fire('click');
  assert.equal(app.get('storage-result').textContent,'');assert.equal(app.get('storage-review').hidden,true);
});
test('storage preview requires explicit review and cancellation never deletes',async()=>{
  let value=storageState();
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state')return response({token:server.token,capabilities:[],stages:{},job:idleJob(),storage:value});
    if(call.path==='/api/storage/action'){
      if(call.body.operation==='preview')value={...value,operation:'preview',finished_at:1,result:{id:'plan-one',bytes:1000,skipped:0,items:[{username:'alice',bytes:1000}]}};
      else value={...value,operation:'cleanup',finished_at:2,result:{removed:1,bytes:1000,skipped:0}};
      return response({ok:true,storage:value});
    }
  }});await settle();
  await app.get('storage-preview-all').fire('click');assert.equal(app.posts().length,1);
  assert.equal(app.posts()[0].body.all_exports,true);assert.equal(app.posts()[0].headers['X-Local-Token'],'token-before');
  await app.get('storage-review').fire('click');assert.equal(app.get('storage-dialog').open,true);
  await app.get('storage-cancel').fire('click');assert.equal(app.posts().length,1);
  await app.get('storage-review').fire('click');await app.get('storage-confirm').fire('click');
  assert.equal(app.posts()[1].body.plan_id,'plan-one');assert.equal(app.posts()[1].body.confirm_zip_only,true);
  assert.equal(app.get('storage-review').hidden,true);
});
test('storage polling preserves edited cache settings and probe uses current account',async()=>{
  const value=storageState();
  const app=createApp({users:['alice'],handler:(call,server)=>{
    if(call.path==='/api/state')return response({token:server.token,capabilities:[],stages:{},job:idleJob(),storage:value});
    if(call.path==='/api/storage/action')return response({ok:true,storage:{...value,running:true,operation:'probe'}});
  }});await settle();
  app.get('storage-cache').value='10';await app.get('storage-cache').fire('change');await app.tick();
  assert.equal(app.get('storage-cache').value,'10');await app.get('storage-probe').fire('click');
  assert.equal(app.posts()[0].body.username,'alice');assert.equal(app.posts()[0].body.operation,'probe');
  assert.equal(app.posts().some(c=>c.path==='/api/start'),false);
});
test('on-demand ZIP is available as an explicit generation action',async()=>{
  const app=createApp();await settle();Object.assign(app.server.manifests.get('alice'),{zip_ready:false,zip_on_demand:true});
  await app.clickUser('alice');assert.equal(app.get('rebuild-zip').textContent,'生成下载包');assert.equal(app.get('rebuild-zip').hidden,false);
  await app.get('rebuild-zip').fire('click');assert.equal(app.posts()[0].path,'/api/zip');
});

test('deferred queue clearly shows automatic retry date and remains pausable',async()=>{
  const due='2026-10-17T10:00:00Z';
  const app=createApp({handler:(call,server)=>call.path==='/api/state'?response({token:server.token,capabilities:[],stages:{},job:idleJob(),
    queue:{state:'running',next_retry_at:due,counts:{pending:1},items:[{id:'one',username:'alice',state:'pending',next_retry_at:due,message:'等待来源恢复'}]}}):undefined});
  await settle();
  assert.equal(app.get('queue-title').textContent,'等待自动重试');
  assert.match(app.get('queue-note').textContent,/3 轮.*48 小时/);
  assert.equal(app.get('queue-pause').hidden,false);
  assert.match(app.get('queue-items').textContent,/等待来源恢复/);
});

test('cloud copy status distinguishes mount receipt from provider upload and preserves edits',async()=>{
  const app=createApp({handler:(call,server)=>call.path==='/api/state'?response({token:server.token,capabilities:[],stages:{},job:idleJob(),
    cloud_backup:{enabled:true,destination:'H:\\backup',state:'retrying',total:3,submitted:1,pending:2,message:'网盘离线',next_retry_at:1800000000}}):undefined});
  await settle();
  assert.match(app.get('cloud-backup-status').textContent,/复制受阻/);
  assert.match(app.get('cloud-backup-status').textContent,/待复制 2 份/);
  assert.doesNotMatch(app.get('cloud-backup-status').textContent,/上传完成/);
  app.get('cloud-backup-path').value='H:\\new-backup'; await app.tick();
  assert.equal(app.get('cloud-backup-path').value,'H:\\new-backup');
  await app.get('cloud-backup-save').fire('click');
  assert.equal(app.posts()[0].path,'/api/cloud-backup');
  assert.equal(app.posts()[0].body.destination,'H:\\new-backup');
  assert.equal(app.posts()[0].headers['X-Local-Token'],'token-before');
});

test('health cooldown and verified backup remain understandable after completion',async()=>{
  const app=createApp({handler:(call,server)=>call.path==='/api/state'?response({token:server.token,capabilities:['reliability'],stages:{},
    job:{...idleJob(),status:'partial',username:'alice',retry_policy:{waiting:2,next_retry_at:new Date(Date.now()-60000).toISOString()},backup:{state:'verified',files:3}},
    reliability:{backup_after_task:true,resume_queue_after_crash:false},guard:{state:'watching'}}):undefined});
  await settle();
  assert.equal(app.get('job-live-detail').hidden,false);
  assert.match(app.get('job-live-detail').textContent,/到达.*可重试时间/);
  assert.match(app.get('job-live-detail').textContent,/备份已校验/);
  assert.match(app.get('guard-status').textContent,/已启用/);
  assert.equal(app.get('resume-after-crash').checked,false);
});

test('finished task never keeps a current-stage clock or promises exhausted retries',async()=>{
  const app=createApp();await settle();
  app.server.job={...idleJob(),status:'partial',username:'alice',stage_started_at:'2020-01-01T00:00:00Z',
    task_summary:{state:'finished',reason:'retry_limit',reason_text:'已达到自动重试上限，需要时可手动继续。'},
    automatic:{enabled:true,batches:3,stop_reason:'已安排延后重试'},
    retry_policy:{waiting:2,next_retry_at:'2020-01-01T00:01:00Z'}};
  await app.tick();
  assert.doesNotMatch(app.get('job-live-detail').textContent,/当前阶段/);
  assert.match(app.get('job-live-detail').textContent,/手动继续/);
  assert.doesNotMatch(app.get('job-progress').textContent,/已安排延后重试/);
  app.server.job={...idleJob(),running:true,status:'running',stage:'pages',stage_started_at:new Date().toISOString()};
  await app.tick();assert.match(app.get('job-live-detail').textContent,/当前阶段/);
});

test('text recovery shows its bounded per-post progress',async()=>{
  const app=createApp();await settle();
  app.server.job={...idleJob(),running:true,status:'running',username:'alice',stage:'content',completed:2,total:4};
  await app.tick();
  assert.equal(app.get('activity').getAttribute('value'),'50');
  assert.equal(app.get('activity').classList.contains('moving'),false);
  assert.match(app.get('job-progress').textContent,/2 \/ 4/);
});

test('reliability setting change uses authenticated POST without starting collection',async()=>{
  const app=createApp();await settle();app.get('resume-after-crash').checked=true;
  await app.get('resume-after-crash').fire('change');
  assert.equal(app.posts().length,1);assert.equal(app.posts()[0].path,'/api/reliability');
  assert.equal(app.posts()[0].headers['X-Local-Token'],'token-before');
  assert.equal(app.posts()[0].body.resume_queue_after_crash,true);
});

test('a failed backup stays visible and never claims media restoration failed',async()=>{
  const app=createApp();await settle();app.server.job={...idleJob(),status:'partial',username:'alice',backup:{state:'failed',error:'disk full'}};
  await app.tick();assert.equal(app.get('job-live-detail').hidden,false);
  assert.match(app.get('job-live-detail').textContent,/备份失败，后续队列已暂停：disk full/);
});
const settle=async()=>{for(let i=0;i<3;i++)await new Promise(resolve=>setImmediate(resolve));};

function createApp({handler,preferred='',users=['alice','bob'],capabilities=['repair','multi_source','post_discovery']}={}){
  const elements=new Map(),calls=[],shown=[],intervals=[],timeouts=new Map(),storage=new Map();let timerId=0;
  if(preferred)storage.set('wayback-selected',preferred);
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('max-pages').value='0';get('max-media-mb').value='512';get('form-error').hidden=true;
  get('cc-indexes').value='6';get('multi-source').checked=true;get('common-crawl').checked=true;get('public-mirrors').checked=true;
  get('auto-continue').checked=true;
  get('history-panel').hidden=true;
  const server={token:'token-before',job:idleJob(),users:[...users],manifests:new Map(users.map(name=>[name,manifest(name)]))};
  const defaultResponse=call=>{
    if(call.method==='POST')return response({ok:true});
    if(call.path==='/api/state')return response({token:server.token,job:server.job,queue:server.queue,version:'1.4',data_dir:'C:\\Users\\sample\\Desktop\\时光集\\collections',capabilities,stages:{pages:'读取快照',media:'下载媒体',sources:'多来源恢复媒体',zip:'生成 ZIP',between_batches:'准备自动继续下一批'}});
    if(call.path==='/api/collections')return response(server.users.map(name=>server.manifests.get(name)));
    if(call.path.startsWith('/api/collection/')){const data=server.manifests.get(decodeURIComponent(call.path.slice('/api/collection/'.length)));return response(data||{error:'missing'},data?200:404);}
    throw new Error('Unexpected request: '+call.path);
  };
  const context={document:{getElementById:get,createElement:tag=>new Element(tag)},AbortController,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},
    Gallery:{show:(data,base)=>shown.push({data:structuredClone(data),base})},
    setInterval:fn=>{intervals.push(fn);return intervals.length;},
    setTimeout:fn=>{timeouts.set(++timerId,fn);return timerId;},clearTimeout:id=>timeouts.delete(id),
    fetch:(path,options={})=>{
      const call={path,method:options.method||'GET',headers:{...options.headers},body:options.body?JSON.parse(options.body):undefined,signal:options.signal};calls.push(call);
      return Promise.resolve().then(()=>handler?.(call,server,defaultResponse)??defaultResponse(call));
    }};
  context.window=context;
  vm.runInNewContext(fs.readFileSync(__dirname+'/../../static/history.js','utf8'),context,{filename:'static/history.js'});
  vm.runInNewContext(fs.readFileSync(__dirname+'/../../static/storage.js','utf8'),context,{filename:'static/storage.js'});
  for(const name of APP_SCRIPTS)vm.runInNewContext(fs.readFileSync(__dirname+'/../../static/'+name,'utf8'),context,{filename:'static/'+name});
  return {get,calls,shown,server,storage,intervals,timeouts,
    tick:()=>Promise.all(intervals.map(fn=>fn())),
    clickUser:name=>{const button=get('library').children.find(n=>n.tagName==='button'&&n.children[0].textContent===(name.startsWith('instagram--')?'Instagram @'+name.slice(11):'X @'+name));assert.ok(button,'Missing account button '+name);return button.fire('click');},
    posts:()=>calls.filter(call=>call.method==='POST')};
}

test('first request offline still installs polling and restores preferred account after reconnect',async()=>{
  let online=false;
  const app=createApp({preferred:'bob',handler:()=>{if(!online)throw new TypeError('Failed to fetch');}});
  await settle();assert.equal(app.intervals.length,1);assert.equal(app.shown.length,0);
  assert.match(app.get('form-error').textContent,/页面会自动重连/);
  online=true;await app.tick();
  assert.equal(app.shown.at(-1).data.username,'bob');assert.equal(app.get('username').value,'bob');
  assert.equal(app.get('form-error').hidden,true);assert.equal(app.get('collection').hidden,false);
});

test('collection-list initialization failure also retries without reloading the page',async()=>{
  let failList=true;
  const app=createApp({handler:call=>{if(call.path==='/api/collections'&&failList)throw new TypeError('list unavailable');}});
  await settle();assert.equal(app.shown.length,0);assert.match(app.get('form-error').textContent,/list unavailable/);
  failList=false;await app.tick();
  assert.equal(app.shown.at(-1).data.username,'alice');assert.equal(app.get('library').children.length,2);
  assert.equal(app.get('form-error').hidden,true);
});

test('a rejected POST token is refreshed and retried once using the new token',async()=>{
  let rejected=false;
  const app=createApp({handler:(call,server)=>{
    if(call.method==='POST'){
      if(!rejected){rejected=true;server.token='token-after';return response({error:'invalid token'},403);}
      server.job={...idleJob(),running:true,status:'running',stage:'pages',username:'alice'};
    }
  }});
  await settle();await app.get('repair-button').fire('click');
  assert.equal(app.posts().length,2);
  assert.deepEqual(app.posts().map(p=>p.headers['X-Local-Token']),['token-before','token-after']);
  assert.deepEqual(app.posts()[0].body,app.posts()[1].body);assert.equal(app.posts()[1].body.mode,'repair');
  assert.equal(app.get('start-button').disabled,true);assert.match(app.get('job-title').textContent,/读取快照/);
  assert.equal(app.get('form-error').hidden,true);
});

test('a second token rejection stops without an unbounded retry',async()=>{
  const app=createApp({handler:call=>call.method==='POST'?response({error:'still rejected'},403):undefined});
  await settle();await app.get('repair-button').fire('click');await app.tick();
  assert.equal(app.posts().length,2);assert.match(app.get('form-error').textContent,/still rejected/);
});

test('an uncertain POST network failure is never automatically resubmitted',async()=>{
  const pending=deferred();
  const app=createApp({handler:call=>call.method==='POST'?pending.promise:undefined});
  await settle();const first=app.get('repair-button').fire('click');await settle();
  assert.equal(app.get('repair-button').disabled,true);await app.get('repair-button').fire('click');
  assert.equal(app.posts().length,1);
  pending.reject(new TypeError('response lost after request'));await first;await app.tick();
  assert.equal(app.posts().length,1);assert.match(app.get('form-error').textContent,/response lost/);
  assert.equal(app.get('repair-button').disabled,false);
});

test('a late response from the previous account cannot replace the newly selected account',async()=>{
  let slowAlice=false;const pending=deferred();
  const app=createApp({handler:call=>slowAlice&&call.path==='/api/collection/alice'?pending.promise:undefined});
  await settle();slowAlice=true;const alice=app.clickUser('alice');await settle();
  await app.clickUser('bob');const count=app.shown.length;
  pending.resolve(response(manifest('alice','2099-01-01T00:00:00Z')));await alice;
  assert.equal(app.shown.length,count);assert.equal(app.shown.at(-1).data.username,'bob');
  assert.equal(app.get('username').value,'bob');assert.equal(app.get('offline-link').href,'/collections/bob/index.html');
  assert.equal(app.storage.get('wayback-selected'),'bob');
});

test('manual refresh during an active poll queues a fresh collection fetch',async()=>{
  const pending=deferred();let hold=false;
  const app=createApp({handler:(call,server,normal)=>{
    if(hold&&call.path==='/api/state'){hold=false;pending.state=normal(call);return pending.promise;}
  }});
  await settle();const before=app.calls.filter(c=>c.path==='/api/collections').length;
  hold=true;const polling=app.tick();await settle();const refresh=app.get('refresh-library').fire('click');
  app.server.manifests.set('alice',manifest('alice','2026-09-12T02:00:00Z'));
  pending.resolve(pending.state);await Promise.all([polling,refresh]);
  assert.ok(app.calls.filter(c=>c.path==='/api/collections').length>before);
  assert.equal(app.shown.at(-1).data.updated_at,'2026-09-12T02:00:00Z');
});

test('action completion supersedes an older state response and refreshes the task controls',async()=>{
  const pending=deferred();let hold=false;
  const app=createApp({handler:(call,server,normal)=>{
    if(hold&&call.path==='/api/state'){hold=false;pending.state=normal(call);return pending.promise;}
    if(call.method==='POST')server.job={...idleJob(),running:true,status:'running',stage:'media',username:'alice',pause_requested:true};
  }});
  await settle();hold=true;const polling=app.tick();await settle();
  const action=app.get('repair-button').fire('click');await settle();
  pending.resolve(pending.state);await Promise.all([polling,action]);
  assert.equal(app.posts().length,1);assert.match(app.get('job-title').textContent,/下载媒体/);
  assert.equal(app.get('pause-button').disabled,true);assert.match(app.get('pause-button').textContent,/正在暂停/);
  assert.equal(app.get('start-button').disabled,true);
});

test('server restart invalidates cached ETags and stale in-flight collection responses',async()=>{
  const pending=deferred();let slow=false;
  const app=createApp({handler:(call,server,normal)=>{
    if(call.path==='/api/collection/alice'&&slow){slow=false;return pending.promise;}
    if(call.path==='/api/collection/alice')return response(server.manifests.get('alice'),200,'"collection-v1"');
    if(call.method==='POST'&&call.headers['X-Local-Token']!==server.token)return response({error:'expired'},403);
  }});
  await settle();slow=true;const oldLoad=app.clickUser('alice');await settle();
  assert.equal(app.calls.at(-1).headers['If-None-Match'],'"collection-v1"');
  app.server.token='new-server-token';app.server.manifests.set('alice',manifest('alice','2026-09-12T03:00:00Z'));
  const action=app.get('repair-button').fire('click');await action;
  const count=app.shown.length;pending.resolve(response(manifest('alice','2099-01-01T00:00:00Z'),200,'"obsolete"'));await oldLoad;
  assert.equal(app.shown.length,count);assert.equal(app.shown.at(-1).data.updated_at,'2026-09-12T03:00:00Z');
  const latest=app.calls.filter(c=>c.path==='/api/collection/alice').at(-1);
  assert.equal(latest.headers['If-None-Match'],undefined);
});

test('an unchanged collection reuses a 304 response without parsing a missing body or redrawing',async()=>{
  let hits=0;
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/collection/alice'){
      if(call.headers['If-None-Match']==='"unchanged"'){
        hits++;return {...response(undefined,304),json:async()=>{throw new Error('A 304 has no JSON body');}};
      }
      return response(server.manifests.get('alice'),200,'"unchanged"');
    }
  }});
  await settle();const shown=app.shown.length;await app.get('refresh-library').fire('click');
  assert.equal(hits,1);assert.equal(app.shown.length,shown);assert.equal(app.get('form-error').hidden,true);
});

test('a request timeout releases polling and the next interval reconnects',async()=>{
  let blocked=true;
  const app=createApp({handler:call=>{
    if(blocked)return new Promise((resolve,reject)=>call.signal.addEventListener('abort',()=>{const error=new Error('aborted');error.name='AbortError';reject(error);},{once:true}));
  }});
  await settle();assert.equal(app.timeouts.size,1);
  for(const expire of [...app.timeouts.values()])expire();await settle();
  assert.match(app.get('form-error').textContent,/响应超时/);assert.equal(app.timeouts.size,0);
  blocked=false;await app.tick();assert.equal(app.shown.at(-1).data.username,'alice');
  assert.equal(app.get('form-error').hidden,true);assert.equal(app.timeouts.size,0);
});

test('missing preferred collection selects an existing account and stale ZIP links stay disabled',async()=>{
  const app=createApp({preferred:'removed',handler:(call,server)=>{
    if(call.path==='/api/collection/alice')return response({...server.manifests.get('alice'),zip_ready:false});
  }});
  await settle();assert.equal(app.shown.at(-1).data.username,'alice');
  assert.equal(app.get('zip-link').getAttribute('aria-disabled'),'true');assert.equal(app.get('rebuild-zip').hidden,false);
  let prevented=false;await app.get('zip-link').fire('click',{preventDefault(){prevented=true;}});assert.equal(prevented,true);
});

test('media repair downloads known media before optional sources and honors source choices',async()=>{
  const app=createApp();await settle();
  assert.equal(app.get('repair-button').textContent,'仅下载已知媒体');
  app.get('public-mirrors').checked=false;app.get('cc-indexes').value='9';
  await app.get('repair-button').fire('click');
  const body=app.posts()[0].body;
  assert.equal(body.mode,'repair');assert.equal(body.multi_source,true);
  assert.equal(body.common_crawl,true);assert.equal(body.public_mirrors,false);assert.equal(body.cc_indexes,9);
  assert.equal(body.username,'alice');
});

test('media repair honors the unchecked multi-source option while keeping repair mode',async()=>{
  const app=createApp();await settle();
  app.get('multi-source').checked=false;
  await app.get('repair-button').fire('click');
  assert.equal(app.posts()[0].body.mode,'repair');
  assert.equal(app.posts()[0].body.multi_source,false);
});

test('normal collection enables multi-source by default and honors opting out or custom settings',async()=>{
  const app=createApp();await settle();
  await app.get('collect-form').fire('submit');
  let body=app.posts()[0].body;assert.equal(body.multi_source,true);
  assert.equal(body.common_crawl,true);assert.equal(body.public_mirrors,true);assert.equal(body.cc_indexes,6);
  app.get('multi-source').checked=false;app.get('common-crawl').checked=false;
  await app.get('collect-form').fire('submit');body=app.posts()[1].body;
  assert.equal(body.multi_source,false);assert.equal(body.common_crawl,false);
  app.get('multi-source').checked=true;await app.get('collect-form').fire('submit');
  assert.equal(app.posts()[2].body.multi_source,true);assert.equal(app.posts()[2].body.common_crawl,false);
});

test('source recovery has its own progress stage and remains pausable',async()=>{
  const app=createApp();await settle();
  app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'sources',completed:2,total:10};
  await app.tick();assert.match(app.get('job-title').textContent,/多来源恢复媒体/);
  assert.equal(app.get('job-progress').textContent,'当前步骤已处理：2 / 10');
  assert.equal(app.get('pause-button').disabled,false);assert.equal(app.get('repair-button').disabled,true);
});

test('old servers fall back to repair without enabling unsupported multi-source recovery',async()=>{
  const app=createApp({handler:call=>call.path==='/api/state'?response({token:'old',job:idleJob(),version:'1.3',capabilities:['repair'],stages:{}}):undefined});
  await settle();assert.equal(app.get('repair-button').textContent,'仅下载已知媒体');assert.equal(app.get('multi-source').disabled,true);
  app.get('multi-source').checked=true;await app.get('repair-button').fire('click');
  assert.equal(app.posts()[0].body.mode,'repair');assert.equal(app.posts()[0].body.multi_source,false);
});

test('HTML retains public mirror disclosure and background lifetime help',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  for(const id of ['multi-source','common-crawl','public-mirrors','cc-indexes'])assert.ok(html.includes(`id="${id}"`));
  assert.match(html,/<input id="multi-source" type="checkbox" checked>/);
  assert.match(html,/向 FxTwitter 公开镜像发送用户名、数字账号 ID 和帖子 ID/);
  assert.match(html,/关闭网页，任务仍会继续/);assert.match(html,/睡眠或关机/);
});

test('sidebar identifies the connected server version and its actual collection folder',async()=>{
  const app=createApp();await settle();
  assert.equal(app.get('runtime-version').textContent,'当前版本：1.4');
  assert.equal(app.get('runtime-folder').textContent,'C:\\Users\\sample\\Desktop\\时光集\\collections');
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  for(const id of ['runtime-version','runtime-folder'])assert.ok(html.includes(`id="${id}"`));
});

test('server identity values are displayed as text and missing old-server fields have fallbacks',async()=>{
  let legacy=false;
  const app=createApp({handler:(call,server)=>call.path==='/api/state'?response({token:server.token,job:idleJob(),stages:{},
    ...(legacy?{}:{version:'1.4 <img src=x>',data_dir:'C:\\folder\\<script>alert(1)</script>'})}):undefined});
  await settle();assert.equal(app.get('runtime-version').textContent,'当前版本：1.4 <img src=x>');
  assert.equal(app.get('runtime-folder').textContent,'C:\\folder\\<script>alert(1)</script>');
  assert.equal(app.get('runtime-folder').children.length,0);
  legacy=true;await app.tick();assert.equal(app.get('runtime-version').textContent,'当前版本：后台未提供');
  assert.match(app.get('runtime-folder').textContent,/未提供保存目录/);
});

test('post discovery sends discover mode and collection settings separately from media repair',async()=>{
  const app=createApp();await settle();assert.equal(app.get('discover-button').hidden,false);
  app.get('max-pages').value='120';app.get('public-mirrors').checked=false;
  await app.get('discover-button').fire('click');
  const body=app.posts()[0].body;assert.equal(body.mode,'discover');assert.equal(body.username,'alice');
  assert.equal(body.max_pages,120);assert.equal(body.public_mirrors,false);assert.equal(body.multi_source,false);
  await app.get('repair-button').fire('click');assert.equal(app.posts()[1].body.mode,'repair');
});

test('old servers hide discovery and never submit an unsupported discovery action',async()=>{
  const app=createApp({handler:call=>call.path==='/api/state'?response({token:'old',job:idleJob(),version:'1.5',capabilities:['repair','multi_source'],stages:{}}):undefined});
  await settle();assert.equal(app.get('discover-button').hidden,true);assert.equal(app.get('discover-button').disabled,true);
  assert.equal(app.get('discover-input-button').hidden,true);assert.equal(app.get('discover-input-note').hidden,true);
  await app.get('discover-input-button').fire('click');assert.equal(app.posts().length,0);
  await app.get('discover-button').fire('click');assert.equal(app.posts().length,0);
  await app.get('repair-button').fire('click');assert.equal(app.posts()[0].body.mode,'repair');
});

test('new-account discovery uses the current input and settings without media recovery',async()=>{
  const app=createApp({users:[]});await settle();
  assert.equal(app.get('discover-input-button').hidden,false);assert.equal(app.get('discover-input-note').hidden,false);
  assert.equal(app.get('discover-input-button').disabled,true);
  app.get('username').value='  @new_account  ';await app.get('username').fire('input');
  assert.equal(app.get('discover-input-button').disabled,false);
  app.get('proxy').value=' http://127.0.0.1:7890 ';app.get('max-pages').value='120';
  app.get('max-media-mb').value='256';app.get('force-recheck').checked=true;
  app.get('public-mirrors').checked=false;app.get('cc-indexes').value='9';
  await app.get('discover-input-button').fire('click');
  assert.equal(app.posts().length,1);assert.deepEqual(app.posts()[0].body,{
    username:'@new_account',platform:'x',instagram_links:'',mode:'discover',proxy:'http://127.0.0.1:7890',max_pages:120,max_media_mb:256,
    force_recheck:true,max_minutes:10,multi_source:false,common_crawl:true,public_mirrors:false,cc_indexes:9
  });
  assert.equal(app.posts()[0].headers['X-Local-Token'],'token-before');
});

test('input discovery ignores blank names and uses edited input instead of selected collection',async()=>{
  const app=createApp();await settle();
  app.get('username').value='  \t ';await app.get('username').fire('input');
  assert.equal(app.get('discover-input-button').disabled,true);
  await app.get('discover-input-button').fire('click');assert.equal(app.posts().length,0);
  app.get('username').value='bob_new';await app.get('username').fire('input');
  await app.get('discover-input-button').fire('click');assert.equal(app.posts()[0].body.username,'bob_new');
});

test('input discovery shares pending protection and does not replay uncertain network failures',async()=>{
  const pending=deferred(),app=createApp({users:[],handler:call=>call.method==='POST'?pending.promise:undefined});
  await settle();app.get('username').value='new_account';await app.get('username').fire('input');
  const first=app.get('discover-input-button').fire('click');await settle();
  assert.equal(app.get('discover-input-button').disabled,true);assert.equal(app.get('start-button').disabled,true);
  await app.get('discover-input-button').fire('click');await app.get('collect-form').fire('submit');
  assert.equal(app.posts().length,1);
  pending.reject(new TypeError('uncertain discovery result'));await first;await app.tick();
  assert.equal(app.posts().length,1);assert.match(app.get('form-error').textContent,/uncertain discovery result/);
  assert.equal(app.get('discover-input-button').disabled,false);
});

test('input discovery refreshes a rejected token and stays disabled while a job runs',async()=>{
  let rejected=false;
  const app=createApp({users:[],handler:(call,server)=>{
    if(call.method==='POST'){
      if(!rejected){rejected=true;server.token='token-after';return response({error:'invalid token'},403);}
      server.job={...idleJob(),running:true,status:'running',stage:'pages',username:'new_account'};
    }
  }});
  await settle();app.get('username').value='new_account';await app.get('username').fire('input');
  await app.get('discover-input-button').fire('click');
  assert.deepEqual(app.posts().map(call=>call.headers['X-Local-Token']),['token-before','token-after']);
  assert.deepEqual(app.posts()[0].body,app.posts()[1].body);assert.equal(app.get('discover-input-button').disabled,true);
  await app.get('discover-input-button').fire('click');assert.equal(app.posts().length,2);
  assert.equal(app.get('form-error').hidden,true);
});

test('input discovery is an optional separate button with an explicit no-download explanation',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  assert.match(html,/<button[^>]+id="discover-input-button"[^>]+type="button"[^>]+hidden[^>]*>仅查找帖子<\/button>/);
  assert.match(html,/只读取帖子和正文，本次不下载媒体/);
  assert.match(html,/<button[^>]+id="start-button"[^>]+type="submit"/);
});

test('empty collections allow the unified full action while ZIP and manual discovery stay secondary',async()=>{
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state'){
      server.manifests.set('alice',{...manifest('alice'),status:'complete',counts:{posts:0,saved:0}});
      server.job={...idleJob(),username:'alice',status:'complete',counts:{posts:0,saved:0}};
    }
  }});await settle();
  assert.match(app.get('library').children[0].textContent,/0 条帖子 · 未发现帖子/);
  assert.equal(app.get('job-title').textContent,'@alice · 未发现帖子');
  assert.match(app.get('zip-link').textContent,/0 条内容/);assert.equal(app.get('zip-link').classes.has('primary'),false);
  assert.equal(app.get('continue-button').disabled,false);
  assert.equal(app.get('discover-button').classes.has('primary'),false);assert.equal(app.get('discover-button').disabled,false);
  assert.equal(app.get('repair-button').disabled,true);assert.match(app.get('offline-link').textContent,/0 条内容/);
  await app.get('continue-button').fire('click');assert.equal(app.posts()[0].body.mode,'full');assert.equal(app.posts()[0].body.username,'alice');
  await app.clickUser('bob');assert.equal(app.get('zip-link').classes.has('primary'),false);
  assert.equal(app.get('repair-button').disabled,false);
  assert.equal(app.get('discover-button').classes.has('primary'),false);assert.equal(app.get('offline-link').textContent,'打开离线版');
});

test('empty failed collections explicitly show query failure in the library and task title',async()=>{
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state'){
      server.manifests.set('alice',{...manifest('alice'),status:'failed',counts:{posts:0,saved:0}});
      server.job={...idleJob(),username:'alice',status:'failed',counts:{posts:0,saved:0}};
    }
  }});await settle();
  assert.match(app.get('library').children[0].textContent,/0 条帖子 · 查询失败/);
  assert.equal(app.get('job-title').textContent,'@alice · 查询失败');
});

test('running task exposes cumulative successful pages and retry wait without opening details',async()=>{
  let online=true;
  const app=createApp({handler:call=>{if(call.path==='/api/state'&&!online)throw new Error('offline');}});
  await settle();
  app.server.job={...idleJob(),running:true,status:'running',username:'alice',stage:'pages',completed:2,total:3,
    page_progress:{parsed:2,total:3,batch_checked:1},stage_started_at:new Date(Date.now()-65000).toISOString(),
    stage_seconds:{media:12},logs:[{time:new Date().toISOString(),text:'网络错误，5 秒后重试'}]};
  await app.tick();
  assert.equal(app.get('job-live-detail').hidden,false);
  assert.match(app.get('job-live-detail').textContent,/累计已解析 2 \/ 3 份快照，本批已检查 1 份/);
  assert.match(app.get('job-live-detail').textContent,/当前阶段 1分/);
  assert.match(app.get('job-live-detail').textContent,/网络错误，5 秒后重试/);
  assert.match(app.get('job-progress').textContent,/下载媒体 12秒/);
  assert.ok(Number(app.get('activity').getAttribute('value'))<100);
  online=false;await app.tick();assert.equal(app.get('job-live-detail').hidden,true);
  online=true;app.server.job={...idleJob(),username:'alice',status:'paused'};
  await app.tick();assert.equal(app.get('job-live-detail').hidden,true);
});

test('first unfinished collection does not offer a nonexistent offline page',async()=>{
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/state')server.manifests.set('alice',{...manifest('alice'),collecting:true,offline_ready:false});
  }});
  await settle();
  assert.equal(app.get('offline-link').getAttribute('aria-disabled'),'true');
  assert.equal(app.get('offline-link').getAttribute('href'),null);
  assert.match(app.get('offline-link').textContent,/暂停或结束后生成离线版/);
});

test('bounded batch shows the stop reason and partial counter and never restarts itself',async()=>{
  const app=createApp({handler:(call,server,defaultResponse)=>{
    if(call.path==='/api/state'){
      server.job={...idleJob(),username:'alice',status:'paused',stage:'done',completed:4,total:100,
        batch:{elapsed_seconds:600,max_seconds:600,requests:80,max_requests:200,new_files:3,stop_reason:'达到本批 10 分钟检索时限'}};
      return response({token:server.token,job:server.job,stages:{},version:'1.8',capabilities:['repair','multi_source','bounded_batches']});
    }
  }});
  await settle();await app.tick();await app.tick();
  assert.equal(app.get('job-title').textContent,'@alice · 本批已结束');
  assert.match(app.get('job-progress').textContent,/4 \/ 100/);
  assert.match(app.get('job-progress').textContent,/本批新增 3 个文件/);
  assert.match(app.get('job-progress').textContent,/达到本批 10 分钟检索时限/);
  assert.equal(app.get('activity').classList.contains('moving'),false);
  assert.equal(app.get('activity-track').hidden,true);
  assert.equal(app.get('activity').getAttribute('value'),null);
  assert.equal(app.get('activity').getAttribute('style'),null);
  assert.equal(app.get('repair-button').textContent,'仅下载已知媒体');
  assert.equal(app.posts().length,0);
  app.get('max-minutes').value='5';
  await app.get('repair-button').fire('click');
  assert.equal(app.posts().length,1);
  assert.equal(app.posts()[0].body.max_minutes,5);
});

const newCapabilities=['repair','multi_source','post_discovery','bounded_batches','auto_batches','delete_collection'];

test('automatic continuation is enabled by default and can be disabled for a single batch',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  assert.equal(app.get('repair-button').textContent,'仅下载已知媒体');
  await app.get('repair-button').fire('click');
  assert.equal(app.posts()[0].body.auto_continue,true);
  assert.equal(app.posts()[0].body.auto_max_minutes,0);
  app.get('auto-continue').checked=false;await app.get('auto-continue').fire('change');
  assert.equal(app.get('repair-button').textContent,'仅下载已知媒体');
  await app.get('repair-button').fire('click');
  assert.equal(app.posts()[1].body.auto_continue,false);
});

test('batch rest is controlled by server and remains pausable without browser replaying start',async()=>{
  const app=createApp({capabilities:newCapabilities,handler:(call,server)=>{
    if(call.path==='/api/state')server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'between_batches',automatic:{enabled:true,batches:2,new_files:4}};
  }});await settle();await app.tick();
  assert.match(app.get('job-title').textContent,/准备自动继续/);
  assert.match(app.get('job-progress').textContent,/累计新增 4 个文件/);
  assert.equal(app.get('pause-button').disabled,false);
  assert.equal(app.posts().length,0);
  await app.get('pause-button').fire('click');
  assert.equal(app.posts().at(-1).path,'/api/pause');
});

test('opening and cancelling deletion does not send a delete request',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  assert.equal(app.get('delete-button').hidden,false);
  await app.get('delete-button').fire('click');
  assert.equal(app.get('delete-dialog').open,true);
  assert.match(app.get('delete-title').textContent,/@alice/);
  assert.equal(app.posts().length,0);
  await app.get('delete-cancel').fire('click');
  assert.equal(app.get('delete-dialog').open,false);
  assert.equal(app.posts().length,0);
});

test('confirmed deletion clears stale gallery and selection then shows the surviving account',async()=>{
  const app=createApp({capabilities:newCapabilities,handler:(call,server)=>{
    if(call.path==='/api/delete'){server.users=server.users.filter(name=>name!==call.body.username);server.manifests.delete(call.body.username);}
  }});await settle();
  await app.get('delete-button').fire('click');await app.get('delete-confirm').fire('click');
  assert.deepEqual(app.posts()[0].body,{username:'alice',confirm_username:'alice'});
  assert.equal(app.get('delete-dialog').open,false);
  assert.equal(app.shown.at(-1).data.username,'bob');
  assert.equal(app.storage.get('wayback-selected'),'bob');
  assert.equal(app.get('library').children.length,1);
});

test('deleting the last account returns to the empty welcome page',async()=>{
  const app=createApp({users:['alice'],capabilities:newCapabilities,handler:(call,server)=>{
    if(call.path==='/api/delete'){server.users=[];server.manifests.clear();}
  }});await settle();
  await app.get('delete-button').fire('click');await app.get('delete-confirm').fire('click');
  assert.equal(app.get('welcome').hidden,false);
  assert.equal(app.get('collection').hidden,true);
  assert.equal(app.storage.has('wayback-selected'),false);
});

test('deletion is disabled for an active account and on old servers',async()=>{
  const app=createApp({capabilities:newCapabilities,handler:(call,server)=>{
    if(call.path==='/api/state')server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'media'};
  }});await settle();
  assert.equal(app.get('delete-button').disabled,true);
  assert.equal(app.get('delete-note').hidden,false);
  await app.get('delete-button').fire('click');assert.notEqual(app.get('delete-dialog').open,true);
  const old=createApp();await settle();assert.equal(old.get('delete-button').hidden,true);
});

test('uncertain deletion does not repeat automatically or falsely close the confirmation',async()=>{
  const request=deferred();
  const app=createApp({capabilities:newCapabilities,handler:call=>call.path==='/api/delete'?request.promise:undefined});
  await settle();await app.get('delete-button').fire('click');
  const first=app.get('delete-confirm').fire('click');await settle();
  await app.get('delete-confirm').fire('click');assert.equal(app.posts().length,1);
  request.reject(new TypeError('connection lost'));await first;await app.tick();
  assert.equal(app.posts().length,1);assert.equal(app.get('delete-dialog').open,true);
  assert.match(app.get('delete-error').textContent,/connection lost/);
  assert.equal(app.get('library').children.length,2);
});

test('sleep prevention stays visible through batch waits and pause saving, then clears on completion',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  assert.equal(app.get('job-sleep').hidden,true);
  for(const stage of ['media','between_batches','export','zip']){
    app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage,
      pause_requested:['export','zip'].includes(stage),sleep_prevention:'active'};
    await app.tick();
    assert.equal(app.get('job-sleep').hidden,false);
    assert.match(app.get('job-sleep').textContent,/已启用任务防休眠/);
    assert.match(app.get('job-sleep').textContent,/屏幕可以熄灭/);
  }
  for(const status of ['paused','complete','failed','idle']){
    // A retained value from an older saved state must never imply idle protection.
    app.server.job={...idleJob(),username:'alice',status,sleep_prevention:'active'};
    await app.tick();
    assert.equal(app.get('job-sleep').hidden,true);
    assert.equal(app.get('job-sleep').textContent,'');
  }
  assert.equal(app.posts().length,0);
});

test('pending, failed and unsupported sleep prevention never claim success or stop the job',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  for(const [state,expected] of [['pending',/正在申请/],['failed',/未能启用防休眠，任务仍在继续/],['unsupported',/不支持/],['off',/未启用/]]){
    app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'media',sleep_prevention:state};
    await app.tick();
    assert.equal(app.get('job-sleep').hidden,false);
    assert.match(app.get('job-sleep').textContent,expected);
    assert.doesNotMatch(app.get('job-sleep').textContent,/已启用/);
    assert.equal(app.get('pause-button').disabled,false);
    assert.match(app.get('job-title').textContent,/下载媒体/);
  }
  assert.equal(app.posts().length,0);
  for(const state of [undefined,'unknown-future-value']){
    app.server.job.sleep_prevention=state;await app.tick();
    assert.equal(app.get('job-sleep').hidden,true);
    assert.equal(app.get('job-sleep').textContent,'');
  }
});

test('connection loss replaces stale sleep protection status until the server confirms it again',async()=>{
  let offline=false;
  const app=createApp({handler:()=>{if(offline)throw new TypeError('offline');}});await settle();
  app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'media',sleep_prevention:'active'};
  await app.tick();assert.match(app.get('job-sleep').textContent,/已启用/);
  offline=true;await app.tick();
  assert.match(app.get('job-sleep').textContent,/状态暂无法确认/);
  assert.doesNotMatch(app.get('job-sleep').textContent,/已启用/);
  offline=false;await app.tick();assert.match(app.get('job-sleep').textContent,/已启用/);
});

test('task sleep status has a hidden accessible element and background note explains its limits',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  assert.match(html,/<p id="job-sleep" class="hint" hidden>/);
  assert.match(html,/以任务状态中的“已启用”为准/);
  assert.match(html,/屏幕可以熄灭/);
  assert.match(html,/勿合盖、手动睡眠或关机/);
});

test('initial connection failure offers desktop instructions without an unverified launch link',async()=>{
  const app=createApp({handler:()=>{throw new TypeError('offline');}});await settle();
  assert.equal(app.get('connection-panel').hidden,false);
  assert.equal(app.get('launch-tool').hidden,true);
  assert.match(app.get('connection-help').textContent,/桌面的“时光集”快捷方式/);
  assert.equal(app.get('start-button').disabled,true);
  app.get('username').value='alice';await app.get('username').fire('input');
  assert.equal(app.get('discover-input-button').disabled,true);
  await app.get('collect-form').fire('submit');
  assert.equal(app.posts().length,0);
});

test('an installed launcher remains available during disconnect and reconnect never resumes an action',async()=>{
  let offline=false;
  const app=createApp({capabilities:[...newCapabilities,'desktop_launcher'],handler:()=>{if(offline)throw new TypeError('offline');}});await settle();
  assert.equal(app.get('connection-panel').hidden,true);
  offline=true;await app.tick();
  assert.equal(app.get('connection-panel').hidden,false);
  assert.equal(app.get('launch-tool').hidden,false);
  assert.match(app.get('connection-help').textContent,/浏览器可能询问/);
  const before=app.calls.length;await app.get('launch-tool').fire('click');
  assert.equal(app.calls.length,before);
  assert.match(app.get('connection-help').textContent,/正在等待本机工具连接/);
  assert.equal(app.get('repair-button').disabled,true);
  offline=false;await app.get('reconnect-tool').fire('click');
  assert.equal(app.get('connection-panel').hidden,true);
  assert.equal(app.get('reconnect-tool').disabled,false);
  assert.equal(app.get('form-error').hidden,true);
  assert.equal(app.get('repair-button').disabled,false);
  assert.equal(app.posts().length,0);
});

test('reconnecting to a server without the launcher capability removes a stale launch link',async()=>{
  let offline=false,installed=true;
  const app=createApp({handler:(call,server,normal)=>{
    if(offline)throw new TypeError('offline');
    if(call.path==='/api/state')return response({token:server.token,job:server.job,version:'1.9.4',stages:{},capabilities:installed?['desktop_launcher']:[]});
  }});await settle();
  offline=true;await app.tick();assert.equal(app.get('launch-tool').hidden,false);
  offline=false;installed=false;await app.tick();
  offline=true;await app.tick();assert.equal(app.get('launch-tool').hidden,true);
  let prevented=false;await app.get('launch-tool').fire('click',{preventDefault(){prevented=true;}});
  assert.equal(prevented,true);assert.equal(app.posts().length,0);
});

test('connection recovery banner is hidden by default and uses only the fixed local launch URI',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  assert.match(html,/<section id="connection-panel" class="connection-panel" hidden aria-live="polite"/);
  assert.match(html,/<a id="launch-tool" class="primary" href="x-wayback-library:\/\/open" hidden>启动时光集<\/a>/);
  assert.match(html,/<button id="reconnect-tool" class="secondary" type="button">重新连接<\/button>/);
});

test('unified continue starts full discovery and downloads for the selected account with automatic continuation',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  app.get('username').value='another_account';app.get('max-pages').value='120';
  app.get('public-mirrors').checked=false;app.get('cc-indexes').value='9';app.get('more-actions').open=true;
  await app.get('continue-button').fire('click');
  const body=app.posts()[0].body;
  assert.equal(body.username,'alice');assert.equal(body.mode,'full');assert.equal(body.max_pages,120);
  assert.equal(body.auto_continue,true);assert.equal(body.multi_source,true);
  assert.equal(body.public_mirrors,false);assert.equal(body.cc_indexes,9);
  assert.equal(app.get('more-actions').open,false);
});

test('the new-account form also uses full mode and honors optional single-batch settings',async()=>{
  const app=createApp({users:[],capabilities:newCapabilities});await settle();
  app.get('username').value='new_account';app.get('auto-continue').checked=false;
  await app.get('collect-form').fire('submit');
  assert.equal(app.posts()[0].body.username,'new_account');assert.equal(app.posts()[0].body.mode,'full');
  assert.equal(app.posts()[0].body.auto_continue,false);
});

test('unified continue blocks duplicate requests and never replays an uncertain submission',async()=>{
  const pending=deferred(),app=createApp({handler:call=>call.method==='POST'?pending.promise:undefined});await settle();
  const first=app.get('continue-button').fire('click');await settle();
  assert.equal(app.get('continue-button').disabled,true);
  await app.get('continue-button').fire('click');await app.get('repair-button').fire('click');
  assert.equal(app.posts().length,1);
  pending.reject(new TypeError('uncertain result'));await first;await app.tick();
  assert.equal(app.posts().length,1);assert.match(app.get('form-error').textContent,/uncertain result/);
  assert.equal(app.get('continue-button').disabled,false);
});

test('switching collections keeps the global task owner clear and cannot start a second job',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  app.server.job={...idleJob(),running:true,status:'running',stage:'media',username:'alice'};await app.tick();
  assert.equal(app.get('continue-button').textContent,'正在处理此博主…');
  await app.clickUser('bob');
  assert.match(app.get('job-title').textContent,/@alice/);assert.equal(app.get('continue-button').textContent,'正在处理 @alice');
  assert.equal(app.get('continue-button').disabled,true);assert.equal(app.get('pause-button').hidden,false);
  await app.get('continue-button').fire('click');await app.get('repair-button').fire('click');
  await app.get('discover-button').fire('click');await app.get('collect-form').fire('submit');
  assert.equal(app.posts().length,0);
  app.server.job={...idleJob(),username:'alice',status:'partial'};await app.tick();
  assert.equal(app.get('continue-button').disabled,false);assert.equal(app.get('pause-button').hidden,true);
});

test('another process collecting the selected account disables unified continue',async()=>{
  const app=createApp({handler:(call,server)=>call.path==='/api/collection/alice'?response({...server.manifests.get('alice'),collecting:true}):undefined});await settle();
  assert.equal(app.get('continue-button').disabled,true);assert.match(app.get('continue-button').textContent,/正在处理/);
  await app.get('continue-button').fire('click');assert.equal(app.posts().length,0);
});

test('run outcome uses the server aggregate including current batch without double-counting or lifetime totals',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'media',counts:{saved:333},batch:{new_files:3},automatic:{enabled:true,new_files:8,batches:1,current_batch:2}};
  await app.tick();assert.equal(app.get('job-outcome').textContent,'本次已新增 8 个媒体文件');
  app.server.job={...app.server.job,running:false,status:'partial',automatic:{enabled:true,new_files:8,batches:2,stop_code:'no_progress'}};
  await app.tick();assert.equal(app.get('job-outcome').textContent,'本次新增 8 个媒体文件');
  assert.match(app.get('job-reason').textContent,/连续 3 批/);assert.match(app.get('job-reason').textContent,/无需反复点击/);
  app.server.job={...app.server.job,automatic:{},batch:{new_files:0}};await app.tick();
  assert.equal(app.get('job-outcome').textContent,'本次新增 0 个媒体文件');
  app.server.job={...app.server.job,batch:{}};await app.tick();
  assert.doesNotMatch(app.get('job-outcome').textContent,/333|新增 0/);
});

test('task end reasons distinguish pause, interruption, failure, configured limits, and completed checks',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  for(const [job,expected] of [
    [{status:'partial',automatic:{enabled:true,stop_code:'pass_finished'}},/本轮可执行检查已完成/],
    [{status:'paused',automatic:{enabled:true,stop_code:'user_pause'}},/已暂停并保存/],
    [{status:'paused',interrupted:true},/上次运行已中断/],
    [{status:'failed',automatic:{enabled:true,stop_code:'no_progress'}},/未能完成.*查看错误/],
    [{status:'paused',automatic:{enabled:true,stop_code:'session_limit',stop_reason:'达到设定的运行时间上限'}},/达到设定的运行时间上限/],
    [{status:'complete',counts:{posts:0}},/未发现可恢复的历史帖子/]
  ]){
    app.server.job={...idleJob(),username:'alice',...job};await app.tick();
    assert.match(app.get('job-reason').textContent,expected);
  }
});

test('adding an account clears only the selected handle on deliberate opening and polling preserves typed input',async()=>{
  const app=createApp();await settle();
  assert.equal(app.get('add-account').open,false);
  app.get('add-account').open=true;await app.get('add-account').fire('toggle');
  assert.equal(app.get('username').value,'');
  app.get('username').value='draft_account';await app.get('username').fire('input');
  await app.tick();await app.get('refresh-library').fire('click');
  assert.equal(app.get('username').value,'draft_account');assert.equal(app.get('add-account').open,true);
  app.get('more-actions').open=true;await app.clickUser('bob');
  assert.equal(app.get('add-account').open,false);assert.equal(app.get('more-actions').open,false);
});

test('input typed before initial collection loading is not lost or collapsed by initialization',async()=>{
  const pending=deferred();
  const app=createApp({handler:call=>call.path==='/api/collections'?pending.promise:undefined});await settle();
  app.get('add-account').open=true;app.get('username').value='draft_account';await app.get('username').fire('input');
  pending.resolve(response([manifest('alice')]));await settle();
  assert.equal(app.shown.at(-1).data.username,'alice');assert.equal(app.get('username').value,'draft_account');
  assert.equal(app.get('add-account').open,true);
});

test('HTML contains every controller target exactly once and keeps secondary tasks behind collapsed controls',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8'),js=APP_SCRIPTS.map(name=>fs.readFileSync(__dirname+'/../../static/'+name,'utf8')).join('\n');
  const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(match=>match[1]);
  assert.equal(new Set(ids).size,ids.length,'IDs must be unique');
  for(const [,id] of js.matchAll(/\$\('([^']+)'\)/g))assert.ok(ids.includes(id),'Missing controller target '+id);
  const menu=html.match(/<details id="more-actions"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(menu,'Secondary collection actions need a More menu');assert.doesNotMatch(menu[0].split('>')[0],/\bopen\b/);
  for(const id of ['repair-button','discover-button','offline-link','rebuild-zip','delete-button'])assert.ok(menu[1].includes(`id="${id}"`));
  assert.ok(html.indexOf('id="continue-button"')<html.indexOf('id="more-actions"'));
  assert.match(html,/<button id="continue-button" class="primary"[^>]*>继续查找与下载<\/button>/);
  assert.match(html,/<a id="zip-link" class="secondary"/);
  assert.match(html,/<details id="advanced-settings"[^>]*><summary>高级设置<\/summary>/);
  assert.doesNotMatch(html.match(/<details id="advanced-settings"[^>]*>/)[0],/\bopen\b/);
  const technical=html.match(/<details id="job-technical"[^>]*>([\s\S]*?)<\/details>/);
  for(const id of ['job-detail','job-progress','job-logs'])assert.ok(technical[1].includes(`id="${id}"`));
  assert.ok(html.indexOf('id="job-outcome"')<html.indexOf('id="job-technical"'));
  assert.doesNotMatch(html,/补全已知媒体|扩展查找帖子|自动补全/);
});

test('index page loads the shared core first, then panels, then the controller',()=>{
  const html=fs.readFileSync(__dirname+'/../../static/index.html','utf8');
  const scripts=[...html.matchAll(/<script src="\/static\/([^"]+)" defer><\/script>/g)].map(match=>match[1]);
  assert.deepEqual(scripts.filter(name=>name.startsWith('app')),APP_SCRIPTS);
  for(const name of ['history.js','storage.js'])assert.ok(scripts.indexOf(name)<scripts.indexOf('app.js'));
  for(const name of APP_SCRIPTS)assert.match(fs.readFileSync(__dirname+'/../../static/'+name,'utf8'),/^\/\*[^\r\n]*\*\/\r?\n\(\(\) => \{\r?\n  'use strict';/);
});
test('native progress reports only current-step completion using CSP-compatible attributes',async()=>{
  const app=createApp();await settle();
  for(const stage of ['pages','media','sources']){
    app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage,completed:10,total:64};
    await app.tick();
    assert.equal(app.get('activity-track').hidden,false);
    assert.equal(app.get('activity').getAttribute('max'),'100');
    assert.equal(app.get('activity').getAttribute('value'),'15.625');
    assert.equal(app.get('activity').classList.contains('moving'),false);
    assert.equal(app.get('activity').getAttribute('style'),null);
  }
  app.server.job={...app.server.job,completed:100,total:64};await app.tick();
  assert.equal(app.get('activity').getAttribute('value'),'100');
});

test('unknown active work stays indeterminate and elapsed budget is never presented as work completion',async()=>{
  const app=createApp();await settle();
  for(const stage of ['index','repair','between_batches','export','zip']){
    app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage,completed:20,total:20,
      batch:{elapsed_seconds:600,max_seconds:600}};
    await app.tick();
    assert.equal(app.get('activity-track').hidden,false);
    assert.equal(app.get('activity').getAttribute('value'),null);
    assert.equal(app.get('activity').classList.contains('moving'),true);
  }
  app.server.job={...app.server.job,stage:'media',total:0};await app.tick();
  assert.equal(app.get('activity').getAttribute('value'),null);
});

test('halted or disconnected tasks hide progress rather than imply overall completion',async()=>{
  let offline=false;const app=createApp({handler:()=>{if(offline)throw new TypeError('offline');}});await settle();
  for(const status of ['idle','paused','partial','complete','failed']){
    app.server.job={...idleJob(),username:'alice',status,completed:100,total:100,
      automatic:{enabled:true,stop_code:'no_progress'}};
    await app.tick();
    assert.equal(app.get('activity-track').hidden,true);
    assert.equal(app.get('activity').getAttribute('value'),null);
    assert.equal(app.get('activity').classList.contains('moving'),false);
  }
  app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'media',completed:5,total:100};
  await app.tick();assert.equal(app.get('activity-track').hidden,false);
  offline=true;await app.tick();assert.equal(app.get('activity-track').hidden,true);
  offline=false;await app.tick();assert.equal(app.get('activity-track').hidden,false);
});


test('history names remain in More and show verified evidence without starting a job',async()=>{
  const app=createApp({capabilities:['account_identity'],handler:call=>call.path==='/api/identity/alice'?response({status:'verified',user_id:'123',aliases:[{username:'oldalice',state:'verified',evidence:[{uid:'123',timestamp:'20200101',source:'https://twitter.com/oldalice'}]}]}):undefined});
  await settle();assert.equal(app.get('identity-button').hidden,false);
  await app.get('identity-button').fire('click');
  assert.equal(app.get('identity-dialog').open,true);
  assert.match(app.get('identity-summary').textContent,/123/);
  assert.match(app.get('identity-list').textContent,/oldalice/);
  assert.match(app.get('identity-list').textContent,/已核对/);
  assert.equal(app.posts().length,0);
});

test('history scan targets the opened account and is explicitly one local scan',async()=>{
  const app=createApp({preferred:'bob',capabilities:['account_identity'],handler:call=>call.path==='/api/identity/bob'?response({status:'unknown',aliases:[]}):undefined});
  await settle();await app.get('identity-button').fire('click');await app.get('identity-scan').fire('click');
  assert.equal(app.posts().length,1);assert.equal(app.posts()[0].body.username,'bob');
  assert.equal(app.posts()[0].body.mode,'identity');assert.equal(app.posts()[0].body.auto_continue,false);
  assert.equal(app.get('identity-dialog').open,false);
});

test('recycled old name explains that each post needs matching author evidence',async()=>{
  const app=createApp({capabilities:['account_identity'],handler:call=>call.path==='/api/identity/alice'?response({status:'verified',user_id:'123',aliases:[{username:'oldalice',state:'reused',user_ids:['123','999'],evidence:[]}]}):undefined});
  await settle();await app.get('identity-button').fire('click');
  assert.match(app.get('identity-list').textContent,/曾被其他账号使用/);
  assert.match(app.get('identity-list').textContent,/逐条核对作者/);
  assert.equal(app.posts().length,0);
});

test('automatic history search explains progress and public profile proofs open the actual source',async()=>{
  const source='https://api.fxtwitter.com/2/profile/id:123';
  const app=createApp({capabilities:['account_identity','auto_alias_search'],handler:call=>call.path==='/api/identity/alice'?response({status:'verified',user_id:'123',search:{status:'searching',checked:8,errors:1},aliases:[{username:'oldalice',state:'verified',evidence:[{uid:'123',timestamp:'20261002000000',source,kind:'current_profile'}]}]}):undefined});
  await settle();await app.get('identity-button').fire('click');
  assert.match(app.get('identity-search-status').textContent,/已保存.*待查/);
  assert.match(app.get('identity-search-status').textContent,/部分来源暂不可用/);
  const proof=app.get('identity-list').children[0].children.find(n=>n.tagName==='a');
  assert.equal(proof.href,source);assert.match(proof.textContent,/当前公开资料/);
  assert.equal(app.posts().length,0);
});

test('a missing automatic name is reported honestly and manual addition remains optional',async()=>{
  const app=createApp({capabilities:['account_identity'],handler:call=>call.path==='/api/identity/alice'?response({status:'verified',user_id:'123',search:{status:'not_found'},aliases:[]}):undefined});
  await settle();await app.get('identity-button').fire('click');
  assert.match(app.get('identity-search-status').textContent,/未提供可核实/);
  assert.equal(app.posts().length,0);
  const html=fs.readFileSync('static/index.html','utf8');
  assert.match(html,/可选：补充额外的旧名线索/);
});

test('untrusted public identity proof URLs cannot become clickable links',async()=>{
  const app=createApp({capabilities:['account_identity'],handler:call=>call.path==='/api/identity/alice'?response({status:'verified',user_id:'123',aliases:[{username:'oldalice',state:'verified',evidence:[{uid:'123',source:'javascript:alert(1)',kind:'current_profile'}]}]}):undefined});
  await settle();await app.get('identity-button').fire('click');
  assert.equal(app.get('identity-list').children[0].children.filter(n=>n.tagName==='a').length,0);
});

test('local reuse is a subset of the displayed automatic file total and has step progress',async()=>{
  const app=createApp({capabilities:newCapabilities});await settle();
  app.server.job={...idleJob(),username:'alice',running:true,status:'running',stage:'local_aliases',completed:4,total:10,
    batch:{new_files:2,reused_files:1},automatic:{enabled:true,new_files:9,reused_files:5,current_batch:2}};
  await app.tick();
  assert.equal(app.get('job-outcome').textContent,'本次已新增 9 个媒体文件（其中 5 个复用自本地旧合集）');
  assert.equal(app.get('activity').getAttribute('value'),'40');
  assert.match(app.get('job-progress').textContent,/4 \/ 10/);
  app.server.job={...app.server.job,running:false,automatic:{enabled:true,new_files:9,reused_files:5}};
  await app.tick();
  assert.equal(app.get('job-outcome').textContent,'本次新增 9 个媒体文件（其中 5 个复用自本地旧合集）');
});

test('manual old name is a candidate and modifications wait for a running job',async()=>{
  const app=createApp({capabilities:['account_identity'],handler:call=>call.path==='/api/identity/alice'?response({status:'unknown',aliases:[]}):undefined});
  await settle();await app.get('identity-button').fire('click');app.get('identity-input').value='@oldalice';
  await app.get('identity-form').fire('submit');
  assert.equal(app.posts()[0].path,'/api/identity');assert.equal(app.posts()[0].body.state,'candidate');
  assert.match(app.get('identity-message').textContent,/待核对/);
  app.server.job={...idleJob(),running:true,username:'alice'};await app.tick();
  await app.get('identity-form').fire('submit');assert.equal(app.posts().length,1);
});

test('verified names are visible beside the collection and in the library without opening More',async()=>{
  const app=createApp({handler:(call,server)=>{
    if(call.path==='/api/collections'||call.path==='/api/collection/alice')server.manifests.get('alice').identity={status:'verified',user_id:'123',aliases:[
      {username:'oldalice',state:'verified'},{username:'candidate',state:'candidate'},{username:'other',state:'excluded'}]};
  }});
  await settle();
  assert.equal(app.get('collection-identity').hidden,false);
  assert.match(app.get('collection-identity').textContent,/同一账号.*@oldalice/);
  assert.doesNotMatch(app.get('collection-identity').textContent,/candidate|other/);
  assert.match(app.get('library').children[0].textContent,/同一账号.*@oldalice/);
  assert.equal(app.posts().length,0);
  await app.clickUser('bob');assert.equal(app.get('collection-identity').hidden,true);
});

test('association text updates even if the collection date and gallery content did not change',async()=>{
  const app=createApp();await settle();const shown=app.shown.length;
  app.server.manifests.get('alice').identity={status:'verified',user_id:'123',aliases:[{username:'oldalice',state:'reused'}]};
  await app.get('refresh-library').fire('click');
  assert.equal(app.shown.length,shown);
  assert.match(app.get('collection-identity').textContent,/@oldalice（逐条核对作者）/);
  app.server.manifests.get('alice').identity.status='conflict';
  await app.get('refresh-library').fire('click');assert.equal(app.get('collection-identity').hidden,true);
});

test('reopening keeps the selected current-name collection instead of the last finished old-name job',async()=>{
  const app=createApp({preferred:'bob',handler:(call,server)=>{
    if(call.path==='/api/state')server.job={...idleJob(),username:'alice',status:'paused'};
  }});
  await settle();assert.equal(app.shown.at(-1).data.username,'bob');
  assert.equal(app.get('username').value,'bob');
});

const taskSummary=(state,reason='')=>({schema_version:1,state,reason,
  label:{running:'正在查找',finalizing:'正在保存与打包',paused:'已暂停，等待继续',finished:'本轮已结束',failed:'ZIP 生成失败',unknown:'已停止，结束原因未记录'}[state],
  tone:state==='finished'?'warning':state==='failed'?'danger':'info',result_label:'部分恢复',
  reason_text:reason==='no_progress'?'连续 3 批没有新增内容或解析进展，已自动结束。':'已保存进度',
  recommended_action:state==='paused'?'continue':reason==='zip_failed'?'rebuild_zip':'browse',
  finished_at:'2026-10-02T10:00:00Z',alias_discovery:{state:'partial',verified_aliases:['oldalice'],checked_items:15,pending_items:0,failed_items:2}});

test('new status shows lifecycle, lifetime coverage, alias failures and correct actions',async()=>{
  const app=createApp();await settle();
  app.server.job={running:false,status:'paused',stage:'done',username:'alice',counts:{posts:20,media:8,saved:3},task_summary:taskSummary('finished','no_progress'),logs:[]};
  app.server.manifests.get('alice').task_summary=taskSummary('finished','no_progress');
  await app.get('refresh-library').fire('click');
  assert.match(app.get('job-status').textContent,/本轮已结束/);
  assert.match(app.get('job-result').textContent,/20 条帖子.*3 条文件记录.*5 条链接记录/);
  assert.match(app.get('job-aliases').textContent,/@oldalice.*15 项.*2 项查询失败/);
  assert.match(app.get('job-reason').textContent,/3 批/);
  assert.equal(app.get('job-resume').hidden,true);assert.equal(app.get('job-actions').hidden,false);
  assert.match(app.get('library').children[0].textContent,/本轮已结束/);
  assert.equal(app.get('continue-button').textContent,'重新查找与下载');
});

test('job continuation targets its own account when a different collection is selected',async()=>{
  const app=createApp({preferred:'bob'});await settle();
  app.server.job={running:false,status:'paused',stage:'done',username:'alice',task_summary:taskSummary('paused','user_pause'),logs:[]};
  await app.tick();assert.equal(app.get('username').value,'bob');
  assert.equal(app.get('job-resume').hidden,false);
  await app.get('job-resume').fire('click');
  assert.equal(app.posts()[0].path,'/api/start');assert.equal(app.posts()[0].body.username,'alice');
});

test('ZIP failure offers export repair instead of repeating collection',async()=>{
  const app=createApp();await settle();
  app.server.job={running:false,status:'failed',stage:'done',username:'alice',task_summary:taskSummary('failed','zip_failed'),logs:[]};
  await app.tick();assert.equal(app.get('job-rebuild').hidden,false);assert.equal(app.get('job-resume').hidden,true);
  await app.get('job-rebuild').fire('click');
  assert.equal(app.posts()[0].path,'/api/zip');assert.equal(app.posts()[0].body.username,'alice');
});

test('disconnect does not mislabel a running task as paused or finished',async()=>{
  let online=true;
  const app=createApp({handler:call=>{if(!online&&call.path==='/api/state')throw new Error('offline');}});await settle();
  app.server.job={running:true,status:'running',stage:'pages',username:'alice',task_summary:taskSummary('running'),logs:[]};
  await app.tick();assert.match(app.get('job-status').textContent,/正在查找/);
  online=false;await app.tick();assert.match(app.get('job-status').textContent,/状态暂无法确认/);
  assert.equal(app.get('job-resume').disabled,true);
  online=true;await app.tick();assert.match(app.get('job-status').textContent,/正在查找/);
  assert.equal(app.posts().length,0);
});

test('status changes refresh the gallery even without a changed collection date',async()=>{
  const app=createApp();await settle();const m=app.server.manifests.get('alice');
  m.task_summary=taskSummary('paused','user_pause');await app.get('refresh-library').fire('click');
  const calls=app.shown.length;m.task_summary=taskSummary('finished','no_progress');
  await app.get('refresh-library').fire('click');assert.equal(app.shown.length,calls+1);
  assert.equal(app.shown.at(-1).data.task_summary.state,'finished');
});

test('automatic alias search result is visible without opening historical-name dialog',async()=>{
  const app=createApp();await settle();
  app.server.manifests.get('alice').identity={status:'verified',user_id:'123',aliases:[],search:{state:'completed',verified_aliases:[],checked_items:14,pending_items:0,failed_items:0}};
  await app.get('refresh-library').fire('click');
  assert.equal(app.get('collection-alias-status').hidden,false);
  assert.match(app.get('collection-alias-status').textContent,/本轮未找到可核实.*14 项/);
  assert.equal(app.posts().length,0);
});

test('current verified aliases supersede old search names and excluded aliases stay excluded',async()=>{
  const app=createApp();await settle();const m=app.server.manifests.get('alice');
  m.identity={status:'verified',user_id:'123',aliases:[{username:'recentalice',state:'verified'},{username:'oldalice',state:'excluded'}],
    search:{state:'completed',verified_aliases:['oldalice'],checked_items:14}};
  await app.get('refresh-library').fire('click');
  const note=app.get('collection-alias-status').textContent;
  assert.match(note,/搜索完成.*@recentalice/);assert.doesNotMatch(note,/@oldalice|未找到/);
});

test('terminal action buttons recover after an action and a connection loss',async()=>{
  let online=true;
  const app=createApp({handler:call=>{if(!online&&call.path==='/api/state')throw new Error('offline');}});await settle();
  for(const [id,state,reason] of [['job-resume','paused','user_pause'],['job-rebuild','failed','zip_failed']]){
    app.server.job={running:false,status:state,stage:'done',username:'alice',task_summary:taskSummary(state,reason),logs:[]};
    await app.tick();assert.equal(app.get(id).disabled,false);
    await app.get(id).fire('click');assert.equal(app.get(id).disabled,false,'operation must release '+id);
    online=false;await app.tick();assert.equal(app.get(id).disabled,true);
    online=true;await app.tick();assert.equal(app.get(id).disabled,false,'reconnect must release '+id);
    app.server.job.running=true;await app.tick();assert.equal(app.get(id).disabled,true);
  }
});

test('paused alias search does not promise automatic continuation',async()=>{
  const app=createApp();await settle();
  app.server.job={running:false,status:'paused',stage:'done',username:'alice',
    task_summary:{...taskSummary('paused','user_pause'),alias_discovery:{state:'pending',pending_items:6}},logs:[]};
  await app.tick();
  assert.match(app.get('job-aliases').textContent,/已保存.*待查 6 项/);
  assert.doesNotMatch(app.get('job-aliases').textContent,/将自动|查找中/);
});

test('identity-only failures keep the actual terminal reason visible',async()=>{
  const app=createApp();await settle();
  app.server.job={running:false,status:'failed',stage:'done',mode:'identity',username:'alice',
    task_summary:{...taskSummary('failed','zip_failed'),reason_text:'采集结果已保存，ZIP 生成失败；请重新生成 ZIP。'},logs:[]};
  await app.tick();
  assert.match(app.get('job-reason').textContent,/ZIP 生成失败/);
});

test('bulk entry can append during active work and retains text on validation failure',async()=>{
  let fail=true;
  const app=createApp({capabilities:['task_queue','multi_source','auto_batches'],handler:call=>{
    if(call.path==='/api/queue/add')return fail?response({error:'第 2 项格式有误'},400):response({ok:true,added:2,duplicates:1});
  }});await settle();
  app.server.job={...idleJob(),running:true,status:'running',username:'alice'};await app.tick();
  assert.equal(app.get('batch-add').hidden,false);assert.equal(app.get('queue-add').disabled,false);
  app.get('queue-input').value='alice bob alice';
  await app.get('queue-form').fire('submit');assert.equal(app.get('queue-input').value,'alice bob alice');
  assert.match(app.get('form-error').textContent,/第 2 项/);
  fail=false;await app.get('queue-form').fire('submit');
  assert.equal(app.get('queue-input').value,'');assert.match(app.get('queue-message').textContent,/2 个账号.*1 个重复/);
  assert.equal(app.posts()[0].body.request_id,app.posts()[1].body.request_id);
  assert.equal(app.posts()[1].body.usernames,'alice bob alice');assert.equal(app.posts()[1].body.multi_source,true);
});

test('queue shows ordered items, removes only selected pending item and pauses through queue API',async()=>{
  const app=createApp({capabilities:['task_queue']});await settle();
  app.server.queue={state:'running',counts:{running:1,pending:1},items:[
    {id:'one',username:'alice',state:'running'}, {id:'two',username:'bob',state:'pending'}]};
  await app.tick();
  assert.match(app.get('queue-note').textContent,/@alice/);
  const rows=app.get('queue-items').children;assert.equal(rows[0].children.length,1);
  assert.equal(rows[1].children[1].getAttribute('aria-label'),'从队列移除 @bob');
  await rows[1].children[1].fire('click');assert.deepEqual(app.posts()[0].body,{id:'two'});
  await app.get('queue-pause').fire('click');assert.equal(app.posts()[1].path,'/api/queue/pause');
});

test('queue reconnect restores controls and restart message never promises automatic work',async()=>{
  let online=true;
  const app=createApp({capabilities:['task_queue'],handler:call=>{if(!online&&call.path==='/api/state')throw new Error('offline');}});await settle();
  app.server.queue={state:'paused',reason:'restart',counts:{paused:1,pending:1},items:[
    {id:'one',username:'alice',state:'paused'}, {id:'two',username:'bob',state:'pending'}]};
  await app.tick();assert.match(app.get('queue-note').textContent,/不会在启动工具时自动采集/);
  assert.equal(app.get('queue-resume').hidden,false);
  online=false;await app.tick();assert.equal(app.get('queue-resume').disabled,true);
  online=true;await app.tick();assert.equal(app.get('queue-resume').disabled,false);
  await app.get('queue-resume').fire('click');assert.equal(app.posts()[0].path,'/api/queue/resume');
  assert.equal(app.get('queue-resume').disabled,false);
});

test('old backend hides batch queue entry and cannot receive queue requests',async()=>{
  const app=createApp();await settle();assert.equal(app.get('batch-add').hidden,true);
  app.get('queue-input').value='alice bob';await app.get('queue-form').fire('submit');
  assert.equal(app.posts().length,0);
});

test('Instagram form sends platform and optional post URLs, with old-name UI disabled for IG collections',async()=>{
  const app=createApp({users:['instagram--alice'],capabilities:['instagram','account_identity','task_queue','post_discovery']});await settle();
  assert.equal(app.get('username').value,'alice');assert.equal(app.get('platform').value,'instagram');
  assert.equal(app.get('identity-button').hidden,true);assert.equal(app.get('instagram-seeds').hidden,false);
  app.get('username').value='example_creator';app.get('instagram-links').value='https://instagram.com/reel/ABC/';
  await app.get('collect-form').fire('submit');
  assert.equal(app.posts()[0].body.platform,'instagram');assert.equal(app.posts()[0].body.username,'example_creator');
  assert.equal(app.posts()[0].body.instagram_links,'https://instagram.com/reel/ABC/');
});

test('mixed-platform queue and collection labels remain distinct for an identical username',async()=>{
  const app=createApp({users:['alice','instagram--alice'],capabilities:['instagram','task_queue','account_identity']});await settle();
  app.server.queue={state:'paused',counts:{pending:2},items:[{id:'one',username:'alice',state:'pending'},{id:'two',username:'instagram--alice',state:'pending'}]};
  await app.intervals[0]();await settle();
  assert.match(app.get('queue-items').textContent,/Instagram @alice/);
  await app.clickUser('instagram--alice');assert.equal(app.get('identity-button').hidden,true);
  await app.get('continue-button').fire('click');assert.equal(app.posts()[0].body.username,'instagram--alice');
  assert.equal(app.posts()[0].body.platform,undefined); // selected key, independent of new-input selector
  await app.clickUser('alice');assert.equal(app.get('identity-button').hidden,false);
});

test('Instagram bulk input specifies its platform and never copies single-account post links',async()=>{
  const app=createApp({users:[],capabilities:['instagram','task_queue']});await settle();
  app.get('platform').value='instagram';await app.get('platform').fire('change');
  app.get('queue-input').value='alice\nbob.name';app.get('instagram-links').value='https://instagram.com/p/ABC/';
  await app.get('queue-form').fire('submit');const sent=app.posts()[0].body;
  assert.equal(sent.platform,'instagram');assert.equal(sent.usernames,'alice\nbob.name');assert.equal(sent.instagram_links,undefined);
  assert.match(app.get('queue-input-label').textContent,/Instagram/);
});

test('old backend keeps Instagram option unavailable',async()=>{
  const app=createApp({capabilities:['task_queue']});await settle();
  assert.equal(app.get('platform').querySelector('option[value=instagram]').disabled,true);
});

const historyEntry=(id='old-run',username='alice',state='paused')=>({id,username,platform:'x',display_username:username,state,label:state==='paused'?'已暂停':'失败',mode:'full',started_at:'2026-01-01T00:00:00Z',counts:{posts:2,saved:1},has_collection:true,origin:'recorded',aliases:[],result_label:'部分恢复'});
const historyResult=(items,extra={})=>response({items,total:items.length,offset:0,note:'旧版记录仅补录可核实的信息',...extra});
const historyRow=app=>app.get('history-items').children[0];
const historyButtons=app=>historyRow(app).children.at(-1).children;

test('history stays lazy and exposes a clear entry only with backend capability',async()=>{
  const app=createApp({capabilities:['task_history'],handler:c=>c.path.startsWith('/api/history?')?historyResult([historyEntry()]):undefined});
  await settle();assert.equal(app.get('history-open').hidden,false);
  assert.equal(app.calls.filter(c=>c.path.startsWith('/api/history?')).length,0);
  await app.get('history-open').fire('click');
  assert.equal(app.get('history-panel').hidden,false);assert.match(historyRow(app).textContent,/alice/);
  await app.get('history-close').fire('click');assert.equal(app.get('history-panel').hidden,true);
});

test('history retry uses the stored record ID and cannot double submit',async()=>{
  const pending=deferred();
  const app=createApp({capabilities:['task_history'],handler:(c,s)=>{
    if(c.path.startsWith('/api/history?'))return historyResult([historyEntry()]);
    if(c.path==='/api/history/retry')return pending.promise;
  }});
  await settle();await app.get('history-open').fire('click');
  const first=historyButtons(app)[1].fire('click');await settle();
  await historyButtons(app)[1].fire('click');assert.equal(app.posts().length,1);
  assert.equal(app.posts()[0].body.id,'old-run');
  pending.resolve(response({ok:true,username:'alice'}));await first;
  assert.equal(app.shown.at(-1).data.username,'alice');
});

test('history filters ignore a late response for an older username',async()=>{
  const old=deferred();let hold=true;
  const app=createApp({capabilities:['task_history'],handler:c=>{
    if(c.path.startsWith('/api/history?')){if(hold)return old.promise;return historyResult([historyEntry('bob-run','bob')]);}
  }});
  await settle();const opening=app.get('history-open').fire('click');await settle();
  hold=false;app.get('history-query').value='bob';await app.get('history-query').fire('input');
  const timers=[...app.timeouts.values()];app.timeouts.clear();for(const fn of timers)fn();await settle();
  assert.match(historyRow(app).textContent,/bob/);
  old.resolve(historyResult([historyEntry()]));await opening;
  assert.match(historyRow(app).textContent,/bob/);assert.doesNotMatch(historyRow(app).textContent,/alice/);
});

test('history supports state and platform filtering with bounded pagination',async()=>{
  const app=createApp({capabilities:['task_history'],handler:c=>c.path.startsWith('/api/history?')?historyResult([historyEntry()],{total:40,offset:c.path.includes('offset=20')?20:0}):undefined});
  await settle();await app.get('history-open').fire('click');
  app.get('history-state').value='unfinished';app.get('history-platform').value='instagram';await app.get('history-state').fire('change');
  assert.match(app.calls.at(-1).path,/state=unfinished.*platform=instagram.*offset=0/);
  await app.get('history-next').fire('click');assert.match(app.calls.at(-1).path,/offset=20&limit=20/);
  await app.get('history-prev').fire('click');assert.match(app.calls.at(-1).path,/offset=0&limit=20/);
});

test('running tasks and lost backend connections disable historical retry',async()=>{
  let online=true;
  const app=createApp({capabilities:['task_history'],handler:c=>{
    if(!online)throw new Error('offline');
    if(c.path.startsWith('/api/history?'))return historyResult([historyEntry()]);
  }});
  await settle();await app.get('history-open').fire('click');
  app.server.job={...idleJob(),username:'bob',running:true,status:'running'};await app.tick();
  assert.equal(historyButtons(app)[1].disabled,true);assert.equal(app.get('history-running-note').hidden,false);
  app.server.job=idleJob();await app.tick();assert.equal(historyButtons(app)[1].disabled,false);
  online=false;await app.tick();assert.equal(historyButtons(app)[1].disabled,true);
  await historyButtons(app)[1].fire('click');assert.equal(app.posts().length,0);
});

test('history read failure stays local and can be retried without losing gallery state',async()=>{
  let fail=true;
  const app=createApp({capabilities:['task_history'],handler:c=>{
    if(c.path.startsWith('/api/history?')){if(fail)return response({error:'history unavailable'},503);return historyResult([historyEntry()]);}
  }});
  await settle();await app.get('history-open').fire('click');
  assert.match(app.get('history-error').textContent,/history unavailable/);assert.equal(app.get('connection-panel').hidden,true);
  fail=false;await app.get('history-refresh').fire('click');assert.equal(app.get('history-error').hidden,true);
  await historyButtons(app)[0].fire('click');assert.equal(app.shown.at(-1).data.username,'alice');
});

test('history preserves failed records without a collection and renders text safely',async()=>{
  const item={...historyEntry('failed-run','<img onerror=attack>','failed'),has_collection:false,origin:'checkpoint',message:'<script>bad</script>'};
  const app=createApp({capabilities:['task_history'],handler:c=>c.path.startsWith('/api/history?')?historyResult([item]):undefined});
  await settle();await app.get('history-open').fire('click');
  assert.equal(historyButtons(app)[0].disabled,true);assert.equal(historyButtons(app)[1].textContent,'重试此任务');
  assert.match(historyRow(app).textContent,/<script>bad<\/script>/);assert.match(historyRow(app).textContent,/旧版记录补录/);
});

test('history retry with an uncertain response is never automatically repeated',async()=>{
  const app=createApp({capabilities:['task_history'],handler:c=>{
    if(c.path.startsWith('/api/history?'))return historyResult([historyEntry()]);
    if(c.path==='/api/history/retry')throw new Error('response lost');
  }});
  await settle();await app.get('history-open').fire('click');await historyButtons(app)[1].fire('click');await app.tick();
  assert.equal(app.posts().length,1);assert.match(app.get('history-error').textContent,/response lost/);
});
