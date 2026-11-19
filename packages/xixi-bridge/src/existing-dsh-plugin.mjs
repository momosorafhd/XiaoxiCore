/** DSH loader plugin. Its bearer token is machine authentication, never a model key. */
import {readFile,lstat,realpath} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {isAbsolute} from 'node:path';
import {ExistingDshBridge} from './existing-dsh.mjs';
import {acquireExistingDataLease} from './existing-lease.mjs';
import {createBridgeServer} from './server.mjs';
export const name='xixi-existing-dsh-bridge';
export const inject=['sessionController','agents','sessions','llm','tools','systemPrompt','agentDefaultModel','tokenMeter','agentPresets'];
// Read exports from the actual executable's module graph, never from Core data
// or user policy. Old unpatched hosts advertise zero and keep Legacy working.
export async function loadNativePromptRenderCapabilities(hostRequire){
  const modules={time:'@deepseek-ai/dsh-time-context',agentInstructions:'@deepseek-ai/dsh-agent-instructions',planMode:'@deepseek-ai/dsh-plan-mode',runtimeContext:'@deepseek-ai/dsh-agent-loop',modelSelection:'@deepseek-ai/dsh-agent',workingDirectory:'@deepseek-ai/dsh-working-directory',sessionTitle:'@deepseek-ai/dsh-session-title-first-prompt-llm'};
  return Object.fromEntries(await Promise.all(Object.entries(modules).map(async([name,module])=>{
    const native=await import(pathToFileURL(hostRequire.resolve(module)).href);
    return [name,(name==='sessionTitle'?native.NATIVE_AUX_PROMPT_RENDER_API:native.NATIVE_PROMPT_RENDER_API)===1?1:0];
  })));
}

export async function apply(ctx,config={}){
  const configFile=config.configFile;
  if(typeof configFile!=='string'||!isAbsolute(configFile))throw Error('EXISTING_BRIDGE_PRIVATE_CONFIG_FILE_REQUIRED');
  const info=await lstat(configFile);
  if(!info.isFile()||info.isSymbolicLink()||(info.mode&0o077)!==0||![0,process.getuid()].includes(info.uid))throw Error('EXISTING_BRIDGE_CONFIG_MUST_BE_PRIVATE');
  const options=JSON.parse(await readFile(configFile,'utf8'));
  if(typeof options.token!=='string'||options.token.length<24)throw Error('EXISTING_BRIDGE_MACHINE_TOKEN_REQUIRED');
  if(typeof options.root!=='string'||!isAbsolute(options.root))throw Error('EXISTING_BRIDGE_ABSOLUTE_DATA_REQUIRED');
  const url=new URL(options.coreUrl??'http://127.0.0.1:18910');
  if(url.protocol!=='http:'||!['127.0.0.1','localhost','[::1]'].includes(url.hostname)||url.username||url.password||url.search||url.hash||url.pathname!=='/')throw Error('CORE_URL_MUST_BE_LOOPBACK_ORIGIN');
  const port=options.port??18766;
  if(!Number.isSafeInteger(port)||port<1024||port>65535)throw Error('INVALID_BRIDGE_PORT');
  // Resolve native helpers from the ACTUAL HOST executable, not the old snapshot dependencies.
  const hostRequire=createRequire(await realpath(process.argv[1]));
  const {defineTool}=await import(pathToFileURL(hostRequire.resolve('@deepseek-ai/dsh-tools')).href);
  const {COMPACTION_PRESENTATION_API:compactionPresentationApi}=await import(pathToFileURL(hostRequire.resolve('@deepseek-ai/dsh-compaction-basic')).href);
  const nativePromptRenderCapabilities=await loadNativePromptRenderCapabilities(hostRequire);
  const {tryLockExclusive}=await import(pathToFileURL(hostRequire.resolve('@deepseek-ai/node-addon-system/flock')).href);
  const hostPackage=JSON.parse(await readFile(hostRequire.resolve('@deepseek-ai/dsh-api-session-controller/package.json'),'utf8'));
  const callback=async(path,body,signal)=>{
    const response=await fetch(new URL(path,url),{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${options.token}`},body:JSON.stringify(body),signal:signal??AbortSignal.timeout(10000)});
    if(!response.ok)throw Error(`CORE_CALLBACK_HTTP_${response.status}`);
    return response.json();
  };
  const bridge=await ExistingDshBridge.open(ctx,{root:options.root,cwd:options.cwd,defineTool,dshVersion:hostPackage.version,compactionPresentationApi,nativePromptRenderCapabilities,
    acquireLease:root=>acquireExistingDataLease(root,tryLockExclusive),
    validationModel:options.validationModel==='loopback-fixture'?'loopback-fixture':undefined,
    callback:(body,signal)=>callback('/api/bridge/tool',body,signal)});
  const server=createBridgeServer(bridge,{token:options.token});
  try{await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(port,'127.0.0.1',resolve);});}
  catch(e){await bridge.close();throw e;}
  ctx.effect(()=>async()=>{await new Promise(resolve=>server.close(resolve));await bridge.close();});
}
export default {name,inject,apply};
