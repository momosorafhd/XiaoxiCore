/** Explicit, session-frozen presentation choices. No native defaults are copied. */
export const TOOL_GUIDANCE = new Map([
  ...['bash','pwsh','read','write','edit','apply_patch','glob','grep','web_fetch','web_search','subagent','subagent_fork','task','workflow','ralph','structured_output'].map(name=>['tool:'+name,[name]]),
  ['tool:jobs',['job_output','job_list','job_kill']],
  ['tool:goal',['get_goal','create_goal','update_goal']],
  ['tool:session-query',['session_search','session_event_search','session_trace','session_event_trace','session_event_read']],
  ['tool:schedule',['schedule_create','schedule_list','schedule_view','schedule_edit']],
  ['tool:pty',['terminal_open','terminal_send','terminal_read','terminal_signal','terminal_close','terminal_list']],
  ['tool:pty-panel',['user_terminal_list','user_terminal_read']],
  ['team:policy',['team_task_create','team_task_list','team_task_get','team_task_update']],
  ['mcp-resource-servers',['list_mcp_resources','list_mcp_resource_templates','read_mcp_resource']],
  ['context:file-reference',['read']],
]);
export const RUNTIME_PRESENTATION_PRODUCERS=Object.freeze(['time','agentInstructions','planMode','runtimeContext','modelSelection','workingDirectory','sessionTitle']);
export const isToolProtocolSection=name=>TOOL_GUIDANCE.has(name)||/^(?:tool:|tools:|mcp:|team:)/.test(name)||name==='mcp-resource-servers';
const fail=(code,detail='')=>{throw Object.assign(Error(code+(detail?': '+detail:'')),{code});};
const object=value=>value&&typeof value==='object'&&!Array.isArray(value);
const keys=(value,allowed)=>object(value)&&Object.keys(value).every(key=>allowed.includes(key));
function textChoice(value,{runtime=false}={}){
  if(!keys(value,['mode','text','templateSyntax']))fail('INVALID_CORE_PROMPT_POLICY');
  const mode=value.mode??'inherit';
  if(!(runtime?['inherit','off','template']:['inherit','override']).includes(mode))fail('INVALID_CORE_PROMPT_POLICY');
  if(mode==='inherit'||mode==='off'){
    if(value.text!==undefined||value.templateSyntax!==undefined)fail('INACTIVE_CORE_PROMPT_TEXT');
    return {mode};
  }
  const syntax=value.templateSyntax??'literal';
  if(typeof value.text!=='string'||value.text.length>100000||!(runtime?['literal','core']:['literal','dsh']).includes(syntax))fail('INVALID_CORE_PROMPT_POLICY');


  if(runtime&&syntax==='core')validateVariables(value.text,['now_ms','now','time_zone','turn','step','previous_ms','elapsed_ms','browser_kind','browser_time_zones']);
  return {mode,text:value.text,templateSyntax:syntax};
}
export const AGENT_PRESENTATION_PARTS=['intro','replacementIntro','emptyReplacementIntro','compactIntro','fileHeader','additionalHeader','applicabilityGlobal','applicabilityProject','additionalGuidance','updatedHeader','updatedGuidance','removedHeader','removedGuidance','budgetOmitted','budgetTruncatedItem','budgetTruncated','budgetSummary'];
const PART_VARIABLES={fileHeader:['path'],additionalHeader:['path'],applicabilityProject:['scope'],updatedHeader:['path'],updatedGuidance:['path'],removedHeader:['path'],removedGuidance:['path'],budgetOmitted:['paths'],budgetTruncatedItem:['path','original_bytes','included_bytes'],budgetTruncated:['items'],budgetSummary:['max_bytes','details'],entered:['active'],exited:['active']};
function validateVariables(text,allowed){renderNativeText({name:'native-presentation',text},Object.fromEntries(allowed.map(name=>[name,''])));}
function instructionChoice(value,parts=AGENT_PRESENTATION_PARTS,modes=['inherit','off','template'],variables=PART_VARIABLES){
  if(!keys(value,['mode','templates']))fail('INVALID_CORE_PROMPT_POLICY');
  const mode=value.mode??'inherit';
  if(!modes.includes(mode))fail('INVALID_CORE_PROMPT_POLICY');
  if(mode!=='template'){if(value.templates!==undefined)fail('INACTIVE_CORE_PROMPT_TEXT');return {mode};}
  if(!object(value.templates))fail('INVALID_CORE_PROMPT_POLICY');
  const templates={};
  for(const [part,row] of Object.entries(value.templates)){
    if(!parts.includes(part)||!keys(row,['text','templateSyntax'])||typeof row.text!=='string'||row.text.length>100000||!['literal','core'].includes(row.templateSyntax??'literal'))fail('INVALID_AGENT_PRESENTATION_PART',part);
    if(row.templateSyntax==='core')validateVariables(row.text,variables[part]??[]);
    templates[part]={text:row.text,templateSyntax:row.templateSyntax??'literal'};
  }
  return {mode,templates};
}
export function normalizePromptPolicy(value){
  if(value===undefined)value={version:1};
  if(!keys(value,['version','standard','minimal','runtime'])||value.version!==1)fail('INVALID_CORE_PROMPT_POLICY');
  const standard=value.standard??{},minimal=value.minimal??{mode:'inherit'},runtime=value.runtime??{};
  if(!keys(standard,['overrides'])||!Array.isArray(standard.overrides??[])||!keys(runtime,RUNTIME_PRESENTATION_PRODUCERS))fail('INVALID_CORE_PROMPT_POLICY');
  const seen=new Set();
  const overrides=(standard.overrides??[]).map(row=>{
    if(!keys(row,['id','text','enabled','templateSyntax'])||typeof row.id!=='string'||!row.id||row.id.length>200||seen.has(row.id)||typeof row.text!=='string'||row.text.length>100000||typeof row.enabled!=='boolean'||!['literal','dsh'].includes(row.templateSyntax??'literal'))fail('INVALID_CORE_PROMPT_OVERRIDE');
    seen.add(row.id);
    if(isToolProtocolSection(row.id)||row.id==='xixi:core:configured'||/^(?:working-directory|sandbox|approval|runtime-context):/.test(row.id))fail('CORE_PROMPT_OVERRIDE_PROTECTED',row.id);
    return {...row,templateSyntax:row.templateSyntax??'literal'};
  });
  return {version:1,standard:{overrides},minimal:textChoice(minimal),runtime:{
    time:textChoice(runtime.time??{mode:'inherit'},{runtime:true}),
    agentInstructions:instructionChoice(runtime.agentInstructions??{mode:'inherit'}),
    planMode:instructionChoice(runtime.planMode??{mode:'inherit'},['entered','exited']),
    runtimeContext:instructionChoice(runtime.runtimeContext??{mode:'inherit'},['initialIntro','replacementIntro','cleared'],['inherit','template']),
    modelSelection:instructionChoice(runtime.modelSelection??{mode:'inherit'},['changed'],['inherit','template'],{changed:['from','to','from_provider','from_model','to_provider','to_model']}),
    workingDirectory:instructionChoice(runtime.workingDirectory??{mode:'inherit'},['changed','recovered'],['inherit','template'],{changed:['from','to','raw_from','raw_to'],recovered:['from','to','raw_from','raw_to']}),
    sessionTitle:instructionChoice(runtime.sessionTitle??{mode:'inherit'},['system','inputIntro'],['inherit','template'],{system:['target_words','target_cjk_characters'],inputIntro:[]}),
  }};
}
/** Match native renderPrompt interpolation, including single-pass replacement. */
export function renderNativeText(section,variables={}){
  if(section.interpolate===false)return section.text;
  let text=section.text,result='',last=0;
  for(let open=text.indexOf('{{');open>=0;open=text.indexOf('{{',last)){
    const group=/^\{\{([^{}]*)\}\}/.exec(text.slice(open));
    if(group===null){
      if(text.indexOf('}}',open+2)>=0)fail('MALFORMED_NATIVE_PROMPT_VARIABLE',section.name);
      result+=text.slice(last,open+2);last=open+2;continue;
    }
    const name=group[1];
    if(!/^[a-z][a-z0-9_]*$/.test(name)||!Object.hasOwn(variables,name)||variables[name]===undefined)fail('INVALID_NATIVE_PROMPT_VARIABLE',name);
    result+=text.slice(last,open)+variables[name];last=open+group[0].length;
  }
  return result+text.slice(last);
}
export const renderNativeSections=(sections,variables)=>sections.map(s=>renderNativeText(s,variables)).filter(text=>text.length>0).join('\n\n');
export function overrideStandardSections(sections,overrides){
  const enabled=new Map(overrides.filter(row=>row.enabled).map(row=>[row.id,row]));
  const known=new Set(sections.map(section=>section.name));
  for(const id of enabled.keys())if(!known.has(id))fail('CORE_PROMPT_SECTION_UNAVAILABLE',id);
  return sections.map(section=>{
    const override=enabled.get(section.name);
    // Preserve the native plan-mode condition; prose cannot activate plan mode.
    if(!override||section.name==='plan:policy'&&!section.text)return section;
    return {...section,text:override.text,interpolate:override.templateSyntax==='dsh'};
  });
}
export function transformRuntimeMessages(messages,policy){
  return messages.filter(message=>{
    const choice=message.source?.kind==='time-context'?policy.time:message.source?.kind==='agent-instructions'?policy.agentInstructions:message.source?.kind==='plan-mode'?policy.planMode:undefined;
    return choice?.mode!=='off';
  });
}
export function renderPresentationTemplate(choice,variables){
  return renderNativeText({name:'native-presentation',text:choice.text,interpolate:choice.templateSyntax==='core'},variables);
}

const part=(variables=[])=>({variables,templateSyntax:['literal','core'],default:'inherit'});
export const PROMPT_PRESENTATION_CATALOG=Object.freeze({
  version:1,
  system:{mechanism:'same-name scoped native presentation overlay',templateSyntax:['literal','dsh'],
    knownNativeIds:['harness:identity','deployment:persona-prefix','deployment:persona-suffix','plan:policy','ui:deliverable-file-references','harness:source','app:web-surface'],
    editableNativeIds:['harness:identity','deployment:persona-prefix','deployment:persona-suffix','plan:policy'],
    protected:'tool protocols and real runtime facts are not writable system prose',unlistedNativeSection:'only an existing non-tool system section may be explicitly overridden'},
  minimal:{originalBody:'You are a helpful software engineer assistant.',originalSource:'dsh-web-app/presets/minimal.patch.yml:persona.prefix',originalOnly:true},
  time:{modes:['inherit','off','template'],parts:{time:part(['now_ms','now','time_zone','turn','step','previous_ms','elapsed_ms','browser_kind','browser_time_zones'])}},
  agentInstructions:{modes:['inherit','off','template'],fileBody:'user instruction files; never a template slot',parts:{
    intro:part(),replacementIntro:part(),emptyReplacementIntro:part(),compactIntro:part(),fileHeader:part(['path']),additionalHeader:part(['path']),applicabilityGlobal:part(),applicabilityProject:part(['scope']),additionalGuidance:part(),updatedHeader:part(['path']),updatedGuidance:part(['path']),removedHeader:part(['path']),removedGuidance:part(['path']),budgetOmitted:part(['paths']),budgetTruncatedItem:part(['path','original_bytes','included_bytes']),budgetTruncated:part(['items']),budgetSummary:part(['max_bytes','details'])}},
  planMode:{modes:['inherit','off','template'],parts:{entered:part(['active']),exited:part(['active'])},state:'read-only; display text does not change plan mode'},
  runtimeContext:{modes:['inherit','template'],parts:{initialIntro:part(),replacementIntro:part(),cleared:part()},facts:'read-only named native sections; machine reminder frame retained'},
  modelSelection:{modes:['inherit','template'],parts:{changed:part(['from','to','from_provider','from_model','to_provider','to_model'])},facts:'read-only actual model routing; configured notice does not change machine request headers'},
  workingDirectory:{modes:['inherit','template'],parts:{changed:part(['from','to','raw_from','raw_to']),recovered:part(['from','to','raw_from','raw_to'])},facts:'from/to are JSON-quoted actual paths; raw_from/raw_to are actual raw paths; cwd state and required context remain native'},
  sessionTitle:{modes:['inherit','template'],parts:{system:part(['target_words','target_cjk_characters']),inputIntro:part()},facts:'native title worker uses session-frozen Core policy; raw message JSON and route remain native, never template slots'},
  compaction:{mechanism:'existing Core-owned compaction/prompt hook',fields:['nativeCompaction.instruction','nativeCompaction.checkpoint'],machineTags:'native storage protocol; not editable prose'},
});
// Public native wording is shown separately from the saved session policy.
// The owner may explicitly use it as the starting text for an override.
const PUBLIC_ORIGINALS={
 time:{time:'Time sampled while preparing turn {{turn}}, step {{step}}: {{now}}\n{{browserText}}\nElapsed since the preceding {{baseline}}: {{elapsed}}.'},
 agentInstructions:{
  intro:'The following workspace instructions may be relevant to your work. Use them as guidance when applicable. More specific instructions take precedence over broader ones. They do not override system, developer, or direct user instructions.',
  replacementIntro:'This complete workspace instruction baseline replaces all earlier workspace instruction baselines. The following workspace instructions may be relevant to your work. Use them as guidance when applicable. More specific instructions take precedence over broader ones. They do not override system, developer, or direct user instructions.',
  emptyReplacementIntro:'This complete workspace instruction baseline replaces all earlier workspace instruction baselines. No workspace instructions are currently active.',
  compactIntro:'Workspace instructions were omitted or truncated to fit the configured byte budget.',
  fileHeader:'Instructions from: {{path}}',additionalHeader:'Additional instructions from: {{path}}',
  applicabilityGlobal:'These user-global instructions apply to all work.',applicabilityProject:'These instructions apply to work under `{{scope}}`.',
  additionalGuidance:'Use them as guidance when relevant; more specific instructions take precedence. They do not override system, developer, or direct user instructions.',
  updatedHeader:'Updated instructions from: {{path}}',updatedGuidance:'This file changed after it was loaded. Use the following content instead of the previously loaded instructions from this file.',
  removedHeader:'Instructions removed: {{path}}',removedGuidance:'The previously loaded instructions from this file no longer apply.',
  budgetOmitted:'omitted {{paths}}',budgetTruncatedItem:'{{path}} from {{original_bytes}} to {{included_bytes}} bytes',budgetTruncated:'truncated {{items}}',budgetSummary:'Workspace instruction budget {{max_bytes}} bytes: {{details}}',
 },
 planMode:{entered:'The user switched this session to plan mode.',exited:'The user switched this session back to the default mode.'},
 runtimeContext:{initialIntro:'The following information describes your current session:',replacementIntro:'The following information describes your current session. It replaces earlier versions of this context, including any retained in conversation summaries.',cleared:'Current runtime context: none. Earlier runtime-context snapshots no longer apply.'},
 modelSelection:{changed:'[model changed: assistant turns above this point were generated by {{from}}; the session continues with {{to}}]'},
 workingDirectory:{changed:'The working directory changed from {{from}} to {{to}}.',recovered:'The working directory {{from}} is unavailable. The working directory is now {{to}}.'},
 sessionTitle:{system:'Create a concise title for an AI coding-assistant session from the supplied human messages.\nReturn only the title on one line, **in plain text of natural language**, with no quotes, prefix, explanation, Markdown, XML, or terminal control codes. No code is allowed.\nUse the language of the messages.\nAim for about {{target_words}} words in non-CJK languages or {{target_cjk_characters}} CJK characters.\nIf the messages give little to name, still return a short best-effort title, such as Greeting, instead of explaining.',inputIntro:'Generate the session title from this JSON array of human messages:'},
 compaction:{instruction:[
  'You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.',
  '',
  'Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.',
  '',
  '## Primary Request and Intent',
  "- [the user's original and evolving goals; quote verbatim where the exact wording matters]",
  '',
  '## Key Technical Concepts',
  '- [technologies, frameworks, patterns, and conventions in play]',
  '',
  '## Files and Code',
  '- [exact path: why it matters, key changes or snippets]',
  '',
  '## Errors and Fixes',
  '- [error: how it was resolved, plus any related user feedback]',
  '',
  '## Pending Jobs',
  '- [explicitly requested work not yet completed]',
  '',
  '## Current Work',
  '- [precisely what was in progress at this checkpoint]',
  '',
  '## Next Step',
  '- [the single next action, directly in line with the most recent request, or "(none)"]',
  '',
  '## Critical Context',
  '- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]',
  '',
  'Rules:',
  '- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.',
  '- Capture user feedback and explicit instructions faithfully, especially corrections.',
  '- Do NOT mention this summarization request or that the context was compacted.',
  '- Output only the checkpoint text: do not call any tool or take any other action.',
  '- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.',
 ].join('\n'),checkpoint:'This is an automatically generated checkpoint condensing an earlier span of the conversation to free up context. Treat the captured context as established background and build on it without restating it. Continue the task directly from the messages that follow, without acknowledging this checkpoint.'},

};
const ORIGINAL_SOURCES={time:'dsh-time-context/src/index.ts:renderText; dsh-time-context/src/request-zone.ts:renderBrowserTimeZoneContext',agentInstructions:'dsh-agent-instructions/src/render.ts',planMode:'dsh-plan-mode/src/index.ts:narration',runtimeContext:'dsh-agent-loop/src/runtime-context.ts:project',modelSelection:'dsh-agent/src/model-selection.ts:modelSwitchNotice',workingDirectory:'dsh-working-directory/src/index.ts:commit',sessionTitle:'dsh-session-title-first-prompt-llm/src/index.ts:systemPrompt/frameMessages',compaction:'dsh-compaction-basic/src/summarizer.ts'};
PROMPT_PRESENTATION_CATALOG.compaction.parts={instruction:part(),checkpoint:part()};
for(const [producer,parts] of Object.entries(PUBLIC_ORIGINALS))for(const [name,originalDefault] of Object.entries(parts)){
 Object.assign(PROMPT_PRESENTATION_CATALOG[producer].parts[name],{originalDefault,originalBody:originalDefault,originalSource:ORIGINAL_SOURCES[producer],originalOnly:true});
}
PROMPT_PRESENTATION_CATALOG.time.parts.time.originalNote='Source-shaped read-only template: browserText/baseline/elapsed are internal rendered variants, not editable template variables. Editable templates use only the variables list above.';
PROMPT_PRESENTATION_CATALOG.time.parts.time.editorText='Time sampled while preparing turn {{turn}}, step {{step}}: {{now}}\nTime zone: {{time_zone}}.';
PROMPT_PRESENTATION_CATALOG.time.parts.time.editorSyntax='core';
PROMPT_PRESENTATION_CATALOG.time.parts.time.originalVariants={
 browserResolved:'Browser time zone for this request: {{timeZone}}. Interpret otherwise-unqualified dates and times in this zone.',
 browserMixed:'Browser time zone for this request: mixed {{timeZones}}. Ask the user to clarify otherwise-unqualified dates and times.',
 browserMissing:'Browser time zone for this request: unavailable. Ask the user to clarify otherwise-unqualified dates and times.',
 baselineFirst:'model-visible message',baselineLater:'step context',elapsedUnavailable:'unavailable',
};
Object.assign(PROMPT_PRESENTATION_CATALOG.system,{
 nativeVariableNames:['provider','model'],
 nativeVariableSource:'dsh-agent-loop/src/index.ts:systemPrompt.variable; actual values are session-selected and not returned here',
 originalSource:'dsh-system-prompt/src/index.ts; dsh-web-app/presets/standard.patch.yml; dsh-web-app/presets/minimal.patch.yml',
 sectionSources:{'plan:policy':'dsh-web-app/presets/standard.patch.yml:plan-mode.section'},
 originalSections:{
  'harness:identity':'You are an AI agent powered by DeepSeek Harness.',
  'deployment:persona-suffix':'',
  'plan:policy':"You are in plan mode. Stay in plan mode until the user approves your plan through exit_plan_mode or switches the session mode. Imperative language to implement changes means plan the implementation, not execute it. A user's conversational agreement — including an answer confirming something you asked — approves nothing and does not end plan mode; fold the confirmed decision into the plan and submit it through exit_plan_mode.\n\nExplore first. Use non-mutating reads, searches, static analysis, and checks to ground the plan in the actual repository. Do not edit or write files, change configuration, run formatters or code generation that rewrites tracked files, commit, or otherwise carry out the plan. Prefer existing functions and patterns over new machinery.\n\nThe tool catalog stays the same across modes for request-cache stability. These plan-mode rules override any later tool description or guidance that suggests using mutation tools; those tools remain listed to keep the tool catalog unchanged. Do not use todo_write to track this planning phase: it tracks implementation after an approved plan, while the plan itself belongs in exit_plan_mode.\n\nResolve discoverable facts by inspection. Use ask_user_question only for user-owned choices or material ambiguity that inspection cannot answer. Do not ask the user where code lives or how current behavior works when you can find out.\n\nMake the plan decision-complete: state the goal and success criteria; group implementation changes by subsystem; identify public API, schema, and data-flow changes; cover edge cases, failure modes, tests, acceptance criteria, and explicit assumptions. Keep it concise enough to review but detailed enough that another engineer can implement it without making design decisions.\n\nWhen ready, call exit_plan_mode with the complete plan markdown, starting with a # title. Make exit_plan_mode the only and final tool call in that assistant response: it presents the plan for approval, and implementation begins only in a later step after approval. Do not paste the final plan as a plain reply or ask \"should I proceed?\" through prose or ask_user_question. If review rejects it, incorporate the feedback and present again. If the review channel is unavailable or aborted, stay in plan mode and ask the user to switch modes manually; do not proceed with implementation.\n",
 },
 originalPresets:{
  standard:{prefix:'You are a coding agent powered by the {{model}} model.',complete:false},
  minimal:{prefix:'You are a helpful software engineer assistant.',complete:true},
 },
});
export function promptPolicyCapabilities(hostCapabilities={}){
  return {version:1,hostCapabilities:Object.fromEntries(RUNTIME_PRESENTATION_PRODUCERS.map(name=>[name,hostCapabilities[name]===1?1:0])),catalog:PROMPT_PRESENTATION_CATALOG};
}
