/* Platform logs inside the original console table and panel layout. */
'use strict';
(() => {
 let pending=false;
 async function refresh(){if(pending)return;pending=true;try{const data=await api('/api/platform/logs?limit=100'),list=$('platform-logs');list.replaceChildren();for(const item of [...(data.items||[])].reverse()){const row=node('tr');const time=new Date(item.at??item.time??item.timestamp);row.append(node('td',Number.isNaN(time.getTime())?'—':time.toLocaleString('zh-CN',{hour12:false})),node('td',item.level||'info'),node('td',item.module||'platform'),node('td',item.message||''));list.append(row);}if(!list.children.length){const row=node('tr'),cell=node('td','暂无运行日志。','muted');cell.colSpan=4;row.append(cell);list.append(row);}$('logs-updated').textContent='更新于 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});}catch(error){notify(error.message,true);}finally{pending=false;}}
 $('refresh-logs').onclick=refresh;perform(async()=>{if(await sessionReady())await refresh();});
})();
