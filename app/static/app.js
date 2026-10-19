const $ = id => document.getElementById(id);
let editingSources = false, visibleJobs = [], sourceList = [], folderStack = [], folderCallback, jobsFilter = 'all', jobsPage = 1, jobsPages = 1, jobsPageSize = 20, jobsMaxId = null, jobsRequest = 0, toastTimer;
const states = {proxy_config_error:'代理配置错误',not_configured:'未配置',connecting:'连接中',connected:'已连接',disconnected:'连接中断',login_required:'待登录',scan_qr:'等待扫码',password_required:'需要两步验证',qr_expired:'二维码已过期',authorized:'已授权',expired:'二维码已过期',failed:'失败',idle:'未登录'};
const jobStates = {queued:'排队中',preparing:'读取分享',submitting:'正在提交',submitted:'云端处理中',success:'已完成',failed:'失败',uncertain:'结果待核对'};
const cleanupStates = {pending:'等待清理',deleting:'正在提交删除',submitted:'云端删除中',done:'已移入回收站',blocked:'清理已停止，需核对',uncertain:'删除结果待核对'};
let cleanupEnabled=false, historyPage=1, cleanupMinutes=60, clearToken=null, delayDirty=false;
function text(tag, value, cls) { const e=document.createElement(tag); e.textContent=value; if(cls)e.className=cls; return e; }
function toast(message) { $('notice').textContent=message; $('notice').hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>$('notice').hidden=true,7000); }
async function api(path, body, signal) {
  const options={headers:{'X-Sbtg-Client':'web'},signal};
  if(body!==undefined){options.method='POST';options.headers['Content-Type']='application/json';options.body=JSON.stringify(body);}
  const response=await fetch('/api'+path,options), data=await response.json();
  if(!response.ok)throw new Error(typeof data.detail==='string'?data.detail:'输入格式有误，请检查填写内容');
  return data;
}
function action(id, handler){$(id).addEventListener('click', async e=>{e.preventDefault();const b=$(id);b.disabled=true;try{await handler();}catch(error){toast(error.message);}finally{b.disabled=false;}});}
function image(id, src){const e=$(id);e.hidden=!src;if(src&&e.src!==src)e.src=src;if(!src)e.removeAttribute('src');}
async function refreshState(){
  const s=await api('/state');
  $('tg-state').textContent=states[s.telegram.state]||s.telegram.state;
  $('tg-error').textContent=s.telegram.error;
  image('tg-image',s.telegram.qr);
  $('password-form').hidden=s.telegram.state!=='password_required';
  $('tg-logout').hidden=s.telegram.state!=='connected';
  $('tg-qr').hidden=s.telegram.state==='connected';
  $('credentials').open=!s.telegram.configured;
  $('cloud-state').textContent=s.cloud.error?'需要处理':s.cloud.state==='scan_qr'?'等待扫码':s.cloud.configured?'已登录':states[s.cloud.state]||'待登录';
  $('cloud-error').textContent=s.cloud.error||'';
  image('cloud-image',s.cloud.qr);
  $('target').textContent=s.target.name;
  cleanupEnabled=s.auto_cleanup;
  cleanupMinutes=s.cleanup_minutes;
  if(!delayDirty)$('cleanup-delay').value=cleanupMinutes;
  $('cleanup-delay-current').textContent='当前等待时间：'+cleanupMinutes+' 分钟';
  const clear=s.directory_clear,labels={queued:'等待清空',running:'清空处理中',done:'已移入回收站',blocked:'清空已停止',uncertain:'结果待核对'};
  $('clear-status').textContent=clear?clear.parent_name+' · '+labels[clear.state]+' · 完成 '+clear.done+'/'+clear.total+' 批'+(clear.error?' · '+clear.error:''):'';
  $('cleanup-state').textContent=cleanupEnabled?'已启用':'关闭 / 暂停';
  $('toggle-cleanup').textContent=cleanupEnabled?'暂停自动清理':'启用自动清理';
  $('queued').textContent=s.counts.queued||0;
  $('active').textContent=(s.counts.preparing||0)+(s.counts.submitting||0)+(s.counts.submitted||0);
  $('success').textContent=s.counts.success||0;
  $('failed').textContent=(s.counts.failed||0)+(s.counts.uncertain||0);
}
$('tg-form').addEventListener('submit',async e=>{e.preventDefault();const f=e.target;try{await api('/telegram/config',{api_id:Number(f.api_id.value),api_hash:f.api_hash.value});f.api_hash.value='';toast('应用凭据已保存，可以扫码登录');await refreshState();}catch(error){toast(error.message);}});
$('password-form').addEventListener('submit',async e=>{e.preventDefault();const input=e.target.password,password=input.value;input.value='';try{await api('/telegram/password',{password});await refreshState();}catch(error){toast(error.message);}});
action('tg-qr',async()=>{await api('/telegram/qr',{});await refreshState();});
action('tg-logout',async()=>{if(confirm('退出此项目使用的 Telegram 会话，并停用所有监听来源？')){await api('/telegram/logout',{});await refreshState();await refreshSources();}});
action('cloud-qr',async()=>{await api('/cloud/qr',{});await refreshState();});
$('cleanup-delay').oninput=()=>{delayDirty=true;};
$('cleanup-delay-form').onsubmit=async e=>{
  e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;
  try{await api('/cleanup/delay',{minutes:Number($('cleanup-delay').value)});delayDirty=false;await refreshState();await refreshJobs();toast('等待时间已保存，对尚未提交的清理任务生效');}
  catch(error){toast(error.message);}finally{button.disabled=false;}
};
action('open-clear',async()=>{
  clearToken=null;$('confirm-clear').disabled=true;$('clear-items').replaceChildren();$('clear-summary').textContent='';
  const targets=await api('/clear/targets');$('clear-target').replaceChildren();
  for(const target of targets){const option=text('option',target.name);option.value=target.id;$('clear-target').append(option);}
  if(!targets.length){toast('请先选择一个非根目录作为转存目录');return;}
  $('clear-dialog').showModal();
});
let clearPreviewRequest=null;
function cancelClearPreview(){if(clearPreviewRequest)clearPreviewRequest.abort();clearPreviewRequest=null;clearToken=null;$('confirm-clear').disabled=true;}
$('clear-target').onchange=()=>{cancelClearPreview();$('clear-items').replaceChildren();$('clear-summary').textContent='请重新预览所选目录';};
$('clear-dialog').addEventListener('close',cancelClearPreview);
$('clear-dialog').addEventListener('cancel',cancelClearPreview);
action('close-clear',()=>$('clear-dialog').close());
action('preview-clear',async()=>{
  cancelClearPreview();const parent=$('clear-target').value;
  const request=new AbortController();clearPreviewRequest=request;
  const timer=setTimeout(()=>request.abort(),50000);
  $('clear-items').replaceChildren();
  $('clear-summary').textContent='正在读取目录…';
  try{
    const result=await api('/clear/preview',{id:parent},request.signal);
    if(clearPreviewRequest!==request)return;
    clearToken=result.id;$('clear-summary').textContent=result.name+'：'+result.files+' 个顶层文件、'+result.folders+' 个文件夹（含全部内部内容）。不受自动清理等待时间限制。';
    for(const item of result.items)$('clear-items').append(text('p',(item.kind===2?'文件夹：':'文件：')+item.name));
    $('confirm-clear').disabled=result.count===0;
  }catch(error){
    if(clearPreviewRequest===request)$('clear-summary').textContent=error.name==='AbortError'?'读取目录超时，未提交删除。请稍后重新预览。':'预览失败：'+error.message+'。未提交删除，请稍后重新预览。';
  }finally{clearTimeout(timer);if(clearPreviewRequest===request)clearPreviewRequest=null;}
});
action('confirm-clear',async()=>{
  if(!clearToken)return;
  try{await api('/clear/'+clearToken+'/confirm',{});}
  catch(error){$('clear-summary').textContent='提交未获确认：'+error.message+'。请核对页面清空状态；不要重复创建清空任务。';return;}
  clearToken=null;$('clear-dialog').close();await refreshState();toast('已提交清空请求；页面会显示各批次处理进度');
});
action('toggle-cleanup',async()=>{
  const enabled=!cleanupEnabled;
  if(enabled&&!confirm('启用后，资源直接保存到选定目录，本次新增且能核对的资源在转存成功达到配置的等待时间后移入回收站。已有到期的待清理任务也会恢复执行。确认启用？'))return;
  await api('/cleanup/settings',{enabled});await refreshState();toast(enabled?'自动清理已启用':'自动清理已暂停；已提交的删除仍会完成');
});
async function previewHistory(page){
  const data=await api('/cleanup/history?page='+page);historyPage=data.page;
  $('history-summary').textContent='历史记录第 '+data.page+' / '+data.pages+' 页，共 '+data.total+' 条。目录列表缓存 5 分钟；选定目标后按配置的等待时间规则清理。';
  $('history-items').replaceChildren();
  for(const job of data.items){
    const item=text('article','','history-item');item.append(text('h3','#'+job.id+' '+(job.resource_name||job.share_id)),text('p','任务创建：'+new Date(job.created*1000).toLocaleString()+' · 到期：'+new Date(job.due_at*1000).toLocaleString(),'hint'),text('p','原保存目录：'+job.target_name,'hint'),text('p',job.note,'hint'));
    for(const candidate of job.candidates){
      const entry=text('div','','history-candidate');
      entry.append(text('strong',(candidate.folder?'文件夹：':'文件：')+candidate.name),text('small','ID：'+candidate.id));
      const button=text('button','清理此目标（已到期）','secondary');
      button.onclick=async()=>{button.disabled=true;try{await api('/cleanup/history/'+job.id,{file_id:candidate.id,name:candidate.name});toast(cleanupEnabled?'已加入清理，后台将核对后移入回收站':'已加入清理；请启用自动清理开关后执行');await previewHistory(historyPage);await refreshJobs();}catch(e){toast(e.message);button.disabled=false;}};
      entry.append(button);item.append(entry);
    }
    $('history-items').append(item);
  }
  $('history-prev').hidden=data.page<=1;$('history-next').hidden=data.page>=data.pages;
}
action('history-preview',()=>previewHistory(1));
action('history-prev',()=>previewHistory(historyPage-1));
action('history-next',()=>previewHistory(historyPage+1));
async function refreshSources(){sourceList=await api('/sources');renderSources();}
function renderSources(){
  $('save-sources').hidden=!editingSources;
  $('cancel-sources').hidden=!editingSources;
  $('refresh-sources').hidden=!editingSources;
  $('edit-sources').hidden=editingSources;
  $('sources-hint').textContent=editingSources?'勾选频道、设置目录后，点击“保存监听”统一生效。':'仅显示已保存的监听频道；点击“编辑监听”可增减频道。';
  const list=$('sources');list.replaceChildren();
  const shown=editingSources?sourceList:sourceList.filter(s=>s.enabled);
  if(!shown.length)list.append(text('p',editingSources?'暂无聊天，请先登录 Telegram 并获取聊天列表。':'尚未选择监听来源，请点击“编辑监听”。','empty'));
  for(const source of shown){
    const row=text('div','','source');
    if(editingSources){const toggle=document.createElement('input');toggle.type='checkbox';toggle.checked=!!source.enabled;toggle.setAttribute('aria-label','监听 '+source.title);toggle.onchange=()=>{source.enabled=toggle.checked;};row.append(toggle);}
    else row.append(text('span','监听中','status'));
    const info=text('div','','source-info');info.append(text('strong',source.title),text('small',source.target_id===null?'保存到默认目录':source.target_name));row.append(info);
    if(editingSources){
      const actions=text('div','','source-actions'),choose=text('button','指定目录','secondary');
      choose.onclick=()=>openFolders(async target=>{source.target_id=target.id;source.target_name=target.name;renderSources();},'目录已选择，点击“保存监听”后生效').catch(e=>toast(e.message));actions.append(choose);
      if(source.target_id!==null){const reset=text('button','恢复默认','secondary');reset.onclick=()=>{source.target_id=null;source.target_name=null;renderSources();};actions.append(reset);}
      row.append(actions);
    }
    list.append(row);
  }
}
action('edit-sources',async()=>{await refreshSources();editingSources=true;renderSources();});
action('cancel-sources',async()=>{await refreshSources();editingSources=false;renderSources();});
action('save-sources',async()=>{
  const sources=sourceList.map(s=>({id:s.id,enabled:!!s.enabled,target:s.target_id===null?null:{id:s.target_id,name:s.target_name}}));
  await api('/sources/save',{sources});
  editingSources=false;renderSources();toast('监听设置已保存，正在监听 '+sources.filter(s=>s.enabled).length+' 个来源');
});
action('refresh-sources',async()=>{
  await api('/sources/refresh',{});
  const fresh=await api('/sources'),draft=new Map(sourceList.map(s=>[s.id,s]));
  sourceList=fresh.map(s=>draft.has(s.id)?{...draft.get(s.id),title:s.title}:s);renderSources();
});
let folderNotice='保存目录已更新';
async function openFolders(callback,notice='保存目录已更新'){folderNotice=notice;folderCallback=callback;folderStack=[{id:'',name:'/'}];$('folder-dialog').showModal();await loadFolders();}
async function loadFolders(){const current=folderStack.at(-1);$('folder-path').textContent=folderStack.map(x=>x.name).join('/').replace(/\/+/g,'/');$('folder-list').replaceChildren(text('p','正在加载…','hint'));$('folder-up').disabled=folderStack.length<2;const folders=await api('/folders?parent='+encodeURIComponent(current.id));$('folder-list').replaceChildren();for(const folder of folders){const b=text('button','📁 '+folder.name,'secondary');b.onclick=async()=>{folderStack.push(folder);try{await loadFolders();}catch(e){toast(e.message);}};$('folder-list').append(b);}if(!folders.length)$('folder-list').append(text('p','当前目录没有子文件夹。','hint'));}
action('choose-default',()=>openFolders(async target=>{await api('/target',target);await refreshState();}));
action('close-folders',()=>$('folder-dialog').close());
action('folder-up',async()=>{if(folderStack.length>1){folderStack.pop();await loadFolders();}});
action('folder-select',async()=>{await folderCallback({id:folderStack.at(-1).id,name:$('folder-path').textContent});$('folder-dialog').close();toast(folderNotice);});
async function refreshJobs(){
  const request=++jobsRequest;
  const snapshot=jobsMaxId===null?'':'&max_id='+jobsMaxId;
  const result=await api('/job-pages?page='+jobsPage+'&page_size='+jobsPageSize+'&status_filter='+jobsFilter+snapshot);
  if(request!==jobsRequest)return;
  jobsPage=result.page;jobsPages=result.pages;
  // First page follows incoming messages; older pages retain their snapshot boundary.
  jobsMaxId=jobsPage===1?null:result.max_id;
  $('next-jobs').dataset.snapshot=result.max_id;
  $('previous-jobs').disabled=jobsPage<=1;
  $('next-jobs').disabled=jobsPage>=jobsPages;
  $('page-number').value=jobsPage;$('page-number').max=jobsPages;
  $('page-summary').textContent='第 '+jobsPage+' / '+jobsPages+' 页 · 共 '+result.total+' 条';
  const jobs=result.items;visibleJobs=jobs;$('jobs').replaceChildren();for(const job of jobs){const row=document.createElement('tr'),share=document.createElement('td'),link=text('a',job.resource_name||'资源名称待补全');link.href=job.url;link.target='_blank';link.rel='noreferrer';share.append(link,text('small',job.share_id),text('small',(job.source_title||'未知来源')+' · '+new Date(job.created*1000).toLocaleString()));const status=text('td',jobStates[job.status]||job.status);if(job.error)status.append(text('small',job.error,'job-error'));if(job.completed_at)status.append(text('small','转存成功：'+new Date(job.completed_at*1000).toLocaleString()));if(job.cleanup_state)status.append(text('small',cleanupStates[job.cleanup_state]||job.cleanup_state));if(job.cleanup_state==='pending')status.append(text('small','到期：'+new Date(((job.completed_at??job.created)+(cleanupMinutes*60))*1000).toLocaleString()+(cleanupEnabled?'':'（已暂停）')));if(job.cleanup_error)status.append(text('small',job.cleanup_error,'job-error'));const operations=document.createElement('td');if(['failed','uncertain'].includes(job.status)){const b=text('button','重试 / 补码','secondary');b.onclick=async()=>{if(job.status==='uncertain'&&!confirm('请先检查云盘。确认这次分享尚未转存，仍要重新提交吗？'))return;const code=prompt('补填提取码（留空保留原值）：','');if(code===null)return;b.disabled=true;try{await api('/jobs/'+job.id+'/retry',{code,confirm_uncertain:job.status==='uncertain'});await refreshJobs();}catch(e){toast(e.message);}finally{b.disabled=false;}};operations.append(b);}row.append(share,text('td',job.target_name+(job.cleanup_folder&&job.remote_task&&job.cleanup_name.startsWith('sbtg-')?'/'+job.cleanup_name:'')),status,operations);$('jobs').append(row);}if(!jobs.length){const r=document.createElement('tr'),c=text('td',jobsFilter==='all'?'暂无记录。启用监听后，新发现的分享会出现在这里。':jobsFilter==='queued'?'暂无待处理记录。':'暂无需要处理的记录。','empty');c.colSpan=4;r.append(c);$('jobs').append(r);}}
async function filterJobs(value,scroll=false){
  jobsFilter=value;jobsPage=1;jobsMaxId=null;$('job-filter').value=value;
  if(scroll){$('jobs-heading').focus({preventScroll:true});$('jobs-section').scrollIntoView({behavior:'smooth',block:'start'});}
  await refreshJobs();
}
action('show-queued',()=>filterJobs('queued',true));
action('show-attention',()=>filterJobs('attention',true));
$('job-filter').onchange=e=>filterJobs(e.target.value).catch(error=>toast(error.message));
action('fill-titles',async()=>{
  const missing=visibleJobs.filter(j=>!j.resource_name);
  if(!missing.length){toast('本页资源名称已齐全');return;}
  let done=0,failed=0;
  for(const job of missing){try{await api('/jobs/'+job.id+'/title',{});done++;}catch(e){failed++;}}
  await refreshJobs();toast('已补全 '+done+' 条名称'+(failed?'；'+failed+' 条暂不可读取，请检查登录或分享有效性':''));
});
action('refresh-jobs',async()=>{jobsPage=1;jobsMaxId=null;await refreshJobs();});
async function navigateJobs(page){
  if(page<1||page>jobsPages)return;
  if(jobsMaxId===null)jobsMaxId=Number($('next-jobs').dataset.snapshot||0);
  jobsPage=page;
  try{await refreshJobs();}catch(e){toast(e.message);}
}
$('previous-jobs').onclick=()=>navigateJobs(jobsPage-1);
$('next-jobs').onclick=()=>navigateJobs(jobsPage+1);
$('page-size').onchange=async e=>{jobsPageSize=Number(e.target.value);jobsPage=1;jobsMaxId=null;try{await refreshJobs();}catch(error){toast(error.message);}};
$('jump-page').onsubmit=e=>{e.preventDefault();navigateJobs(Number($('page-number').value));};
async function poll(){try{await refreshState();await refreshJobs();}catch(error){toast('管理服务连接异常：'+error.message);}setTimeout(poll,4000);}
refreshSources().catch(e=>toast(e.message));poll();
