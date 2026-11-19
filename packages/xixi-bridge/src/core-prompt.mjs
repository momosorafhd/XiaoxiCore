import {installCorePrompt as installLegacyPrompt} from './legacy-core-prompt.mjs';
import {createHash} from 'node:crypto';
import {TOOL_GUIDANCE,isToolProtocolSection,normalizePromptPolicy,overrideStandardSections,renderNativeSections,transformRuntimeMessages,renderPresentationTemplate,RUNTIME_PRESENTATION_PRODUCERS} from './prompt-policy.mjs';
/** Native standard sections are inherited unless explicitly overridden.
 * Minimal uses the native single-complete persona slot. Tools remain host-owned.
 */
export function configuredSystemText(config) {
  const sections = config.promptSections;
  const configured = sections === undefined
    ? [config.roleCard ?? '', config.rules ?? ''].filter(Boolean).join('\n\n')
    : sections.filter(s => s.placement === 'system').map(s => s.text).filter(Boolean).join('\n\n');
  return [configured, (config.contextFragments ?? []).filter(f => f.placement === 'system').map(f => f.text).join('\n\n'), config.outputProtocol ?? '']
    .filter(Boolean).join('\n\n');
}

const digest=text=>createHash('sha256').update(text).digest('hex');
const fail=(code,detail='')=>{throw Object.assign(Error(code+(detail?': '+detail:'')),{code});};
function activeToolNames(context,config){
  const view=context.toolSet?.view?.(context.agent);
  return new Set(view?.keys?.()??config.toolScopes??[]);
}
function assertMinimalTools(assembly,names){
  for(const section of assembly.sections??[]){
    if(!section.text?.trim())continue;
    if(['tools:ptc-only','tools:sdk'].includes(section.name))fail('CORE_UNSUPPORTED_TOOL_EXECUTOR',section.name);
    const needs=TOOL_GUIDANCE.get(section.name);
    if(needs?.some(name=>names.has(name)))fail('CORE_MINIMAL_ACTIVE_NATIVE_TOOL_GUIDANCE',section.name);
    if(!needs&&isToolProtocolSection(section.name))fail('CORE_UNSUPPORTED_TOOL_GUIDANCE',section.name);
  }
}

export function installCorePrompt(scoped,agent,config,observed=()=>{},hostCapabilities={}){
  if(config.promptPolicy===undefined)return installLegacyPrompt(scoped,agent,config,observed);
  if(typeof scoped.on!=='function'||typeof scoped.systemPrompt?.section!=='function')fail('EXISTING_DSH_PROMPT_POLICY_UNAVAILABLE');
  const policy=normalizePromptPolicy(config.promptPolicy);
  const preset=config.agentPreset??config.modelOptions?.agentPreset??'standard';
  if(!['standard','minimal'].includes(preset))fail('INVALID_CORE_AGENT_PRESET');
  const modes=(config.promptSections??[]).filter(s=>['mode.standard','mode.minimal'].includes(s.id)).map(s=>s.id);
  if(new Set(modes).size>1)fail('CORE_PROMPT_MODE_CONFLICT');
  const configured=configuredSystemText(config),disposers=[];
  for(const producer of RUNTIME_PRESENTATION_PRODUCERS)if(policy.runtime[producer].mode==='template'&&hostCapabilities[producer]!==1)fail('HOST_NATIVE_PROMPT_RENDER_PATCH_REQUIRED',producer);
  if(preset==='standard'){
    if(configured)disposers.push(scoped.systemPrompt.section({name:'xixi:core:configured',order:400,interpolate:false,text:configured}));
  }else if(policy.minimal.mode==='override'){
    const custom=policy.minimal;
    if(custom.templateSyntax==='dsh'){
      if(typeof scoped.systemPrompt.variable!=='function')fail('EXISTING_DSH_PROMPT_VARIABLE_UNAVAILABLE');
      disposers.push(scoped.systemPrompt.variable('xixi_core_system',()=>configured));
    }
    const text=custom.templateSyntax==='dsh'
      ?[custom.text,configured?'{{xixi_core_system}}':''].filter(Boolean).join('\n\n')
      :[custom.text,configured].filter(Boolean).join('\n\n');
    // SAME native slot: replaces ancestor complete rather than adding another.
    disposers.push(scoped.systemPrompt.section({name:'deployment:persona-prefix',order:0,complete:true,
      interpolate:custom.templateSyntax==='dsh',text}));
  }else if(configured){
    // Native complete is restored after the presentation waterfall. Fail rather
    // than silently dropping system-placement Core text or copying a native default.
    fail('CORE_MINIMAL_INHERIT_HAS_SYSTEM_TEXT');
  }
  disposers.push(scoped.on('system-prompt/assemble',async(_assembly,context,next)=>{
    if(context.agent!==agent)return next();
    try{
      const assembly=await next();
      let sections=assembly.sections;
      if(preset==='standard')sections=overrideStandardSections(sections,policy.standard.overrides);
      else assertMinimalTools(assembly,activeToolNames(context,config));
      const effectiveSections=preset==='minimal'?sections.filter(s=>s.name==='deployment:persona-prefix'):sections;
      const effective=renderNativeSections(effectiveSections,assembly.variables);
      observed({promptPolicyVersion:1,agentPreset:preset,selectedCoreModeSection:modes[0]??null,
        presentationMode:preset==='standard'?'native-sections':'native-complete-persona',nativeContextsSuppressed:false,
        overriddenNativeSectionNames:preset==='standard'?policy.standard.overrides.filter(row=>row.enabled).map(row=>row.id):policy.minimal.mode==='override'?['deployment:persona-prefix']:[],
        retainedNativeToolSectionNames:effectiveSections.filter(s=>isToolProtocolSection(s.name)&&s.text).map(s=>s.name),
        nativeContextNames:(assembly.contexts??[]).map(s=>s.name),effectiveSystemSha256:digest(effective),effectiveSystemChars:effective.length,
        runtimePresentationCapabilities:{inherit:true,off:true,template:'native-render-parts',hostApi:hostCapabilities},
        runtimePolicy:Object.fromEntries(RUNTIME_PRESENTATION_PRODUCERS.map(name=>[name,policy.runtime[name].mode]))});
      return sections===assembly.sections?assembly:{...assembly,sections};
    }catch(error){observed({promptPolicyError:error.code??error.message});throw error;}
  },{prepend:true}));
  if(RUNTIME_PRESENTATION_PRODUCERS.some(name=>name!=='sessionTitle'&&policy.runtime[name].mode==='template')){
    disposers.push(scoped.on('native-prompt/render',request=>{
      if(request.agent!==agent)return;
      const choice=policy.runtime[request.producer];
      if(choice?.mode!=='template')return;
      const selected=request.producer==='time'?choice:choice.templates[request.part];
      if(!selected)return;
      return renderPresentationTemplate(selected,request.variables);
    }));
  }
  // This is a separate, opt-in source-specific policy. Inherit installs no
  // message filter and never clears unrelated pre-step or tool feedback.
  if(['time','agentInstructions','planMode'].some(name=>policy.runtime[name].mode==='off')){
    disposers.push(scoped.on('agent/pre-step',async({agent:owner},next)=>{
      const decision=await next();
      if(owner!==agent||decision.kind!=='enter')return decision;
      const messages=transformRuntimeMessages(decision.messages,policy.runtime);
      observed({runtimeMessagesBefore:decision.messages.length,runtimeMessagesAfter:messages.length});
      return {...decision,messages};
    },{prepend:true}));
  }
  return()=>{for(const dispose of disposers.reverse())dispose();};
}

export function normalizeNativeCompaction(value) {
  if (value === undefined) return undefined;
  const parts = ['instruction','checkpoint'];
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => ![...parts,'versions'].includes(key)) ||
      !parts.some(key => Object.hasOwn(value,key)) ||
      (Object.hasOwn(value,'instruction') && (typeof value.instruction !== 'string' || !value.instruction.trim())) ||
      !value.versions || typeof value.versions !== 'object' || Array.isArray(value.versions) ||
      Object.keys(value.versions).some(key => !parts.includes(key)) ||
      parts.some(key => Object.hasOwn(value,key)
        ? typeof value[key] !== 'string' || value[key].length > 100000 || !['number','string'].includes(typeof value.versions[key])
        : Object.hasOwn(value.versions,key)))
    throw Error('INVALID_NATIVE_COMPACTION_PRESENTATION');
  return JSON.parse(JSON.stringify(value));
}
