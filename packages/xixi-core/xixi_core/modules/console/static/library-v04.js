'use strict';
// Original v0.4 reader connected to the platform Library plugin.
(() => {
  const by=id=>document.getElementById(id), all=selector=>[...document.querySelectorAll(selector)];
  const CATEGORIES=['事实与经历','认识与判断','兴趣','黑话与名词解释','资料'], SEARCH_CATEGORIES=CATEGORIES.slice(1);
  const base=globalThis.XIXI_ADMIN_BASE||'', libraryPath=base+'/api/modules/library/v04/';
  const state={restoring:false,view:'items',query:'',items:[],next:null,total:0,current:null,versions:[],historical:false,listEpoch:0,readEpoch:0,editing:null,chatRequest:null,chatCursor:null};
  const intents=new Map(),restoreIntents=new Map();let toastTimer;
  const el=(tag,text,cls)=>{const result=document.createElement(tag);if(text!==undefined)result.textContent=String(text);if(cls)result.className=cls;return result;};
  const split=value=>String(value||'').split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);
  const formatDate=stamp=>stamp?new Date(typeof stamp==='number'?stamp*1000:stamp).toLocaleString('zh-CN',{hour12:false}):'—';
  const title=item=>item.title||item.day||String(item.body||item.excerpt||'').split('\n').find(x=>x.trim())?.slice(0,55)||item.id;
  const toast=(message,bad=false)=>{clearTimeout(toastTimer);by('message').textContent=message;by('message').className='toast'+(bad?' bad':'');by('message').hidden=false;toastTimer=setTimeout(()=>by('message').hidden=true,bad?10000:4500);};
  const action=async(button,fn)=>{if(button)button.disabled=true;try{return await fn();}catch(error){toast(error.message,true);}finally{if(button?.isConnected)button.disabled=false;}};
  async function request(path,args) {
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),25000);
    try {
      const response=await fetch(path,{method:args===undefined?'GET':'POST',credentials:'same-origin',cache:'no-store',signal:controller.signal,...(args===undefined?{}:{headers:{'Content-Type':'application/json',...(typeof csrf==='string'?{'X-CSRF-Token':csrf}:{})},body:JSON.stringify(args)})});
      const data=await response.json();
      if(response.status===401){location.assign(base+'/login');throw new Error('请重新登录。');}
      if(!response.ok){const error=new Error(data.error?.message||'图书馆操作失败。');error.httpStatus=response.status;throw error;}
      return data;
    } catch(error) {if(error.name==='AbortError')throw new Error('请求超时，结果尚未确认。内容已保留；再次保存会复用本次操作编号。');throw error;} finally {clearTimeout(timer);}
  }
  const call=(operation,args={})=>request(libraryPath+operation,args);
  async function mutate(operation,args) {
    const key=operation+'\n'+JSON.stringify(args);
    if(!intents.has(key))intents.set(key,crypto.randomUUID());
    const result=await call(operation,{...args,requestId:intents.get(key)});intents.delete(key);return result;
  }
  function route(replace=false) {
    const params=new URLSearchParams({view:state.view});
    if(state.view==='items'){
      if(by('catalog-category').value)params.set('category',by('catalog-category').value);
      if(state.query)params.set('q',state.query);
      if(by('entry-status').value!=='active')params.set('status',by('entry-status').value);
      if(by('filter-tags').value)params.set('tags',by('filter-tags').value);
      const cats=all('[name="search-category"]:checked').map(x=>x.value);if(cats.length)params.set('categories',cats.join(','));
    }
    if(state.current?.id&&['items','diary'].includes(state.view)){params.set('id',state.current.id);if(state.historical)params.set('version',String(state.current.version));}
    if(state.view==='diary'&&by('diary-date').value)params.set('date',by('diary-date').value);
    history[replace?'replaceState':'pushState']({},'',location.pathname+'?'+params);
  }
  function syncView() {
    all('[data-view]').forEach(tab=>tab.setAttribute('aria-selected',String(tab.dataset.view===state.view)));
    by('items-tools').hidden=state.view!=='items';by('items-caption').hidden=!['items','diary'].includes(state.view);
    by('diary-tools').hidden=state.view!=='diary';by('chat-tools').hidden=state.view!=='chat';by('maintenance-tools').hidden=state.view!=='maintenance';
    by('library-layout').hidden=!['items','diary'].includes(state.view);by('new-entry').hidden=state.view!=='items';
    all('.layout-toggle').forEach(button=>button.hidden=!['items','diary'].includes(state.view));
    document.title='小希图书馆-'+({items:by('catalog-category').value||'全部分类',diary:'日记',chat:'原始对话',maintenance:'维护'}[state.view]);
    by('clear-search').hidden=!state.query;
  }
  function clearReader() {
    state.readEpoch++;state.current=null;state.versions=[];state.historical=false;
    by('reader-content').hidden=true;by('reader-empty').hidden=false;by('library-layout').classList.remove('reader-open','reading-expanded');
  }
  function renderList() {
    const list=by('entry-list');list.replaceChildren();
    if(!state.items.length){const empty=el('div',undefined,'list-empty');empty.append(el('h3','暂时没有内容'),el('p',state.query?'可调整关键词和查询分类。':'可以更换分类或日期。'));list.append(empty);}
    for(const item of state.items) {
      const button=el('button',undefined,'entry-card'+(state.current?.id===item.id?' selected':''));button.type='button';button.dataset.entryId=item.id;
      button.append(el('div',title(item),'entry-title'));
      const excerpt=state.view==='diary'?item.body:item.category==='资料'?item.summary:item.excerpt;
      button.append(el('div',String(excerpt||'').slice(0,180),'entry-excerpt'));
      const meta=el('div',undefined,'entry-meta');meta.append(el('span',item.category||'日记','badge'),el('span','v'+item.version),el('time',formatDate(item.updated)));
      if(item.status==='delete_marked')meta.append(el('span','待删除','badge retired'));if(item.needsReview)meta.append(el('span','待整理','badge'));
      button.append(meta);button.onclick=()=>action(button,()=>state.view==='diary'?openDiary(item):openItem(item.id));list.append(button);
    }
    by('list-caption').textContent=state.view==='diary'?'独立日记':state.query?'查询结果 · '+all('[name="search-category"]:checked').map(x=>x.value).join('、'):'分类目录 · '+(by('catalog-category').value||'五个并列分类');
    by('list-progress').textContent=state.items.length+' / '+state.total+' 条';by('load-more').hidden=state.next===null;
  }
  async function loadEntries(append=false) {
    if(!['items','diary'].includes(state.view))return;
    const epoch=++state.listEpoch,view=state.view,offset=append?state.next:0;
    if(append&&offset===null)return;
    by('list-error').hidden=true;by('entry-list').setAttribute('aria-busy','true');
    try {
      let result;
      if(view==='diary')result=await call('diary',{limit:30,offset,...(by('diary-date').value?{date:by('diary-date').value}:{})});
      else {
        const args={limit:30,offset,status:by('entry-status').value};const tags=split(by('filter-tags').value);if(tags.length)args.tags=tags;
        if(state.query){const categories=all('[name="search-category"]:checked').map(x=>x.value);if(!categories.length)throw new Error('查询需要至少选择一个分类；事实与经历请使用目录浏览。');if(args.status!=='active')throw new Error('简单查询只读取有效条目；查看待删除内容请清除查询后浏览目录。');result=await call('search',{...args,query:state.query,categories});}
        else {if(by('catalog-category').value)args.category=by('catalog-category').value;result=await call('list',args);}
      }
      if(epoch!==state.listEpoch||view!==state.view)return;
      state.items=append?[...state.items,...result.items]:result.items;state.next=result.pagination.nextOffset;state.total=result.pagination.total;renderList();
      by('library-status').textContent=result.semanticIndexComplete===false?'新图书馆 · 关键词可用，本次语义索引尚未完整':'新图书馆 · v0.4';
    } catch(error){if(epoch!==state.listEpoch)return;by('list-error').textContent=error.message;by('list-error').hidden=false;if(error.httpStatus===503)by('library-status').textContent='图书馆暂不可用';throw error;}
    finally{if(epoch===state.listEpoch)by('entry-list').setAttribute('aria-busy','false');}
  }
  function renderBody(item) {
    renderMarkdown(item.body||'',by('detail-body'),by('article-toc'));by('detail-source').textContent=item.body||'';
    by('detail-body').hidden=false;by('detail-source').hidden=true;by('article-toc').hidden=by('article-toc').childElementCount<4;
    by('read-formatted').setAttribute('aria-selected','true');by('read-source').setAttribute('aria-selected','false');
    by('reading-info').textContent=Array.from(item.body||'').length+' 字符';
  }
  function renderReader(item,versions=[],currentVersion=versions[0]?.version||item.version) {
    state.current=item;state.versions=versions;state.historical=item.version!==currentVersion;
    by('reader-content').hidden=false;by('reader-empty').hidden=true;by('library-layout').classList.add('reader-open');
    by('detail-title').textContent=title(item);by('reader-type').textContent=item.category||'独立日记';
    by('detail-meta').textContent=item.id+' · v'+item.version+' · '+formatDate(item.updated)+(item.status==='delete_marked'?' · 已设置删除标记':'');
    by('historical-banner').hidden=!state.historical;by('historical-banner').textContent=state.historical?'当前查看历史版本，编辑请先切回最新版本。':'';
    by('edit-entry').disabled=state.historical;by('reader-menu-button').hidden=item.kind==='diary';by('reader-menu').hidden=true;
    by('retire-entry').hidden=item.status==='delete_marked';by('restore-entry').hidden=item.status!=='delete_marked';by('retire-entry').disabled=state.historical;by('restore-entry').disabled=state.historical;
    renderBody(item);
    const sources=by('detail-links');sources.replaceChildren();
    if(item.tags?.length)sources.append(el('p','标签：'+item.tags.join('、')));
    if(item.aliases?.length)sources.append(el('p','别名：'+item.aliases.join('、')));
    for(const ref of item.sourceRefs||[])sources.append(el('p',ref,'source-ref'));
    if(!item.sourceRefs?.length)sources.append(el('p','没有登记来源。'));
    else {const button=el('button','展开来源正文','text-button');button.type='button';button.onclick=()=>action(button,async()=>{const data=await call('sources',{sourceRefs:item.sourceRefs});const text=el('pre',JSON.stringify(data,null,2));sources.append(text);button.remove();});sources.append(button);}
    const history=by('history-list');history.replaceChildren();by('history-details').hidden=!versions.length;
    for(const version of versions) {
      const row=el('div',undefined,'version-row'),info=el('div',undefined,'version-info');row.dataset.version=String(version.version);info.append(el('strong','v'+version.version),el('p',formatDate(version.updated)));row.append(info);
      const read=el('button',version.version===item.version?'正在查看':'查看','text-button');read.type='button';read.disabled=version.version===item.version;read.dataset.versionAction='read';read.onclick=()=>action(read,()=>item.kind==='diary'?openDiary(item,version.version):openItem(item.id,version.version));row.append(read);
      if(version.version!==currentVersion){const restore=el('button','恢复此版本内容','text-button');restore.type='button';restore.dataset.versionAction='restore';restore.onclick=()=>action(restore,()=>restoreVersion(item,version));row.append(restore);}
      history.append(row);
    }
    renderList();route(state.restoring);
  }
  async function openItem(id,version) {
    const epoch=++state.readEpoch;const result=await call('get',{id,...(version===undefined?{}:{version})});if(epoch!==state.readEpoch)return;renderReader(result.item,result.versions||[]);
  }
  async function openDiary(item,version) {
    const epoch=++state.readEpoch;const result=await call('diary',{id:item.id,includeVersions:true,...(version===undefined?{}:{version})});
    if(epoch!==state.readEpoch)return;
    if(!result.items?.[0])throw new Error('这份日记或历史版本不存在。');
    renderReader(result.items[0],result.versions||[],result.currentVersion);
  }
  async function restoreVersion(item,version) {
    if(!confirm('将此版本正文恢复为一个新版本？当前版本历史会保留。'))return;
    const key=(item.kind==='diary'?'diary':'item')+':'+item.id+':'+version.version;
    let prepared=restoreIntents.get(key);
    if(!prepared){
      if(item.kind==='diary'){
        const snapshot=(await call('diary',{id:item.id,version:version.version})).items[0];
        const latest=(await call('diary',{id:item.id})).items[0];
        if(!snapshot||!latest)throw new Error('日记版本不存在，请刷新后再试。');
        prepared={operation:'write_diary',args:{day:latest.day,expectedVersion:latest.version,title:snapshot.title,body:snapshot.body,sourceRefs:snapshot.sourceRefs||[]}};
      }else{
        const latest=(await call('get',{id:item.id})).item;
        prepared={operation:'revise',args:{id:item.id,expectedVersion:latest.version,changes:Object.fromEntries(['category','title','body','summary','tags','aliases','sourceRefs'].map(k=>[k,version[k]]))}};
      }
      restoreIntents.set(key,prepared);
    }
    try{await mutate(prepared.operation,prepared.args);restoreIntents.delete(key);}
    catch(error){if(error.httpStatus&&error.message.includes('版本冲突'))restoreIntents.delete(key);throw error;}
    if(item.kind==='diary')await openDiary(item);else await openItem(item.id);
    await loadEntries();toast('已恢复为一个新版本，旧版本仍保留。');
  }
  function showEditor(item=null) {
    state.editing=item;by('editor-title').textContent=item?'编辑内容':'新增内容';by('editor-error').hidden=true;
    const diary=item?.kind==='diary';by('editor-category-label').hidden=diary;by('editor-tags-label').hidden=diary;by('editor-aliases-label').hidden=diary;
    by('new-category').value=item?.category||by('catalog-category').value||'认识与判断';by('new-title').value=item?.title||'';by('new-content').value=item?.body||'';by('new-summary').value=item?.summary||'';by('new-tags').value=(item?.tags||[]).join('，');by('new-aliases').value=(item?.aliases||[]).join('，');by('summary-valid').checked=false;syncSummary();
    by('editor-hint').textContent=diary?'保存现有日记的新版本，不加入常规查询。':'保存到新图书馆，自动同步关键词并排队生成向量。';by('editor').showModal();
  }
  function syncSummary() {const material=state.editing?.kind!=='diary'&&by('new-category').value==='资料';by('summary-label').hidden=!material;by('new-summary').required=material;by('summary-valid-label').hidden=!(material&&state.editing?.category==='资料');}
  async function saveEditor() {
    const editing=state.editing,body=by('new-content').value,title=by('new-title').value.trim();if(!body.trim())throw new Error('正文不能为空。');let saved;
    if(editing?.kind==='diary')saved=await mutate('write_diary',{day:editing.day,expectedVersion:editing.version,title,body,sourceRefs:editing.sourceRefs||[]});
    else {
      const category=by('new-category').value,summary=category==='资料'?by('new-summary').value.trim():'';
      if(category==='资料'&&!summary)throw new Error('资料必须单独填写摘要。');
      if(editing?.category==='资料'&&category==='资料'&&body!==editing.body&&summary===editing.summary&&!by('summary-valid').checked)throw new Error('请更新资料摘要，或确认沿用的摘要仍然适用。');
      const changes={category,title,body,summary,tags:split(by('new-tags').value),aliases:split(by('new-aliases').value)};
      saved=editing?await mutate('revise',{id:editing.id,expectedVersion:editing.version,changes}):await mutate('record',changes);
    }
    by('editor').close();await loadEntries();if(editing?.kind==='diary')await openDiary(saved);else await openItem(saved.id);toast('已保存到新图书馆。');
  }
  async function markDeleted(cancel=false) {
    if(!state.current||state.historical)return;
    if(!confirm(cancel?'取消该条目的删除标记？':'设置删除标记？内容和历史仍然保留，不会永久删除。'))return;
    const item=state.current;await mutate(cancel?'cancel_delete':'mark_delete',{id:item.id,expectedVersion:item.version,reason:'主人在图书馆设置'+(cancel?'取消':'')+'删除标记'});await openItem(item.id);await loadEntries();toast(cancel?'删除标记已取消。':'已设置删除标记，未执行永久删除。');
  }
  async function loadIndex() {
    const result=await request(libraryPath+'status');const jobs=result.jobs||{};
    by('index-summary').textContent='关键词索引：'+(result.keyword==='ready'?'就绪':result.keyword||'未报告')+'；向量编码器：'+(result.encoderConfigured?'已配置':'未配置')+'；后台编码：'+(result.workerRunning?'运行中':'未运行')+'；等待 '+(jobs.pending||0)+'，失败 '+(jobs.failed||0)+'。';
    by('index-details').textContent=JSON.stringify(result,null,2);by('cleanup-policy').textContent='自动永久清理未启用：没有已确认的保留期限、清理策略和执行流程。删除只打标记，本页不提供永久删除。';return result;
  }
  async function changeView(view,push=true) {
    state.listEpoch++;clearReader();state.view=view;state.items=[];state.next=null;syncView();if(push)route();
    if(['items','diary'].includes(view))await loadEntries();else if(view==='maintenance')await loadIndex();
  }
  async function readChat(args,append=false) {
    const result=await call('chat_history',args);state.chatRequest=args;state.chatCursor=result.pagination.nextCursor;
    by('chat-result').textContent=append?by('chat-result').textContent+'\n'+result.text:result.text;by('chat-more').hidden=!state.chatCursor;
  }
  all('[data-view]').forEach(button=>button.onclick=()=>action(button,()=>changeView(button.dataset.view)));
  by('catalog-category').onchange=()=>action(null,async()=>{state.query='';by('search-keywords').value='';all('[name="search-category"]').forEach(box=>box.checked=box.value===by('catalog-category').value);clearReader();syncView();route();await loadEntries();});
  by('search-form').onsubmit=event=>{event.preventDefault();state.query=by('search-keywords').value.trim();syncView();route();action(event.submitter,()=>loadEntries());};
  by('clear-search').onclick=()=>action(null,async()=>{state.query='';by('search-keywords').value='';syncView();route();await loadEntries();});
  by('toggle-filter').onclick=()=>{by('filters').hidden=!by('filters').hidden;by('toggle-filter').setAttribute('aria-expanded',String(!by('filters').hidden));};
  by('apply-filter').onclick=()=>action(null,async()=>{route();await loadEntries();});
  by('reset-filter').onclick=()=>action(null,async()=>{by('entry-status').value='active';by('filter-tags').value='';all('[name="search-category"]').forEach(box=>box.checked=box.value==='认识与判断');route();await loadEntries();});
  by('refresh').onclick=()=>action(by('refresh'),async()=>{if(['items','diary'].includes(state.view))await loadEntries();else if(state.view==='maintenance')await loadIndex();else if(state.chatRequest)await readChat({...state.chatRequest,cursor:undefined});});
  by('load-more').onclick=()=>action(by('load-more'),()=>loadEntries(true));by('new-entry').onclick=()=>showEditor();
  by('edit-entry').onclick=()=>showEditor(state.current);by('new-category').onchange=syncSummary;
  by('entry-form').onsubmit=event=>{event.preventDefault();const button=by('save-entry');button.disabled=true;by('editor-error').hidden=true;saveEditor().catch(error=>{by('editor-error').textContent=error.message;by('editor-error').hidden=false;}).finally(()=>button.disabled=false);};
  for(const id of ['close-editor','cancel-editor'])by(id).onclick=()=>by('editor').close();
  by('reader-menu-button').onclick=()=>{by('reader-menu').hidden=!by('reader-menu').hidden;by('reader-menu-button').setAttribute('aria-expanded',String(!by('reader-menu').hidden));};
  by('retire-entry').onclick=()=>action(by('retire-entry'),()=>markDeleted());by('restore-entry').onclick=()=>action(by('restore-entry'),()=>markDeleted(true));
  by('read-formatted').onclick=()=>{by('detail-body').hidden=false;by('detail-source').hidden=true;by('read-formatted').setAttribute('aria-selected','true');by('read-source').setAttribute('aria-selected','false');};
  by('read-source').onclick=()=>{by('detail-body').hidden=true;by('detail-source').hidden=false;by('read-formatted').setAttribute('aria-selected','false');by('read-source').setAttribute('aria-selected','true');};
  by('back-to-list').onclick=()=>by('library-layout').classList.remove('reader-open');by('expand-reader').onclick=()=>by('library-layout').classList.toggle('reading-expanded');
  by('copy-link').onclick=()=>action(null,async()=>{await navigator.clipboard.writeText(location.href);toast('链接已复制。');});
  by('diary-form').onsubmit=event=>{event.preventDefault();clearReader();route();action(event.submitter,()=>loadEntries());};
  by('chat-form').onsubmit=event=>{event.preventDefault();if(by('chat-date').value&&(by('chat-from').value||by('chat-to').value)){toast('日期和起止时间范围请选择一种，避免范围冲突。',true);return;}const args={recent:Number(by('chat-limit').value),limit:Number(by('chat-limit').value)};if(by('chat-date').value)args.date=by('chat-date').value;for(const key of ['from','to'])if(by('chat-'+key).value)args[key]=new Date(by('chat-'+key).value).toISOString();if(by('chat-around').value.trim()){args.around=by('chat-around').value.trim();args.before=10;args.after=10;delete args.recent;}action(event.submitter,()=>readChat(args));};
  by('chat-more').onclick=()=>action(by('chat-more'),()=>readChat({cursor:state.chatCursor},true));
  by('chat-search-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{const result=await call('chat_search',{query:by('chat-query').value.trim(),limit:30});by('chat-result').textContent=JSON.stringify(result,null,2);by('chat-more').hidden=true;});};
  by('refresh-index').onclick=()=>action(by('refresh-index'),loadIndex);by('service-details-open').onclick=()=>action(null,()=>changeView('maintenance'));
  by('retry-index').onclick=()=>action(by('retry-index'),async()=>{await mutate('retry_index',{});await loadIndex();toast('失败任务已重新排队，实际完成情况以索引状态为准。');});
  by('sql-form').onsubmit=event=>{event.preventDefault();action(event.submitter,async()=>{const result=await call('sql',{sql:by('sql-query').value,limit:100});by('sql-result').textContent=JSON.stringify(result,null,2);});};
  by('read-schema').onclick=()=>action(by('read-schema'),async()=>{by('sql-result').textContent=JSON.stringify(await call('schema',{}),null,2);});
  async function restoreRoute() {
    state.restoring=true;try {
    const params=new URLSearchParams(location.search),view=params.get('view')||'items';state.view=['items','diary','chat','maintenance'].includes(view)?view:'items';state.query=params.get('q')||'';by('search-keywords').value=state.query;
    if(params.has('category'))by('catalog-category').value=CATEGORIES.includes(params.get('category'))?params.get('category'):'';
    if(['active','delete_marked','all'].includes(params.get('status')))by('entry-status').value=params.get('status');
    if(params.has('tags'))by('filter-tags').value=params.get('tags');if(params.has('date'))by('diary-date').value=params.get('date');
    if(params.has('categories')){const cats=split(params.get('categories'));all('[name="search-category"]').forEach(box=>box.checked=cats.includes(box.value));}
    clearReader();syncView();if(['items','diary'].includes(state.view)){await loadEntries();const id=params.get('id');if(id){const version=/^[1-9][0-9]*$/.test(params.get('version')||'')?Number(params.get('version')):undefined;if(state.view==='items')await openItem(id,version);else await openDiary({id},version);}}else if(state.view==='maintenance')await loadIndex();
    } finally {state.restoring=false;}
  }
  window.addEventListener('popstate',()=>action(null,restoreRoute));
  // Tests can wait on actual API completion without exposing private content globally.
  window.xixiLibraryReady=(async()=>{if(typeof sessionReady==='function'&&await sessionReady()===false)return;await restoreRoute();})().catch(error=>{toast(error.message,true);throw error;});
})();
