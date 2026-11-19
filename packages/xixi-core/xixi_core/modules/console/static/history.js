(function(win){
'use strict';
const doc=win.document,by=id=>doc.getElementById(id),api=win.XixiStatus;
const text=(id,value)=>{if(by(id).textContent!==value)by(id).textContent=value;};
const format=value=>new Date(value*1000).toLocaleString('zh-CN',{hour12:false});
const percent=value=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(1)+'%':'—';
let rows=[],diskColumns=[],page=0,schema='';
function table(){
 const pages=Math.max(1,Math.ceil(rows.length/100));page=Math.min(page,pages-1);
 const headers=['时间','CPU 均值','内存均值','CPU 温度峰值',...diskColumns.map(d=>(d.mount==='/'?'系统盘':d.mount)+' 已用')],nextSchema=JSON.stringify(headers);
 if(schema!==nextSchema){const head=doc.createElement('tr');for(const label of headers){const th=doc.createElement('th');th.textContent=label;head.append(th);}by('history-table-head').replaceChildren(head);schema=nextSchema;}
 let visible=rows.slice(page*100,(page+1)*100);if(!visible.length)visible=[{at:'empty'}];
 api.syncList(by('history-table-body'),visible,p=>p.at,()=>doc.createElement('tr'),(el,p)=>{const values=p.at==='empty'?['尚无历史数据']:[format(p.at),percent(p.cpu),percent(p.memory),typeof p.temperature==='number'?p.temperature.toFixed(1)+' °C':'—',...diskColumns.map(d=>percent(d.values.get(p.at)))];while(el.children.length>values.length)el.lastChild.remove();values.forEach((value,i)=>{let cell=el.children[i];if(!cell){cell=doc.createElement('td');el.append(cell);}cell.colSpan=p.at==='empty'?headers.length:1;cell.className=p.at==='empty'?'empty-cell':'';if(cell.textContent!==value)cell.textContent=value;});});
 text('history-total',rows.length+' 个时间段');text('history-page',(page+1)+' / '+pages);by('history-prev').disabled=page===0;by('history-next').disabled=page>=pages-1;
}
function render(data){
 const points=data.points||[],monitor=data.monitor||{};
 for(const key of ['cpu','memory','temperature'])api.plot(by('history-'+key+'-chart'),points.map(p=>p[key]),key==='temperature'?150:100);
 text('history-range',format(data.start)+' — '+format(data.end));
 const disks=new Map();for(const d of data.disks||[]){if(!disks.has(d.id))disks.set(d.id,{id:d.id,mount:d.mount,values:new Map()});disks.get(d.id).values.set(d.bucket,d.percent);}
 diskColumns=[...disks.values()];rows=points.filter(p=>p.count>0).slice().reverse();table();
 const items=diskColumns.length?diskColumns:[{id:'empty',mount:'尚无磁盘历史数据',values:new Map()}];
 api.syncList(by('history-disks'),items,d=>d.id,()=>{const el=doc.createElement('article');el.className='disk-history history-chart';const title=doc.createElement('h3'),box=doc.createElement('div');box.className='chart-box';const svg=doc.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 600 180');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('role','img');for(const cls of ['area','trend']){const path=doc.createElementNS('http://www.w3.org/2000/svg','path');path.setAttribute('class',cls);svg.append(path);}box.append(svg);el.append(title,box);el._refs={title,svg};return el;},(el,d)=>{el._refs.title.textContent=d.mount;el._refs.svg.setAttribute('aria-label',d.mount+' 历史已用比例');api.plot(el._refs.svg,points.map(p=>d.values.get(p.at)??null));});
 text('history-updated',monitor.last_sample?'最近采样 '+format(monitor.last_sample):'');
 text('message',rows.length?'':'历史会从采样器启动后逐分钟积累。');
}
const monitor=new api.Monitor({interval:60000,visible:()=>doc.visibilityState==='visible',render,state:value=>{if(value==='error')text('message','历史暂时无法读取，保留现有数据。');},request:async signal=>{const response=await win.fetch(((globalThis.XIXI_ADMIN_BASE||'')+'/v1/system/history?interval=')+by('history-interval').value,{credentials:'same-origin',cache:'no-store',signal});if(response.status===401){win.location.replace(((globalThis.XIXI_ADMIN_BASE||'')+'/login'));const error=new Error('expired');error.code='authentication_required';throw error;}if(!response.ok)throw new Error('history_failed');return response.json();}});
by('history-prev').onclick=()=>{if(page>0){page--;table();}};by('history-next').onclick=()=>{page++;table();};by('history-interval').onchange=()=>{page=0;monitor.stop('change');monitor.start();};by('refresh-history').onclick=()=>monitor.refresh();doc.addEventListener('visibilitychange',()=>{if(doc.visibilityState==='visible')monitor.start();else monitor.stop('hidden');});win.addEventListener('pagehide',()=>monitor.stop('hidden'));win.addEventListener('pageshow',()=>monitor.start());monitor.start();
})(window);
