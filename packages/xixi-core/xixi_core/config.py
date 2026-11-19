"""Validated public configuration. Credentials only come from environment."""
import copy
from .prompt_policy import DEFAULT as DEFAULT_PROMPT_POLICY, normalize as normalize_prompt_policy
from zoneinfo import ZoneInfo
from .model_settings import bounded_id, normalize_binding

DEFAULT = {
    'threshold': 0.66, 'idleSeconds': 1800, 'sleepSeconds': 7200,
    'roleCard': '你是小希，主人的私人助手。保持诚实，必要时主动查阅原话与记忆。',
    'rules': '只处理主人私聊。新输入含真实时间与来源；工具错误可修正重试。没有自动记忆召回。',
    'timezone': 'Etc/UTC', 'historyLimit': 100, 'selectedOriginalLimit': 12,
    'handoffMaxChars': 12000, 'taskConcurrency': 2, 'stageSummaryMaxChars': 20000,
    'maintenanceAutoRecover': False, 'maintenanceAutoRetryLimit': 2, 'maintenanceRetryDelaySeconds': 60,
    'unknownAutoQuarantine': False, 'unknownQuarantineDelaySeconds': 30,
    'restartDrainSeconds':30, 'restartStopWaitSeconds':5, 'restartHealthTimeoutSeconds':15,
    'environmentDescription':'',
    'promptPolicy':copy.deepcopy(DEFAULT_PROMPT_POLICY),
    'sessionToolScopes': {
        'daily': ['library_search','library_list','library_get','library_glossary','library_diary','library_sources','library_chat_history','library_chat_search','library_schema','library_sql','library_record','library_revise','library_mark_delete','library_cancel_delete','send_message','memory_search','library_query','library_terms','library_read','history_query','history_read','todo_record','task_create','task_status','task_result_read','task_artifact_read','task_cancel','service_restart','session_new'],
        'work': ['library_search','library_list','library_get','library_glossary','library_diary','library_sources','library_chat_history','library_chat_search','library_schema','library_sql','library_record','library_revise','library_mark_delete','library_cancel_delete','memory_search','library_query','library_terms','library_read','history_query','history_read','task_status','task_result_read','task_artifact_read','task_artifact_write'],
        'idle': ['library_search','library_list','library_get','library_glossary','library_diary','library_sources','library_chat_history','library_chat_search','library_schema','library_sql','library_read','history_query','history_read'], 'dream': ['library_search','library_list','library_get','library_glossary','library_diary','library_sources','library_chat_history','library_chat_search','library_schema','library_sql','library_read','history_query','history_read']},
    'modelOptions': {'daily':{},'maintenance':{},'dream':{},'work':{}},
    'modelSettings': {'leisure': None},
    'models': {'daily': 'deepseek-v4-pro', 'maintenance': 'deepseek-v4-pro',
               'dream': 'deepseek-v4-pro', 'work': 'deepseek-v4-pro'},
}


def validate(patch, current=None):
    if not isinstance(patch, dict):
        raise ValueError('配置必须为对象')
    result = copy.deepcopy(DEFAULT)
    if current: result.update(copy.deepcopy(current))
    if set(patch) - set(DEFAULT):
        raise ValueError('未知配置项: ' + ', '.join(sorted(set(patch) - set(DEFAULT))))
    result.update(patch)
    result['promptPolicy']=normalize_prompt_policy(result['promptPolicy'])
    for key, lo, hi in [('threshold', .01, 1), ('idleSeconds', 1, 604800),
                         ('sleepSeconds', 1, 604800), ('historyLimit', 1, 1000),
                         ('selectedOriginalLimit', 0, 100), ('handoffMaxChars', 100, 100000),
                         ('taskConcurrency', 1, 8), ('stageSummaryMaxChars',100,100000), ('maintenanceAutoRetryLimit',0,10),
                         ('maintenanceRetryDelaySeconds',1,86400), ('unknownQuarantineDelaySeconds',0,86400),
                         ('restartDrainSeconds',0,3600), ('restartStopWaitSeconds',1,300), ('restartHealthTimeoutSeconds',1,300)]:
        val = result[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not lo <= val <= hi:
            raise ValueError(f'{key} 必须在 {lo}～{hi} 之间')
        if key != 'threshold' and not isinstance(val, int):
            raise ValueError(f'{key} 必须为整数')
    if result['sleepSeconds'] < result['idleSeconds']:
        raise ValueError('sleepSeconds 不能小于 idleSeconds')
    for key in ('roleCard', 'rules', 'timezone','environmentDescription'):
        if not isinstance(result[key], str) or len(result[key]) > 100000:
            raise ValueError(f'{key} 必须为字符串且长度合理')
    ZoneInfo(result['timezone'])
    if not isinstance(result['models'], dict) or set(result['models']) != set(DEFAULT['models']):
        raise ValueError('models 必须包含 daily/maintenance/dream/work')
    if any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in result['models'].values()):
        raise ValueError('模型名称无效')
    if not isinstance(result['modelOptions'],dict) or set(result['modelOptions'])!=set(DEFAULT['models']):raise ValueError('modelOptions需要daily/maintenance/dream/work')
    for options in result['modelOptions'].values():
        if not isinstance(options,dict) or set(options)-{'provider','reasoningEffort','agentPreset','maxTokens'}:raise ValueError('未知模型选项')
        if 'provider' in options and (not isinstance(options['provider'],str) or not options['provider'].strip() or len(options['provider'])>180 or any(ord(c)<32 for c in options['provider'])):raise ValueError('provider需要有效渠道标识')
        if 'reasoningEffort' in options:bounded_id(options['reasoningEffort'],'reasoningEffort',100)
        if 'agentPreset' in options and (not isinstance(options['agentPreset'],str) or options['agentPreset'] not in {'standard','minimal'}):raise ValueError('agentPreset只支持standard/minimal')
        if 'maxTokens' in options and (isinstance(options['maxTokens'],bool) or not isinstance(options['maxTokens'],int) or not 1<=options['maxTokens']<=1000000):raise ValueError('maxTokens必须为正整数且不超过1000000')
    settings=result['modelSettings']
    if not isinstance(settings,dict) or set(settings)!={'leisure'}:raise ValueError('modelSettings仅支持独立leisure槽')
    if settings['leisure'] is not None:settings['leisure']=normalize_binding(settings['leisure'])
    for key in ('maintenanceAutoRecover','unknownAutoQuarantine'):
        if not isinstance(result[key],bool): raise ValueError(key+' 必须为布尔值')
    scopes=result['sessionToolScopes']
    if not isinstance(scopes,dict) or set(scopes)!=set(DEFAULT['sessionToolScopes']): raise ValueError('sessionToolScopes 需要 daily/work/idle/dream')
    all_tools=set().union(*map(set,DEFAULT['sessionToolScopes'].values()))
    for kind,names in scopes.items():
        if not isinstance(names,list) or any(not isinstance(n,str) or n not in all_tools for n in names) or len(names)!=len(set(names)): raise ValueError('工具范围无效')
        if kind in {'idle','dream'} and set(names)&{'send_message','todo_record','task_create','task_artifact_write','task_cancel','service_restart','session_new','library_record','library_revise','library_mark_delete','library_cancel_delete'}: raise ValueError('整理会话不允许副作用工具')
        if kind!='work' and 'task_artifact_write' in names:raise ValueError('产物写入只适用于独立工作')
        if kind!='daily' and set(names)&{'task_cancel','service_restart','session_new'}:raise ValueError('维护控制只适用于日常会话')
    return result
