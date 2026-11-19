/** Core protocol adapter hosted INSIDE an already running DSH Context.
 * No Context, model client, credential reader or agent-loop is constructed here.
 */
import { randomUUID, createHash } from 'node:crypto';
import { resolve } from 'node:path';
import { BridgeStore } from './store.mjs';
import { configuredSystemText, installCorePrompt, normalizeNativeCompaction } from './core-prompt.mjs';
import { KeyedSerial, claim, snapshot, canonicalJSON } from './coordination.mjs';
import { IsolatedOperations } from './isolated.mjs';
import { definitions } from './tool-definitions.mjs';
import { safeModelCatalog } from './native-settings.mjs';
import { normalizePromptPolicy, renderPresentationTemplate, RUNTIME_PRESENTATION_PRODUCERS, promptPolicyCapabilities as safePromptPolicyCapabilities } from './prompt-policy.mjs';
import { normalizeFragments } from './context.mjs';
import { normalizePromptSections, sectionText, presentation } from './presentation.mjs';
import { validateLibraryMode, isLibraryToolName, requireAvailableMaintenance } from './capabilities.mjs';

const READ_NAMES = Object.keys(definitions).filter(n => /^(?:memory_search|library_(?:search|list|get|glossary|diary|sources|chat_history|chat_search|schema|sql|query|terms|read)|history_|task_(?:status|result_read|artifact_read))/.test(n));
const TERMINAL = new Set(['completed','failed','stopped','quarantined']);
const PLATFORM_LIBRARY_TOOLS = ['library_search','library_read','library_record'];
const TASK_READ_TOOLS = ['task_status','task_result_read','task_artifact_read'];
const PLATFORM_ASSISTANT_TOOLS = {
  daily:['task_create','task_cancel',...TASK_READ_TOOLS],
  work:[...TASK_READ_TOOLS,'task_artifact_write'],
};
const platformTools = input => [...(input?.libraryTools??[]), ...(input?.assistantTools??[])];
const hash = v => createHash('sha256').update(JSON.stringify(v)).digest('hex');
const validId = id => typeof id === 'string' && /^[A-Za-z0-9_.:-]{1,180}$/.test(id);
const textOf = e => (e?.data?.message?.content ?? e?.data?.content ?? []).filter(x=>x.type==='text').map(x=>x.text).join('');
const signal = () => AbortSignal.timeout(15000);
const presetOf = config => {
  const direct=config?.agentPreset,option=config?.modelOptions?.agentPreset;
  if(direct!==undefined&&option!==undefined&&direct!==option)throw Error('AGENT_PRESET_CONFLICT');
  const preset=direct??option??'standard';
  if(!['standard','minimal'].includes(preset))throw Error('INVALID_CORE_AGENT_PRESET');
  return preset;
};

export class ExistingDshBridge {
  static async open(ctx, options = {}) {
    for (const name of ['sessionController','agents','sessions','llm','tools','systemPrompt'])
      if (!ctx[name]) throw Error(`EXISTING_DSH_CAPABILITY_MISSING:${name}`);
    if (typeof options.defineTool !== 'function') throw Error('HOST_DEFINE_TOOL_REQUIRED');
    if(typeof options.acquireLease!=='function')throw Error('HOST_DATA_LEASE_REQUIRED');
    const lease = await options.acquireLease(resolve(options.root));
    try {
      const b = new ExistingDshBridge(ctx, options, lease);
      return b;
    } catch(e) { await lease.release(); throw e; }
  }
  constructor(ctx, options, lease) {
    this.ctx=ctx; this.lease=lease; this.root=lease.root; this.store=new BridgeStore(this.root);
    this.cwd=options.cwd; this.defineTool=options.defineTool; this.callback=options.callback; this.eventCallback=options.eventCallback;
    this.provider='existing-dsh'; this.dshVersion=options.dshVersion ?? '0.2.2-rc.2';
    this.validationModel=options.validationModel;
    this.compactionPresentationApi=options.compactionPresentationApi??0;
    this.nativePromptRenderCapabilities=Object.freeze(Object.fromEntries(RUNTIME_PRESENTATION_PRODUCERS.map(name=>[name,options.nativePromptRenderCapabilities?.[name]===1?1:0])));
    this.handles=new Map(); this.serial=new KeyedSerial(); this.operations=new IsolatedOperations(this);
    this.installationId=this.store.get('host','installation') ?? this.store.set('host','installation',randomUUID());
    // Merely loading this plugin never resumes/reads a pre-existing user Session.
    // Unknown in-flight effects are reconciled on explicit Core status queries.
    for (const r of this.store.all('input')) if(['preparing','accepted','running'].includes(r.status))
      this.store.set('input',r.inputId,{...r,status:'unknown',error:'DSH plugin restarted; reconcile the owned durable Session before retrying'});
    this.offCompactionPrompt=ctx.on('compaction/prompt',({agent,purpose})=>{
      const id=this.store.get('remote-map',agent.id),config=id&&this.store.get('session',id);
      if(!config?.nativeCompaction)return undefined;
      const text=config.nativeCompaction[purpose];
      if(text===undefined)return undefined;
      if(typeof text!=='string'||(purpose==='instruction'&&!text.trim()))throw Error('CORE_COMPACTION_PRESENTATION_INVALID');
      this.store.set('compaction-prompt-observed',id,{purpose,sha256:createHash('sha256').update(text).digest('hex'),
        version:config.nativeCompaction.versions?.[purpose],observedAt:new Date().toISOString()});
      return text;
    });
    // Auxiliary title workers can outlive their live Agent. Bind only by the
    // installation's durable remote-map and its immutable stored session policy.
    // The original message JSON never enters this event or the template renderer.
    this.offNativeAuxPrompt=ctx.on('native-aux-prompt/render',request=>{
      if(this.closing||request?.kind!=='sessionTitle'||typeof request.sessionId!=='string')return;
      const id=this.store.get('remote-map',request.sessionId),frozen=id&&this.store.get('session',id);
      if(!frozen||frozen.remoteId!==request.sessionId)return;
      const choice=frozen.promptPolicy?.runtime?.sessionTitle;
      if(choice?.mode!=='template')return;
      const rendered={};
      for(const part of ['system','inputIntro']){
        const template=choice.templates?.[part];
        if(template)rendered[part]=renderPresentationTemplate(template,request.variables);
      }
      if(!Object.keys(rendered).length)return;
      this.store.set('native-aux-prompt-observed',id,{kind:'sessionTitle',
        policySha256:createHash('sha256').update(canonicalJSON(frozen.promptPolicy)).digest('hex'),
        partSha256:Object.fromEntries(Object.entries(rendered).map(([part,text])=>[part,createHash('sha256').update(text).digest('hex')])),
        observedAt:new Date().toISOString(),source:'owned-frozen-session-policy'});
      return rendered;
    });
    this.offEvent=ctx.on('session/event',(session,event)=>this.onEvent(session,event));
    this.offCreated=ctx.on('agent/created',async({agent})=>{const id=this.store.get('remote-map',agent.id);if(id)await this.bindAgent(id,agent);});
  }
  scopes(c) {
    const names=c.toolScopes ?? (c.kind==='daily' ? Object.keys(definitions) : []);
    if(!Array.isArray(names)||names.some(n=>!Object.hasOwn(definitions,n))) throw Error('INVALID_TOOL_SCOPES');
    return [...new Set(names)].filter(n=>(c.libraryMode!=='deferred'||!isLibraryToolName(n)) &&
      (n!=='task_artifact_write'||c.kind==='work') &&
      (!['service_restart','session_new','task_cancel'].includes(n)||c.kind==='daily') &&
      (!['idle','dream','maintenance'].includes(c.kind)||READ_NAMES.includes(n)));
  }
  async session(config) {
    config=snapshot(config);
    if(!validId(config?.id)) throw Error('INVALID_SESSION_ID');
    validateLibraryMode(config.libraryMode);requireAvailableMaintenance(config);
    const agentPreset=presetOf(config); // Do not add a default field to old durable idempotence specifications.
    config.kind ??= 'daily'; if(!['daily','work','idle','dream','maintenance'].includes(config.kind))throw Error('INVALID_SESSION_KIND'); config.promptSections=normalizePromptSections(config.promptSections??[]);
    config.contextFragments=normalizeFragments(config.contextFragments??[]); this.scopes(config);
    if(config.platformLibraryTools && (config.platformLibraryTools!==true || !['daily','idle','dream','work'].includes(config.kind) || config.toolScopes?.length!==0))
      throw Error('INVALID_PLATFORM_TOOL_SESSION');
    if(config.promptPolicy!==undefined){
      config.promptPolicy=normalizePromptPolicy(config.promptPolicy);
      for(const producer of RUNTIME_PRESENTATION_PRODUCERS){
        if(config.promptPolicy.runtime[producer].mode==='template'&&this.nativePromptRenderCapabilities[producer]!==1)
          throw Object.assign(Error('HOST_NATIVE_PROMPT_RENDER_PATCH_REQUIRED: '+producer),{code:'HOST_NATIVE_PROMPT_RENDER_PATCH_REQUIRED'});
      }
      if(agentPreset==='minimal'&&config.promptPolicy.minimal.mode==='inherit'&&configuredSystemText(config))
        throw Object.assign(Error('CORE_MINIMAL_INHERIT_HAS_SYSTEM_TEXT'),{code:'CORE_MINIMAL_INHERIT_HAS_SYSTEM_TEXT'});
    }

    if(config.nativeCompaction!==undefined){
      config.nativeCompaction=normalizeNativeCompaction(config.nativeCompaction);
      if(this.compactionPresentationApi!==1)throw Error('HOST_COMPACTION_PRESENTATION_PATCH_REQUIRED');
    }
    return this.serial.run('session:'+config.id,async()=>{
      claim(this.store,'session-spec',config.id,config);
      if(this.store.get('session-release',config.id))throw Error('SESSION_RELEASED');
      let saved=this.store.get('session',config.id);
      if(!saved){
        const remoteId='xixi-'+hash([this.installationId,config.id]).slice(0,40);
        saved=this.store.set('session',config.id,{...config,remoteId});
        this.store.set('remote-map',remoteId,config.id);
        const created=await this.ctx.sessionController.create({sessionId:remoteId,agentPreset,...(this.cwd?{cwd:this.cwd}:{})});
        if(created.sessionId!==remoteId)throw Error('DSH_SESSION_ID_CONFLICT');
      }
      const agent=await this.getAgent(config.id);await this.ctx.sessions.flush(agent.session);
      return {id:config.id,state:'idle',remoteSessionId:saved.remoteId,agentPreset,connectionMode:'existing-dsh'};
    });
  }
  async getAgent(id) {
    const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');
    if(this.store.get('session-release',id))throw Error('SESSION_RELEASED');
    const cached=this.handles.get(id);
    if(cached && this.ctx.agents.get(c.remoteId)===cached.agent)return cached.agent;
    const resolved=await this.ctx.sessionController.resolveAgent(c.remoteId);
    if(resolved.error)throw resolved.error;
    const agent=resolved.agent;if(!agent||agent.id!==c.remoteId)throw Error('DSH_AGENT_ID_MISMATCH');
    return this.bindAgent(id,agent);
  }
  async bindAgent(id,agent){
    const c=this.store.get('session',id);if(!c||agent.id!==c.remoteId)throw Error('SESSION_NOT_OWNED_BY_CORE');
    if(this.handles.get(id)?.agent===agent)return agent;
    const current=agent.model.current();
    let selected=current;
    if(c.model){
      let provider=c.provider??c.modelOptions?.provider??current?.provider;
      if(!provider){const catalogue=await this.ctx.sessionController.modelCatalog();provider=catalogue.default.provider;}
      selected={provider,model:c.model,...(c.modelOptions?.reasoningEffort!==undefined?{reasoningEffort:c.modelOptions.reasoningEffort}:{})};
      await this.ctx.llm.resolveCallConfig(selected);
      // The public selectModel Remote also saves the HOST default. Do not use it.
      agent.model.select(selected);
    }
    if(c.modelOptions?.maxTokens!==undefined){
      if(!Number.isSafeInteger(c.modelOptions.maxTokens)||c.modelOptions.maxTokens<1)throw Error('INVALID_MAX_TOKENS');
      agent.options.maxTokens=c.modelOptions.maxTokens;
    }
    const disposers=[];
    disposers.push(installCorePrompt(agent.ctx,agent,c,details=>
      this.store.set('prompt-assembly',id,{...(this.store.get('prompt-assembly',id)??{}),...details,observedAt:new Date().toISOString(),nativeContextsSuppressed:c.promptPolicy===undefined}),this.nativePromptRenderCapabilities));
    // Scope only this Agent: do not alter the human-facing DSH tool registry.
    disposers.push(agent.ctx.tools.restrict({allow:[]}));
    const handle={agent,disposers,platformToolNames:[],platformDisposers:[]};
    this.handles.set(id,handle);
    if(c.platformLibraryTools){
      const inputId=this.store.get('active-input',id), active=inputId&&this.store.get('input',inputId);
      if(active&&!TERMINAL.has(active.status))this.bindPlatformTools(id,platformTools(active.input));
    }else for(const name of this.scopes(c))disposers.push(this.registerTool(id,agent,c,name));
    return agent;
  }
  bindPlatformTools(id,names){
    const handle=this.handles.get(id), config=this.store.get('session',id);
    if(names.length===handle.platformToolNames.length&&names.every((name,index)=>name===handle.platformToolNames[index]))return;
    for(const dispose of handle.platformDisposers)dispose();
    handle.platformDisposers=[];
    handle.platformToolNames=[];
    for(const name of names)handle.platformDisposers.push(this.registerTool(id,handle.agent,config,name));
    handle.platformToolNames=names;
  }
  registerTool(id,agent,c,name){
      const d=definitions[name];
      return agent.ctx.tools.register(this.defineTool({name,...d,output:{schema:{type:'json'},render:(_a,v)=>[{type:'text',text:JSON.stringify(v)}]},execute:async(args,exec)=>{
        const inputId=this.store.get('active-input',id);
        if(!inputId||this.store.get('task-control',id)?.stopRequested)throw Error('CORE_INPUT_NOT_ACTIVE_OR_STOPPED');
        const input=c.platformLibraryTools?this.store.get('input',inputId):undefined;
        if(input&&TERMINAL.has(input.status))throw Error('CORE_INPUT_NOT_ACTIVE_OR_STOPPED');
        const scopes=c.platformLibraryTools?platformTools(input?.input):this.scopes(this.store.get('session',id));
        if(!scopes?.includes(name))throw Error('CORE_TOOL_SCOPE_DENIED');
        const requestId=`${id}:${exec.callId}`;
        if(name==='send_message')this.store.set('send',requestId,{sessionId:id,inputId,status:'unknown'});
        this.store.set('work-tool',requestId,{sessionId:id,inputId,name,status:'running'});
        const origin=input?.input.assistantOrigin;
        const result=await this.callback({sessionId:id,sessionKind:c.kind,inputId,tool:name,args,requestId,...(origin?{origin}:{})},exec.signal);
        if(name==='send_message'){
          const status=result?.data?.state??result?.data?.status??result?.state??result?.status;
          this.store.set('send',requestId,{sessionId:id,inputId,status:result?.ok===false&&!['unknown','sending'].includes(status)?'failed':status??'unknown'});
        }
        this.store.set('work-tool',requestId,{sessionId:id,inputId,name,status:'completed'});
        if(name==='task_artifact_write'){
          const ref=result?.data?.artifactRef??result?.artifactRef;
          if(result?.ok!==false&&typeof ref==='string')this.store.set('work-artifact',ref,{sessionId:id,ref});
        }
        return result;
      }}));
  }
  async deliver(id,input) {
    if(this.closing)throw Error('BRIDGE_CLOSING');
    if(this.store.get('session-maintenance-stop',id))throw Error('MAINTENANCE_STOPPED');
    if(this.store.get('task-control',id)?.stopRequested)throw Error('TASK_STOPPED');
    if(!validId(input?.inputId)||typeof input.text!=='string')throw Error('INVALID_INPUT');
    input=snapshot(input); normalizePromptSections(input.promptSections??[]); normalizeFragments(input.contextFragments??[]); presentation(input);
    return this.serial.run('deliver:'+id,async()=>{
      const old=this.store.get('input',input.inputId);
      if(old){if(old.sessionId!==id||hash(old.input)!==hash(input))throw Error('INPUT_ID_CONFLICT');return this.input(id,input.inputId);}
      const agent=await this.getAgent(id);
      const activeId=this.store.get('active-input',id),active=activeId&&this.store.get('input',activeId);
      if(active&&!TERMINAL.has(active.status))throw Error('SESSION_HAS_UNSETTLED_INPUT');
      const c=this.store.get('session',id);
      if(input.libraryTools!==undefined&&(!c.platformLibraryTools||!Array.isArray(input.libraryTools)||
        input.libraryTools.some(name=>!PLATFORM_LIBRARY_TOOLS.includes(name)||(c.kind!=='daily'&&name==='library_record'))||input.libraryTools.length!==new Set(input.libraryTools).size))
        throw Error('INVALID_PLATFORM_LIBRARY_TOOLS');
      if(input.assistantTools!==undefined&&(!c.platformLibraryTools||!Array.isArray(input.assistantTools)||
        input.assistantTools.some(name=>!PLATFORM_ASSISTANT_TOOLS[c.kind]?.includes(name))||input.assistantTools.length!==new Set(input.assistantTools).size))
        throw Error('INVALID_PLATFORM_ASSISTANT_TOOLS');
      if(input.assistantTools?.includes('task_create')&&(!input.assistantOrigin||typeof input.assistantOrigin.channel!=='string'||typeof input.assistantOrigin.peer!=='string'))
        throw Error('INVALID_PLATFORM_ASSISTANT_ORIGIN');
      const start=this.store.get('session-started',id)?'':sectionText(c.promptSections,'start');
      const runSections=input.presentationText===undefined?sectionText(input.promptSections??c.promptSections,'run'):'';
      const runFragments=(input.contextFragments??c.contextFragments).filter(f=>f.placement==='run').map(f=>f.text).join('\n\n');
      const content=[start,runSections,runFragments,presentation(input)].filter(Boolean).join('\n\n');
      this.store.set('input',input.inputId,{sessionId:id,inputId:input.inputId,input,status:'preparing',cursor:agent.session.seq,output:''});
      this.store.set('active-input',id,input.inputId);
      try{
        if(c.platformLibraryTools)this.bindPlatformTools(id,platformTools(input));
        await this.ctx.sessionController.prompt({sessionId:c.remoteId,requestId:input.inputId,mode:'queue',content:[{type:'text',text:content}]},signal());
        await this.ctx.sessions.flush(agent.session);
        this.store.set('session-started',id,true);
        const r=this.store.get('input',input.inputId);
        if(r.status==='preparing')this.store.set('input',input.inputId,{...r,status:'accepted'});
        return {inputId:input.inputId,status:this.store.get('input',input.inputId).status};
      }catch(e){
        const r=this.store.get('input',input.inputId);
        this.store.set('input',input.inputId,{...r,status:'unknown',error:'Prompt admission failed or outcome is uncertain; query durable status before retrying'});
        throw e;
      }
    });
  }
  onEvent(session,event){
    const id=this.store.get('remote-map',session.id);if(!id||this.closing)return;
    if(event.type==='system/message'){
      const actual=textOf(event),expected=configuredSystemText(this.store.get('session',id));
      const sha256=createHash('sha256').update(actual).digest('hex');
      const effective=this.store.get('prompt-assembly',id)?.effectiveSystemSha256??createHash('sha256').update(expected).digest('hex');
      this.store.set('prompt-system-observed',id,{sha256,chars:actual.length,
        matchesConfigured:actual===expected,matchesEffective:sha256===effective,observedAt:new Date().toISOString(),source:'native-system-message-event'});
    }
    if(event.type==='request/context')this.store.set('request-context',id,event.data);
    if(event.type==='compaction/end')this.store.set('native-compaction',id,true);
    if(['request/context','request/usage','step/end','compaction/end'].includes(event.type))void this.publishUsage(id,event.type==='compaction/end').catch(()=>{});
    const inputId=this.store.get('active-input',id),r=inputId&&this.store.get('input',inputId);
    if(!r||TERMINAL.has(r.status))return;
    if(event.type==='user/message'&&event.data?.source?.rpcId===inputId)this.store.set('input',inputId,{...r,status:'running'});
    if(event.type==='assistant/message')this.store.set('input',inputId,{...r,output:textOf(event)});
    if(event.type==='turn/end'){
      void this.serial.run('settle:'+id,async()=>{
        await this.ctx.sessions.flush(session);await this.settle(id,inputId,event.data.reason);
      }).catch(()=>{});
    }
  }
  async settle(id,inputId,reason){
    const r=this.store.get('input',inputId);if(!r||TERMINAL.has(r.status))return r;
    const sent=this.store.all('send').some(s=>s.inputId===inputId&&s.status!=='failed');
    const result={...r,status:reason.kind==='completed'?'completed':reason.kind==='aborted'?'stopped':'failed',text:sent?'':r.output??'',usage:await this.usage(id),nativeCompaction:!!this.store.get('native-compaction',id),...(reason.kind==='completed'?{}:{error:JSON.stringify(reason)})};
    if(reason.kind==='completed'){const c=this.store.get('session',id),a=this.ctx.agents.get(c.remoteId);this.store.set('host','online-verification',{onlineVerified:true,verifiedAt:new Date().toISOString(),provider:a?.model.current()?.provider,model:a?.model.current()?.model,method:'completed-existing-dsh-turn'});}
    return this.store.set('input',inputId,result);
  }
  async reconcile(id,inputId){
    const c=this.store.get('session',id),r=this.store.get('input',inputId);if(!c||!r)return;
    // Read only the Session created by this installation; never list/search user Sessions.
    const records=[];let beforeSeq;
    for(let page=0;page<100;page++){
      const result=await this.ctx.sessionController.events({address:{kind:'session',sessionId:c.remoteId},maxMessages:100,...(beforeSeq===undefined?{}:{beforeSeq})},signal());
      records.unshift(...result.records);if(!result.hasMore||!result.records.length)break;
      beforeSeq=result.records[0].event.seq;if(beforeSeq<=r.cursor)break;
    }
    const events=records.map(x=>x.event).filter(e=>e.seq>=r.cursor);
    const user=events.find(e=>e.type==='user/message'&&e.data?.source?.rpcId===inputId);
    const inbox={'next-turn':[],'next-step':[]};
    for(const e of events)if(e.type==='agent/inbox/spliced'&&inbox[e.data.target])inbox[e.data.target].splice(e.data.start,e.data.removedCount??0,...(e.data.inserted??[]));
    const queued=Object.values(inbox).some(items=>items.some(m=>m.source?.rpcId===inputId));
    if(user){
      const after=events.filter(e=>e.seq>user.seq);const end=after.find(e=>e.type==='turn/end');
      if(end){const output=after.filter(e=>e.seq<end.seq&&e.type==='assistant/message').map(textOf).at(-1)??'';this.store.set('input',inputId,{...r,output});return this.settle(id,inputId,end.data.reason);}
      this.store.set('input',inputId,{...r,status:this.ctx.agents.get(c.remoteId)?.status==='running'?'running':'unknown',error:'Claimed input has no durable turn end; awaiting terminal evidence'});
    }else if(queued){
      this.store.set('input',inputId,{...r,status:'accepted',recoveryEvidence:{durablePendingInput:true,claimed:false}});
      // The durable inbox proves this exact input was never claimed. Activate the
      // owned Agent to consume its original queued message; never submit it again.
      await this.getAgent(id);
    }
    // Absence is not permission to replay after an ambiguous admission.
  }
  async input(id,inputId){
    const r=this.store.get('input',inputId);if(!r||r.sessionId!==id)return {status:'not_found'};
    if(r.status==='unknown')await this.reconcile(id,inputId).catch(()=>{});
    const latest=this.store.get('input',inputId);const {input,output,...status}=latest;return status;
  }
  async waitInput(id,inputId){
    for(let n=0;n<600;n++){
      const r=await this.input(id,inputId);if(TERMINAL.has(r.status)||['unknown','not_found'].includes(r.status))return r;
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    return {status:'running',inputId};
  }
  async state(id){
    const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');
    const s=await this.ctx.sessionController.state({sessionId:c.remoteId},signal());
    return {id,kind:c.kind,agentPreset:presetOf(c),status:s.running?'running':'idle',busy:s.running,inbox:s.queue?.length??0,pendingInput:s.pendingInput,remoteSessionId:c.remoteId};
  }
  async usage(id){
    const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');
    const agent=this.ctx.agents.get(c.remoteId),context=this.store.get('request-context',id);
    const measuredAt=new Date().toISOString();
    if(!agent||!this.ctx.tokenMeter?.measure)return {source:'unavailable',measuredAt};
    const m=this.ctx.tokenMeter.measure(agent.session);
    const selection=agent.model.current();
    const info=context?.contextWindow?undefined:(selection?await this.ctx.llm.resolveModelInfo(selection.provider,selection.model):undefined);
    return {usedTokens:m.totalTokens,contextWindow:context?.contextWindow??info?.context?.contextWindow??info?.contextWindow,source:'dsh',estimated:true,method:'existing DSH tokenMeter; current surface estimate, not cumulative billing',measuredAt,...(this.store.get('usage-event',id)?{usageSamples:[this.store.get('usage-event',id)]}:{})};
  }
  async publishUsage(id,compaction){
    const measured=await this.usage(id),{usageSamples,...usage}=measured;if(!usage.contextWindow)return;
    const old=this.store.get('usage-event',id);if(!old||usage.usedTokens>old.usedTokens)this.store.set('usage-event',id,usage);
    const peak=this.store.get('usage-event',id);
    if(!this.eventCallback)return;
    await this.eventCallback({type:'usage',sessionId:id,usage:peak});await this.eventCallback({type:'usage',sessionId:id,usage});
    if(compaction)await this.eventCallback({type:'native_compaction',sessionId:id,usage});
  }
  async cancel(id,{keepInbox=true,inputId}={}){
    if(typeof keepInbox!=='boolean')throw Error('INVALID_KEEP_INBOX');
    if(inputId===undefined){
      const agent=await this.getAgent(id);
      agent.cancel({kind:'user'},{keepInbox});
      await agent.whenIdle();
      await this.ctx.sessions.flush(agent.session);
      return this.state(id);
    }
    return this.serial.run('deliver:'+id,async()=>{
      const active=this.store.get('active-input',id),record=active&&this.store.get('input',active);
      if(active!==inputId||!record||TERMINAL.has(record.status))return this.state(id);
      const agent=await this.getAgent(id);
      agent.cancel({kind:'user'},{keepInbox});
      await agent.whenIdle();
      await this.ctx.sessions.flush(agent.session);
      // Finish the matching abort's durable event before admitting a new input.
      await this.serial.run('settle:'+id,async()=>{
        const latest=this.store.get('input',inputId);
        if(!TERMINAL.has(latest.status))await this.reconcile(id,inputId);
      });
      const state=await this.state(id);
      const latest=this.store.get('input',inputId);
      if(!keepInbox&&!state.busy&&!state.inbox&&!state.pendingInput&&!TERMINAL.has(latest.status))
        await this.settle(id,inputId,{kind:'aborted'});
      return state;
    });
  }
  async quarantine(id,inputId,{stop=false}={}){
    if(stop)await this.cancel(id,{keepInbox:false});const r=await this.input(id,inputId),s=await this.state(id);
    if(TERMINAL.has(r.status)||r.status==='not_found')return {...r,safeToRelease:!s.busy&&s.inbox===0};
    return {...r,status:'unknown',safeToRelease:false,safeToContinue:false,evidence:{idle:!s.busy,inbox:s.inbox,reason:'No durable terminal result proven; effects not replayed'}};
  }
  async releaseSession(id){
    const s=await this.state(id);const used=this.store.all('input').some(r=>r.sessionId===id);
    if(s.busy||s.inbox||used)return {status:'blocked',safeToRelease:false};
    this.store.set('session-release',id,true);return {status:'released',safeToRelease:true};
  }
  async contextInventory(id){
    const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');
    const text=configuredSystemText(c),agent=this.ctx.agents.get(c.remoteId);
    const nativeTools=agent?.session.requestHeader?.()?.tools?.map(t=>t.name)??[];
    return {sessionId:id,remoteSessionId:c.remoteId,promptSections:c.promptSections,fragments:c.contextFragments,
      policy:c.promptPolicy===undefined?'Legacy complete Core prompt; existing session policy retained':'Version 1: standard native sections with explicit overrides, or minimal single complete persona; tools and runtime facts remain host-owned',
      promptEvidence:{configuredSystemSha256:createHash('sha256').update(text).digest('hex'),configuredSystemChars:text.length,
        effectiveSystemSha256:this.store.get('prompt-assembly',id)?.effectiveSystemSha256??createHash('sha256').update(text).digest('hex'),
        configuredAgentPreset:presetOf(c),nativeAgentPreset:agent&&this.ctx.agentPresets?.composedPreset(agent.ctx)||null,
        nativeSessionAgentPreset:agent?.session.header?.agentPreset??null,
        frozenPromptVersions:Object.fromEntries((c.promptSections??[]).map(section=>[section.id,section.version])),
        toolAuthorization:{source:'Core session toolScopes',inheritedNativeTools:false},
        observedSystem:this.store.get('prompt-system-observed',id)??null,assembly:this.store.get('prompt-assembly',id)??null,
        nativeRequestToolNames:nativeTools,configuredNativeCompactionVersions:c.nativeCompaction?.versions??null,observedCompaction:this.store.get('compaction-prompt-observed',id)??null,nativeAuxiliary:this.store.get('native-aux-prompt-observed',id)??null}};
  }
  async history(id){const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');return this.ctx.sessionController.events({address:{kind:'session',sessionId:c.remoteId},maxMessages:100},signal());}
  async surface(id){const c=this.store.get('session',id);if(!c)throw Error('SESSION_NOT_OWNED_BY_CORE');return this.ctx.sessionController.projections({sessionId:c.remoteId},signal());}
  async pluginInventory(){return {host:'dsh',connectionMode:'existing-dsh',plugins:[{id:'xixi-existing-dsh-bridge',name:'既有 DSH 接线',enabled:true,loaded:true,tools:Object.keys(definitions)}],failures:[]};}
  async updatePlugin(){throw Error('EXISTING_DSH_PLUGIN_CONFIGURATION_IS_ADMIN_MANAGED');}
  async modelCatalog(){
    try {
      const catalog=safeModelCatalog(await this.ctx.sessionController.modelCatalog());
      const roster=await this.ctx.agentPresets.list();
      if(!Array.isArray(roster))throw Error('INVALID_PRESET_ROSTER');
      return {...catalog,agentPresets:['standard','minimal'].map(id=>{
        const row=roster.find(p=>p?.id===id);return {id,available:Boolean(row&&row.broken===undefined)};
      })};
    } catch(error) {
      if(error?.code==='NATIVE_MODEL_CATALOG_SCHEMA_INVALID')throw error;
      throw Object.assign(Error('The existing DSH model catalog is unavailable.'),{code:'DSH_MODEL_CATALOG_UNAVAILABLE'});
    }
  }
  async promptPolicyCapabilities(){
    const capabilities=safePromptPolicyCapabilities(this.nativePromptRenderCapabilities);
    if(this.compactionPresentationApi===1)capabilities.hostCapabilities.compaction=1;
    return snapshot(capabilities);
  }
  async modelConnectionHealth(){
    const defaultModel=this.ctx.agentDefaultModel?.currentSelection?.();
    const verification=this.store.get('host','online-verification')??{onlineVerified:false};
    const connection={source:'existing-dsh',configured:true,defaultModel,connectionMode:'existing-dsh',endpoint:'loopback',version:this.dshVersion,
      ...verification,credentialSource:'existing-dsh-owned',meaning:'configured describes the existing host connection; onlineVerified requires an observed completed model turn'};
    if(this.validationModel)return {...connection,validationModel:this.validationModel,onlineVerified:false,
      syntheticTurnObserved:verification.onlineVerified,credentialSource:'synthetic-fixture',meaning:'isolated DSH against a loopback synthetic model only'};
    return connection;
  }
  async health(){const modelConnection=await this.modelConnectionHealth();return {ok:!this.closing,name:'XixiBridge',connectionMode:'existing-dsh',endpoint:'loopback',dshVersion:this.dshVersion,defaultModel:modelConnection.defaultModel,architecture:{runtimeOwner:'existing-dsh',bridgeHostedBy:'dsh',coreHoldsModelCredentials:false,promptPolicy:'versioned-legacy-or-native-per-mode',nativePromptRenderApi:this.nativePromptRenderCapabilities,toolAuthorization:'Core scopes; inherited native tools masked',compactionPresentationApi:this.compactionPresentationApi},onlineVerified:modelConnection.onlineVerified,modelConnection,capabilities:['create','resume','followup','cancel-keepInbox','current-pressure','native-compaction','Core-owned-tools','tasks','prompt-policy-capabilities']};}
  maintenance(r){return this.operations.maintenance(r);}
  task(r){return this.operations.task(r);}
  taskStatus(id){return this.operations.taskStatus(id);}
  cancelTask(id){return this.operations.cancelTask(id);}
  async maintenanceStatus(id,attempt=0){
    const sid=attempt?`maint-${id}-attempt-${attempt}`:`maint-${id}`;
    if(!this.store.get('session',sid))return {status:'idle',safeToRelease:true,evidence:{notCreated:true}};
    const r=await this.input(sid,`${sid}:request`),s=await this.state(sid);return {...r,safeToRelease:TERMINAL.has(r.status)&&!s.busy&&s.inbox===0};
  }
  async stopMaintenance(id,attempt=0){const sid=attempt?`maint-${id}-attempt-${attempt}`:`maint-${id}`;if(!this.store.get('session',sid))return this.maintenanceStatus(id,attempt);this.store.set('session-maintenance-stop',sid,true);const outcome=await this.quarantine(sid,`${sid}:request`,{stop:true});this.store.set('maintenance-control',`maintenance:${id}:attempt:${attempt}`,outcome);return outcome;}
  async close(){
    this.closing=true;this.offCreated?.();this.offCompactionPrompt?.();this.offNativeAuxPrompt?.();
    // Stop only Core-owned live Agents before lifting their scoped restrictions.
    // Never leave a running Core Agent to fall through to the host global tools.
    for(const [id,h] of this.handles){
      if(h.agent.status!=='idle'){h.agent.cancel({kind:'user'},{keepInbox:false});await h.agent.whenIdle();await this.ctx.sessions.flush(h.agent.session);}
      const inputId=this.store.get('active-input',id),r=inputId&&this.store.get('input',inputId);
      if(r&&!TERMINAL.has(r.status))await this.reconcile(id,inputId).catch(()=>{});
    }
    this.offEvent?.();
    for(const h of this.handles.values())for(const dispose of [...h.platformDisposers,...h.disposers])dispose();
    this.handles.clear();this.store.close();await this.lease.release();
    // The host DSH Context, agents, model adapters and login remain owned by DSH.
  }
}
