/** Narrow access to existing DSH settings. No credentials, plugin loading or model calls.
 * Host services: ctx.agentPresets (AgentPresetRegistry), ctx.settings (SettingsForms).
 * These are NOT the generated remote.agentPresets / remote.settings response envelopes.
 */
export const AGENT_PRESET_NAMESPACE = 'agent-preset-registry';
export const NATIVE_SETTINGS_INJECT = Object.freeze(['agentPresets', 'settings']);
const MESSAGES = Object.freeze({
  INVALID_NATIVE_SETTINGS_REQUEST: 'Provide a presetId and the revision read from the native catalog.',
  NATIVE_SETTINGS_UNAVAILABLE: 'The existing DSH settings services are unavailable.',
  NATIVE_SETTINGS_READ_FAILED: 'The existing DSH settings could not be read.',
  NATIVE_SETTINGS_SCHEMA_INVALID: 'The native settings response does not match the supported contract.',
  NATIVE_MODEL_CATALOG_SCHEMA_INVALID: 'The native model catalog does not match the supported contract.',
  NATIVE_SETTINGS_READ_ONLY: 'The existing DSH profile does not allow settings writes.',
  NATIVE_PRESET_UNAVAILABLE: 'The requested preset is absent or unavailable in the native roster.',
  SETTINGS_CONFLICT: 'The native settings changed. Refresh and review the latest selection before saving.',
  NATIVE_WRITE_OUTCOME_UNKNOWN: 'The write outcome is unknown. Refresh and reconcile it; do not automatically retry.',
});

export class NativeSettingsError extends Error {
  constructor(code, {outcome = 'not_applied', details} = {}) {
    super(MESSAGES[code] ?? MESSAGES.NATIVE_SETTINGS_UNAVAILABLE);
    this.name = 'NativeSettingsError'; this.code = code; this.outcome = outcome; this.retryable = false;
    if (details !== undefined) this.details = details;
  }
  toJSON() { return {ok:false, error:{code:this.code, message:this.message, outcome:this.outcome, retryable:false, ...(this.details === undefined ? {} : {details:this.details})}}; }
}
const fail = (code, options) => { throw new NativeSettingsError(code, options); };
const plain = v => v !== null && typeof v === 'object' && !Array.isArray(v) && [Object.prototype, null].includes(Object.getPrototypeOf(v));
const revision = v => Number.isSafeInteger(v) && v >= 0;
const id = v => typeof v === 'string' && v.length > 0 && v.length <= 256 && v === v.trim() && !/[\u0000-\u001f\u007f]/u.test(v);
function record(v, code) { if (!plain(v)) fail(code); return v; }
function identifier(v, code) { if (!id(v)) fail(code); return v; }
function array(v, max, code) { if (!Array.isArray(v) || v.length > max) fail(code); return v; }
function unique(rows, key, code) { const seen = new Set(); for(const row of rows) { const value=key(row); if(seen.has(value)) fail(code); seen.add(value); } return rows; }
function text(v, max, code) {
  if(typeof v !== 'string' || v.length > 65536) fail(code);
  // Public author metadata only. Never pass through exception messages or secret-bearing object fields.
  return v.replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/gu,'').slice(0,max);
}
function label(row, key, max, code) { return row[key] === undefined ? {} : {[key]:text(row[key],max,code)}; }
function safePresetRows(raw, defaultPresetId) {
  const code='NATIVE_SETTINGS_SCHEMA_INVALID';
  const rows=array(raw,256,code).map(value=>{
    const row=record(value,code), presetId=identifier(row.id,code);
    if(row.broken !== undefined && typeof row.broken !== 'string')fail(code);
    return {id:presetId, ...label(row,'name',256,code), ...label(row,'description',2000,code), available:row.broken === undefined, isDefault:presetId === defaultPresetId};
  });
  return unique(rows,row=>row.id,code);
}
function safeSelection(raw, code) {
  const value=record(raw,code);
  return {provider:identifier(value.provider,code),model:identifier(value.model,code),...(value.reasoningEffort === undefined ? {} : {reasoningEffort:identifier(value.reasoningEffort,code)})};
}

/** Validate and project the actual sessionController.modelCatalog() contract.
 * No URLs, credential metadata, raw provider failures, badges or arbitrary schemas escape.
 * Reasoning effort IDs are provider-authored strings, not one global hard-coded enum.
 */
export function safeModelCatalog(raw) {
  try { return projectModelCatalog(raw); }
  catch(error) { if(error instanceof NativeSettingsError)throw error; fail('NATIVE_MODEL_CATALOG_SCHEMA_INVALID'); }
}
function projectModelCatalog(raw) {
  const code='NATIVE_MODEL_CATALOG_SCHEMA_INVALID', value=record(raw,code);
  const defaultSelection=safeSelection(value.default,code);
  const groups=unique(array(value.groups,256,code).map(rawGroup=>{
    const group=record(rawGroup,code);
    const models=unique(array(group.models,4096,code).map(rawModel=>{
      const model=record(rawModel,code);
      const safe={id:identifier(model.id,code),name:text(model.name,256,code),...label(model,'description',2000,code)};
      if(model.reasoning !== undefined){
        const reasoning=record(model.reasoning,code);
        const efforts=unique(array(reasoning.efforts,128,code).map(rawEffort=>{
          const effort=record(rawEffort,code);
          return {id:identifier(effort.id,code),name:text(effort.name,256,code),...label(effort,'description',2000,code)};
        }),row=>row.id,code);
        const defaultEffort=reasoning.defaultEffort === undefined ? undefined : identifier(reasoning.defaultEffort,code);
        if(defaultEffort !== undefined && !efforts.some(e=>e.id === defaultEffort))fail(code);
        safe.reasoning={efforts,...(defaultEffort === undefined ? {} : {defaultEffort})};
      }
      return safe;
    }),row=>row.id,code);
    return {id:identifier(group.id,code),name:text(group.name,256,code),models};
  }),row=>row.id,code);
  const routableProviders=unique(array(value.routableProviders,256,code).map(x=>identifier(x,code)),x=>x,code);
  // A failed/default configured route may be missing from groups; routable routes may not.
  if(routableProviders.some(provider=>!groups.some(g=>g.id === provider && g.models.length > 0)))fail(code);
  const failures=unique(array(value.failures,256,code).map(rawFailure=>{
    const failure=record(rawFailure,code);
    return {id:identifier(failure.id,code),name:text(failure.name,256,code),code:'MODEL_CATALOG_UNAVAILABLE'};
  }),row=>row.id,code);
  return {source:'existing-dsh',metadataOnly:true,default:defaultSelection,routableProviders,groups,failures};
}

/** Safe original-DSH default-preset access. It never selects a preset on an existing Agent. */
export class NativeSettings {
  constructor({agentPresets, settings} = {}) {
    if(typeof agentPresets?.list !== 'function' || typeof settings?.describe !== 'function' || typeof settings?.update !== 'function')
      fail('NATIVE_SETTINGS_UNAVAILABLE');
    this.agentPresets=agentPresets; this.settings=settings;
  }
  async catalog() {
    try {
      const rows=await this.agentPresets.list();
      // Actual SettingsForms returns an array; the Remote settings.describe envelope is different.
      const described=await this.settings.describe({redactSecrets:true});
      const code='NATIVE_SETTINGS_SCHEMA_INVALID';
      if(!Array.isArray(described))fail(code);
      const matches=described.filter(d=>d?.ns === AGENT_PRESET_NAMESPACE);
      if(matches.length !== 1)fail(code);
      const descriptor=record(matches[0],code), value=record(descriptor.value,code);
      if(!revision(descriptor.revision)||descriptor.applies !== 'live'||typeof this.settings.writable !== 'boolean')fail(code);
      const selectedDefault=value.selectedDefault === undefined ? null : identifier(value.selectedDefault,code);
      const defaultPresetId=identifier(this.agentPresets.defaultId,code);
      if(selectedDefault !== null && defaultPresetId !== selectedDefault)fail(code);
      const presets=safePresetRows(rows,defaultPresetId);
      return {source:'existing-dsh',scope:'native-new-sessions',writable:this.settings.writable,revision:descriptor.revision,defaultPresetId,selectedDefault,defaultAvailable:presets.some(p=>p.isDefault&&p.available),presets};
    }catch(error){
      if(error instanceof NativeSettingsError)throw error;
      fail('NATIVE_SETTINGS_READ_FAILED');
    }
  }
  async updateDefaultPreset(request) {
    if(!plain(request)||Object.keys(request).some(k=>!['presetId','expectedRevision'].includes(k))||!id(request.presetId)||!revision(request.expectedRevision))
      fail('INVALID_NATIVE_SETTINGS_REQUEST');
    const {presetId,expectedRevision}=request;
    const before=await this.catalog();
    if(!before.writable)fail('NATIVE_SETTINGS_READ_ONLY');
    if(before.revision !== expectedRevision)fail('SETTINGS_CONFLICT',{details:{expectedRevision,actualRevision:before.revision}});
    if(!before.presets.some(row=>row.id === presetId && row.available))fail('NATIVE_PRESET_UNAVAILABLE');
    try {
      // Exactly one native CAS write. Never mutate default, replace the namespace, or rewrite YAML.
      await this.settings.update(AGENT_PRESET_NAMESPACE,{selectedDefault:presetId},expectedRevision);
    }catch(error){
      if(error?.code === 'SETTINGS_CONFLICT' || error?.code === 'settings/conflict' || error?.name === 'SettingsConflictError'){
        const actual=error?.actual ?? error?.details?.actual;
        fail('SETTINGS_CONFLICT',{details:{expectedRevision,...(revision(actual)?{actualRevision:actual}:{})}});
      }
      // The native call can throw after committing or during its post-write read. No automatic replay.
      fail('NATIVE_WRITE_OUTCOME_UNKNOWN',{outcome:'unknown'});
    }
    let after;
    try { after=await this.catalog(); }
    catch { fail('NATIVE_WRITE_OUTCOME_UNKNOWN',{outcome:'unknown'}); }
    const observedMatches=after.defaultPresetId === presetId && after.selectedDefault === presetId;
    if(!observedMatches && after.revision <= expectedRevision)fail('NATIVE_WRITE_OUTCOME_UNKNOWN',{outcome:'unknown'});
    return {status:observedMatches?'updated':'superseded',outcome:'applied',requestedPresetId:presetId,observedMatches,...after};
  }
}
