const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
function setup(fetch){
  const elements=new Map(), timers=new Map();let timer=0;
  function element(){return {value:'parent',disabled:false,textContent:'',handlers:{},children:[],
    addEventListener(event,fn){this.handlers[event]=fn;},
    replaceChildren(...items){this.children=items;},append(item){this.children.push(item);}};}
  const get=id=>{if(!elements.has(id))elements.set(id,element());return elements.get(id);};
  const context={document:{getElementById:get,createElement:element},fetch,AbortController,
    setTimeout(fn,ms){timers.set(++timer,{fn,ms});return timer;},clearTimeout(id){timers.delete(id);}};
  const source=fs.readFileSync(path.join(__dirname,'../app/static/app.js'),'utf8');
  vm.runInNewContext(source.replace('refreshSources().catch(e=>toast(e.message));poll();',''),context);
  return {get,timers,click:id=>get(id).handlers.click({preventDefault(){}})};
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
