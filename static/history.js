/* History search stays separate from gallery selection and task polling. */
(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  window.HistoryPanel={create({api,select,retry,options}){
    let supported=false,offline=false,running=false,pending=false,sequence=0,offset=0,total=0,items=[],timer=null,dirty=true,lastRun='';
    const pageSize=20, panel=$('history-panel');
    const name=item=>(item.platform==='instagram'?'Instagram @':'X @')+item.display_username;
    const date=value=>{if(!value)return '时间未知';const d=new Date(value);return Number.isFinite(d.getTime())?d.toLocaleString('zh-CN',{hour12:false}):value;};
    function render(){
      const list=$('history-items');list.replaceChildren();
      $('history-count').textContent=total?`共 ${total} 条记录 · 第 ${offset+1}–${offset+items.length} 条`:'没有符合条件的历史记录';
      for(const item of items){
        const row=document.createElement('li'),title=document.createElement('strong'),state=document.createElement('span'),detail=document.createElement('p'),evidence=document.createElement('p'),actions=document.createElement('div');
        title.textContent=name(item);
        state.className='task-badge task-'+({failed:'danger',paused:'warning',interrupted:'warning',running:'info',finished:'success'}[item.state]||'neutral');
        state.textContent=item.label||({running:'正在处理',failed:'失败',paused:'已暂停',interrupted:'意外中断',finished:'本轮已结束'}[item.state]||'状态未记录');
        detail.className='hint';detail.textContent=`${date(item.started_at)} · ${item.result_label||'恢复结果未记录'} · ${item.counts?.posts||0} 条帖子 / ${item.counts?.saved||0} 项已保存媒体`;
        evidence.className='hint';evidence.textContent=[item.message,item.user_id?'数字 ID '+item.user_id:'',(item.aliases||[]).length?'已核实旧名：'+item.aliases.map(n=>'@'+n).join('、'):'',item.origin!=='recorded'?'旧版记录补录，设置可能不完整':'',!item.has_collection?'尚无可浏览的合集':''].filter(Boolean).join(' · ');
        actions.className='actions';
        const open=document.createElement('button');open.type='button';open.className='secondary small';open.textContent='打开合集';open.disabled=!item.has_collection||offline;
        open.addEventListener('click',async()=>{if(offline||!item.has_collection)return;try{await select(item.username);$('collection').scrollIntoView?.({behavior:'smooth',block:'start'});}catch(e){message(e.message);}});
        const again=document.createElement('button');again.type='button';again.className='secondary small';
        again.textContent=item.mode==='zip'||item.reason==='zip_failed'?'重新生成 ZIP':['paused','interrupted'].includes(item.state)?'继续此任务':item.state==='failed'?'重试此任务':'再次查询';
        again.disabled=offline||running||pending||item.state==='running';
        again.addEventListener('click',async()=>{
          if(offline||running||pending||item.state==='running')return;
          pending=true;render();message('');
          try{await retry(item.id,options());dirty=true;await load();}catch(e){message(e.message);}finally{pending=false;render();}
        });
        actions.append(open,again);row.append(title,state,detail,evidence,actions);list.append(row);
      }
      $('history-prev').disabled=offline||pending||offset===0;
      $('history-next').disabled=offline||pending||offset+items.length>=total;
      $('history-refresh').disabled=offline||pending;
      $('history-running-note').hidden=!running;
    }
    function message(text){$('history-error').textContent=text;$('history-error').hidden=!text;}
    async function load(){
      if(!supported||offline||panel.hidden)return;
      const requestId=++sequence;
      const params={q:$('history-query').value.trim(),state:$('history-state').value||'all',platform:$('history-platform').value||'all',offset,limit:pageSize};
      $('history-count').textContent='正在读取历史记录…';
      try{
        const query=Object.entries(params).map(([k,v])=>encodeURIComponent(k)+'='+encodeURIComponent(v)).join('&');
        const result=await api('/api/history?'+query);
        if(requestId!==sequence||panel.hidden)return;
        items=result.items;total=result.total;offset=result.offset;dirty=false;
        // A deletion can remove the last page while the panel stays open.
        if(!items.length&&offset>0){offset=Math.max(0,Math.ceil(total/pageSize)-1)*pageSize;return load();}
        $('history-note').textContent=result.note;message('');render();
      }catch(e){if(requestId===sequence){message(e.message);$('history-count').textContent='历史记录读取失败，可点击刷新重试';}}
    }
    function filter(){sequence++;offset=0;dirty=true;clearTimeout(timer);timer=setTimeout(load,250);}
    $('history-open').addEventListener('click',()=>{if(!supported)return;panel.hidden=false;panel.scrollIntoView?.({behavior:'smooth',block:'start'});return load();});
    $('history-close').addEventListener('click',()=>{panel.hidden=true;sequence++;clearTimeout(timer);});
    $('history-query').addEventListener('input',filter);
    for(const id of ['history-state','history-platform'])$(id).addEventListener('change',()=>{clearTimeout(timer);sequence++;offset=0;return load();});
    $('history-prev').addEventListener('click',()=>{if(offset<=0||pending||offline)return;offset=Math.max(0,offset-pageSize);return load();});
    $('history-next').addEventListener('click',()=>{if(offset+items.length>=total||pending||offline)return;offset+=pageSize;return load();});
    $('history-refresh').addEventListener('click',()=>load());
    return {
      update(state,isOffline=false){
        const previousOffline=offline;offline=isOffline;
        if(state){supported=Boolean(state.capabilities?.includes('task_history'));running=Boolean(state.job?.running);const key=JSON.stringify([state.job?.run_id,state.job?.running,state.job?.status]);if(key!==lastRun){dirty=true;lastRun=key;}}
        $('history-open').hidden=!supported;$('history-open').disabled=offline;
        if(offline){sequence++;dirty=true;}
        if(!panel.hidden){render();if(!offline&&(dirty||previousOffline))return load();}
      },
      invalidate(){dirty=true;sequence++;}
    };
  }};
})();
