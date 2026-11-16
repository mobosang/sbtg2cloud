const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function setup(fetch,confirm=()=>true){
  const elements=new Map(), timers=new Map();let timer=0;
  function element(){return {value:'parent',disabled:false,textContent:'',handlers:{},children:[],
    addEventListener(event,fn){this.handlers[event]=fn;},removeAttribute(){},
    replaceChildren(...items){this.children=items;},append(...items){this.children.push(...items);}};}
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const context={document:{getElementById:get,createElement:element},fetch,AbortController,confirm,
    setTimeout(fn,ms){timers.set(++timer,{fn,ms});return timer;},clearTimeout(id){timers.delete(id);}};
  const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
  vm.runInNewContext(source.replace('refreshSources().catch(e=>toast(e.message));poll();',''),context);
  return {get,timers,context,click:id=>get(id).handlers.click({preventDefault(){}})};
}
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
test('interval form saves seconds and polling preserves unsaved input',async()=>{
  const state={telegram:{state:'connected',error:'',configured:true},cloud:{state:'idle',configured:true},
    target:{name:'/Media'},auto_cleanup:true,cleanup_minutes:30,cleanup_interval_seconds:30,
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
    target:{name:'/Media'},auto_cleanup:false,cleanup_minutes:30,cleanup_interval_seconds:30,
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

test('cache list shows safe names, expiry, hit counts and pagination without exposing URLs',async()=>{
  const calls=[];
  const entry={path:'/Media/<video>.mkv',created:100,expires_at:200,retained_until:86500,last_used:150,hits:7,usable:true};
  const ui=setup(async url=>{
    calls.push(url);const page=url.includes('page=2')?2:1;
    return {ok:true,json:async()=>({page,pages:2,page_size:20,total:21,valid:20,retention_hours:24,
      items:[{...entry,usable:page===1}]})};
  });
  await ui.context.refreshMediaCache();
  assert.match(ui.get('media-cache-summary').textContent,/24.*21.*20.*1/);
  const row=ui.get('media-cache-items').children[0];
  assert.equal(row.children[0].children[0].textContent,'<video>.mkv');
  assert.equal(row.children[3].textContent,'7');
  assert.equal(ui.get('media-cache-prev').disabled,true);
  await ui.get('media-cache-next').onclick();
  assert.equal(ui.get('media-cache-next').disabled,true);
  assert.match(ui.get('media-cache-items').children[0].children[2].children[0].textContent,/已过期.*下次播放刷新/);
  assert.equal(calls.length,2);
});

test('clear cache requires confirmation and then refreshes the local list',async()=>{
  const calls=[];
  const ui=setup(async url=>{
    calls.push(url);return {ok:true,json:async()=>url.endsWith('/clear')?{cleared:2}:
      {page:1,pages:1,page_size:20,total:0,valid:0,retention_hours:24,items:[]}};
  });
  await ui.click('clear-media-cache');
  assert.deepEqual(calls,['/api/media/cache/clear','/api/media/cache?page=1']);
  assert.match(ui.get('notice').textContent,/2.*直链缓存/);
  assert.match(ui.get('media-cache-items').children[0].children[0].textContent,/暂无缓存/);
  const cancelled=[];
  const other=setup(async url=>{cancelled.push(url);},()=>false);
  await other.click('clear-media-cache');assert.deepEqual(cancelled,[]);
});
