/* Platform module controls, using the original console's shared UI helpers. */
'use strict';
(() => {
 const busy=new Set();let modules=[],configId=null,polling=false,stopped=false;
 const stateLabel=item=>item.operation?({start:'启动中',stop:'停止中',reload:'重载中'}[item.operation]||'处理中'):({running:'运行中',stopped:'未启动',error:'异常'}[item.state]||'未知');
 const message=value=>typeof value==='string'?value:value?.message||JSON.stringify(value);
 function duration(value){if(!Number.isFinite(value))return '—';const minutes=Math.floor(value/60),hours=Math.floor(minutes/60);return hours?hours+' 时 '+minutes%60+' 分':minutes+' 分 '+Math.floor(value%60)+' 秒';}
 function configNotice(value,bad=false){$('config-message').textContent=value;$('config-message').hidden=!value;$('config-message').classList.toggle('bad',bad);}
 function render(){
  const list=$('module-list');
  const opened=new Set([...list.querySelectorAll('details[open]')].map(item=>item.dataset.module));
  const focus=document.activeElement;
  const activeId=focus.closest('.module-card')?.id;
  const focusKey=focus.dataset.control;
  const cards=document.createDocumentFragment();
  for(const item of modules){
   const card=node('section',undefined,'panel module-card');
   card.id='module-'+item.id;
   const head=node('div',undefined,'module-head');
   head.append(node('h2',item.name||item.id),node('span',stateLabel(item),'module-state state-'+item.state));
   card.append(head);
   if(item.config_pending)card.append(node('p','配置已保存，待重新载入。','notice'));
   if(item.error||item.restart_required)card.append(node('p',[item.error&&message(item.error),item.restart_required&&'需重启 Core 后恢复。'].filter(Boolean).join(' '),'notice'));
   const actions=node('div',undefined,'module-actions');
   const action=item.state==='running'?'stop':'start';
   function addAction(label,key,callback,disabled=false){
    const control=button(label,callback);
    control.dataset.control=key;
    control.disabled=disabled;
    actions.append(control);
   }
   const pending=busy.has(item.id)||!!item.operation;
   addAction(action==='stop'?'停用':'启用','toggle',()=>operate(item.id,action),pending||(action==='start'&&!!item.restart_required));
   if(item.reloadable)addAction('重载插件','reload',()=>operate(item.id,'reload'),pending||!!item.restart_required);
   addAction('配置','config',()=>openConfig(item));
   card.append(actions);
   const details=node('details');
   details.dataset.module=item.id;
   details.open=opened.has(item.id);
   const summary=node('summary','详情');
   summary.dataset.control='details';
   details.append(summary,node('p',item.id+' · '+(item.builtin?'内置模块':'本地插件')+' · v'+(item.version||'—'),'muted'),
    node('p','下次启动时'+(item.enabled?'自动启用':'保持停用'),'muted'));
   if(item.details&&Object.keys(item.details).length)details.append(node('pre',JSON.stringify(item.details,null,2)));
   card.append(details);
   cards.append(card);
  }
  list.replaceChildren(cards);
  if(activeId&&focusKey)document.getElementById(activeId)?.querySelector('[data-control="'+focusKey+'"]')?.focus({preventScroll:true});
 }
 async function refresh(){
  if(polling||stopped)return;polling=true;
  try{const [platform,result]=await Promise.all([api('/api/platform/status'),api('/api/platform/modules')]);modules=result.items||[];render();$('platform-state').textContent=platform.version+' · 本次运行 '+duration(platform.uptime_seconds)+' · '+modules.filter(item=>item.state==='running').length+'/'+modules.length+' 个模块运行中';const consoleItem=modules.find(item=>item.id==='console');$('console-state').hidden=consoleItem?.state==='running';$('console-state').textContent='管理台模块已停用，其他模块仍在运行。当前页面可通过保留的控制接口重新启用管理台。';}
  catch(error){notify(error.message,true);}finally{polling=false;}
 }
 async function operate(id,action){
  busy.add(id);render();
  try{await api('/api/platform/modules/'+encodeURIComponent(id)+'/'+action,'POST');notify({start:'模块已启用。',stop:'模块已停用。',reload:'插件代码与已保存配置已重新载入。'}[action]);}
  catch(error){notify(error.message,true);}finally{busy.delete(id);await refresh();}
 }
 async function openConfig(item){
  try{
   const result=await api('/api/platform/config/'+encodeURIComponent(item.id));
   configId=item.id;
   const bridge=$('bridge-settings');
   bridge.hidden=bridge.disabled=item.id!=='message_bridge';
   $('json-settings').hidden=!bridge.hidden;
   if(bridge.hidden){
    $('config-value').value=JSON.stringify(result.config,null,2);
   }else{
    for(const field of bridge.elements)field.value=result.config[field.name]??(field.name==='mode'?'offline':'');
   }
   $('config-title').textContent=(item.name||item.id)+' · 配置';
   $('config-caption').textContent=item.reloadable?'保存后重载插件使配置生效。':'保存后重启 Core 使配置生效。';
   configNotice(result.config_pending?'已保存的配置尚未载入。':'');
   $('reload-after-save').hidden=!item.reloadable;
   $('reload-after-save').disabled=!!item.restart_required;
   $('config-dialog').showModal();
  }
  catch(error){notify(error.message,true);}
 }
 $('config-form').addEventListener('submit',async event=>{
  event.preventDefault();const control=event.submitter||$('config-form').querySelector('[type=submit]');control.disabled=true;
  try{
   let config;
   if(configId==='message_bridge'){
    config={};
    for(const field of $('bridge-settings').elements){
     const value=field.value.trim();
     if(value)config[field.name]=field.name==='listenPort'?Number(value):value;
    }
   }else{
    config=JSON.parse($('config-value').value);
    if(!config||typeof config!=='object'||Array.isArray(config))throw new Error('配置必须是 JSON 对象。');
   }
   await api('/api/platform/config/'+encodeURIComponent(configId),'PUT',{config});
   configNotice('配置已保存。重新载入后生效，启用状态未改变。');await refresh();
  }
  catch(error){configNotice(error.message,true);}finally{control.disabled=false;}
 });
 $('close-config').onclick=()=>$('config-dialog').close();
 $('reload-after-save').onclick=async()=>{const control=$('reload-after-save');control.disabled=true;try{await api('/api/platform/modules/'+encodeURIComponent(configId)+'/reload','POST');configNotice('重载成功，已载入保存的配置。');}catch(error){configNotice(error.message,true);}finally{await refresh();control.disabled=!!modules.find(item=>item.id===configId)?.restart_required;}};
 $('refresh-modules').onclick=refresh;
 window.addEventListener('pagehide',()=>{stopped=true;});window.addEventListener('pageshow',()=>{stopped=false;});
 perform(async()=>{if(!await sessionReady())return;await refresh();setInterval(()=>{if(document.visibilityState==='visible')refresh();},5000);});
})();
