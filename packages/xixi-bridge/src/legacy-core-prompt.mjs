/** Core owns all non-tool text for its Agent, including native runtime context.
 * Native tool declarations remain assembled by the host. No global hook/config is changed.
 */
export function configuredSystemText(config) {
  const sections = config.promptSections;
  const configured = sections === undefined
    ? [config.roleCard ?? '', config.rules ?? ''].filter(Boolean).join('\n\n')
    : sections.filter(s => s.placement === 'system').map(s => s.text).filter(Boolean).join('\n\n');
  return [configured, (config.contextFragments ?? []).filter(f => f.placement === 'system').map(f => f.text).join('\n\n'), config.outputProtocol ?? '']
    .filter(Boolean).join('\n\n');
}

export function installCorePrompt(scoped, agent, config, observed = () => {}) {
  if (typeof scoped.on !== 'function' || typeof scoped.systemPrompt.suppressRuntimeContext !== 'function')
    throw Error('EXISTING_DSH_COMPLETE_PROMPT_POLICY_UNAVAILABLE');
  const disposers = [];
  disposers.push(scoped.systemPrompt.section({
    name: 'xixi:core:complete', order: 0, complete: true,
    interpolate: false, text: configuredSystemText(config),
  }));
  disposers.push(scoped.systemPrompt.suppressRuntimeContext());
  // The native working-directory listener restores a required context AFTER next().
  // prepend makes this the outer wrapper, so filtering occurs after native wrappers
  // finish. Exact identity and native scope routing keep ordinary DSH Agents intact.
  disposers.push(scoped.on('system-prompt/assemble', async (_assembly, context, next) => {
    const assembly = await next();
    if (context.agent !== agent) return assembly;
    observed({suppressedContextNames: (assembly.contexts ?? []).map(item => item.name)});
    return {...assembly, contexts: []};
  }, {prepend: true}));
  const nativeKinds = new Set(['time-context','agent-instructions','plan-mode','repeat-tool-reminder','runtime-context']);
  disposers.push(scoped.on('agent/pre-step', async ({agent: owner, messages}, next) => {
    const decision = await next();
    if (owner !== agent || decision.kind !== 'enter') return decision;
    const claimed = new Set(messages.map(message => message.id));
    const kept = decision.messages.filter(message =>
      claimed.has(message.id) && !nativeKinds.has(message.source?.kind));
    observed({suppressedStepSourceKinds: decision.messages.filter(message => !kept.includes(message)).map(message => message.source?.kind ?? 'unknown')});
    return {...decision, messages: kept};
  }, {prepend: true}));
  disposers.push(scoped.on('tools/post-execute', async (exec, _result, next) => {
    const decision = await next();
    if (exec.agent !== agent || !decision.additionalContexts?.length) return decision;
    observed({suppressedToolContextSourceKinds: decision.additionalContexts.map(message => message.source?.kind ?? 'unknown')});
    return {...decision, additionalContexts: []};
  }, {prepend: true}));
  return () => { for (const dispose of disposers.reverse()) dispose(); };
}

export function normalizeNativeCompaction(value) {
  if (value === undefined) return undefined;
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).some(key => !['instruction','checkpoint','versions'].includes(key)) ||
      typeof value.instruction !== 'string' || !value.instruction.trim() || value.instruction.length > 100000 ||
      typeof value.checkpoint !== 'string' || value.checkpoint.length > 100000 ||
      !value.versions || typeof value.versions !== 'object' ||
      Object.keys(value.versions).some(key => !['instruction','checkpoint'].includes(key)) ||
      ['instruction','checkpoint'].some(key => !['number','string'].includes(typeof value.versions[key])))
    throw Error('INVALID_NATIVE_COMPACTION_PRESENTATION');
  return JSON.parse(JSON.stringify(value));
}
