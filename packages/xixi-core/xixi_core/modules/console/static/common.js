'use strict';
globalThis.XIXI_ADMIN_BASE=new URL(document.currentScript.src).pathname.split('/static/')[0];
const $ = id => document.getElementById(id);
const errors = {protected_path:'该路径受保护，不允许读取或下载。',reader_unavailable:'服务器文件读取服务暂不可用，请稍后刷新。',file_changed:'文件正在变化，请重新打开。',missing_path:'目录不存在或不可访问，当前列表已保留。',unsafe_or_missing_path:'目录不存在或不可访问，当前列表已保留。',root_not_found:'这个目录入口不可用。',links_forbidden:'链接文件暂不支持查看。',revision_conflict:'内容已变化，请重新加载后再保存。',csrf_failed:'会话已变化，请刷新页面。',dsh_catalog_not_ready:'DSH 目录不可用，已保存的设置未改变。',catalog_selection_unavailable:'所选渠道或模型不在当前目录中。',unsupported_reasoning_effort:'模型不支持这个思考等级。',group_subscription_forbidden:'群聊不能使用该订阅渠道。',read_only:'当前目录只读。',already_exists:'目标名称已存在。',upload_publication_conflict:'目标文件已存在或发生变化，请检查目录。',insufficient_space:'剩余空间不足。',unsafe_filename:'文件名不符合要求。',protected_filename:'该名称受保护。',file_too_large:'文件太大，不能预览，请下载查看。',not_utf8:'不是 UTF-8 文本，请下载查看。',preview_too_large:'文件太大，不能预览，请下载查看。',preview_type_forbidden:'该文件请下载查看。',preview_requires_utf8:'不是 UTF-8 文本，请下载查看。'};
let csrf='';
function node(tag, content, cls) { const e=document.createElement(tag); if(content!==undefined)e.textContent=content; if(cls)e.className=cls; return e; }
function notify(message,bad=false){$('message').textContent=message; $('message').classList.toggle('bad',bad);}
function date(at){return at ? new Date(at*1000).toLocaleString() : '—';}
function label(title,control){const e=node('label',title);e.append(control);return e;}
function button(title,fn){const e=node('button',title,'secondary small');e.type='button';e.onclick=fn;return e;}
function option(select,value,title,disabled=false){const o=node('option',title);o.value=value;o.disabled=disabled;select.append(o);}
async function api(path,method='GET',value,signal){
 if(method!=='GET'&&!csrf&&!await sessionReady())throw new Error('请重新登录。');
 const headers={}; let body;
 if(method!=='GET')headers['X-CSRF-Token']=csrf;
 if(value instanceof Blob){body=value;headers['Content-Type']='application/octet-stream';}
 else if(value!==undefined){body=JSON.stringify(value);headers['Content-Type']='application/json';}
 const response=await fetch(path.startsWith((globalThis.XIXI_ADMIN_BASE||'')+'/')?path:(globalThis.XIXI_ADMIN_BASE||'')+path,{method,body,headers,signal,credentials:'same-origin',cache:'no-store'});
 const data=await response.json();
 if(response.status===401){window.location.replace(((globalThis.XIXI_ADMIN_BASE||'')+'/login'));throw new Error('请重新登录。');}
 if(!response.ok||data.success===false){const detail=typeof data.error==='object'?data.error:null,code=detail?.code||data.error||'operation_failed';const e=new Error(detail?.message||errors[code]||data.item?.error||('操作失败（'+code+'）。'));e.code=code;throw e;}
 return data;
}
async function perform(fn,success){try{await fn();if(success)notify(success);}catch(e){notify(e.message,true);}}
async function sessionReady(){const session=await api(((globalThis.XIXI_ADMIN_BASE||'')+'/v1/session'));if(!session.authenticated){window.location.replace(((globalThis.XIXI_ADMIN_BASE||'')+'/login'));return false;}csrf=session.csrf;return true;}
$('logout').onclick=()=>perform(async()=>{await api(((globalThis.XIXI_ADMIN_BASE||'')+'/v1/logout'),'POST',{});window.location.replace(((globalThis.XIXI_ADMIN_BASE||'')+'/login'));});
