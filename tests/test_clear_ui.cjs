const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const emptyQuota={limit_gb:1000,used_bytes:0,remaining_bytes:1e12,completed_bytes:0};
function setup(fetch,confirm=()=>true,hash=''){
  const elements=new Map(), timers=new Map(), events={};let timer=0;
  function element(){return {value:'parent',disabled:false,textContent:'',handlers:{},children:[],attributes:{},dataset:{},
    addEventListener(event,fn){this.handlers[event]=fn;},setAttribute(name,value){this.attributes[name]=value;},
    removeAttribute(name){delete this.attributes[name];},focus(){},scrollIntoView(){},
    replaceChildren(...items){this.children=items;},append(...items){this.children.push(...items);}};}
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const context={document:{getElementById:get,createElement:element},fetch,AbortController,confirm,
    location:{hostname:'192.0.2.1',hash},window:{addEventListener(event,fn){events[event]=fn;}},
    setTimeout(fn,ms){timers.set(++timer,{fn,ms});return timer;},clearTimeout(id){timers.delete(id);}};
  const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
  vm.runInNewContext(source.replace('refreshSources().catch(e=>toast(e.message));poll();',''),context);
  return {get,timers,context,events,click:id=>get(id).handlers.click({preventDefault(){}})};
}

test('sidebar shows only the selected feature and follows back navigation',()=>{
  const ui=setup(undefined,undefined,'#sources');
  for(const page of ['overview','telegram','cloud','sources','jobs','cleanup','media'])
    assert.equal(ui.get('page-'+page).hidden,page!=='sources');
  assert.equal(ui.get('nav-sources').attributes['aria-current'],'page');
  ui.context.location.hash='#media';ui.events.hashchange();
  assert.equal(ui.get('page-media').hidden,false);
  assert.equal(ui.get('page-sources').hidden,true);
  assert.equal(ui.get('nav-sources').attributes['aria-current'],undefined);
  ui.context.location.hash='#sources';ui.events.hashchange();
  assert.equal(ui.get('page-sources').hidden,false);
  ui.context.location.hash='#invalid';ui.events.hashchange();
  assert.equal(ui.get('page-overview').hidden,false);
});

test('record shortcuts open the records page as well as its filter',async()=>{
  const ui=setup();ui.context.refreshJobs=async()=>{};
  await ui.click('show-cleanup');
  assert.equal(ui.context.location.hash,'jobs');
  assert.equal(ui.get('page-jobs').hidden,false);
  assert.equal(ui.get('page-overview').hidden,true);
  assert.equal(ui.get('job-filter').value,'cleanup');
});

test('manual share queues once, refreshes records and reports existing tasks',async()=>{
  for(const created of [true,false]){
    const calls=[];
    const ui=setup(async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});
      return {ok:true,json:async()=>({created,job_id:42,status:created?'queued':'success'})};});
    let refreshed=0;ui.context.filterJobs=async value=>{assert.equal(value,'all');refreshed++;};
    ui.context.refreshState=async()=>{refreshed++;};
    ui.get('manual-share-link').value=' https://www.guangyapan.com/s/example 提取码：A123 ';
    await ui.get('manual-share-form').onsubmit({preventDefault(){}});
    assert.deepEqual(calls,[{url:'/api/jobs/manual',body:{text:'https://www.guangyapan.com/s/example 提取码：A123'}}]);
    assert.equal(ui.get('manual-share-link').value,'');
    assert.equal(ui.get('manual-share-submit').disabled,false);
    assert.equal(refreshed,2);
    assert.match(ui.get('notice').textContent,created?/已加入转存队列.*#42/:/手动添加.*原任务 #42.*重复.*已完成.*重复跳过/);
  }
});

test('transfer records identify the manual duplicate and original job even when collapsed',async()=>{
  const ui=setup(async()=>({ok:true,json:async()=>({
    items:[],page:1,pages:1,total:0,
    duplicates:[{job_id:42,created:1,manual:1,source_title:'手动提交',title:'Example',status:'success'}]
  })}));
  await ui.context.refreshJobs();
  for(const line of [ui.get('duplicate-summary').textContent,ui.get('duplicate-list').children[0].textContent])
    assert.match(line,/手动添加.*Example.*原任务 #42.*重复.*已完成.*已跳过/);
});

test('manual share preserves input on failure and blocks duplicate submissions in flight',async()=>{
  let resolve;const calls=[];
  const ui=setup(url=>{calls.push(url);return new Promise(r=>{resolve=r;});});
  const input=ui.get('manual-share-link');input.value='https://re0.me/resource/example';
  const pending=ui.get('manual-share-form').onsubmit({preventDefault(){}});
  await ui.get('manual-share-form').onsubmit({preventDefault(){}});
  assert.equal(calls.length,1);assert.equal(ui.get('manual-share-submit').disabled,true);
  resolve({ok:false,json:async()=>({detail:'不支持中转站链接'})});await pending;
  assert.equal(input.value,'https://re0.me/resource/example');
  assert.equal(ui.get('manual-share-submit').disabled,false);
  assert.match(ui.get('notice').textContent,/不支持中转站/);
});

test('manual share does not clear another link typed while the request is pending',async()=>{
  let resolve;
  const ui=setup(()=>new Promise(r=>{resolve=r;}));
  ui.context.filterJobs=async()=>{};ui.context.refreshState=async()=>{};
  const input=ui.get('manual-share-link');input.value='https://www.guangyapan.com/s/first';
  const pending=ui.get('manual-share-form').onsubmit({preventDefault(){}});
  input.value='https://www.guangyapan.com/s/second';
  resolve({ok:true,json:async()=>({created:true,job_id:1,status:'queued'})});await pending;
  assert.equal(input.value,'https://www.guangyapan.com/s/second');
});
test('preview failure is visible in dialog and clears loading state',async()=>{
  const ui=setup(async()=>({ok:false,json:async()=>({detail:'有云端任务正在处理'})}));
  await ui.click('preview-clear');
  assert.match(ui.get('clear-summary').textContent,/预览失败.*有云端任务/);
  assert.equal(ui.get('confirm-clear').disabled,true);
  assert.equal(ui.get('preview-clear').disabled,false);
});
test('preview timeout releases button without any deletion request',async()=>{
  const calls=[];
  const ui=setup((url,options)=>{calls.push(url);return new Promise((resolve,reject)=>{
    options.signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})));
  });});
  const pending=ui.click('preview-clear');
  [...ui.timers.values()].find(x=>x.ms===50000).fn();await pending;
  assert.match(ui.get('clear-summary').textContent,/超时.*未提交删除/);
  assert.equal(ui.get('preview-clear').disabled,false);
  assert.deepEqual(calls,['/api/clear/preview']);
});
test('late preview cannot enable deletion after changing destination',async()=>{
  let resolve;
  const ui=setup(()=>new Promise(r=>{resolve=r;}));
  const pending=ui.click('preview-clear');ui.get('clear-target').onchange();
  resolve({ok:true,json:async()=>({id:'draft',name:'/Media',files:1,folders:0,count:1,items:[]})});
  await pending;
  assert.equal(ui.get('confirm-clear').disabled,true);
  assert.match(ui.get('clear-summary').textContent,/请重新预览/);
});
test('successful preview enables confirmation, submission error remains visible',async()=>{
  const ui=setup(async url=>url.endsWith('/preview')?{ok:true,json:async()=>({id:'draft',name:'/Media',files:1,folders:0,count:1,items:[]})}:{ok:false,json:async()=>({detail:'预览已超过五分钟'})});
  await ui.click('preview-clear');assert.equal(ui.get('confirm-clear').disabled,false);
  await ui.click('confirm-clear');assert.match(ui.get('clear-summary').textContent,/提交未获确认.*五分钟/);
});
test('bulk discard submits the confirmed cross-page snapshot once',async()=>{
  const ids=Array.from({length:575},(_,i)=>i+1),calls=[];
  let message;
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    return {ok:true,json:async()=>url.endsWith('discard-preview')?{ids,count:575,uncertain:1}:{discarded:574,skipped:1}};
  },text=>{message=text;return true;});
  ui.context.refreshJobs=async()=>{};ui.context.refreshState=async()=>{};
  await ui.click('discard-attention');
  assert.match(message,/575.*跨全部分页/);
  assert.match(message,/可能已在云盘转存/);
  assert.equal(calls.length,2);
  assert.deepEqual(JSON.parse(calls[1].body),{ids});
  assert.match(ui.get('notice').textContent,/574.*1.*跳过/);
});
test('cancelling bulk discard sends no mutation',async()=>{
  const calls=[];
  const ui=setup(async url=>{calls.push(url);return {ok:true,json:async()=>({ids:[1],count:1,uncertain:0})};},()=>false);
  await ui.click('discard-attention');
  assert.deepEqual(calls,['/api/jobs/discard-preview']);
});
test('bulk retry queues all confirmed IDs and refreshes counts',async()=>{
  const ids=Array.from({length:575},(_,i)=>i+1),calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    return {ok:true,json:async()=>url.endsWith('retry-preview')?{ids,count:575,uncertain:0}:{retried:574,skipped:1}};
  });
  let refreshed=0;ui.context.refreshJobs=async()=>{refreshed++;};ui.context.refreshState=async()=>{refreshed++;};
  await ui.click('retry-attention');
  assert.equal(calls.length,2);
  assert.deepEqual(JSON.parse(calls[1].body),{ids,confirm_uncertain:false});
  assert.equal(refreshed,2);
  assert.match(ui.get('notice').textContent,/574.*1.*跳过/);
});
test('uncertain bulk retry requires a separate confirmation',async()=>{
  const calls=[],messages=[];
  const ui=setup(async url=>{
    calls.push(url);return {ok:true,json:async()=>({ids:[1,2],count:2,uncertain:1})};
  },message=>{messages.push(message);return messages.length===1;});
  await ui.click('retry-attention');
  assert.equal(messages.length,2);
  assert.match(messages[1],/均尚未转存/);
  assert.deepEqual(calls,['/api/jobs/retry-preview']);
});
test('confirmed uncertain bulk retry passes explicit permission',async()=>{
  const calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    return {ok:true,json:async()=>url.endsWith('retry-preview')?{ids:[1,2],count:2,uncertain:1}:{retried:2,skipped:0}};
  });
  ui.context.refreshJobs=async()=>{};ui.context.refreshState=async()=>{};
  await ui.click('retry-attention');
  assert.deepEqual(JSON.parse(calls[1].body),{ids:[1,2],confirm_uncertain:true});
});
test('share size limit saves GB and polling preserves unsaved input',async()=>{
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},share_size_limit_gb:200,auto_cleanup:true,cleanup_minutes:30,cleanup_interval_seconds:30,
    daily_transfer_quota:emptyQuota,
    cleanup_queue:{},cleanup_progress:{last:null,current:null,next:null},directory_clear:null,counts:{}};
  const calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    if(url.endsWith('/shares/limit'))state.share_size_limit_gb=JSON.parse(options.body).gb;
    return {ok:true,json:async()=>url.endsWith('/state')?state:{ok:true}};
  });
  await ui.context.refreshState();
  assert.equal(ui.get('share-limit').value,200);
  const input=ui.get('share-limit');input.value='500';input.oninput();
  await ui.context.refreshState();assert.equal(input.value,'500');
  const button={disabled:false};
  await ui.get('share-limit-form').onsubmit({preventDefault(){},target:{querySelector:()=>button}});
  assert.equal(state.share_size_limit_gb,500);
  assert.equal(button.disabled,false);
  assert.match(ui.get('share-limit-current').textContent,/500 GB/);
  assert.deepEqual(JSON.parse(calls.find(x=>x.url.endsWith('/shares/limit')).body),{gb:500});
});

test('failed limit save keeps the input editable and reports the error',async()=>{
  const ui=setup(async()=>({ok:false,json:async()=>({detail:'输入格式有误'})}));
  ui.get('share-limit').value='0';ui.get('share-limit').oninput();
  const button={disabled:false};
  await ui.get('share-limit-form').onsubmit({preventDefault(){},target:{querySelector:()=>button}});
  assert.equal(button.disabled,false);
  assert.equal(ui.get('share-limit').value,'0');
  assert.equal(ui.get('notice').textContent,'输入格式有误');
});

test('daily transfer limit displays usage and saves the edited value',async()=>{
  const quota={limit_gb:1000,used_bytes:25e9,remaining_bytes:975e9,completed_bytes:20e9};
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},share_size_limit_gb:200,daily_transfer_quota:quota,auto_cleanup:true,
    cleanup_minutes:30,cleanup_interval_seconds:30,cleanup_queue:{},cleanup_progress:{},counts:{}};
  const calls=[];
  const ui=setup(async(url,options)=>{
    if(url.endsWith('/shares/daily-limit')){
      const body=JSON.parse(options.body);calls.push(body);
      quota.limit_gb=body.gb;quota.remaining_bytes=body.gb*1e9-quota.used_bytes;
    }
    return {ok:true,json:async()=>url.endsWith('/state')?state:{ok:true}};
  });
  await ui.context.refreshState();
  const input=ui.get('daily-transfer-limit');assert.equal(input.value,1000);
  assert.match(ui.get('daily-transfer-current').textContent,/成功合计 20 GB.*25 \/ 1000 GB.*975 GB/);
  input.value='750';input.oninput();
  await ui.context.refreshState();assert.equal(input.value,'750');
  const button={disabled:false};
  await ui.get('daily-transfer-form').onsubmit({preventDefault(){},target:{querySelector:()=>button}});
  assert.deepEqual(calls,[{gb:750}]);assert.equal(button.disabled,false);
  assert.match(ui.get('daily-transfer-current').textContent,/25 \/ 750 GB.*725 GB/);
});

test('interval form saves seconds and polling preserves unsaved input',async()=>{
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},share_size_limit_gb:200,auto_cleanup:true,cleanup_minutes:30,cleanup_interval_seconds:30,
    daily_transfer_quota:emptyQuota,
    cleanup_queue:{pending:75,submitted:1},cleanup_progress:{last:null,current:null,next:null},directory_clear:null,counts:{}};
  const calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    if(url.endsWith('/cleanup/interval'))state.cleanup_interval_seconds=JSON.parse(options.body).seconds;
    return {ok:true,json:async()=>url.endsWith('/state')?state:{ok:true}};
  });
  await ui.context.refreshState();
  assert.match(ui.get('cleanup-queue-summary').textContent,/全部分页.*75.*1/);
  const input=ui.get('cleanup-interval');input.value='5';input.oninput();
  await ui.context.refreshState();assert.equal(input.value,'5');
  const button={disabled:false};
  await ui.get('cleanup-interval-form').onsubmit({preventDefault(){},target:{querySelector:()=>button}});
  assert.equal(state.cleanup_interval_seconds,5);
  assert.equal(state.cleanup_minutes,30);
  assert.equal(button.disabled,false);
  assert.deepEqual(JSON.parse(calls.find(x=>x.url.endsWith('/cleanup/interval')).body),{seconds:5});
});
test('cleanup shortcut opens the full cleanup filter',async()=>{
  const ui=setup();let selected;
  ui.context.filterJobs=async(value,scroll)=>{selected={value,scroll};};
  await ui.click('show-cleanup');
  assert.deepEqual(selected,{value:'cleanup',scroll:true});
});

test('media settings preserve edits during polling and save the playback mapping',async()=>{
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},share_size_limit_gb:200,daily_transfer_quota:emptyQuota,auto_cleanup:false,cleanup_minutes:30,cleanup_interval_seconds:30,
    cleanup_queue:{},cleanup_progress:{last:null,current:null,next:null},directory_clear:null,counts:{},
    media_proxy:{enabled:false,listening:true,upstream_url:'http://emby.test:8096',emby_prefix:'/Cloud/Guangya',
      cloud_prefix:'/',public_port:'12359',redirects:1,last:{name:'<video>.mkv',at:100},error:''}};
  const calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    if(url.endsWith('/media/settings'))Object.assign(state.media_proxy,JSON.parse(options.body));
    return {ok:true,json:async()=>url.endsWith('/state')?state:{ok:true}};
  });
  ui.context.location={hostname:'192.0.2.1'};
  await ui.context.refreshState();
  assert.equal(ui.get('media-address').value,'http://192.0.2.1:12359');
  assert.match(ui.get('media-last').textContent,/<video>\.mkv/);
  ui.get('media-prefix').value='/CloudNAS/Guangya';ui.get('media-enabled').checked=true;
  ui.get('media-form').handlers.input();
  await ui.context.refreshState();assert.equal(ui.get('media-prefix').value,'/CloudNAS/Guangya');
  const button={disabled:false};
  await ui.get('media-form').onsubmit({preventDefault(){},target:{querySelector:()=>button}});
  assert.deepEqual(JSON.parse(calls.find(x=>x.url.endsWith('/media/settings')).body),{
    enabled:true,upstream_url:'http://emby.test:8096',emby_prefix:'/CloudNAS/Guangya',cloud_prefix:'/'});
  assert.equal(ui.get('media-state').textContent,'已启用');
  assert.equal(button.disabled,false);
});
test('cleanup resource cards show safe names, real stages and empty states',()=>{
  const ui=setup();
  for(const [phase,label] of [['checking','正在核对目录内容'],['deleting','正在提交删除'],
                            ['submitted','云端删除中'],['polling','正在查询'],['queued','已到期'],['paused','已暂停']]){
    ui.context.cleanupResource('cleanup-current',{id:7,resource_name:'<img src=x onerror=alert(1)>',phase},'空');
    const children=ui.get('cleanup-current').children;
    assert.equal(children[0].textContent,'#7 <img src=x onerror=alert(1)>');
    assert.ok(children[1].textContent.includes(label));
  }
  ui.context.cleanupResource('cleanup-current',null,'当前没有正在处理的清理任务');
  assert.equal(ui.get('cleanup-current').children.length,1);
  assert.match(ui.get('cleanup-current').children[0].textContent,/没有正在处理/);
  ui.context.cleanupResource('cleanup-last',{id:8,share_id:'share',phase:'legacy_done'},'空');
  assert.match(ui.get('cleanup-last').children[1].textContent,/历史完成时间未记录/);
});

test('secondary playback settings do not overwrite the existing entrance or its draft',async()=>{
  const primary={enabled:true,listening:true,upstream_url:'http://first.test:8096',emby_prefix:'/Cloud/Guangya',
    cloud_prefix:'/',public_port:'12359',redirects:1,error:''};
  const secondary={...primary,upstream_url:'http://second.test:8096',public_port:'12360',redirects:0};
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},share_size_limit_gb:200,daily_transfer_quota:emptyQuota,auto_cleanup:false,cleanup_minutes:30,cleanup_interval_seconds:10,
    cleanup_queue:{},cleanup_progress:{last:null,current:null,next:null},directory_clear:null,counts:{},
    media_proxy:primary,media_proxy_secondary:secondary};
  const calls=[];
  const ui=setup(async(url,options)=>{
    calls.push({url,body:options.body});
    if(url.endsWith('/media/secondary/settings'))Object.assign(secondary,JSON.parse(options.body));
    return {ok:true,json:async()=>url.endsWith('/state')?state:{ok:true}};
  });
  ui.context.location={hostname:'192.0.2.1'};
  await ui.context.refreshState();
  assert.equal(ui.get('media-address').value,'http://192.0.2.1:12359');
  assert.equal(ui.get('media-secondary-address').value,'http://192.0.2.1:12360');
  ui.get('media-prefix').value='/UnchangedDraft';ui.get('media-form').handlers.input();
  ui.get('media-secondary-upstream').value='http://new-second.test:8096';
  ui.get('media-secondary-form').handlers.input();
  await ui.get('media-secondary-form').onsubmit({preventDefault(){},target:{querySelector:()=>({})}});
  assert.equal(primary.upstream_url,'http://first.test:8096');
  assert.equal(secondary.upstream_url,'http://new-second.test:8096');
  assert.equal(ui.get('media-prefix').value,'/UnchangedDraft');
  assert.equal(calls.filter(x=>x.body).length,1);
  assert.equal(calls.find(x=>x.body).url,'/api/media/secondary/settings');
});

test('cache list shows safe names, expiry, hit counts and pagination without exposing URLs',async()=>{
  const calls=[];
  const entry={path:'/Media/<video>.mkv',created:100,expires_at:200,retained_until:200,last_used:150,hits:7,usable:true};
  const ui=setup(async url=>{
    calls.push(url);const page=url.includes('page=2')?2:1;
    return {ok:true,json:async()=>({page,pages:2,page_size:20,total:21,valid:21,
      items:[{...entry,usable:page===1}]})};
  });
  await ui.context.refreshMediaCache();
  assert.match(ui.get('media-cache-summary').textContent,/与直链同步到期.*21/);
  const row=ui.get('media-cache-items').children[0];
  assert.equal(row.children[0].children[0].textContent,'<video>.mkv');
  assert.equal(row.children[3].textContent,'7');
  assert.equal(ui.get('media-cache-prev').disabled,true);
  await ui.get('media-cache-next').onclick();
  assert.equal(ui.get('media-cache-next').disabled,true);
  assert.match(ui.get('media-cache-items').children[0].children[2].children[0].textContent,/已到期.*等待列表刷新/);
  assert.equal(calls.length,2);
});

test('clear cache requires confirmation and then refreshes the local list',async()=>{
  const calls=[];
  const ui=setup(async url=>{
    calls.push(url);return {ok:true,json:async()=>url.endsWith('/clear')?{cleared:2}:
      {page:1,pages:1,page_size:20,total:0,valid:0,items:[]}};
  });
  await ui.click('clear-media-cache');
  assert.deepEqual(calls,['/api/media/cache/clear','/api/media/cache?page=1']);
  assert.match(ui.get('notice').textContent,/2.*直链缓存/);
  assert.match(ui.get('media-cache-items').children[0].children[0].textContent,/暂无缓存/);
  const cancelled=[];
  const other=setup(async url=>{cancelled.push(url);},()=>false);
  await other.click('clear-media-cache');assert.deepEqual(cancelled,[]);
});
