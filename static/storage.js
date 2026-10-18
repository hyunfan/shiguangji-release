/* Storage controls are optional on older backends and never start collection. */
const StoragePanel=(()=>{
  function create({api,getSelected,isBusy,refresh}){
    const $=id=>document.getElementById(id);
    const size=n=>{
      n=Math.max(0,Number(n)||0);
      if(n<1024)return `${Math.round(n)} B`;
      if(n<1024**2)return `${(n/1024).toFixed(1)} KiB`;
      if(n<1024**3)return `${(n/1024**2).toFixed(2)} MiB`;
      return `${(n/1024**3).toFixed(2)} GiB`;
    };
    let current=null,dirty=false,pending=false,shown='';
    const fields=['storage-demand','storage-days','storage-versions','storage-cache'];
    fields.forEach(id=>$(id).addEventListener('change',()=>{dirty=true;}));
    function controls(){
      const busy=pending||Boolean(current?.running)||isBusy();
      for(const id of ['storage-preview','storage-preview-all','storage-probe','storage-clear','storage-confirm','storage-compress'])$(id).disabled=busy;
      $('storage-probe').disabled=busy||!getSelected();
      $('storage-compress-pause').disabled=pending||!current?.running||current.operation!=='compress';
      $('storage-save').disabled=pending||Boolean(current?.running);
    }
    async function send(path,body){
      if(pending)return;
      pending=true;controls();
      try{const result=await api(path,body);update(result.storage);await refresh();return result;}
      catch(error){$('storage-message').textContent=error.message;}
      finally{pending=false;controls();}
    }
    function update(value){
      $('storage-options').hidden=!value;
      if(!value)return;
      current=value;
      if(!dirty){
        $('storage-demand').checked=value.settings.zip_on_demand;
        $('storage-days').value=String(value.settings.zip_keep_days);
        $('storage-versions').value=String(value.settings.backup_versions);
        $('storage-cache').value=String(value.settings.cache_gb);
      }
      $('storage-cache-status').textContent=`试读缓存 ${size(value.cache.bytes)} / ${size(value.cache.limit_bytes)}；网盘客户端自身缓存和上传临时文件另计。`;
      $('storage-message').textContent=value.message||'';
      if(value.running){$('storage-result').replaceChildren();$('storage-review').hidden=true;}
      const result=value.result;
      if(result&&!value.running&&value.finished_at&&shown!==String(value.finished_at)){
        shown=String(value.finished_at);$('storage-result').replaceChildren();
        if(value.operation==='preview'){
          const p=document.createElement('p');p.textContent=`可释放 ${size(result.bytes)}，共 ${result.items.length} 个 ZIP；跳过 ${result.skipped} 个未满足条件的文件。`;$('storage-result').append(p);
          const list=document.createElement('ul');
          for(const item of result.items){const li=document.createElement('li');li.textContent=`@${item.username}：${size(item.bytes)}`;list.append(li);}
          $('storage-result').append(list);$('storage-review').hidden=!result.items.length;
        }else{
          $('storage-review').hidden=true;
          if(value.operation==='cleanup')$('storage-result').textContent=`实际释放 ${size(result.bytes)}。需要时可重新生成 ZIP。`;
          if(value.operation==='compress')$('storage-result').textContent=`本轮无损压缩释放 ${size(result.saved_bytes)}；${result.errors?.length||0} 项未完成。新网页自动压缩，媒体原件保持不变。`;
          if(value.operation==='probe')for(const item of result.files){
            const p=document.createElement('p');
            p.textContent=item.verified?`${item.kind==='video'?'视频':'图片'} ${size(item.bytes)}：校验通过，${item.seconds} 秒${item.cache_hit?'（缓存命中）':''}。`:`试读未完成：${item.error}`;
            if(item.verified){const a=document.createElement('a');a.textContent='打开试读文件';a.href='/api/cloud-media/'+encodeURIComponent(result.username)+'/'+item.path.split('/').map(encodeURIComponent).join('/');a.target='_blank';a.rel='noopener';p.append(a);}
            $('storage-result').append(p);
          }
        }
      }
      controls();
    }
    $('storage-save').addEventListener('click',async()=>{
      const result=await send('/api/storage/settings',{zip_on_demand:$('storage-demand').checked,zip_keep_days:Number($('storage-days').value),backup_versions:Number($('storage-versions').value),cache_gb:Number($('storage-cache').value)});
      if(result){dirty=false;update(result.storage);}
    });
    $('storage-preview').addEventListener('click',()=>send('/api/storage/action',{operation:'preview'}));
    $('storage-preview-all').addEventListener('click',()=>send('/api/storage/action',{operation:'preview',all_exports:true}));
    $('storage-probe').addEventListener('click',()=>{if(getSelected()&&!isBusy())return send('/api/storage/action',{operation:'probe',username:getSelected()});});
    $('storage-clear').addEventListener('click',()=>send('/api/storage/action',{operation:'clear_cache'}));
    $('storage-compress').addEventListener('click',()=>send('/api/storage/action',{operation:'compress'}));
    $('storage-compress-pause').addEventListener('click',()=>send('/api/storage/action',{operation:'pause_compression'}));
    $('storage-review').addEventListener('click',()=>{
      if(current?.operation!=='preview'||!current.result?.items.length)return;
      $('storage-confirm-description').textContent=`将删除清单中的 ${current.result.items.length} 个生成的 ZIP，预计释放 ${size(current.result.bytes)}。保留原始媒体、数据库、离线页面和恢复备份。以后下载合集需要重新打包。`;
      $('storage-dialog').showModal();
    });
    $('storage-cancel').addEventListener('click',()=>$('storage-dialog').close());
    $('storage-confirm').addEventListener('click',()=>{
      const id=current?.result?.id;if(!id||current.operation!=='preview')return;
      $('storage-dialog').close();return send('/api/storage/action',{operation:'cleanup',plan_id:id,confirm_zip_only:true});
    });
    return {update};
  }
  return {create};
})();
