/* DOM contract tests with Node's built-in test runner; no browser or external packages. */
'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.parentNode=null;this.listeners={};this.attributes={};this.value='';this.detaches=0;}
  get firstChild(){return this.children[0]||null;}
  get nextSibling(){if(!this.parentNode)return null;return this.parentNode.children[this.parentNode.children.indexOf(this)+1]||null;}
  get options(){return this.children;}
  set textContent(value){this._text=String(value);this.replaceChildren();}
  get textContent(){return (this._text||'')+this.children.map(c=>c.textContent).join('');}
  append(...nodes){for(const node of nodes)this.insertBefore(node,null);}
  add(node){this.append(node);}
  insertBefore(node,cursor){if(node.parentNode)node.remove();const index=cursor?this.children.indexOf(cursor):this.children.length;assert.ok(index>=0);this.children.splice(index,0,node);node.parentNode=this;}
  remove(){if(!this.parentNode)return;const parent=this.parentNode;parent.children.splice(parent.children.indexOf(this),1);this.parentNode=null;this.detaches++;}
  replaceChildren(...nodes){for(const child of [...this.children])child.remove();this.append(...nodes);}
  setAttribute(key,value){this.attributes[key]=value;}
  removeAttribute(key){delete this.attributes[key];}
  addEventListener(type,fn){(this.listeners[type]??=[]).push(fn);}
  fire(type,values={}){const event={target:this,currentTarget:this,preventDefault(){this.defaultPrevented=true;},...values};for(const fn of this.listeners[type]||[])fn(event);return event;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
function createGallery(storage=new Map(),href='http://127.0.0.1:8765/'){
  const elements=new Map();
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  for(const id of ['type-filter','month-filter'])get(id).value='all';get('sort').value='desc';
  const context={document:{getElementById:get,createElement:tag=>new Element(tag)},window:{},location:{href},URL,
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    Option:function(text,value){const node=new Element('option');node.textContent=text;node.value=value;return node;}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/../../static/gallery.js','utf8'),context);
  return {show:context.window.Gallery.show,get,storage};
}
function manifest(username='alice', count=85){
  const posts=Array.from({length:count},(_,i)=>({id:String(1000-i),username,text:'post '+i,created_at:'2021-01-06T20:00:00Z',date_source:'page',captures:[],media:[{kind:'video',status:'saved',path:`media/${i}.mp4`}]}));
  return {username,posts,counts:{posts:count,saved:count},status:'partial',scope_note:'test'};
}
const articles=g=>g.get('timeline').children.filter(n=>n.tagName==='article');
const settle=()=>new Promise(resolve=>setImmediate(resolve));

test('text origin changes rebuild card and explain unresolved page states',()=>{
  const g=createGallery(),data=manifest('alice',1);g.show(data);
  data.posts[0].text_source='public_metadata';data.posts[0].text_provenance={source:'syndication'};
  data.recovery_report={text:{page_not_read:2,needs_parsing_or_other_snapshot:3,no_capture:1}};
  g.show(data);
  assert.match(articles(g)[0].textContent,/正文来自当前公开帖子资料/);
  assert.match(g.get('recovery-summary').textContent,/页面尚未成功读取 2 条/);
  assert.match(g.get('recovery-summary').textContent,/待解析或换快照 3 条/);
});

test('online pages append without resetting an existing playing video',async()=>{
  const g=createGallery(),all=manifest(),calls=[];
  const summary={...all,posts:[],months:['2021-01'],content_revision:'one'};
  const loader=async params=>{calls.push(params);return {posts:all.posts.slice(Number(params.offset),Number(params.offset)+Number(params.limit)),total:85,has_more:Number(params.offset)+Number(params.limit)<85};};
  g.show(summary,'/collections/alice/',loader);await settle();
  assert.equal(articles(g).length,40);const first=articles(g)[0],video=find(first,'video');video.currentTime=42;
  g.get('load-more').fire('click');await settle();
  assert.equal(articles(g).length,80);assert.equal(articles(g)[0],first);assert.equal(video.currentTime,42);assert.equal(calls[1].offset,40);
  g.show({...summary,status:'complete'},'/collections/alice/',loader);await settle();
  assert.equal(calls.length,2);assert.equal(articles(g).length,80);
  g.show({...summary,content_revision:'two'},'/collections/alice/',loader);await settle();
  assert.equal(articles(g).length,80);assert.equal(articles(g)[0],first);
});

test('online filtering asks the server and ignores out of order results',async()=>{
  const g=createGallery(),all=manifest(),pending=[];
  const loader=params=>new Promise(resolve=>pending.push({params,resolve}));
  g.show({...all,posts:[],content_revision:'one'},'',loader);
  g.get('search').value='post 70';g.get('search').fire('input');
  assert.equal(pending[1].params.search,'post 70');assert.equal(pending[1].params.offset,0);
  pending[1].resolve({posts:[all.posts[70]],total:1,has_more:false});await settle();
  pending[0].resolve({posts:all.posts.slice(0,40),total:85,has_more:true});await settle();
  assert.equal(articles(g).length,1);assert.match(articles(g)[0].textContent,/post 70/);
});

test('online failure offers retry and another account discards an old response',async()=>{
  const g=createGallery(),all=manifest();let fail=true;
  g.show({...all,posts:[],content_revision:'one'},'',async()=>{if(fail)throw Error('offline');return {posts:[all.posts[0]],total:1,has_more:false};});await settle();
  assert.match(g.get('result-count').textContent,/载入失败/);assert.equal(g.get('load-more').hidden,false);
  fail=false;g.get('load-more').fire('click');await settle();assert.equal(articles(g).length,1);
  let resolve;g.show({...all,posts:[],content_revision:'two'},'',()=>new Promise(done=>{resolve=done;}));
  g.show(manifest('bob',1));resolve({posts:all.posts,total:85,has_more:false});await settle();
  assert.equal(g.get('collection-title').textContent,'X @bob');assert.equal(articles(g).length,1);
});

test('a slow new account never shows the previous account media under its title',async()=>{
  const g=createGallery(),alice=manifest('alice',1),bob=manifest('bob',1);
  g.show(alice);assert.equal(articles(g).length,1);
  let resolve;g.show({...bob,posts:[],content_revision:'bob-one'},'/collections/bob/',()=>new Promise(done=>{resolve=done;}));
  assert.equal(g.get('collection-title').textContent,'X @bob');assert.equal(articles(g).length,0);
  resolve({posts:bob.posts,total:1,has_more:false});await settle();assert.equal(articles(g).length,1);
});
function find(node,tag){if(node.tagName===tag)return node;for(const child of node.children){const result=find(child,tag);if(result)return result;}return null;}

test('load more leaves already visible video cards attached',()=>{
  const g=createGallery();g.show(manifest());const first=articles(g)[0],video=find(first,'video');video.currentTime=42;
  assert.equal(articles(g).length,40);g.get('load-more').fire('click');
  assert.equal(articles(g).length,80);assert.equal(articles(g)[0],first);assert.equal(first.detaches,0);assert.equal(find(first,'video'),video);assert.equal(video.currentTime,42);
});
test('polling and updates to another post preserve the playing card',()=>{
  const g=createGallery(),data=manifest();g.show(data);const first=articles(g)[0],second=articles(g)[1];
  const next=structuredClone(data);next.status='complete';next.posts[1].text='recovered extra text';g.show(next);
  assert.equal(articles(g)[0],first);assert.equal(first.detaches,0);assert.notEqual(articles(g)[1],second);assert.match(articles(g)[1].textContent,/recovered extra text/);
});
test('inserting a newer post does not detach existing cards',()=>{
  const g=createGallery(),data=manifest();g.show(data);const first=articles(g)[0];
  const next=structuredClone(data);next.posts.unshift({...next.posts[0],id:'2000'});g.show(next);
  assert.equal(articles(g)[1],first);assert.equal(first.detaches,0);
});
test('filters and loaded count survive reload and are scoped to each account',()=>{
  const g=createGallery();g.show(manifest());g.get('search').value='post';g.get('search').fire('input');g.get('load-more').fire('click');
  const restored=createGallery(g.storage);restored.show(manifest());assert.equal(restored.get('search').value,'post');assert.equal(articles(restored).length,80);
  restored.show(manifest('bob'));assert.equal(restored.get('search').value,'');assert.equal(articles(restored).length,40);
  restored.show(manifest());assert.equal(restored.get('search').value,'post');assert.equal(articles(restored).length,80);
});

function imageManifest(count=45){const data=manifest('alice',count);for(const p of data.posts)p.media=[{kind:'image',status:'saved',path:`media/${p.id}.jpg`}];return data;}
function findClass(root,name){if(root.className===name)return root;for(const child of root.children){const result=findClass(child,name);if(result)return result;}return null;}
function findAll(root,tag){return [...(root.tagName===tag?[root]:[]),...root.children.flatMap(child=>findAll(child,tag))];}
function videoVariant(id,size,path,extra={}) {
  return {kind:'video',status:'saved',path:'media/'+path,key:`video.twimg.com/ext_tw_video/${id}/pu/vid/avc1/${size}/clip.mp4`,...extra};
}

test('identical fallback files appear once per video without removing original records',()=>{
  const g=createGallery(),data=manifest('alice',1);
  data.posts[0].media=[videoVariant('123','640x360','first.mp4',{sha256:'a'.repeat(64),bytes:512}),
    videoVariant('123','1280x720','fallback.mp4',{sha256:'A'.repeat(64),bytes:512})];
  const before=structuredClone(data);g.show(data);
  assert.equal(findAll(articles(g)[0],'video').length,1);
  assert.equal(find(articles(g)[0],'select'),null);
  assert.deepEqual(data,before);
});

test('missing or invalid hashes and different sizes do not hide video versions',()=>{
  for(const other of [{sha256:'b'.repeat(64),bytes:512},{sha256:'a'.repeat(64),bytes:1024},{bytes:512},{sha256:'invalid',bytes:512}]) {
    const g=createGallery(),data=manifest('alice',1);
    data.posts[0].media=[videoVariant('123','640x360','one.mp4',{sha256:'a'.repeat(64),bytes:512}),videoVariant('123','1280x720','two.mp4',other)];
    g.show(data);assert.equal(find(articles(g)[0],'select').options.length,2);
  }
});

test('whole-collection inventory is not recalculated from the first page',async()=>{
  const g=createGallery(),all=manifest('alice',1),summary={...all,posts:[],content_revision:'inventory',
    media_inventory:{schema:1,video_saved:48,video_missing:31,image_saved:3,image_records:7,cover_saved:40,cover_records:79,
      groups_with_missing_variants:48,duplicate_saved_records:50,video_unidentified:2,video_identity_conflicts:1}};
  g.show(summary,'',async()=>({posts:all.posts,total:1,has_more:false}));await settle();
  const text=g.get('media-inventory').textContent;
  assert.match(text,/已保存 48 组 · 未保存 31 组/);assert.match(text,/视频封面：40 \/ 79/);
  assert.match(text,/50 条已存记录的文件摘要和大小相同/);assert.match(text,/其中 1 条存在 ID 冲突/);
  g.get('search').value='nothing';g.get('search').fire('input');await settle();
  assert.equal(g.get('media-inventory').textContent,text);
});

test('legacy manifests hide unknown inventory rather than claim zero media',()=>{
  const g=createGallery(),data=manifest('alice',1);
  g.show({...data,media_inventory:{schema:1,video_saved:1}});assert.equal(g.get('media-inventory').hidden,false);
  g.show(manifest('bob',1));assert.equal(g.get('media-inventory').hidden,true);assert.equal(g.get('media-inventory').textContent,'');
});

test('same video variants use one player and highest saved pixels, leaving missing candidates visible',()=>{
  const g=createGallery(),data=manifest('alice',1);
  data.posts[0].media=[videoVariant('123','320x180','small.mp4'),videoVariant('123','1280x720','large.mp4'),videoVariant('123','1920x1080','missing.mp4',{status:'pending'}),videoVariant('123','3840x2160','invalid.mp4',{path:'media/../invalid.mp4'})];
  const original=structuredClone(data);g.show(data);
  const article=articles(g)[0],select=find(article,'select');
  assert.equal(findAll(article,'video').length,1);assert.equal(find(article,'video').src,'media/large.mp4');
  assert.equal(select.options.length,2);assert.match(select.options[0].textContent,/1280 × 720/);
  assert.equal(select.attributes['aria-label'],'选择已保存的视频版本');
  assert.match(article.textContent,/已保存 2 个版本/);assert.match(article.textContent,/2 条媒体链接未保存/);
  assert.match(article.textContent,/不代表整段视频未恢复/);
  assert.deepEqual(data,original,'grouping must not remove or reorder stored media');
});

test('switching saved video variants updates local playback, download and source online and offline',()=>{
  for(const [href,prefix] of [['http://127.0.0.1:8765/','/collections/alice/'],['file:///C:/collections/alice/index.html','']]) {
    const g=createGallery(new Map(),href),data=manifest('alice',1);
    data.posts[0].media=[videoVariant('123','320x180','small file.mp4',{source:'common_crawl',source_url:'https://video.twimg.com/ext_tw_video/123/pu/vid/320x180/clip.mp4'}),videoVariant('123','1280x720','large.mp4',{source:'live_cdn'})];
    g.show(data,prefix);const article=articles(g)[0],video=find(article,'video'),select=find(article,'select');
    assert.match(findClass(article,'source-tag').textContent,/原始媒体链接/);
    video.error={code:4};video.fire('error');assert.equal(video.hidden,true);
    select.value='1';select.fire('change');
    assert.equal(video.src,prefix+'media/small%20file.mp4');assert.equal(video.hidden,false);assert.equal(findClass(article,'media-error').hidden,true);
    const download=findClass(article,'media-download');
    assert.equal(download.href,new URL(prefix+'media/small%20file.mp4',href).href);assert.equal(download.download,'small file.mp4');
    assert.equal(findClass(article,'source-tag').textContent,'Common Crawl 存档');
    video.currentTime=12;g.show(structuredClone(data),prefix);
    assert.equal(find(articles(g)[0],'video'),video);assert.equal(video.currentTime,12);assert.equal(select.value,'1');
  }
});

test('video identity includes namespace and numeric ID and cannot be inferred from similar filenames',()=>{
  const g=createGallery(),data=manifest('alice',2);
  data.posts[0].media=[
    videoVariant('123','320x180','ext.mp4'),
    videoVariant('123','640x360','amplify.mp4',{key:'video.twimg.com/amplify_video/123/vid/640x360/clip.mp4'}),
    videoVariant('124','320x180','other-id.mp4'),
    videoVariant('123','320x180','spoof.mp4',{key:'https://video.twimg.com.example.org/ext_tw_video/123/vid/320x180/clip.mp4'}),
    videoVariant('123','320x180','text-id.mp4',{key:'https://video.twimg.com/ext_tw_video/not-a-number/vid/320x180/clip.mp4'}),
    videoVariant('123','320x180','conflict.mp4',{urls:['https://video.twimg.com/ext_tw_video/124/vid/320x180/clip.mp4']}),
    {kind:'video',status:'saved',path:'media/unknown.mp4'},
  ];
  data.posts[1].media=[videoVariant('123','320x180','other-post.mp4')];g.show(data);
  assert.equal(findAll(articles(g)[0],'video').length,7);assert.equal(findAll(articles(g)[1],'video').length,1);
  assert.equal(findAll(articles(g)[0],'select').length,0);
});

test('known dimensions beat unknown dimensions and variant URL changes invalidate cached cards',()=>{
  const g=createGallery(),data=manifest('alice',1);
  data.posts[0].media=[videoVariant('123','320x180','known.mp4'),{kind:'video',status:'saved',path:'media/unknown.mp4',urls:['https://video.twimg.com/ext_tw_video/123/pu/vid/clip.mp4']}];
  g.show(data);let article=articles(g)[0];
  assert.equal(find(article,'video').src,'media/known.mp4');assert.match(find(article,'select').options[1].textContent,/清晰度未标注/);
  const next=structuredClone(data);next.posts[0].media[1].urls=['https://video.twimg.com/ext_tw_video/124/pu/vid/clip.mp4'];g.show(next);
  assert.notEqual(articles(g)[0],article);assert.equal(findAll(articles(g)[0],'video').length,2);
});

test('actual saved source dimensions override higher resolution candidate URLs including Wayback fallbacks',()=>{
  const low='https://video.twimg.com/ext_tw_video/123/pu/vid/avc1/320x180/clip.mp4';
  for(const actual of [
    {source_url:low},
    {archive_url:'https://web.archive.org/web/20210906010203id_/'+low},
    {source_url:'https://web.archive.org/web/20210906010203/'+low},
    {provenance:{source_url:low}},
  ]) {
    const g=createGallery(),data=manifest('alice',1);
    data.posts[0].media=[videoVariant('123','3840x2160','fallback.mp4',{urls:['https://video.twimg.com/ext_tw_video/123/vid/1920x1080/clip.mp4'],...actual}),videoVariant('123','1280x720','actual-high.mp4')];
    g.show(data);const article=articles(g)[0],select=find(article,'select');
    assert.equal(findAll(article,'video').length,1);assert.equal(find(article,'video').src,'media/actual-high.mp4');
    assert.match(select.options[0].textContent,/1280 × 720/);assert.match(select.options[1].textContent,/320 × 180/);
    select.value='1';select.fire('change');assert.equal(find(article,'video').src,'media/fallback.mp4');
  }
});

test('source priority and absent dimensions use only available saved-source dimensions before candidates',()=>{
  const low='https://video.twimg.com/ext_tw_video/123/vid/320x180/clip.mp4';
  const high='https://video.twimg.com/ext_tw_video/123/vid/1920x1080/clip.mp4';
  const g=createGallery(),data=manifest('alice',1);
  data.posts[0].media=[videoVariant('123','3840x2160','source-priority.mp4',{source_url:low,archive_url:'https://web.archive.org/web/20210906010203id_/'+high}),videoVariant('123','1280x720','dimensionless-source.mp4',{source_url:'https://video.twimg.com/ext_tw_video/123/vid/clip.mp4'}),videoVariant('123','3840x2160','archive-size.mp4',{source_url:'https://video.twimg.com/ext_tw_video/123/vid/clip.mp4',archive_url:'https://web.archive.org/web/20210906010203id_/'+low})];
  g.show(data);const article=articles(g)[0],select=find(article,'select');
  assert.equal(find(article,'video').src,'media/dimensionless-source.mp4');
  assert.match(select.options[0].textContent,/1280 × 720/);assert.match(select.options[1].textContent,/320 × 180/);assert.match(select.options[2].textContent,/320 × 180/);
});

test('lightbox navigates all filtered images including unloaded cards with keyboard boundaries',()=>{
  const g=createGallery();g.show(imageManifest());find(articles(g)[0],'button').fire('click');
  assert.equal(g.get('lightbox').open,true);assert.equal(g.get('image-count').textContent,'1 / 45');assert.equal(g.get('previous-image').disabled,true);
  const event=g.get('lightbox').fire('keydown',{key:'ArrowRight'});assert.equal(event.defaultPrevented,true);assert.equal(g.get('image-count').textContent,'2 / 45');
  for(let i=0;i<50;i++)g.get('next-image').fire('click');
  assert.equal(g.get('image-count').textContent,'45 / 45');assert.equal(g.get('next-image').disabled,true);assert.equal(articles(g).length,40);
  assert.equal(find(g.get('lightbox-content'),'img').src,'media/956.jpg');
  g.get('lightbox').fire('keydown',{key:'ArrowLeft'});assert.equal(g.get('image-count').textContent,'44 / 45');
});

test('lightbox follows search and sort, preserves unchanged preview during polling',()=>{
  const g=createGallery(),data=imageManifest(3);g.show(data);
  g.get('search').value='post 1';g.get('search').fire('input');find(articles(g)[0],'button').fire('click');
  assert.equal(g.get('image-count').textContent,'1 / 1');assert.match(g.get('image-caption').textContent,/#999/);
  const img=find(g.get('lightbox-content'),'img');g.show(structuredClone(data));assert.equal(find(g.get('lightbox-content'),'img'),img);
  g.get('search').value='';g.get('search').fire('input');g.get('sort').value='asc';g.get('sort').fire('change');
  assert.equal(g.get('image-count').textContent,'2 / 3');g.get('previous-image').fire('click');assert.match(g.get('image-caption').textContent,/#998/);
  g.get('search').value='no matches';g.get('search').fire('input');assert.equal(g.get('lightbox').open,false);
});

test('lightbox and card reload repaired media but keep other unchanged cards',()=>{
  const g=createGallery(),data=imageManifest(2);g.show(data);find(articles(g)[0],'button').fire('click');
  const oldImg=find(g.get('lightbox-content'),'img'),other=articles(g)[1],next=structuredClone(data);next.posts[0].media[0].sha256='new verified bytes';g.show(next);
  assert.notEqual(find(g.get('lightbox-content'),'img'),oldImg);assert.equal(articles(g)[1],other);
  g.show(imageManifest(1));g.show(manifest('bob',1));assert.equal(g.get('lightbox').open,false);
});

test('offline lightbox reads local files and offers an encoded file download',()=>{
  const g=createGallery(new Map(),'file:///C:/collections/alice/index.html'),data=imageManifest(1);data.posts[0].media[0].path='media/2021/09/a b.jpg';g.show(data);
  find(articles(g)[0],'button').fire('click');assert.equal(find(g.get('lightbox-content'),'img').src,'media/2021/09/a%20b.jpg');
  const dl=find(g.get('image-download'),'a');assert.equal(dl.href,'file:///C:/collections/alice/media/2021/09/a%20b.jpg');assert.equal(dl.download,'a b.jpg');
});

test('media error shows useful explanation without removing the original download',()=>{
  const g=createGallery(),data=imageManifest(1);g.show(data);let article=articles(g)[0];
  find(article,'button').fire('click');find(g.get('lightbox-content'),'img').fire('error');assert.match(g.get('lightbox-content').textContent,/图片无法读取/);assert.ok(find(g.get('image-download'),'a').href);
  const img=find(article,'img');img.fire('error');
  assert.match(article.textContent,/图片无法读取/);assert.equal(findClass(article,'media-error').hidden,false);assert.equal(find(article,'button').hidden,true);assert.ok(findClass(article,'media-download').href);
  g.show(manifest('bob',1));article=articles(g)[0];const video=find(article,'video');video.error={code:4};video.fire('error');
  assert.match(article.textContent,/本地播放器/);assert.equal(video.hidden,true);assert.ok(findClass(article,'media-download').href);
});

test('only valid local saved images are previewed and missing paths can be filtered',()=>{
  const g=createGallery(),data=imageManifest(4);data.posts[1].media[0].path='https://example.com/tracking.jpg';data.posts[2].media[0].path='media/../private.jpg';delete data.posts[3].media[0].path;g.show(data);
  assert.equal(articles(g).filter(p=>find(p,'img')).length,1);find(articles(g)[0],'button').fire('click');assert.equal(g.get('image-count').textContent,'1 / 1');
  g.get('type-filter').value='missing';g.get('type-filter').fire('change');assert.equal(articles(g).length,3);assert.match(articles(g)[0].textContent,/路径缺失或无效/);
});

test('downloaded-media filter shows posts with valid saved paths online and offline',()=>{
  for(const [href,prefix] of [['http://127.0.0.1:8765/','/collections/alice/'],['file:///C:/collections/alice/index.html','']]) {
    const g=createGallery(new Map(),href),data=imageManifest(5);
    data.posts[1].media[0].status='found';
    data.posts[2].media[0].path='media/../private.jpg';
    delete data.posts[3].media[0].path;
    data.posts[4].media=[{kind:'video',status:'saved',path:'media/recovered.mp4'},{kind:'image',status:'missing',path:'media/missing.jpg'}];
    g.show(data,prefix);
    g.get('type-filter').value='saved';g.get('type-filter').fire('change');
    assert.deepEqual(articles(g).map(p=>findClass(p,'post-id').textContent),['#1000','#996']);
    assert.equal(g.get('result-count').textContent,'2 条帖子');
    assert.ok(find(articles(g)[0],'img'));
    assert.ok(find(articles(g)[1],'video'));
    assert.ok(findClass(articles(g)[0],'media-download').href.includes('/media/1000.jpg'));
    assert.ok(findClass(articles(g)[1],'media-download').href.includes('/media/recovered.mp4'));
    const restored=createGallery(g.storage,href);restored.show(data,prefix);
    assert.equal(restored.get('type-filter').value,'saved');
    assert.equal(articles(restored).length,2);
    restored.get('type-filter').value='saved-video';restored.get('type-filter').fire('change');
    assert.deepEqual(articles(restored).map(p=>findClass(p,'post-id').textContent),['#996']);
    assert.ok(find(articles(restored)[0],'video'));
  }
});

test('missing text filter includes empty or whitespace text and is remembered',()=>{
  const g=createGallery(),data=manifest('alice',3);data.posts[0].text='';data.posts[0].content_note='快照只有登录提示';data.posts[1].text='   ';g.show(data);
  g.get('type-filter').value='missing-text';g.get('type-filter').fire('change');assert.equal(articles(g).length,2);assert.match(articles(g)[0].textContent,/登录提示/);
  const restored=createGallery(g.storage);restored.show(data);assert.equal(restored.get('type-filter').value,'missing-text');assert.equal(articles(restored).length,2);
});

test('online and offline HTML supply lightbox controls and content filters',()=>{
  for(const name of ['index.html','offline.html']){const html=fs.readFileSync(__dirname+'/../../static/'+name,'utf8');for(const id of ['image-count','image-caption','image-download','previous-image','next-image'])assert.ok(html.includes(`id="${id}"`),name+' missing '+id);assert.ok(html.includes('<option value="saved">仅已下载媒体</option>'));assert.ok(html.includes('value="missing-text"'));}
});

test('saved media shows its actual source and treats old saved records as Wayback',()=>{
  const g=createGallery(),data=imageManifest(3);
  data.posts[0].media[0].source='live_cdn';data.posts[0].media[0].source_url='https://pbs.twimg.com/media/photo.jpg';
  data.posts[0].media[0].source_evidence='https://api.fxtwitter.com/alice/status/1000';
  Object.assign(data.posts[1].media[0],{source:'common_crawl',source_url:'https://pbs.twimg.com/media/old.jpg',source_captured_at:'20210302040506',source_scope:['CC-MAIN-2021-10']});
  g.show(data);
  assert.match(articles(g)[0].textContent,/原始媒体链接（当前）/);assert.match(articles(g)[0].textContent,/并非历史页面快照/);
  const live=findClass(articles(g)[0],'source-details');assert.ok(live.children.some(n=>n.href==='https://pbs.twimg.com/media/photo.jpg'));
  assert.ok(live.children.some(n=>n.href==='https://api.fxtwitter.com/alice/status/1000'));
  assert.match(articles(g)[1].textContent,/Common Crawl 存档/);assert.match(articles(g)[1].textContent,/2021-03-02 04:05:06 UTC/);
  assert.match(articles(g)[1].textContent,/CC-MAIN-2021-10/);assert.match(articles(g)[2].textContent,/Wayback 存档/);
});

test('missing media reports individual source failures and the bounded query scope',()=>{
  const g=createGallery(),data=imageManifest(1);
  Object.assign(data.posts[0].media[0],{status:'missing',error:'仍未恢复',recovery_checks:[
    {source:'fxtwitter',status:'failed',error:'连接超时',checked_at:'2026-09-21T03:00:00Z',evidence_url:'https://api.fxtwitter.com/alice/status/1000'},
    {source:'common_crawl',status:'not_found',scope:['CC-MAIN-2021-10','CC-MAIN-2021-17'],indexes_limit:6},
    {source:'live_cdn',status:'not_found',variants_checked:3}]});
  g.show(data);const article=articles(g)[0];
  assert.match(article.textContent,/各来源检查（3）/);assert.match(article.textContent,/FxTwitter 公开镜像/);
  assert.match(article.textContent,/请求或读取失败/);assert.match(article.textContent,/连接超时/);
  assert.match(article.textContent,/2026-09-21 03:00:00 UTC/);assert.match(article.textContent,/CC-MAIN-2021-10、CC-MAIN-2021-17/);
  assert.match(article.textContent,/不能据此判断全部历史存档/);assert.match(article.textContent,/3 个媒体地址变体/);
});

test('source checks update a changed card without replacing unrelated playing media',()=>{
  const g=createGallery(),data=manifest('alice',2);data.posts[0].media[0].status='missing';g.show(data);
  const old=articles(g)[0],playing=articles(g)[1];
  const next=structuredClone(data);next.posts[0].media[0].recovery_checks=[{source:'common_crawl',status:'failed',error:'new network error'}];g.show(next);
  assert.notEqual(articles(g)[0],old);assert.match(articles(g)[0].textContent,/new network error/);
  assert.equal(articles(g)[1],playing);assert.equal(playing.detaches,0);
});

test('source evidence is inert text unless it is an absolute HTTP or HTTPS URL',()=>{
  const g=createGallery(),data=imageManifest(2);
  Object.assign(data.posts[0].media[0],{source:'live_cdn',source_url:'javascript:alert(1)',source_evidence:'file:///C:/private'});
  Object.assign(data.posts[1].media[0],{status:'missing',recovery_checks:[{source:'fxtwitter',status:'failed',error:'<img src=x onerror=alert(1)>',evidence_url:'javascript:alert(1)'}]});
  g.show(data);
  assert.equal(find(findClass(articles(g)[0],'source-details'),'a'),null);
  assert.equal(find(findClass(articles(g)[1],'source-checks'),'a'),null);
  assert.match(articles(g)[1].textContent,/<img src=x onerror=alert\(1\)>/);assert.equal(find(articles(g)[1],'img'),null);
});

test('collection summary distinguishes current-media and archival recoveries',()=>{
  const g=createGallery(),data=imageManifest(1);
  Object.assign(data.counts,{live_recovered:3,cc_recovered:2});data.last_run={multi_source:{checked:12,recovered:5,cached:7}};g.show(data);
  const text=g.get('recovery-summary').textContent;
  assert.match(text,/3 项已从当前原始媒体链接补回/);assert.match(text,/2 项已从 Common Crawl 历史存档补回/);
  assert.match(text,/检查 12 项，补回 5 项，复用 7 项近期检查/);
});

test('saved Twitter video thumbnails are labeled as cover images in cards and the lightbox',()=>{
  const g=createGallery(),data=imageManifest(3);
  data.posts[0].media[0].key='pbs.twimg.com/ext_tw_video_thumb/12345/pu/img/cover.jpg';
  data.posts[1].media[0].urls=['https://pbs.twimg.com/amplify_video_thumb/67890/img/cover.jpg'];
  data.posts[2].media[0].key='pbs.twimg.com/tweet_video_thumb/example.jpg';
  const originalCounts=structuredClone(data.counts);g.show(data);
  for(const article of articles(g)){
    assert.match(article.textContent,/视频封面（图片）/);assert.match(find(article,'img').alt,/视频封面/);
    assert.equal(find(article,'video'),null);
  }
  find(articles(g)[0],'button').fire('click');assert.match(g.get('image-caption').textContent,/视频封面（图片）/);
  assert.match(find(g.get('lightbox-content'),'img').alt,/视频封面/);assert.deepEqual(data.counts,originalCounts);
});

test('missing covers and missing full videos have different labels without reclassifying ordinary photos',()=>{
  const g=createGallery(),data=imageManifest(3);
  Object.assign(data.posts[0].media[0],{status:'missing',key:'pbs.twimg.com/ext_tw_video_thumb/12345/pu/img/cover.jpg'});
  Object.assign(data.posts[1].media[0],{status:'missing',kind:'video',key:'video.twimg.com/ext_tw_video/12345/'});
  data.posts[2].media[0].key='pbs.twimg.com/media/ordinary-photo.jpg';g.show(data);
  assert.equal(findClass(articles(g)[0],'missing-row').children[0].textContent,'视频封面');
  assert.equal(findClass(articles(g)[1],'missing-row').children[0].textContent,'视频');
  assert.doesNotMatch(articles(g)[2].textContent,/视频封面/);assert.match(find(articles(g)[2],'img').alt,/存档图片/);
  find(articles(g)[2],'button').fire('click');assert.doesNotMatch(g.get('image-caption').textContent,/视频封面/);
});

test('a newly identified cover redraws only its own card',()=>{
  const g=createGallery(),data=imageManifest(2);g.show(data);const old=articles(g)[0],other=articles(g)[1];
  const next=structuredClone(data);next.posts[0].media[0].key='pbs.twimg.com/tweet_video_thumb/example.jpg';g.show(next);
  assert.notEqual(articles(g)[0],old);assert.equal(articles(g)[1],other);assert.match(articles(g)[0].textContent,/视频封面/);
});

test('empty collections direct users to the unified main-page action online and offline',()=>{
  for(const href of ['http://127.0.0.1:8765/','file:///C:/collections/alice/index.html']) {
    const g=createGallery(new Map(),href),data=manifest('alice',0);data.status='complete';g.show(data);
    assert.match(g.get('collection-note').textContent,/未发现帖子/);assert.doesNotMatch(g.get('collection-note').textContent,/本轮已完成/);
    assert.match(g.get('timeline').textContent,/时光集主页面点击“继续查找与下载”/);
    assert.doesNotMatch(g.get('timeline').textContent,/补全已知媒体|扩展查找帖子/);
  }
});

test('empty failed collections show query failure while an empty filter keeps its separate message',()=>{
  const g=createGallery(),data=manifest('alice',0);data.status='failed';g.show(data);
  assert.match(g.get('collection-note').textContent,/查询失败/);assert.match(g.get('timeline').textContent,/查询失败/);
  g.show(manifest('bob',1));g.get('search').value='no such post';g.get('search').fire('input');
  assert.equal(g.get('timeline').textContent,'没有符合筛选条件的帖子。');
});

test('post discovery shows post and text gains separately from resource diagnostics',()=>{
  const g=createGallery(),data=manifest('alice',4);
  data.last_run={discovery:{posts_before:1,posts_after:4,new_posts:3,text_before:0,text_after:2,new_text:2,captures_before:3,captures_after:17,resource_captures:14,queried_hosts:6,failed_hosts:1}};
  data.discovery={new_posts:999,new_text:999};
  g.show(data);const records=g.get('discovery-records');
  assert.equal(records.hidden,false);assert.match(records.textContent,/新增 3 条帖子 · 新增 2 条正文/);
  assert.doesNotMatch(records.textContent,/999/);assert.match(find(records,'summary').textContent,/查找诊断/);
  assert.match(records.textContent,/资源记录：14 条 · 已查询域名：6 个 · 失败域名：1 个/);
  assert.match(records.textContent,/不代表媒体已下载/);
});

test('zero discovery results stay visible while old or invalid records are hidden and cleared',()=>{
  const g=createGallery(),data=manifest('alice',0);
  data.last_run={discovery:{new_posts:0,new_text:0,resource_captures:0,queried_hosts:6,failed_hosts:0}};
  g.show(data);assert.equal(g.get('discovery-records').hidden,false);assert.match(g.get('discovery-records').textContent,/新增 0 条帖子 · 新增 0 条正文/);
  g.show(manifest('bob',1));assert.equal(g.get('discovery-records').hidden,true);assert.equal(g.get('discovery-records').textContent,'');
  data.last_run.discovery={new_posts:'<script>alert(1)</script>'};g.show(data);
  assert.equal(g.get('discovery-records').hidden,true);assert.equal(g.get('discovery-records').textContent,'');
});

test('discovery report container exists online and offline',()=>{
  for(const filename of ['index.html','offline.html'])assert.ok(fs.readFileSync(__dirname+'/../../static/'+filename,'utf8').includes('id="discovery-records"'));
});

test('unrecovered statistics preserve totals and distinguish download failures from missing archives',()=>{
  const g=createGallery(),data=manifest('alice',5);
  Object.assign(data.counts,{missing:4,saved:1,not_indexed:1,retryable:3});
  Object.assign(data.posts[0].media[0],{status:'missing',error_kind:'network'});
  Object.assign(data.posts[1].media[0],{status:'missing',error_kind:'not_indexed',recovery_error_kind:'network',cc_more:true});
  Object.assign(data.posts[2].media[0],{status:'missing',error_kind:'not_found'});
  Object.assign(data.posts[3].media[0],{status:'link_only',kind:'hls',error_kind:'unsupported_hls',cc_more:true});
  Object.assign(data.posts[4].media[0],{error_kind:'network',recovery_error_kind:'network',cc_more:true});
  g.show(data);
  const stats=g.get('stats'),text=g.get('recovery-summary').textContent;
  assert.equal(stats.children[2].textContent,'1已存文件记录');
  assert.equal(stats.children[3].textContent,'4未存链接记录');
  assert.doesNotMatch(stats.textContent,/待恢复/);
  assert.match(text,/1 项媒体：已查范围内未找到可下载存档，不代表其他来源一定没有保存/);
  assert.match(text,/2 项媒体：网络或读取失败/);
  assert.doesNotMatch(text,/3 项媒体：网络或读取失败/,'legacy retryable includes unavailable URLs and must not be reported as network failure');
  assert.match(text,/1 项媒体：已知文件地址暂不可用/);
  assert.match(text,/1 项视频分片记录尚未恢复/);
  assert.match(text,/1 项媒体还有 Common Crawl 存档范围未检查完/,'saved media and unsupported HLS must not be counted as an ongoing media lookup');
  assert.match(text,/同一文件，未恢复总数以上方统计为准/);
});

test('source scope stays in detailed recovery information instead of the collection status line',()=>{
  const g=createGallery(),data=manifest('alice',1);
  data.scope_note='来源覆盖范围说明';data.counts.captures=8;data.counts.parsed=7;g.show(data);
  assert.equal(g.get('collection-note').textContent,'部分恢复 · 已读 7 / 8 份快照');
  assert.match(g.get('recovery-summary').textContent,/来源覆盖范围说明/);
});

test('empty online and offline collections show nearby usernames as unmerged suggestions',()=>{
  for(const href of ['http://127.0.0.1:8765/','file:///C:/collections/alice/index.html']) {
    const g=createGallery(new Map(),href),data=manifest('alice',0);
    data.last_run={discovery:{nearby_handles:['alice_old','Alice_2']}};
    g.show(data);const hint=findClass(g.get('timeline'),'nearby-handles');
    assert.ok(hint);assert.equal(hint.textContent,'索引出现相近用户名：@alice_old、@Alice_2；请核对后单独输入。相近账号的帖子未计入当前合集。');
    assert.equal(find(hint,'a'),null);assert.equal(articles(g).length,0);assert.equal(data.counts.posts,0);
    g.show(manifest('bob',0));assert.equal(findClass(g.get('timeline'),'nearby-handles'),null);
    data.last_run.discovery.nearby_handles=[];g.show(data);assert.equal(findClass(g.get('timeline'),'nearby-handles'),null);
  }
});

test('nearby usernames are validated and bounded, and never displayed for a nonempty collection filter',()=>{
  const g=createGallery(),data=manifest('alice',0);
  data.last_run={discovery:{nearby_handles:['ALICE','good_1','GOOD_1','<img src=x>','abcdefghijklmnop',null,'good_2','good_3','good_4']}};
  g.show(data);assert.match(g.get('timeline').textContent,/@good_1、@good_2、@good_3/);
  assert.doesNotMatch(g.get('timeline').textContent,/@ALICE|GOOD_1|img src|abcdefghijklmnop|good_4/);
  const populated=manifest('bob',1);populated.last_run=data.last_run;g.show(populated);
  g.get('search').value='no matching post';g.get('search').fire('input');
  assert.equal(g.get('timeline').textContent,'没有符合筛选条件的帖子。');
  data.last_run.discovery.nearby_handles='alice_old';g.show(data);assert.equal(findClass(g.get('timeline'),'nearby-handles'),null);
});

test('online and offline galleries distinguish task outcome from partial restoration',()=>{
  for(const href of ['http://127.0.0.1:8766/','file:///C:/saved/index.html']){
    const g=createGallery(new Map(),href),data=manifest('alice',1);
    data.status='paused';data.task_summary={state:'finished',label:'本轮已结束',tone:'warning',result_label:'部分恢复',reason_text:'连续 3 批没有新增内容，已自动结束。'};
    g.show(data);assert.match(g.get('collection-note').textContent,/本轮已结束.*部分恢复.*3 批/);
    assert.equal(g.get('collection-status').hidden,false);assert.match(g.get('collection-status').textContent,/本轮已结束/);
  }
});

test('Instagram gallery shows platform, shortcode, source failures and searches shortcode offline',()=>{
  const g=createGallery(new Map(),'file:///C:/collections/instagram--alice/index.html'),data=manifest('instagram--alice',1);
  data.platform='instagram';data.display_username='alice';data.posts[0].username='alice';data.posts[0].platform='instagram';data.posts[0].shortcode='AbC_123';
  data.errors=[{stage:'Instagram 索引暂不可用',error:'Connection timed out'}];
  g.show(data);assert.equal(g.get('collection-title').textContent,'Instagram @alice');
  assert.equal(findClass(articles(g)[0],'post-id').textContent,'#AbC_123');
  assert.match(g.get('error-list').textContent,/Connection timed out/);assert.doesNotMatch(g.get('error-list').textContent,/undefined/);
  g.get('search').value='abc_123';g.get('search').fire('input');assert.equal(articles(g).length,1);
});
