/* Core integration within the approved administration shell. */
'use strict';
(() => {
 const by=id=>document.getElementById(id);
 const kinds={daily:'日常对话',work:'独立工作',idle:'闲时整理',dream:'做梦回顾'};
 const placements={system:'系统提示词',start:'会话开始',run:'每轮递交'};
 const newId=()=>globalThis.crypto?.randomUUID?.()||('console-'+Date.now()+'-'+Math.random().toString(16).slice(2));
 let summaries=null,summaryIntent=null,environmentVersion=null;
 const injectionItems=new Map(),injectionDrafts=new Map();let selectedInjectionGroup='common';
 function el(tag,text,cls){const x=document.createElement(tag);if(text!==undefined)x.textContent=text;if(cls)x.className=cls;return x;}
 function show(message,bad=false){if(typeof notify==='function')notify(message,bad);const target=by('message');if(target){target.textContent=message;target.classList.toggle('bad',bad);target.hidden=!message;}}
 const core=(resource,body)=>api('/api/persona/'+resource,body===undefined?'GET':'POST',body);
 function bindForm(form,action){
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(form.dataset.saving==='true')return;
   const data=new FormData(form),controls=Array.from(form.elements),disabled=controls.map(x=>x.disabled);
   form.dataset.saving='true';controls.forEach(x=>x.disabled=true);
   try{await action(data);}catch(error){show(error.message+' 原输入已保留。',true);}
   finally{delete form.dataset.saving;controls.forEach((x,i)=>x.disabled=disabled[i]);}
  });
 }
 function field(title,input){const label=el('label',title);label.append(input);return label;}
 function input(name,value,type='text'){const x=el('input');x.name=name;x.type=type;x.value=value??'';return x;}
 function option(select,value,title){const x=el('option',title);x.value=value;select.append(x);}
 async function loadInjections(){
  const data=await core('injections');injectionItems.clear();injectionDrafts.clear();
  for(const item of data.items||[])injectionItems.set(item.id,item);
  renderInjections();
 }
 function renderInjections(){
  const root=by('core-injection-list');root.replaceChildren();
  const all=Array.from(injectionItems.values()),items=all.filter(item=>selectedInjectionGroup==='common'?item.group!=='agent-mode':item.group==='agent-mode'&&item.agentPreset===selectedInjectionGroup).sort((a,b)=>{
   const special={'daily.role':-2,'daily.rules':-1};return (special[a.id]??0)-(special[b.id]??0)||(a.order||0)-(b.order||0)||a.id.localeCompare(b.id);
  });
  for(const item of items)root.append(renderInjection(item));
  for(const button of document.querySelectorAll('[data-injection-group]'))button.setAttribute('aria-selected',String(button.dataset.injectionGroup===selectedInjectionGroup));
  by('core-injection-group-note').textContent=selectedInjectionGroup==='common'?'共用内容按适用会话装配。现有共用系统正文保留；Core的消息、交接与整理等默认文字不是DSH原生标准/极简提示词。':'这里只编辑'+(selectedInjectionGroup==='standard'?'标准':'极简')+'模式追加文字。初始为空时只使用现有共用提示，不自动加入原版对照；正文按原样使用，{{model}} 等也不会自动替换。';
  by('core-injections-state').textContent='当前分组 '+items.length+' 项，共 '+all.length+' 项；保存后按各项生效时机装配。';
 }
 const effect=item=>item.formatOnly?'随引用的提示生效':item.effectScope==='next_delivery'?'下次递交':'新会话';
 const savedEffect=id=>{const item=injectionItems.get(id);return item?item.enabled?effect(item)+'采用。':'对应的注入模板已停用。':'按对应注入模板的时机采用。';};
 function renderInjection(item){
  const isMode=item.group==='agent-mode',fixedPlacement=isMode||item.formatOnly||item.inputOnly,display=injectionDrafts.get(item.id)||item;const details=el('details',undefined,'core-injection');details.dataset.injectionId=item.id;details.open=['daily.role','daily.rules'].includes(item.id);
  const title=el('summary'),name=el('span',item.id==='dsh.system'?'Core 共用系统正文':item.name),version=el('small','v'+item.version+' · '+effect(item));
  title.append(name,version);details.append(title);
  const form=el('form',undefined,'core-form'),body=el('textarea');body.name='body';body.rows=8;body.maxLength=100000;body.value=display.body??item.template??'';
  const nameInput=input('name',display.name);nameInput.required=true;nameInput.maxLength=120;
  const placement=el('select');placement.name='placement';for(const [key,text] of Object.entries(placements))option(placement,key,text);placement.value=display.placement;placement.disabled=fixedPlacement;
  const order=input('order',display.order,'number');order.min=-1000000;order.max=1000000;order.required=true;order.disabled=Boolean(item.formatOnly);
  const grid=el('div',undefined,'core-form-grid');grid.append(field('名称',nameInput),field('注入位置',placement),field('顺序',order));
  const enabled=input('enabled','true','checkbox');enabled.checked=display.enabled;const enabledLabel=field('启用',enabled);enabledLabel.className='core-enabled';
  const kindsBox=el('fieldset',undefined,'core-kind-choices');kindsBox.append(el('legend','适用会话'));
  if(item.inputOnly)kindsBox.append(el('p',item.runtimeHint||'日常对话与启动补收有消息递交时使用。','core-caption'));
  else for(const [key,text] of Object.entries(kinds)){const box=input('sessionKinds',key,'checkbox');box.checked=(display.sessionKinds||[]).includes(key);box.disabled=fixedPlacement;kindsBox.append(field(text,box));}
  if(!item.inputOnly&&item.runtimeHint)kindsBox.append(el('p',item.runtimeHint,'core-caption'));
  const reference=el('p',item.id+' · '+(item.literal?'正文原样保留':'使用 {变量名} 引用数据，字面花括号写作 {{ 和 }}。'),'core-caption');
  const source=el('p',isMode?'来源：Core可配置模式项；下方DSH原生模式原文仅供对照。':item.id==='dsh.system'?'来源：现有 Core 共用系统正文，原存储名称可能含“完整覆盖”。标准模式将其作为 Core 公共段追加；极简模式与其整体策略合并装配，不能据此认定所有原生段已覆盖。原生分段可从页首进入对应设置。':item.source==='core.custom-injection'?'来源：Core自定义注入，不是DSH原生模式提示词。':'来源：Core内置注入默认，不是DSH原生标准/极简前缀。','core-caption');
  form.append(source,grid,enabledLabel,kindsBox,field(isMode?'此模式追加文字':'内容',body),reference);
  if(typeof item.originalBody==='string'){const compare=el('details',undefined,'core-original-compare');compare.append(el('summary',isMode?'DSH原版对照（只读，不自动使用）':'初始默认对照（只读）'));if(item.originalSource)compare.append(el('p',item.originalSource,'core-caption'));compare.append(el('pre',item.originalBody||'（初始默认正文为空）'));if(!isMode)compare.append(el('p','此对照是该Core配置项的初始文本，不代表DSH原生模式提示词。','core-caption'));form.append(compare);}
  if(!item.literal&&(item.availableVariables||[]).length){const vars=el('details'),summary=el('summary','可用变量');vars.append(summary,el('p',item.availableVariables.map(x=>'{'+x+'}').join(' '),'core-caption'));form.append(vars);}
  const actions=el('div',undefined,'actions'),save=el('button','保存此项'),state=el('span','','muted');save.type='submit';actions.append(save,state);form.append(actions);details.append(form);
  const captureDraft=()=>{const data=new FormData(form);injectionDrafts.set(item.id,{name:data.get('name'),body:data.get('body'),placement:fixedPlacement?item.placement:data.get('placement'),order:item.formatOnly?item.order:Number(data.get('order')),enabled:data.has('enabled'),sessionKinds:fixedPlacement?item.sessionKinds:data.getAll('sessionKinds')});state.textContent='尚未保存。';};form.addEventListener('input',captureDraft);form.addEventListener('change',captureDraft);
  bindForm(form,async data=>{
   const current=injectionItems.get(item.id),sessionKinds=fixedPlacement?current.sessionKinds:data.getAll('sessionKinds');if(!sessionKinds.length)throw new Error('至少选择一种适用会话。');
   const request={id:item.id,expectedVersion:current.version,name:data.get('name'),body:data.get('body'),placement:fixedPlacement?current.placement:data.get('placement'),order:item.formatOnly?item.order:Number(data.get('order')),enabled:data.has('enabled'),sessionKinds};
   const saved=await core('injections',request);injectionItems.set(saved.id,saved);injectionDrafts.delete(saved.id);name.textContent=saved.id==='dsh.system'?'Core 共用系统正文':saved.name;
   version.textContent='v'+saved.version+' · '+effect(saved);
   state.textContent='已保存 v'+saved.version;show('已保存“'+saved.name+'”；'+(saved.formatOnly?'按引用处的时机采用。':effect(saved)+'采用。'));
  });
  return details;
 }
 async function loadEnvironment(){const state=await core('environment');environmentVersion=state.version;by('core-environment').value=state.environmentDescription;by('core-environment-state').textContent='当前 v'+environmentVersion;}
 async function loadSummaries(){
  const result=await core('summaries');
  if(!Number.isInteger(result.version)||typeof result.selfUnderstanding!=='string'||typeof result.ownerUnderstanding!=='string')throw new Error('阶段认识结果不完整，未覆盖当前内容。');
  summaries=result;summaryIntent=null;by('core-self-summary').value=result.selfUnderstanding;by('core-owner-summary').value=result.ownerUnderstanding;by('core-summaries-version').textContent='当前 v'+result.version+(result.version===0?'；尚无有效阶段认识':'');
 }
 async function startPersona(){
  for(const button of document.querySelectorAll('[data-injection-group]'))button.onclick=()=>{selectedInjectionGroup=button.dataset.injectionGroup;renderInjections();};
  bindForm(by('core-environment-form'),async data=>{if(environmentVersion===null)throw new Error('请先读取运行环境');const result=await core('environment',{environmentDescription:data.get('environmentDescription')||'',expectedVersion:environmentVersion});environmentVersion=result.version;by('core-environment-state').textContent='已保存 v'+environmentVersion;show('运行环境描述已保存。'+savedEffect('initial.environment'));});
  bindForm(by('core-summaries-form'),async data=>{
   if(!summaries)throw new Error('请先读取阶段认识。');
   const payload={expectedVersion:summaries.version,selfUnderstanding:String(data.get('selfUnderstanding')||''),ownerUnderstanding:String(data.get('ownerUnderstanding')||'')};
   if(!payload.selfUnderstanding.trim()||!payload.ownerUnderstanding.trim())throw new Error('请填写两份完整正文，空内容不会覆盖有效版本。');
   const digest=JSON.stringify(payload);if(!summaryIntent||summaryIntent.digest!==digest)summaryIntent={digest,requestId:newId()};
   const result=await core('summaries',{...payload,requestId:summaryIntent.requestId});summaries=result;summaryIntent=null;by('core-summaries-version').textContent='已保存 v'+result.version;show('两份阶段认识已保存。'+savedEffect('stage.summaries'));
  });
  by('core-injections-reload').onclick=()=>{if(injectionDrafts.size&&!confirm('重新读取会替换未保存的提示词草稿，继续？'))return;loadInjections().catch(error=>show(error.message,true));};
  by('core-summaries-reload').onclick=()=>loadSummaries().catch(error=>show(error.message,true));
  const loaded=await Promise.allSettled([loadInjections(),loadEnvironment(),loadSummaries()]);for(const result of loaded)if(result.status==='rejected')show(result.reason.message,true);
 }
 (async()=>{try{if(await sessionReady())await startPersona();}catch(error){show(error.message,true);}})();
})();
