import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {ExistingDshBridge} from '../src/existing-dsh.mjs';
import {definitions} from '../src/tool-definitions.mjs';
import {createBridgeServer} from '../src/server.mjs';
import {inject as hostInject,loadNativePromptRenderCapabilities} from '../src/existing-dsh-plugin.mjs';
import {readFileSync} from 'node:fs';

function fakeHost(){
  const agents=new Map(),listeners=new Set(),createdListeners=new Set(),history=new Map(),otherListeners=new Map();
  const calls={createdRequests:[],create:0,prompt:0,selectGlobal:0,resume:[],callbacks:[],failAdmission:false,toolsByAgent:new Map(),unrelatedToolUntouched:true};
  const emit=(agent,type,data)=>{const event={type,data,seq:agent.session.seq++,time:Date.now()};history.get(agent.id).push({type:'event',event});for(const listener of listeners)listener(agent.session,event);};
  const ctx={
    on(name,listener){if(name==='session/event')listeners.add(listener);else if(name==='agent/created')createdListeners.add(listener);else {const entries=otherListeners.get(name)??new Set();entries.add(listener);otherListeners.set(name,entries);}return ()=>{listeners.delete(listener);createdListeners.delete(listener);otherListeners.get(name)?.delete(listener);};},
    bail(name,payload){for(const listener of otherListeners.get(name)??[]){const result=listener(payload);if(result!==undefined)return result;}},
    agents:{get:id=>agents.get(id)},sessions:{async flush(){}},
    agentDefaultModel:{currentSelection(){return {provider:'native-provider',model:'native-default'};}},
    agentPresets:{async list(){return [{id:'standard'},{id:'minimal'},{id:'ptc',broken:'SYNTHETIC_SECRET'}];},composedPreset(ctx){return ctx._preset;}},
    llm:{async resolveCallConfig(c){return c;},async resolveModelInfo(){return {contextWindow:8192};}},
    tools:{},systemPrompt:{},tokenMeter:{measure(){return {totalTokens:256};}},
    sessionController:{
      async create(request){const {sessionId}=request;calls.createdRequests.push({...request});calls.create++;history.set(sessionId,[]);let current={provider:'native-provider',model:'native-default'};
        const tools=new Map();calls.toolsByAgent.set(sessionId,tools);
        const agent={id:sessionId,session:{id:sessionId,seq:0,header:{agentPreset:request.agentPreset??'minimal'}},options:{},status:'idle',inbox:{nextTurn:[],nextStep:[]},
          model:{current:()=>current,select:v=>{current=v;}},
          ctx:{_preset:request.agentPreset??'minimal',on(){return ()=>{};},systemPrompt:{section(v){agent.system=v;return ()=>{};},suppressRuntimeContext(){agent.runtimeContextSuppressed=true;return ()=>{};}},tools:{restrict(v){agent.restriction=v;return ()=>{};},register(v){tools.set(v.name,v);return ()=>tools.delete(v.name);}}},
          cancel(_cause,options){agent.cancelOptions=options;agent.status='idle';emit(agent,'turn/end',{turn:1,reason:{kind:'aborted'}});},async whenIdle(){}
        };agents.set(sessionId,agent);for(const f of createdListeners)await f({agent});return {sessionId};},
      async resolveAgent(id){calls.resume.push(id);const agent=agents.get(id);return agent?{agent}:{error:Error('NOT_FOUND')};},
      async prompt(r){calls.prompt++;if(calls.failAdmission)throw Error('transport lost');const a=agents.get(r.sessionId);a.status='running';
        emit(a,'agent/inbox/spliced',{target:'next-turn',start:0,inserted:[{source:{rpcId:r.requestId}}]});
        emit(a,'user/message',{source:{rpcId:r.requestId},content:r.content});return {accepted:true};},
      async selectModel(){calls.selectGlobal++;throw Error('global default must not change');},
      async state({sessionId}){const a=agents.get(sessionId);if(!a)throw Error('NOT_FOUND');return {running:a.status==='running',queue:[],pendingInput:[]};},
      async events({address,beforeSeq}){const records=history.get(address.sessionId)??[];return {records:records.filter(r=>beforeSeq===undefined||r.event.seq<beforeSeq),hasMore:false};},
      async modelCatalog(){return {default:{provider:'native-provider',model:'native-default'},routableProviders:['native-provider'],groups:[{id:'native-provider',name:'Native',apiKey:'SYNTHETIC_SECRET',models:[{id:'native-default',name:'Default'}]}],failures:[]};},
    }
  };
  return {ctx,calls,agents,history,emit,finish(id,text='answer',reason={kind:'completed'}){const a=agents.get(id);emit(a,'assistant/message',{content:[{type:'text',text}]});a.status='idle';emit(a,'turn/end',{turn:1,reason});}};
}
async function fixture(t,options={}){
  const root=await mkdtemp(join(tmpdir(),'xixi-existing-'));const host=fakeHost();
  const b=await ExistingDshBridge.open(host.ctx,{root,defineTool:v=>v,acquireLease:async root=>({root,release:async()=>{}}),callback:async body=>{host.calls.callbacks.push(body);return {ok:true,data:{status:'accepted'}};},...options});
  t.after(async()=>{if(!b.closing)await b.close();await rm(root,{recursive:true,force:true});});return {b,root,...host};
}
const config=(id='daily')=>({id,kind:'daily',model:'chosen-model',libraryMode:'external',toolScopes:['send_message','task_create','library_search'],promptSections:[{id:'role',placement:'system',version:1,text:'role literal {{x}}'},{id:'start',placement:'start',version:1,text:'initial summary'}]});
const tick=()=>new Promise(resolve=>setTimeout(resolve,10));

test('loading plugin does not enumerate or resume any existing DSH session',async t=>{const f=await fixture(t);assert.equal(f.calls.create,0);assert.deepEqual(f.calls.resume,[]);assert.equal((await f.b.health()).connectionMode,'existing-dsh');assert.equal((await f.b.health()).onlineVerified,false);});
test('creates dedicated stable host session and changes only its local model',async t=>{const f=await fixture(t);const a=await f.b.session(config());await f.b.session(config());assert.equal(f.calls.create,1);assert.equal(f.calls.selectGlobal,0);assert.match(a.remoteSessionId,/^xixi-/);const agent=f.agents.get(a.remoteSessionId);assert.equal(agent.model.current().model,'chosen-model');assert.deepEqual(agent.restriction,{allow:[]});assert.equal(agent.system.complete,true);assert.match(agent.system.text,/role literal/);assert.deepEqual([...f.calls.toolsByAgent.get(agent.id).keys()],['send_message','task_create','library_search']);});
test('Core pins standard even when host new-task default changes and provider is explicit per purpose',async t=>{
 const f=await fixture(t),c={...config('other-provider'),modelOptions:{provider:'other-provider',reasoningEffort:'high'}};
 const created=await f.b.session(c),agent=f.agents.get(created.remoteSessionId);
 assert.equal(f.calls.createdRequests[0].agentPreset,'standard');
 assert.deepEqual(agent.model.current(),{provider:'other-provider',model:'chosen-model',reasoningEffort:'high'});
 assert.equal(f.calls.selectGlobal,0);
});
test('different Core installations do not reuse the same host session id',async t=>{const a=await fixture(t),b=await fixture(t);assert.notEqual((await a.b.session(config())).remoteSessionId,(await b.b.session(config())).remoteSessionId);});
test('session id cannot grant access to arbitrary DSH session',async t=>{const f=await fixture(t);await assert.rejects(f.b.state('human-session'),/OWNED/);await assert.rejects(f.b.getAgent('human-session'),/OWNED/);assert.deepEqual(f.calls.resume,[]);});
test('accepted is separate from turn completion and repeated input is not replayed',async t=>{const f=await fixture(t);const s=await f.b.session(config());const input={inputId:'msg1',text:'hello'};assert.equal((await f.b.deliver('daily',input)).status,'running');assert.equal((await f.b.input('daily','msg1')).status,'running');await f.b.deliver('daily',input);assert.equal(f.calls.prompt,1);f.finish(s.remoteSessionId);await tick();const result=await f.b.input('daily','msg1');assert.equal(result.status,'completed');assert.equal(result.text,'answer');assert.equal(result.usage.usedTokens,256);});
test('changed content on the same request id fails before another model turn',async t=>{const f=await fixture(t);await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'one'});await assert.rejects(f.b.deliver('daily',{inputId:'m',text:'two'}),/CONFLICT/);assert.equal(f.calls.prompt,1);});
test('uncertain admission is durable unknown and never automatically replayed',async t=>{const f=await fixture(t);await f.b.session(config());f.calls.failAdmission=true;await assert.rejects(f.b.deliver('daily',{inputId:'m',text:'one'}));assert.equal((await f.b.input('daily','m')).status,'unknown');await f.b.deliver('daily',{inputId:'m',text:'one'});assert.equal(f.calls.prompt,1);});
test('Core tool callbacks keep Core ids and task_create does not create an internal subagent',async t=>{const f=await fixture(t);const s=await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'do work'});const tool=f.calls.toolsByAgent.get(s.remoteSessionId).get('task_create');await tool.execute({title:'t',objective:'o'},{callId:'c1',signal:AbortSignal.timeout(1000)});assert.equal(f.calls.create,1);assert.equal(f.calls.callbacks[0].sessionId,'daily');assert.equal(f.calls.callbacks[0].inputId,'m');assert.equal(f.calls.callbacks[0].tool,'task_create');});
test('Platform task tools preserve the daily origin and separate work-side artifact writes',async t=>{
  const f=await fixture(t);
  const daily=await f.b.session({...config(),platformLibraryTools:true,toolScopes:[]});
  const work=await f.b.session({...config('work'),kind:'work',platformLibraryTools:true,toolScopes:[]});
  const origin={channel:'fixture',peer:'synthetic-owner'};
  await assert.rejects(f.b.deliver('daily',{inputId:'invalid',text:'owner',assistantTools:['task_create']}),/INVALID_PLATFORM_ASSISTANT_ORIGIN/);
  await assert.rejects(f.b.deliver('work',{inputId:'invalid',text:'work',assistantTools:['task_create'],assistantOrigin:origin}),/INVALID_PLATFORM_ASSISTANT_TOOLS/);
  await f.b.deliver('daily',{inputId:'owner-turn',text:'owner',assistantTools:['task_create','task_result_read'],assistantOrigin:origin});
  const dailyTools=f.calls.toolsByAgent.get(daily.remoteSessionId);
  const exec={callId:'synthetic-tool',signal:AbortSignal.timeout(1000)};
  await dailyTools.get('task_create').execute({objective:'isolated work'},exec);
  assert.deepEqual(f.calls.callbacks[0],{sessionId:'daily',sessionKind:'daily',inputId:'owner-turn',tool:'task_create',
    args:{objective:'isolated work'},requestId:'daily:synthetic-tool',origin});
  await f.b.deliver('work',{inputId:'work-turn',text:'work',assistantTools:['task_artifact_write'],libraryTools:['library_search']});
  const workTools=f.calls.toolsByAgent.get(work.remoteSessionId);
  assert.deepEqual([...workTools.keys()],['library_search','task_artifact_write']);
  await workTools.get('task_artifact_write').execute({name:'artifact',text:'payload'},exec);
  assert.equal(f.calls.callbacks[1].sessionKind,'work');
  assert.equal(f.calls.callbacks[1].origin,undefined);
  f.finish(daily.remoteSessionId);await tick();
  await f.b.deliver('daily',{inputId:'system-event',text:'result',assistantTools:['task_result_read']});
  assert.deepEqual([...dailyTools.keys()],['task_result_read']);
});
test('accepted or unknown send suppresses duplicate final reply',async t=>{const f=await fixture(t);const s=await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'hello'});await f.calls.toolsByAgent.get(s.remoteSessionId).get('send_message').execute({text:'sent'},{callId:'c',signal:AbortSignal.timeout(1000)});f.finish(s.remoteSessionId,'duplicate');await tick();assert.equal((await f.b.input('daily','m')).text,'');});
test('failed DSH turn is never reported completed',async t=>{const f=await fixture(t);const s=await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'hello'});f.finish(s.remoteSessionId,'partial',{kind:'error',error:{message:'fixture failure'}});await tick();assert.equal((await f.b.input('daily','m')).status,'failed');});
test('daily cancellation preserves inbox as requested and does not stop host',async t=>{const f=await fixture(t);const s=await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'hello'});await f.b.cancel('daily',{keepInbox:true});assert.deepEqual(f.agents.get(s.remoteSessionId).cancelOptions,{keepInbox:true});assert.equal(f.b.closing,undefined);});
test('native usage uses current surface and calls Core event independently',async t=>{const events=[];const f=await fixture(t,{eventCallback:async e=>events.push(e)});const s=await f.b.session(config());f.emit(f.agents.get(s.remoteSessionId),'request/context',{contextWindow:8192});await tick();assert.ok(events.some(e=>e.type==='usage'&&e.sessionId==='daily'&&e.usage.contextWindow===8192));});
test('new Library schema requires query and categories and declares separate raw-chat/SQL tools',()=>{assert.equal(definitions.memory_search.parameters.categories.required,true);assert.equal(definitions.library_search.parameters.categories.required,true);assert.ok(definitions.library_chat_history);assert.ok(definitions.library_sql);assert.ok(definitions.library_record.parameters.summary);});
test('HTTP existing-host health requires machine authentication and cannot claim online verified',async t=>{const f=await fixture(t);const server=createBridgeServer(f.b,{token:'a'.repeat(30)});await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));const url=`http://127.0.0.1:${server.address().port}/health`;assert.equal((await fetch(url)).status,401);const body=await (await fetch(url,{headers:{Authorization:'Bearer '+'a'.repeat(30)}})).json();assert.equal(body.connectionMode,'existing-dsh');assert.equal(body.onlineVerified,false);assert.equal(body.modelConnection.credentialSource,'existing-dsh-owned');});

test('host agent/created binds Core tool scopes before any resumed work is released',async t=>{const f=await fixture(t);const s=await f.b.session(config());const a=f.agents.get(s.remoteSessionId);assert.equal(a.system.complete,true);assert.ok(f.calls.toolsByAgent.get(s.remoteSessionId).has('task_create'));assert.equal(f.calls.selectGlobal,0);});
test('health exposes only safe host default selection metadata before real acceptance',async t=>{const f=await fixture(t);const h=await f.b.health();assert.equal(h.defaultModel.model,'native-default');assert.equal(h.modelConnection.source,'existing-dsh');assert.equal(h.modelConnection.onlineVerified,false);assert.equal(h.architecture.coreHoldsModelCredentials,false);});
test('successful model turn records online acceptance, separately from loading the plugin',async t=>{const f=await fixture(t);const s=await f.b.session(config());await f.b.deliver('daily',{inputId:'m',text:'hello'});f.finish(s.remoteSessionId);await tick();assert.equal((await f.b.health()).modelConnection.onlineVerified,true);assert.equal((await f.b.health()).modelConnection.model,'chosen-model');});

test('restart reconciliation reads only the owned Session and proves completed input without another prompt',async t=>{
 const f=await fixture(t);const c=config();const a=await f.b.session(c);await f.b.deliver('daily',{inputId:'m',text:'one'});f.finish(a.remoteSessionId,'persisted');await tick();
 // Simulate the small crash window after the host commits but before adapter checkpoint.
 const saved=f.b.store.get('input','m');f.b.store.set('input','m',{...saved,status:'unknown'});const result=await f.b.input('daily','m');assert.equal(result.status,'completed');assert.equal(result.text,'persisted');assert.equal(f.calls.prompt,1);
});

test('host entry declares every root Context service read by the existing adapter',()=>{
 const source=readFileSync(new URL('../src/existing-dsh.mjs',import.meta.url),'utf8');
 const builtins=new Set(['on','effect','fiber','logger']);
 const names=new Set([...source.matchAll(/this\.ctx\.([A-Za-z][A-Za-z0-9]*)/g)].map(m=>m[1]));
 for(const name of names)if(!builtins.has(name))assert.ok(hostInject.includes(name),`Actual Cordis host requires inject for ${name}`);
 assert.ok(hostInject.includes('agentDefaultModel'),'health default model selection requires injection');
 assert.ok(hostInject.includes('tokenMeter'),'usage measurement requires injection');
});


test('minimal is selected per Core session and reflected by native header and live preset; old omitted spec stays compatible',async t=>{
 const f=await fixture(t),c={...config('minimal-owned'),agentPreset:'minimal',modelOptions:{agentPreset:'minimal'}};
 const first=await f.b.session(c);await f.b.session(c);
 assert.equal(f.calls.create,1);assert.equal(f.calls.createdRequests[0].agentPreset,'minimal');
 const proof=(await f.b.contextInventory(c.id)).promptEvidence;
 assert.equal(proof.configuredAgentPreset,'minimal');assert.equal(proof.nativeAgentPreset,'minimal');assert.equal(proof.nativeSessionAgentPreset,'minimal');
 assert.equal(proof.toolAuthorization.inheritedNativeTools,false);
 await f.b.session(config('legacy-omitted'));
 assert.equal(f.b.store.get('session','legacy-omitted').agentPreset,undefined);
 await f.b.session(config('legacy-omitted'));assert.equal(f.calls.create,2);
});
test('unsupported or conflicting Core preset is rejected before native session creation',async t=>{
 const f=await fixture(t);
 await assert.rejects(f.b.session({...config(),agentPreset:'ptc'}),/INVALID_CORE_AGENT_PRESET/);
 await assert.rejects(f.b.session({...config(),agentPreset:'minimal',modelOptions:{agentPreset:'standard'}}),/AGENT_PRESET_CONFLICT/);
 assert.equal(f.calls.create,0);
});
test('read-only native model catalog requires machine auth and strips raw metadata',async t=>{
 const f=await fixture(t);const server=createBridgeServer(f.b,{token:'m'.repeat(30)});await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));
 const url=`http://127.0.0.1:${server.address().port}/models`;
 assert.equal((await fetch(url)).status,401);
 const response=await fetch(url,{headers:{Authorization:'Bearer '+'m'.repeat(30)}}),value=await response.json();
 assert.equal(response.status,200);assert.equal(value.default.provider,'native-provider');assert.equal(value.groups.length,1);assert.ok(!JSON.stringify(value).includes('SYNTHETIC_SECRET'));
 assert.deepEqual(value.agentPresets,[{id:'standard',available:true},{id:'minimal',available:true}]);assert.equal(f.calls.selectGlobal,0);assert.equal(f.calls.create,0);
});


test('explicit v1 prompt policy is normalized before storing and does not migrate legacy omitted session specs',async t=>{
 const f=await fixture(t),legacy=config('legacy-policy');await f.b.session(legacy);
 assert.equal(Object.hasOwn(f.b.store.get('session','legacy-policy'),'promptPolicy'),false);
 const request={...config('new-policy'),promptPolicy:{version:1}};await f.b.session(request);await f.b.session(request);
 const saved=f.b.store.get('session','new-policy');assert.deepEqual(saved.promptPolicy.standard.overrides,[]);assert.equal(saved.promptPolicy.runtime.time.mode,'inherit');assert.deepEqual(request.promptPolicy,{version:1});
 assert.equal(f.agents.get(saved.remoteId).runtimeContextSuppressed,undefined);assert.deepEqual(f.agents.get(saved.remoteId).restriction,{allow:[]});assert.equal(f.calls.create,2);
});
test('missing native render API and invalid explicit policy fail before host session creation',async t=>{
 const f=await fixture(t);
 await assert.rejects(f.b.session({...config('bad-policy'),promptPolicy:null}),/INVALID_CORE_PROMPT_POLICY/);
 await assert.rejects(f.b.session({...config('missing-hook'),promptPolicy:{version:1,runtime:{time:{mode:'template',text:'literal time'}}}}),/HOST_NATIVE_PROMPT_RENDER_PATCH_REQUIRED/);
 assert.equal(f.calls.create,0);
});
test('safe prompt-policy capability endpoint is authenticated, source-derived and never exposes stored session content',async t=>{
 const f=await fixture(t,{nativePromptRenderCapabilities:{time:1,agentInstructions:1,planMode:99,runtimeContext:0,privateValue:'SHOULD_NOT_LEAK'}});
 await f.b.session({...config(),promptSections:[{id:'private-role',placement:'system',version:1,text:'PRIVATE_ROLE_SENTINEL'}]});
 const server=createBridgeServer(f.b,{token:'p'.repeat(30)});await new Promise(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise(r=>server.close(r)));
 const url=`http://127.0.0.1:${server.address().port}/prompt-policy/capabilities`;
 assert.equal((await fetch(url)).status,401);const response=await fetch(url,{headers:{Authorization:'Bearer '+'p'.repeat(30)}}),body=await response.json();
 assert.equal(response.status,200);assert.deepEqual(body.hostCapabilities,{time:1,agentInstructions:1,planMode:0,runtimeContext:0,modelSelection:0,workingDirectory:0,sessionTitle:0});assert.ok(body.catalog.agentInstructions.parts.fileHeader);
 assert.ok(!JSON.stringify(body).includes('PRIVATE_ROLE_SENTINEL'));assert.ok(!JSON.stringify(body).includes('SHOULD_NOT_LEAK'));assert.equal(f.calls.create,1);
 assert.equal((await f.b.health()).architecture.promptPolicy,'versioned-legacy-or-native-per-mode');
});
test('native render capability loader resolves seven actual module exports and cannot be set by user policy',async t=>{
 const root=await mkdtemp(join(tmpdir(),'xixi-native-api-'));t.after(()=>rm(root,{recursive:true,force:true}));
 const names=['@deepseek-ai/dsh-time-context','@deepseek-ai/dsh-agent-instructions','@deepseek-ai/dsh-plan-mode','@deepseek-ai/dsh-agent-loop','@deepseek-ai/dsh-agent','@deepseek-ai/dsh-working-directory','@deepseek-ai/dsh-session-title-first-prompt-llm'],files=new Map();
 for(const [index,name] of names.entries()){const path=join(root,index+'.mjs');await writeFile(path,index===0?'export const NATIVE_PROMPT_RENDER_API=1;':index===1?'export const NATIVE_PROMPT_RENDER_API=2;':index===6?'export const NATIVE_AUX_PROMPT_RENDER_API=1;':'export const unrelated=true;');files.set(name,path);}
 const seen=[];const flags=await loadNativePromptRenderCapabilities({resolve(name){seen.push(name);assert.ok(files.has(name));return files.get(name);}});
 assert.deepEqual(seen,names);assert.deepEqual(flags,{time:1,agentInstructions:0,planMode:0,runtimeContext:0,modelSelection:0,workingDirectory:0,sessionTitle:1});
});


test('real host capability flags are passed through to v1 installation before binding an owned Agent',async t=>{
 const f=await fixture(t,{nativePromptRenderCapabilities:{time:1,agentInstructions:1,planMode:1,runtimeContext:1}});
 const c={...config('render-capability-owned'),promptPolicy:{version:1,runtime:{time:{mode:'template',text:'Synthetic configured time'}}}};
 const result=await f.b.session(c);assert.equal(f.calls.create,1);assert.equal(f.b.store.get('session',c.id).promptPolicy.runtime.time.mode,'template');assert.deepEqual(f.agents.get(result.remoteSessionId).restriction,{allow:[]});
});


test('title auxiliary hook uses durable Core ownership and frozen policy even after live Agent unload',async t=>{
 const f=await fixture(t,{nativePromptRenderCapabilities:{sessionTitle:1}});
 const request={...config('owned-title-worker'),promptPolicy:{version:1,runtime:{sessionTitle:{mode:'template',templates:{system:{text:'FROZEN {{target_words}}',templateSyntax:'core'},inputIntro:{text:'FROZEN INTRO'}}}}}};
 const owned=await f.b.session(request);
 request.promptPolicy.runtime.sessionTitle.templates.system.text='FUTURE ONLY';
 f.agents.delete(owned.remoteSessionId); // Worker is late; no live Agent lookup is allowed.
 const rendered=f.ctx.bail('native-aux-prompt/render',{kind:'sessionTitle',sessionId:owned.remoteSessionId,system:'native system',inputIntro:'native intro',variables:{target_words:5,target_cjk_characters:10}});
 assert.deepEqual(rendered,{system:'FROZEN 5',inputIntro:'FROZEN INTRO'});
 assert.equal(f.ctx.bail('native-aux-prompt/render',{kind:'sessionTitle',sessionId:'ordinary-native-session',variables:{target_words:5}}),undefined);
 const proof=f.b.store.get('native-aux-prompt-observed','owned-title-worker');assert.equal(proof.source,'owned-frozen-session-policy');assert.deepEqual(Object.keys(proof.partSha256),['system','inputIntro']);assert.ok(!JSON.stringify(proof).includes('FROZEN INTRO'));
 await f.b.session({...config('legacy-title'),promptPolicy:undefined});const legacy=f.b.store.get('session','legacy-title');
 assert.equal(f.ctx.bail('native-aux-prompt/render',{kind:'sessionTitle',sessionId:legacy.remoteId,variables:{}}),undefined);
});
