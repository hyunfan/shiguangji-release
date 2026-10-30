/* Task panel: current task status, live progress, outcome and the archive queue. */
(() => {
  'use strict';
  const App=window.ShiguangjiApp;
  const {$,state,isInstagram,statusBadge,aliasText,localTime,displayName}=App;
  const statusLabels={complete:'已整理',partial:'部分恢复',paused:'已暂停',failed:'未完成',running:'整理中'};
  function resultStatus(value) {
    if(value.task_summary?.schema_version===1)return value.task_summary.label;
    if(value.mode==='identity'&&!value.running)return value.status==='paused'?
      '检查已暂停':value.status==='failed'?'检查未完成':'检查已结束';
    if(value.interrupted)return '已暂停';
    if(!value.running&&value.automatic?.enabled&&value.automatic?.stop_reason)return value.automatic.stop_code==='user_pause'?
      '已暂停':'自动处理已结束';
    if(value.status==='paused'&&value.batch?.stop_reason)return '本批已结束';
    if(!value.running&&value.status!=='running'&&value.counts?.posts===0)return value.status==='failed'?'查询失败':'未发现帖子';
    return statusLabels[value.status]||value.status||'本轮结束';
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
    for(const id of ['job-resume','job-rebuild'])$(id).disabled=
      state.actionPending||state.connectionError||state.jobRunning||!state.jobHandle;
    $('job-status').hidden=!summary;
    $('job-result').hidden=!summary;
    $('job-aliases').hidden=!summary||isInstagram(job.username);
    $('job-ended').hidden=!summary||!summary.finished_at||job.running;
    $('job-actions').hidden=!summary||job.running;
    if(summary){
      statusBadge($('job-status'),summary);
      const c=job.counts||{};
      $('job-result').textContent=
        `${summary.result_label} · 已保存 ${c.posts||0} 条帖子 · 已存 ${c.saved||0} 条文件记录 · 未存 ${Math.max(0,(c.media||0)-(c.saved||0))} 条链接记录`;
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
    if(Number.isFinite(count)&&Number.isFinite(reused)&&reused>0)
      $('job-outcome').textContent+=`（其中 ${Math.min(count,reused)} 个复用自本地旧合集）`;
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
    $('batch-add').hidden=!state.supportsQueue;
    $('queue-add').disabled=!state.supportsQueue||state.actionPending||state.connectionError;
    const items=queue?.items||[],counts=queue?.counts||{},
      pending=(counts.pending||0)+(counts.paused||0),active=items.find(i=>i.state==='running');
    $('queue-panel').hidden=!state.supportsQueue||(!items.length&&!queue?.error);
    const labels={idle:'归档队列',running:active?'队列正在归档':'队列等待当前任务结束',paused:'队列已暂停',finished:'队列已结束'};
    $('queue-title').textContent=labels[queue?.state]||'归档队列';
    if(queue?.state==='running'&&!active&&queue.next_retry_at)$('queue-title').textContent='等待自动重试';
    $('queue-counts').textContent=
      `共 ${items.length} 个 · 本轮结束 ${counts.finished||0} · 失败 ${counts.failed||0} · 待处理 ${pending}`;
    $('queue-note').textContent=queue?.error||(queue?.state==='running'?
      (active?`正在处理 ${displayName(active.username)}；保存与打包结束后自动转到下一个。`:'当前任务结束后会自动开始队列。'):
      queue?.state==='paused'?(queue.reason==='restart'?
      '队列和设置已保留。点击“继续队列”恢复，不会在启动工具时自动采集。':
      '队列已暂停，剩余账号保留原顺序；可点击“继续队列”。'):
      '本轮排队已结束。每个账号的恢复结果可在合集中查看。');
    $('queue-pause').hidden=queue?.state!=='running';
    if(queue?.state==='running'&&!active&&queue.next_retry_at)
      $('queue-note').textContent=`下次重试：${localTime(queue.next_retry_at)}。工具运行时自动继续；每项最多延后重试 3 轮、最长 48 小时。可暂停或移除。`;
    $('queue-resume').hidden=queue?.state!=='paused'||!pending;
    $('queue-clear').hidden=!(counts.finished||counts.failed);
    $('queue-pause').disabled=state.actionPending||state.connectionError;
    $('queue-resume').disabled=state.actionPending||state.connectionError||Boolean(job?.pause_requested)||Boolean(queue?.error);
    $('queue-clear').disabled=state.actionPending||state.connectionError;
    const list=$('queue-items');list.replaceChildren();
    const states={pending:'等待中',running:'正在归档',paused:'已暂停',finished:'本轮已结束',failed:'失败'};
    items.forEach(item=>{
      const row=document.createElement('li'),text=document.createElement('div'),
        name=document.createElement('strong'),detail=document.createElement('p');
      name.textContent=displayName(item.username)+' · '+states[item.state];
      detail.className='hint';
      detail.textContent=[item.outcome?.label,item.outcome?.result_label,item.message].filter(Boolean).join(' · ');
      if(item.next_retry_at&&item.state==='pending')detail.textContent+=' · '+localTime(item.next_retry_at);
      text.append(name,detail);row.append(text);
      if(['pending','paused'].includes(item.state)){
        const remove=document.createElement('button');
        remove.className='secondary small';
        remove.type='button';remove.textContent='移除';remove.disabled=state.actionPending||state.connectionError;
        remove.setAttribute('aria-label','从队列移除 '+displayName(item.username));
        remove.addEventListener('click',()=>App.action('/api/queue/remove',{id:item.id}));row.append(remove);
      }
      list.append(row);
    });
  }
  function renderJobStatus(s,j,logs){
    $('start-button').disabled=j.running||state.actionPending;
    $('pause-button').hidden=!j.running;
    $('pause-button').disabled=state.actionPending||j.pause_requested||
      (!j.automatic?.enabled&&['zip','export','done'].includes(j.stage));
    $('pause-button').textContent=j.pause_requested?'正在暂停并保存…':'暂停并保存';
    $('repair-button').disabled=j.running||state.actionPending||!state.selected||state.selectedPosts===0||!state.supportsRepair;
    $('rebuild-zip').disabled=j.running||state.actionPending;
    $('job-panel').hidden=j.status==='idle';
    $('job-title').textContent=
      `${j.username?displayName(j.username)+' · ':''}${j.running?(s.stages[j.stage]||'正在整理'):resultStatus(j)}`;
    sleepStatus(j);taskOutcome(j);
    $('job-detail').textContent=logs.length?logs[logs.length-1].text:
      j.status==='paused'?'上次整理已暂停，选择对应合集后可继续补全。':'准备检索存档…';
    const live=$('job-live-detail'),page=j.page_progress;
    live.hidden=!j.running&&!j.retry_policy?.waiting&&!j.backup?.state;
    const elapsedStage=Math.max(0,Math.floor((Date.now()-Date.parse(j.stage_started_at))/1000)||0);
    const stageTime=j.running&&j.stage_started_at?`当前阶段 ${Math.floor(elapsedStage/60)}分${elapsedStage%60}秒` : '';
    const liveParts=[];
    if(j.stage==='pages'&&page&&Number.isInteger(page.total))
      liveParts.push(`累计已解析 ${page.parsed||0} / ${page.total} 份快照，本批已检查 ${page.batch_checked||0} 份`);
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
  }
  function renderJobProgress(s,j,logs){
    const batch=j.batch,details=[];
    if(j.total&&['identity','local_aliases','pages','content','media','sources','done'].includes(j.stage))
      details.push(`当前步骤已处理：${Math.min(j.completed||0,j.total)} / ${j.total}`);
    if(batch?.max_seconds){
      const active=j.running&&!['export','zip','done','between_batches'].includes(j.stage);
      const elapsed=active?
        Math.max(batch.elapsed_seconds||0,(Date.now()-Date.parse(j.batch_started_at||j.started_at))/1000||0):
        batch.elapsed_seconds||0;
      details.push(`本批新增 ${batch.new_files||0} 个文件`,
        `请求 ${batch.requests||0} / ${batch.max_requests}`,
        `已检索 ${Math.floor(elapsed/60)}分${Math.floor(elapsed%60)}秒`);
      if(active)details.push(`检索剩余至多 ${Math.ceil(Math.max(0,batch.max_seconds-elapsed))} 秒`);
      if(batch.stop_reason)details.push(batch.stop_reason);
    }
    if(j.automatic?.enabled){
      details.unshift(
        `自动处理 · ${j.running&&j.automatic.current_batch?'第 '+j.automatic.current_batch+' 批':'已处理 '+j.automatic.batches+' 批'} · 累计新增 ${j.automatic.new_files||0} 个文件`);
      if(Number.isFinite(j.automatic.new_text))
        details.push(`累计补回 ${j.automatic.new_text} 条正文 · 新增 ${j.automatic.new_posts||0} 条帖子 · 已结束批次请求 ${j.automatic.requests||0} 次`);
      if(j.task_summary?.reason==='retry_limit')details.push(j.task_summary.reason_text);
      else if(j.automatic.stop_reason)details.push(j.automatic.stop_reason);
    }
    $('job-progress').textContent=details.join(' · ');
    const spent=Object.entries(j.stage_seconds||{}).filter(([,seconds])=>Number.isFinite(seconds)&&seconds>=1)
      .sort((a,b)=>b[1]-a[1]);
    if(spent.length)$('job-progress').textContent+=' · 已结束阶段累计用时：'+
      spent.map(([stage,seconds])=>`${s.stages[stage]||stage} ${Math.round(seconds)}秒`).join('、');
    const total=Number(j.total),completed=Number(j.completed||0);
    const knownProgress=j.running&&['identity','local_aliases','pages','content','media','sources'].includes(j.stage)&&
      Number.isFinite(total)&&total>0&&Number.isFinite(completed);
    $('activity-track').hidden=!j.running;
    $('activity').setAttribute('max','100');
    $('activity').classList.toggle('moving',j.running&&!knownProgress);
    if(knownProgress)$('activity').setAttribute('value',String(Math.max(0,Math.min(100,completed/total*100))));
    else $('activity').removeAttribute('value');
    $('job-logs').replaceChildren(...logs.slice(-60).map(log=>{
      const li=document.createElement('li');
      li.textContent=log.time.slice(11,19)+' '+log.text;
      return li;
    }));
  }
  Object.assign(App,{resultStatus,renderQueue,renderJobStatus,renderJobProgress});
})();
