/* One writable model-settings page, backed by Core and the existing DSH catalog. */
'use strict';
(() => {
 const by=id=>document.getElementById(id),INHERIT='__inherit__',BASE='/api/modules/model_settings/';
 const purposes={daily:'日常对话',consolidation:'闲时换聊',dream:'做梦',work:'工作',leisure:'摸鱼'};
 const notes={daily:'用于新建的日常会话，包括换聊完成后建立的会话。',consolidation:'用于生成换聊交接摘要。',dream:'用于整理记忆并写日记。',work:'每项后台工作在接纳时确定模型，完成后将结果交回日常对话。',leisure:'可保存，自动触发流程尚未接入。'};
 const modeNames={standard:'标准',minimal:'极简'};
 let settings=null,drafts={},catalog=null,catalogError='',saveIntent=null,saving=false,stash={};
 const newId=()=>globalThis.crypto?.randomUUID?.()||('model-settings-'+Date.now()+'-'+Math.random().toString(16).slice(2));
 const clone=x=>JSON.parse(JSON.stringify(x));
 const element=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
 function show(text,bad=false){if(typeof notify==='function')notify(text,bad);const n=by('message');if(n){n.textContent=text;n.classList.toggle('bad',bad);n.hidden=!text;}}
 function cleaned(value){if(value===null)return null;return {provider:value.provider??null,model:value.model,reasoningEffort:value.reasoningEffort??null,agentPreset:value.agentPreset||'standard',...(value.maxTokens!==undefined?{maxTokens:value.maxTokens}:{})};}
 function ready(){return Boolean(catalog&&(catalog.groups||[]).some(group=>(catalog.routableProviders||[]).includes(group.id)&&(group.models||[]).length));}
 function changed(){const result={};if(!settings)return result;for(const key of Object.keys(purposes))if(JSON.stringify(cleaned(drafts[key]))!==JSON.stringify(cleaned(settings.bindings[key])))result[key]=cleaned(drafts[key]);return result;}
 function dirty(){saveIntent=null;by('model-settings-result').textContent='尚未保存。';displayStatus();}
 function label(title,control){const n=element('label',title);n.append(control);return n;}
 function option(select,value,text,disabled=false){const n=element('option',text);n.value=value;n.disabled=disabled;select.append(n);}
 function selectedGroup(binding){const id=binding?.provider??catalog?.default?.provider;return (catalog?.groups||[]).find(group=>group.id===id);}
 function efforts(model){return (model?.reasoning?.efforts||[]).map(item=>typeof item==='string'?{id:item,name:item}:item);}
 function defaultBinding(){
  const groups=(catalog?.groups||[]).filter(group=>(catalog?.routableProviders||[]).includes(group.id)&&group.models?.length);
  const inherited=groups.find(group=>group.id===catalog?.default?.provider);
  const group=inherited||groups[0];
  const model=group?.models.find(item=>item.id===catalog?.default?.model)||group?.models[0];
  const presets=catalog?.agentPresets||[];
  const preset=presets.find(item=>item.id==='standard'&&item.available)||presets.find(item=>item.available);
  return {provider:inherited?null:group?.id??null,model:model?.id||'',reasoningEffort:null,agentPreset:preset?.id||'standard'};
 }
 function displayStatus(){
  const n=by('model-settings-status');n.classList.toggle('bad',!ready());
  if(catalogError)n.textContent='未能读取 DSH 模型目录。可以保留现有选择或改用默认设置。';
  else if(!ready())n.textContent='DSH 当前未返回可选择的模型，已保存的设置保留。';
  else n.textContent='已读取现有 DSH 模型目录。继承渠道会跟随 DSH 当前默认渠道，模型标识仍按本页保存值使用。'+((catalog.failures||[]).length?' 部分渠道读取失败，请只使用已返回的选项。':'');
  const edits=Object.values(changed());
  by('model-settings-save').disabled=saving||!settings||!edits.length||(!ready()&&edits.some(value=>value!==null));
 }
 function renderPurpose(key){
  const binding=drafts[key],card=element('section',undefined,'model-purpose');card.dataset.purpose=key;
  card.append(element('h3',purposes[key]),element('p',notes[key],'purpose-note'));
  const toggle=element('input');toggle.type='checkbox';toggle.checked=binding!==null;toggle.name=key+'_enabled';toggle.disabled=saving||(!ready()&&binding===null);
  const wrap=label('指定'+purposes[key]+'模型',toggle);wrap.className='model-purpose-toggle';card.append(wrap);
  toggle.onchange=()=>{
   if(toggle.checked)drafts[key]=clone(stash[key]||defaultBinding());
   else{stash[key]=clone(drafts[key]);drafts[key]=null;}
   dirty();renderBindings();
  };
  const value=binding||defaultBinding(),fields=element('fieldset'),grid=element('div',undefined,'model-purpose-fields');
  fields.disabled=saving||!ready()||binding===null;
  const provider=element('select');provider.name=key+'_provider';provider.setAttribute('aria-label',purposes[key]+'渠道');
  const inherited=(catalog?.groups||[]).find(group=>group.id===catalog?.default?.provider);
  option(provider,INHERIT,'继承 DSH 默认渠道'+(inherited?'（当前：'+inherited.name+'）':''),!(catalog?.routableProviders||[]).includes(catalog?.default?.provider));
  for(const group of catalog?.groups||[]){const routable=(catalog.routableProviders||[]).includes(group.id);option(provider,group.id,(group.name||group.id)+(routable?'':'（当前不可路由）'),!routable);}
  if(value.provider&&!Array.from(provider.options).some(x=>x.value===value.provider))option(provider,value.provider,value.provider+'（当前目录不可用）',true);
  provider.value=value.provider??INHERIT;
  const group=selectedGroup(value),model=element('select');model.name=key+'_model';model.setAttribute('aria-label',purposes[key]+'模型');option(model,'','请选择模型');
  for(const item of group?.models||[])option(model,item.id,item.name||item.id);
  if(value.model&&!Array.from(model.options).some(x=>x.value===value.model))option(model,value.model,value.model+'（当前目录不可用）',true);
  model.value=value.model||'';
  const selected=group?.models?.find(item=>item.id===value.model),effort=element('select');effort.name=key+'_effort';effort.setAttribute('aria-label',purposes[key]+'思考等级');option(effort,'','模型默认');
  for(const item of efforts(selected))option(effort,item.id,item.name||item.id);
  if(value.reasoningEffort&&!Array.from(effort.options).some(x=>x.value===value.reasoningEffort))option(effort,value.reasoningEffort,value.reasoningEffort+'（当前模型不提供）',true);
  effort.value=value.reasoningEffort??'';
  const preset=element('select');preset.name=key+'_preset';preset.setAttribute('aria-label',purposes[key]+'DSH模式');
  for(const id of ['standard','minimal']){const entry=catalog?.agentPresets?.find(x=>x.id===id);option(preset,id,modeNames[id]+(entry?.available===false?'（当前不可用）':''),entry?.available!==true);}
  preset.value=value.agentPreset;
  const minimalNote=element('p','角色卡包含系统提示时，请在「原生提示」中将极简模式设为覆盖，或选择标准模式。','muted');
  minimalNote.id=key+'-minimal-note';minimalNote.hidden=binding===null||preset.value!=='minimal';preset.setAttribute('aria-describedby',minimalNote.id);
  const maxTokens=element('input');maxTokens.type='number';maxTokens.name=key+'_max_tokens';maxTokens.min=1;maxTokens.max=1000000;maxTokens.value=value.maxTokens??'';maxTokens.placeholder='使用 DSH 默认值';maxTokens.setAttribute('aria-label',purposes[key]+'最大生成 Token 数');
  provider.onchange=()=>{const current=drafts[key];current.provider=provider.value===INHERIT?null:provider.value;const next=selectedGroup(current);if(!next?.models?.some(item=>item.id===current.model))current.model='';const item=next?.models?.find(x=>x.id===current.model);if(!efforts(item).some(x=>x.id===current.reasoningEffort))current.reasoningEffort=null;dirty();renderBindings();};
  model.onchange=()=>{drafts[key].model=model.value;drafts[key].reasoningEffort=null;dirty();renderBindings();};
  effort.onchange=()=>{drafts[key].reasoningEffort=effort.value||null;dirty();};
  preset.onchange=()=>{drafts[key].agentPreset=preset.value;minimalNote.hidden=preset.value!=='minimal';dirty();};
  maxTokens.oninput=()=>{if(maxTokens.value==='')delete drafts[key].maxTokens;else drafts[key].maxTokens=Number(maxTokens.value);dirty();};
  grid.append(label('渠道',provider),label('模型',model),label('思考等级',effort),label('DSH 模式',preset),label('最大生成 Token 数',maxTokens));fields.append(grid,minimalNote);card.append(fields);return card;
 }
 function renderBindings(){const root=by('model-settings-bindings');root.replaceChildren();if(settings)for(const key of Object.keys(purposes))root.append(renderPurpose(key));displayStatus();}
 function applySettings(value){
  if(!Number.isInteger(value.revision)||!value.bindings||Object.keys(purposes).some(key=>!(key in value.bindings)))throw new Error('模型设置结果不完整，当前内容未覆盖。');
  settings=clone(value);drafts=clone(value.bindings);stash={};saveIntent=null;
  by('model-settings-revision').textContent='当前 v'+value.revision;
  renderBindings();
 }
 async function loadSettings(){const value=await api(BASE+'settings');applySettings(value);by('model-settings-result').textContent='';}
 async function loadCatalog(){try{catalog=await api(BASE+'catalog');catalogError='';}catch(error){catalog=null;catalogError=error.message;}renderBindings();}
 function validateChanges(bindings){
  for(const [key,value] of Object.entries(bindings)){
   if(value===null)continue;
   const group=selectedGroup(value),model=group?.models?.find(x=>x.id===value.model);if(!model||!(catalog?.routableProviders||[]).includes(group.id))throw new Error('请从当前目录选择“'+purposes[key]+'”的可路由渠道和模型。');
   if(value.reasoningEffort!==null&&!efforts(model).some(x=>x.id===value.reasoningEffort))throw new Error('“'+purposes[key]+'”的思考等级不在当前模型目录中。');
   if(!catalog.agentPresets?.some(x=>x.id===value.agentPreset&&x.available===true))throw new Error('“'+purposes[key]+'”的 DSH 模式当前不可用。');
  }
 }
 by('model-settings-form').addEventListener('submit',async event=>{
  event.preventDefault();if(saving||!settings)return;
  try{
   const bindings=changed();if(!Object.keys(bindings).length)return show('设置未变化，无需保存。');validateChanges(bindings);
   const body={expectedRevision:settings.revision,bindings},digest=JSON.stringify(body);if(!saveIntent||saveIntent.digest!==digest)saveIntent={digest,requestId:newId()};
   saving=true;renderBindings();by('model-settings-reload').disabled=true;by('model-catalog-refresh').disabled=true;
   const value=await api(BASE+'settings','POST',{...body,requestId:saveIntent.requestId});applySettings(value);by('model-settings-result').textContent='已保存 v'+value.revision+'，新建会话时生效。';show('模型设置已保存。');
  }catch(error){by('model-settings-result').textContent='保存未确认或失败；当前草稿保留。';show(error.message,true);}
  finally{saving=false;by('model-settings-reload').disabled=false;by('model-catalog-refresh').disabled=false;renderBindings();}
 });
 by('model-settings-reload').onclick=async()=>{if(settings&&Object.keys(changed()).length&&!confirm('重新读取会替换未保存草稿，继续？'))return;try{await loadSettings();}catch(error){show(error.message,true);}};
 by('model-catalog-refresh').onclick=async()=>{await loadCatalog();show(ready()?'模型目录已刷新，草稿保留。':'目录暂不可用，已保存值和草稿保留。',!ready());};
 (async()=>{try{if(typeof sessionReady==='function'&&!await sessionReady())return;const results=await Promise.allSettled([loadSettings(),loadCatalog()]);for(const r of results)if(r.status==='rejected')show(r.reason.message,true);}catch(error){show(error.message,true);}})();
})();
