"""Owner-editable presentation; no user prompt body is synthesized or migrated."""
import copy
import hashlib
import json
import re

DEFAULT = {'version':1,'standard':{'overrides':[]},'minimal':{'mode':'override','text':'','templateSyntax':'literal'},
           'runtime':{name:{'mode':'inherit'} for name in ('time','agentInstructions','planMode','runtimeContext','modelSelection','workingDirectory','sessionTitle')}}
PARTS={'agentInstructions':('intro','replacementIntro','emptyReplacementIntro','compactIntro','fileHeader','additionalHeader','applicabilityGlobal','applicabilityProject','additionalGuidance','updatedHeader','updatedGuidance','removedHeader','removedGuidance','budgetOmitted','budgetTruncatedItem','budgetTruncated','budgetSummary'),
       'planMode':('entered','exited'),'runtimeContext':('initialIntro','replacementIntro','cleared'),
       'modelSelection':('changed',),'workingDirectory':('changed','recovered'),'sessionTitle':('system','inputIntro')}


TIME_VARIABLES=('now_ms','now','time_zone','turn','step','previous_ms','elapsed_ms','browser_kind','browser_time_zones')
PART_VARIABLES={'fileHeader':('path',),'additionalHeader':('path',),'applicabilityProject':('scope',),
 'updatedHeader':('path',),'updatedGuidance':('path',),'removedHeader':('path',),'removedGuidance':('path',),
 'budgetOmitted':('paths',),'budgetTruncatedItem':('path','original_bytes','included_bytes'),
 'budgetTruncated':('items',),'budgetSummary':('max_bytes','details'),'entered':('active',),'exited':('active',)}


VARIABLES_BY_PRODUCER={
 'modelSelection':{'changed':('from','to','from_provider','from_model','to_provider','to_model')},
 'workingDirectory':{'changed':('from','to','raw_from','raw_to'),'recovered':('from','to','raw_from','raw_to')},
 'sessionTitle':{'system':('target_words','target_cjk_characters'),'inputIntro':()}}

def validate_variables(value,allowed):
    # Match the single-pass native renderer; literal templates bypass this.
    offset=0
    while True:
        start=value.find('{{',offset)
        if start<0:return
        match=re.match(r'\{\{([^{}]*)\}\}',value[start:])
        if match is None:
            if value.find('}}',start+2)>=0:raise ValueError('提示模板变量括号不完整')
            offset=start+2;continue
        name=match.group(1)
        if not re.fullmatch('[a-z][a-z0-9_]*',name) or name not in allowed:raise ValueError('提示模板包含未知变量：'+name)
        offset=start+len(match.group(0))

def exact(value,allowed):
    if not isinstance(value,dict) or set(value)-set(allowed):raise ValueError('提示装配配置包含不支持字段')
    return value

def text(value):
    if not isinstance(value,str) or len(value)>100000:raise ValueError('提示文字须为至多100000字的字符串')
    return value

def protected(name):
    return name.startswith(('tool:','tools:','mcp:','team:','working-directory:','sandbox:','approval:','runtime-context:')) or name in {'mcp-resource-servers','context:file-reference','xixi:core:configured'}

def choice(value,runtime=False):
    exact(value,('mode','text','templateSyntax'));mode=value.get('mode','inherit')
    if mode not in (('inherit','off','template') if runtime else ('inherit','override')):raise ValueError('提示覆盖模式无效')
    if mode in ('inherit','off'):
        if set(value)-{'mode'}:raise ValueError('继承或关闭时不能另含覆盖正文')
        return {'mode':mode}
    syntax=value.get('templateSyntax','literal')
    if syntax not in (('literal','core') if runtime else ('literal','dsh')):raise ValueError('提示模板语法无效')
    body=text(value.get('text'))
    if runtime and syntax=='core':validate_variables(body,TIME_VARIABLES)
    return {'mode':mode,'text':body,'templateSyntax':syntax}

def normalize(value):
    exact(value,('version','standard','minimal','runtime'))
    if isinstance(value.get('version'),bool) or value.get('version')!=1:raise ValueError('提示装配只支持version=1')
    standard=exact(value.get('standard',{}),('overrides',));rows=standard.get('overrides',[])
    if not isinstance(rows,list) or len(rows)>128:raise ValueError('标准分段覆盖须为至多128项数组')
    overrides=[];seen=set()
    for row in rows:
        exact(row,('id','text','enabled','templateSyntax'));name=row.get('id');enabled=row.get('enabled');syntax=row.get('templateSyntax','literal')
        if not isinstance(name,str) or not name or len(name)>200 or any(ord(c)<32 for c in name) or name in seen or not isinstance(enabled,bool):raise ValueError('标准分段名称或启用状态无效')
        if protected(name):raise ValueError('工具协议和真实运行事实不能作为角色分段覆盖')
        if syntax not in ('literal','dsh'):raise ValueError('标准分段语法无效')
        seen.add(name);overrides.append({'id':name,'text':text(row.get('text')),'enabled':enabled,'templateSyntax':syntax})
    runtime=exact(value.get('runtime',{}),('time','agentInstructions','planMode','runtimeContext','modelSelection','workingDirectory','sessionTitle'))
    output={'version':1,'standard':{'overrides':overrides},'minimal':choice(value.get('minimal',{'mode':'inherit'})),
            'runtime':{'time':choice(runtime.get('time',{'mode':'inherit'}),True)}}
    for name,parts in PARTS.items():
        row=exact(runtime.get(name,{'mode':'inherit'}),('mode','templates'));mode=row.get('mode','inherit')
        if mode not in (('inherit','template') if name in {'runtimeContext','modelSelection','workingDirectory','sessionTitle'} else ('inherit','off','template')):raise ValueError('运行注入模式无效；真实运行事实不能关闭')
        if mode!='template':
            if 'templates' in row:raise ValueError('非模板模式不能另含正文')
            output['runtime'][name]={'mode':mode};continue
        templates=exact(row.get('templates'),parts);mapped={}
        for part,template in templates.items():
            exact(template,('text','templateSyntax'));syntax=template.get('templateSyntax','literal')
            if syntax not in ('literal','core'):raise ValueError('运行文案模板语法无效')
            body=text(template.get('text'))
            if syntax=='core':validate_variables(body,VARIABLES_BY_PRODUCER.get(name,{}).get(part,PART_VARIABLES.get(part,())))
            mapped[part]={'text':body,'templateSyntax':syntax}
        output['runtime'][name]={'mode':mode,'templates':mapped}
    if len(json.dumps(output,ensure_ascii=False))>500000:raise ValueError('提示装配配置总量过大')
    return output

def snapshot(config):return normalize(copy.deepcopy(config.get('promptPolicy',DEFAULT)))

class PromptPolicyMixin:
    def prompt_policy(self):
        state=self.configuration();method=getattr(self.runtime,'prompt_policy_capabilities',None)
        try:capabilities=method() if callable(method) else {'version':1,'hostCapabilities':{},'catalog':{},'unavailable':True}
        except Exception:capabilities={'version':1,'hostCapabilities':{},'catalog':{},'unavailable':True}
        return {'revision':state['version'],'policy':snapshot(state['config']),'capabilities':capabilities,'effect':'new_sessions_and_new_defined_tasks','existingSessions':'frozen; no prompt text migrated'}
    def update_prompt_policy(self,data,request_id=None):
        exact(data,('expectedRevision','policy'));expected=data.get('expectedRevision')
        if isinstance(expected,bool) or not isinstance(expected,int) or expected<1:raise ValueError('expectedRevision需要当前版本')
        if not isinstance(request_id,str) or not request_id or len(request_id)>300:raise ValueError('requestId需要稳定且有界的编号')
        policy=normalize(data.get('policy'));fingerprint=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=True,separators=(',',':')).encode()).hexdigest()
        receipt='prompt-policy-request:'+hashlib.sha256(request_id.encode()).hexdigest()
        def replay():
            old=self.store.get(receipt)
            if old:
                if old['fingerprint']!=fingerprint:raise ValueError('同一requestId的提示配置内容冲突')
                return copy.deepcopy(old['result'])
        with self.config_lock,self.store.lock:
            old=replay()
            if old:return old
            if self.store.get('config_version',1)!=expected:raise ValueError('提示配置版本冲突，请重新读取')
        required=[name for name,row in policy['runtime'].items() if row['mode']=='template']
        if required:
            caps=self.prompt_policy()['capabilities'].get('hostCapabilities',{})
            if any(caps.get(name)!=1 for name in required):raise ValueError('对应原生文案渲染接口尚未安装，未保存此配置')
        from .config import validate
        with self.config_lock:
            with self.store.tx():
                old=replay()
                if old:return old
                if self.store.get('config_version',1)!=expected:raise ValueError('提示配置版本冲突，请重新读取')
                config=validate({'promptPolicy':policy},self.config);revision=expected+1
                self.store.put('config',config);self.store.put('config_version',revision);self.store.put('config_version:'+str(revision),config)
                self.log('prompt_policy.changed',{'version':revision})
                result={'revision':revision,'policy':policy,'effect':'new_sessions_and_new_defined_tasks'}
                self.store.put(receipt,{'fingerprint':fingerprint,'result':result})
            self.config=copy.deepcopy(config)
        return result
