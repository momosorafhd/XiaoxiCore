(function(root,factory){'use strict';const exports=factory();if(typeof module==='object'&&module.exports)module.exports=exports;else{root.XixiStatus=exports;exports.monitor=exports.mount(root);}})(typeof window==='object'?window:globalThis,function(){
'use strict';
class Monitor {
 constructor({request,visible=()=>true,render=()=>{},state=()=>{},setTimer=(fn,ms)=>setTimeout(fn,ms),clearTimer=id=>clearTimeout(id),clock=()=>Date.now(),interval=1000,timeout=7000,makeAbort=()=>new AbortController()}){Object.assign(this,{request,visible,render,state,setTimer,clearTimer,clock,interval,timeout,makeAbort});this.generation=0;this.running=false;this.manual=false;this.timer=null;this.abort=null;this.deadline=null;}
 start(){if(this.manual||!this.visible()||this.running)return;this.running=true;this.generation++;this.tick();}
 stop(reason='paused'){this.running=false;this.generation++;if(this.timer!==null)this.clearTimer(this.timer);if(this.deadline!==null)this.clearTimer(this.deadline);this.timer=this.deadline=null;if(this.abort)this.abort.abort();this.abort=null;this.state(reason);}
 pause(){this.manual=true;this.stop('paused');}
 resume(){this.manual=false;this.start();}
 refresh(){if(!this.running||this.abort)return;if(this.timer!==null)this.clearTimer(this.timer);this.timer=null;this.tick();}
 async tick(){if(!this.running||!this.visible()){this.stop('hidden');return;}const generation=this.generation,start=this.clock(),abort=this.makeAbort();this.abort=abort;this.state('sampling');this.deadline=this.setTimer(()=>abort.abort(),this.timeout);
  try{const value=await this.request(abort.signal);if(this.running&&generation===this.generation&&this.visible()){this.render(value);this.state(value.status==='ok'?'live':'unavailable');}}
  catch(error){if(this.running&&generation===this.generation){if(error&&error.code==='authentication_required'){this.manual=true;this.stop('expired');return;}this.state('error');}}
  finally{if(generation===this.generation){if(this.deadline!==null)this.clearTimer(this.deadline);this.deadline=null;this.abort=null;if(this.running&&this.visible())this.timer=this.setTimer(()=>{this.timer=null;this.tick();},Math.max(0,this.interval-(this.clock()-start)));}}
 }
}
function plot(svg,series,maximum=100){
 const count=Math.max(2,series.length),segments=[];let segment=[];
 series.forEach((value,index)=>{if(typeof value!=='number'||!Number.isFinite(value)){if(segment.length)segments.push(segment);segment=[];return;}segment.push([600*index/(count-1),180*(1-Math.min(maximum,Math.max(0,value))/maximum)]);});
 if(segment.length)segments.push(segment);
 const path=points=>points.map((p,i)=>(i?'L':'M')+p.map(v=>v.toFixed(2)).join(' ')).join(' ');
 const line=segments.map(path).join(' '),area=segments.map(points=>path(points)+' L'+points.at(-1)[0].toFixed(2)+' 180 L'+points[0][0].toFixed(2)+' 180 Z').join(' ');
 for(const [selector,value] of [['.trend',line],['.area',area]]){const el=svg.querySelector(selector);if(el.getAttribute('d')!==value)el.setAttribute('d',value);}
}
function syncList(list,items,key,create,update){
 if(!list._items){list._items=new Map();list.replaceChildren();}
 const keys=new Set();items.forEach((item,index)=>{const id=key(item);keys.add(id);let el=list._items.get(id);if(!el){el=create(item);list._items.set(id,el);}update(el,item);if(list.children[index]!==el)list.insertBefore(el,list.children[index]||null);});
 for(const [id,el] of list._items){if(!keys.has(id)){el.remove();list._items.delete(id);}}
}
function gpuMemoryPercent(gpu){
 const used=gpu.vram_used_bytes,total=gpu.vram_total_bytes;
 return typeof used==='number'&&Number.isFinite(used)&&typeof total==='number'&&Number.isFinite(total)&&total>0&&used>=0&&used<=total?100*used/total:null;
}
function fanState(fan){
 if(fan.status==='sleeping')return {mode:'sleeping',rpm:null,label:'设备休眠',running:false};
 if(fan.status==='ok'&&typeof fan.rpm==='number'&&Number.isFinite(fan.rpm)&&fan.rpm>=0&&fan.rpm<=100000)return {mode:fan.rpm===0?'stopped':'measured',rpm:fan.rpm,label:fan.rpm===0?'风扇停转':'传感器读数',running:fan.rpm>0};
 return {mode:'unavailable',rpm:null,label:'转速未公开',running:false};
}
function mount(win){
 const doc=win.document,by=id=>doc.getElementById(id);if(!by('monitor-state'))return;
 const histories={cpu:Array(60).fill(null),memory:Array(60).fill(null),cores:new Map(),gpus:new Map(),gpuVram:new Map()};let manual=false,gap=false;
 const set=(el,value)=>{if(el.textContent!==String(value))el.textContent=value;};
 const text=(id,value)=>set(by(id),value);
 const number=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(1):'—';
 const bytes=value=>typeof value==='number'?(value/1073741824).toFixed(2)+' GiB':'—';
 const node=(tag,content,cls)=>{const el=doc.createElement(tag);if(content!==undefined)el.textContent=content;if(cls)el.className=cls;return el;};
 function render(data){
  const cpu=data.cpu||{},memory=data.memory||{};
  text('cpu-mode',cpu.power_mode?.label||'模式未确认');by('cpu-mode').dataset.mode=cpu.power_mode?.consistent===false?'mismatch':cpu.power_mode?.selected||'unknown';text('cpu-value',number(cpu.percent));text('cpu-model',cpu.model||'CPU');text('core-count',cpu.logical_cores||'—');
  text('memory-value',number(memory.percent));text('memory-used',bytes(memory.used_bytes));text('memory-available',bytes(memory.available_bytes)+(memory.available_estimated?' ≈':''));text('memory-total',bytes(memory.total_bytes));text('memory-cached',bytes(memory.cached_bytes));text('memory-swap',memory.swap_total_bytes?bytes(memory.swap_used_bytes)+' / '+bytes(memory.swap_total_bytes):memory.status==='ok'?'未启用':'—');by('memory-progress').value=memory.percent||0;
  for(const key of ['cpu','memory']){if(gap)histories[key].push(null);histories[key].push(data[key]?.percent??null);histories[key]=histories[key].slice(-60);if(by(key+'-chart'))plot(by(key+'-chart'),histories[key]);}
  const appendSeries=(map,id,value)=>{let series=map.get(id)||Array(60).fill(null);if(gap)series.push(null);series.push(value??null);series=series.slice(-60);map.set(id,series);return series;};
  function graph(){const box=node('div',undefined,'chart-box'),svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 600 180');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');for(const cls of ['grid','area','trend']){const path=doc.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class',cls);if(cls==='grid')path.setAttribute('d','M0 45H600 M0 90H600 M0 135H600 M100 0V180 M200 0V180 M300 0V180 M400 0V180 M500 0V180');svg.append(path);}box.append(svg);return {box,svg};}
  function facts(id,items){syncList(by(id),items,x=>x[0],()=>{const row=node('div'),dt=node('dt'),dd=node('dd');row.append(dt,dd);row._refs={dt,dd};return row;},(el,x)=>{set(el._refs.dt,x[0]);set(el._refs.dd,x[1]);});}
  text('cpu-frequency',cpu.current_frequency_mhz!=null?(cpu.current_frequency_mhz/1000).toFixed(2)+' GHz':'—');
  const hardware=[['物理核心',cpu.physical_cores],['插槽',cpu.sockets],['架构',cpu.architecture],['基准频率',cpu.base_frequency_mhz!=null?(cpu.base_frequency_mhz/1000).toFixed(2)+' GHz':null],['最大频率',cpu.maximum_frequency_mhz!=null?(cpu.maximum_frequency_mhz/1000).toFixed(2)+' GHz':null],['虚拟化能力',cpu.virtualization],['地址位宽',cpu.address_sizes],['当前频率上限',cpu.power_mode?.maximum_frequency_mhz!=null?(cpu.power_mode.maximum_frequency_mhz/1000).toFixed(2)+' GHz':null],['调频策略',cpu.power_mode?.governors?.join(' / ')],['睿频',cpu.power_mode?.turbo_enabled===true?'开启':cpu.power_mode?.turbo_enabled===false?'关闭':null],...(cpu.caches||[]).map(c=>[c.level+' 缓存',c.bytes>=1048576?(c.bytes/1048576).toFixed(1)+' MiB':(c.bytes/1024).toFixed(0)+' KiB'])].filter(x=>x[1]!=null);
  facts('cpu-hardware',hardware);facts('memory-extra',[['空闲内存',memory.free_bytes],['缓冲区',memory.buffers_bytes],['内核 Slab',memory.slab_bytes],['已提交',memory.committed_bytes],['提交上限',memory.commit_limit_bytes]].filter(x=>x[1]!=null).map(x=>[x[0],bytes(x[1])]));
  syncList(by('core-list'),cpu.cores||[],c=>c.id,()=>{const item=node('article',undefined,'core-graph'),head=node('div',undefined,'core-caption'),label=node('span'),value=node('span'),chart=graph();head.append(label,value);item.append(head,chart.box);item._refs={label,value,svg:chart.svg};return item;},(el,c)=>{set(el._refs.label,c.id.toUpperCase());set(el._refs.value,number(c.percent)+'%');el._refs.svg.setAttribute('aria-label',c.id+' 最近 60 次采样使用率');plot(el._refs.svg,appendSeries(histories.cores,c.id,c.percent));});
  syncList(by('disk-list'),data.disks||[],d=>d.id,()=>{const item=node('article',undefined,'disk'),head=node('div',undefined,'disk-head'),label=node('strong'),value=node('span'),bar=node('progress'),detail=node('p');bar.max=100;head.append(label,value);item.append(head,bar,detail);item._refs={label,value,bar,detail};return item;},(el,d)=>{set(el._refs.label,(d.mount==='/'?'系统盘':d.mount)+' · '+d.device);set(el._refs.value,number(d.percent)+'%');el._refs.bar.value=d.percent||0;el._refs.bar.setAttribute('aria-label',d.mount+' 容量已用比例');el.dataset.full=d.percent>80?'true':'false';set(el._refs.detail,bytes(d.used_bytes)+' 已用 / '+bytes(d.available_bytes)+' 可用 · 共 '+bytes(d.total_bytes));});
  function gpuPlot(title){const panel=node('div',undefined,'gpu-plot'),caption=node('div',undefined,'gpu-plot-caption'),label=node('span',title),value=node('strong'),chart=graph(),stamp=node('div','离线','offline-stamp');stamp.setAttribute('aria-hidden','true');chart.box.append(stamp);caption.append(label,value);panel.append(caption,chart.box);return {panel,label,value,svg:chart.svg,stamp};}
  syncList(by('gpu-list'),[...(data.gpus||[])].sort((a,b)=>({integrated:0,unknown:1,discrete:2}[a.kind]??1)-({integrated:0,unknown:1,discrete:2}[b.kind]??1)),g=>g.id,()=>{
   const item=node('article',undefined,'gpu'),head=node('div',undefined,'gpu-head'),label=node('strong'),detail=node('p'),reason=node('p',undefined,'dim'),charts=node('div',undefined,'gpu-metric-grid'),usage=gpuPlot('占用'),vram=gpuPlot('显存使用'),memory=node('div',undefined,'gpu-memory'),used=node('div'),total=node('div'),power=node('div'),info=node('dl',undefined,'gpu-facts');
   head.append(label);charts.append(usage.panel,vram.panel);memory.append(used,total,power);item.append(head,detail,charts,reason,memory,info);item._refs={label,detail,reason,usage,vram,used,total,power,info};return item;
  },(el,g)=>{
   const usage=g.status==='ok'?(g.display_percent??g.percent):null,vram=gpuMemoryPercent(g),kind=g.metric_kind==='power'?'功耗占比':'占用';
   el.dataset.state=g.status;set(el._refs.label,(g.name||g.vendor+' · '+g.device_id)+(g.kind==='integrated'?' · 集显':g.kind==='discrete'?' · 独显':''));set(el._refs.detail,g.vendor+' · PCI '+g.device_id+' · '+(g.driver||'未加载驱动'));
   set(el._refs.usage.label,kind);set(el._refs.usage.value,usage==null?'—':number(usage)+'%');el._refs.usage.panel.dataset.state=usage==null?'offline':'ok';el._refs.usage.svg.setAttribute('aria-label',(g.name||g.vendor)+' '+kind);plot(el._refs.usage.svg,appendSeries(histories.gpus,g.id,usage));
   set(el._refs.vram.value,vram==null?'—':number(vram)+'%');el._refs.vram.panel.dataset.state=vram==null?'offline':'ok';set(el._refs.usage.stamp,g.reason==='suspended'?'离线':'无读数');set(el._refs.vram.stamp,g.reason==='suspended'?'离线':'无读数');el._refs.vram.svg.setAttribute('aria-label',(g.name||g.vendor)+' 显存使用比例');plot(el._refs.vram.svg,appendSeries(histories.gpuVram,g.id,vram));
   set(el._refs.reason,g.reason==='suspended'?'设备已休眠':g.reason==='power_state_unknown'?'电源状态未公开':g.reason==='metrics_unavailable'?'设备活动 · 监测读数未公开':g.metric_kind==='power'?'功耗 / 功耗上限':'总体使用率');
   set(el._refs.used,'显存已用 '+bytes(g.vram_used_bytes));set(el._refs.total,'显存总量 '+bytes(g.vram_total_bytes));set(el._refs.power,'功耗 '+(g.power_watts!=null?number(g.power_watts)+' W':'—')+' / 上限 '+(g.power_limit_watts!=null?number(g.power_limit_watts)+' W':'—'));
   const gpuFacts=[['电源状态',g.power_state==='active'?'活动':g.power_state==='suspended'?'休眠':g.power_state||'未公开'],['监测状态',g.status==='ok'?'可用':g.reason==='suspended'?'休眠离线':'读数未公开'],['PCI 位置',g.pci_location||g.id],['设备编号',g.device_id],['驱动模块',g.driver],['驱动版本',g.driver_version],['内核版本',g.kernel_version],['子系统编号',g.subsystem_id],['硬件修订',g.revision]].filter(x=>x[1]);
   syncList(el._refs.info,gpuFacts,x=>x[0],()=>{const row=node('div'),dt=node('dt'),dd=node('dd');row.append(dt,dd);row._refs={dt,dd};return row;},(row,x)=>{set(row._refs.dt,x[0]);set(row._refs.dd,x[1]);});

  });
  gap=false;
  const fanReadings=data.fans||[];text('fan-count',fanReadings.length?fanReadings.length+' 个转速通道':'未公开转速通道');
  const fanSlots=fanReadings.filter(f=>fanState(f).mode==='measured'||fanState(f).mode==='stopped').slice(0,2).map((f,index)=>({...f,index}));by('fan-list').closest('section').hidden=fanSlots.length===0;by('fan-list').dataset.count=String(fanSlots.length);
  function fanGraphic(){const svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 160 160');svg.setAttribute('aria-hidden','true');const part=(tag,attributes,parent=svg)=>{const el=doc.createElementNS('http://www.w3.org/2000/svg',tag);for(const [key,value] of Object.entries(attributes))el.setAttribute(key,String(value));parent.append(el);return el;};part('circle',{cx:80,cy:80,r:69,class:'fan-ring'});part('circle',{cx:80,cy:80,r:63,class:'fan-ring-inner'});const rotor=part('g',{class:'fan-rotor'});for(let i=0;i<5;i++)part('path',{d:'M80 68 C101 45 137 47 134 74 C130 91 104 93 89 85 C92 79 90 73 80 68 Z',transform:'rotate('+i*72+' 80 80)',class:'fan-blade'},rotor);part('circle',{cx:80,cy:80,r:13,class:'fan-hub'});part('circle',{cx:80,cy:80,r:5,class:'fan-center'});return {svg,rotor};}
  syncList(by('fan-list'),fanSlots,f=>f.id,()=>{const item=node('article',undefined,'fan-card'),head=node('div',undefined,'fan-heading'),label=node('strong'),rpm=node('div',undefined,'fan-rpm'),graphic=fanGraphic(),status=node('p',undefined,'fan-status'),details=node('p',undefined,'fan-details');head.append(label);item.append(head,graphic.svg,rpm,status,details);item._refs={label,rpm,rotor:graphic.rotor,status,details};return item;},(el,f)=>{const state=fanState(f);el.dataset.mode=state.mode;el.dataset.running=String(state.running);set(el._refs.label,f.label||'风扇 '+(f.index+1));set(el._refs.rpm,(state.rpm==null?'—':state.rpm.toFixed(0))+' RPM');set(el._refs.status,state.label);set(el._refs.details,f.component?[f.component,f.max_rpm>0?'上限 '+f.max_rpm+' RPM':null].filter(Boolean).join(' · '):'');el._refs.rotor.style.animationDuration=(state.rpm>0?Math.min(3.5,Math.max(.5,6000/state.rpm)):4+f.index*.7)+'s';});
  const sensors=data.temperatures||[];text('sensor-count',sensors.filter(s=>s.status==='ok').length+' 个有效读数');
  syncList(by('temperature-list'),sensors,s=>s.id,()=>{const item=node('article',undefined,'sensor'),driver=node('div',undefined,'sensor-driver'),label=node('div',undefined,'sensor-label'),value=node('div',undefined,'sensor-value'),note=node('div',undefined,'sensor-note');item.append(driver,label,value,note);item._refs={driver,label,value,note};return item;},(el,s)=>{el.dataset.state=s.status;el.dataset.level=s.celsius!=null&&s.critical_celsius!=null&&s.celsius>=s.critical_celsius?'critical':s.celsius!=null&&s.max_celsius!=null&&s.celsius>=s.max_celsius?'warning':'normal';set(el._refs.driver,s.component);set(el._refs.label,s.label);set(el._refs.value,s.status==='ok'?number(s.celsius)+' °C':s.status==='sleeping'?'离线':'暂无读数');set(el._refs.note,s.status==='sleeping'?'设备已休眠':s.critical_celsius!=null?'临界 '+number(s.critical_celsius)+' °C':'');});
  if(data.uptime_seconds!=null){const days=Math.floor(data.uptime_seconds/86400),hours=Math.floor(data.uptime_seconds/3600)%24,mins=Math.floor(data.uptime_seconds/60)%60;text('uptime',(days?days+'天 ':'')+hours+'时 '+mins+'分');}
  text('sample-time',data.collected_at?'采样于 '+new Date(data.collected_at).toLocaleTimeString('zh-CN',{hour12:false}):'暂无采样');
 }
 function state(value){
  if(value==='sampling')return;
  const labels={live:'实时 · 每秒更新',paused:'已暂停',hidden:'后台暂停',expired:'登录已过期',error:'采集暂不可用',unavailable:'暂无读数'};
  text('monitor-state',labels[value]||value);by('refresh-status').disabled=manual||value==='hidden'||value==='expired';
  text('message',value==='error'?'采集暂时失败，保留上次读数。':value==='unavailable'?'当前系统未提供可用读数。':'');
  if(['hidden','paused','error','unavailable'].includes(value))gap=true;
 }
 const monitor=new Monitor({visible:()=>doc.visibilityState==='visible',render,state,request:async signal=>{const response=await win.fetch(((globalThis.XIXI_ADMIN_BASE||'')+'/v1/system/status'),{credentials:'same-origin',cache:'no-store',signal,headers:{Accept:'application/json'}});if(response.status===401){const error=new Error('expired');error.code='authentication_required';win.location.replace(((globalThis.XIXI_ADMIN_BASE||'')+'/login'));throw error;}if(!response.ok)throw new Error('snapshot_unavailable');return response.json();}});
 by('toggle-monitor').onclick=()=>{manual=!manual;text('toggle-monitor',manual?'继续采集':'暂停采集');if(manual)monitor.pause();else monitor.resume();};by('refresh-status').onclick=()=>monitor.refresh();
 doc.addEventListener('visibilitychange',()=>{if(doc.visibilityState==='visible')monitor.start();else monitor.stop('hidden');});win.addEventListener('pagehide',()=>monitor.stop('hidden'));win.addEventListener('pageshow',()=>monitor.start());monitor.start();return monitor;
}
return {Monitor,mount,plot,syncList,gpuMemoryPercent,fanState};
});
