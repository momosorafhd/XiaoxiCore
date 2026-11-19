"""Versioned, editable non-tool prompt presentation. Rendering never queries memory.

Tool schemas remain runtime-owned; non-tool output instructions are editable. Raw facts are passed
in by the caller; this module does not retrieve Library records or execute code.
"""
import json
import re
import string
from .store import encode, decode

KINDS = {'daily', 'work', 'idle', 'dream'}
PLACEMENTS = {'system', 'start', 'run'}
IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}$')
MAX_BODY = 100000
LEGACY = {'daily.role': 'roleCard', 'daily.rules': 'rules'}
MODE_IDS={'mode.standard':'standard','mode.minimal':'minimal'}
MODE_ORIGINALS={'standard':'You are a coding agent powered by the {{model}} model.',
                'minimal':'You are a helpful software engineer assistant.'}
VARIABLES = set('itemId itemCategory itemSummary itemTags itemStatus needsReview diaryId diaryVersion diaryTitle diaryBody selfUnderstanding ownerUnderstanding stageSummariesVersion environment previousError runtime timezone sessionKind sender sentAt receivedAt channel text source messageId replyId replyText replySentAt attachments handoff selectedOriginals day objective contextRefs sourceMessages existingMemories priorHandoff rangeFrom rangeTo cutoffAt taskId taskTitle taskStatus taskSummary resultRef artifactRefs truncationNote wakeTitle wakeText dueAt eventText restartSummary restartDetails faultSummary faultDetails recoverySummary recoveryDetails title body sourceRefs itemVersion reference attachmentName attachmentType attachmentRef'.split())


def definition(identifier, name, body, placement, kinds, category, condition='always', order=0, literal=False):
    return dict(id=identifier, name=name, body=body, placement=placement, sessionKinds=kinds,
                category=category, condition=condition, order=order, enabled=True, literal=literal)


ALL = ['daily', 'work', 'idle', 'dream']
DEFAULTS = [
    definition('mode.standard','标准模式系统提示','', 'system', ALL, 'mode', 'mode_only', order=5, literal=True),
    definition('mode.minimal','极简模式系统提示','', 'system', ALL, 'mode', 'mode_only', order=5, literal=True),
    definition('native.compaction.instructions', 'DSH原生压缩生成说明', '请将上方对话压缩为能继续交流的交接。保留主人的目标与明确要求、重要事实与原话、已形成的认识、当前话题、未完成工作及下一步；区分事实和推测，不补造遗漏。已有压缩交接要与后续变化合并，删除失效信息。只输出交接正文，不调用工具，不回应本条压缩要求。', 'system', ALL, 'format', 'format_only', order=910, literal=True),
    definition('native.compaction.checkpoint', 'DSH原生压缩交接说明', '这是对先前对话的自动压缩交接。将以下保留的信息作为既有背景，结合后续新消息继续交流；无需复述压缩过程。', 'system', ALL, 'format', 'format_only', order=920, literal=True),
    definition('dsh.system', 'DSH系统提示词（完整覆盖）', '这是小希的专用会话。角色身份、关系和表达方式依照本会话角色卡与规则；工作范围依照本会话任务说明。只使用实际开放的工具，依据真实结果和回执说明完成情况。', 'system', ALL, 'initial', order=0),
    definition('initial.intro', '系统初始介绍', '这是由 XiaoxiCore 组织的私人助手会话。角色身份、关系与表达方式以角色卡和规则为准。', 'start', ALL, 'initial', order=10),
    definition('initial.environment', '运行环境介绍', '当前运行环境：{runtime}\n配置时区：{timezone}\n{environment}', 'start', ALL, 'initial', order=20),
    definition('initial.library_deferred', '图书馆暂缓说明', '当前图书馆暂缓接入，原话仅保留为未归档临时缓存。图书馆、记忆与原话检索工具不可用，自动整理、做梦及换聊已暂停。请连续处理当前会话，仅使用实际开放的消息与工作工具，不声称已归档或已查询记忆。', 'system', ['daily','work'], 'initial', 'library_deferred', 25),
    definition('daily.role', '角色卡', '', 'system', ['daily'], 'role', order=10, literal=True),
    definition('daily.rules', '日常规则', '', 'system', ['daily'], 'role', order=20, literal=True),
    definition('handoff.summary', '上次会话交接', '上一段会话的交接：\n{handoff}', 'start', ['daily'], 'handoff', 'handoff', 30),
    definition('handoff.originals', '保留原话呈现', '需要保留的原话：\n{selectedOriginals}', 'start', ['daily'], 'handoff', 'originals', 40),
    definition('stage.summaries', '当前阶段认识', '自我认识：\n{selfUnderstanding}\n\n对主人的认识：\n{ownerUnderstanding}', 'start', ['daily'], 'initial', 'stage_summaries', 45),
    definition('message.owner', '主人消息正文', '{sender}，{sentAt}：\n{text}', 'run', ['daily'], 'message', 'owner', 100),
    definition('message.provenance', '输入来源与接收时间', '来源：{channel}；接收时间：{receivedAt}', 'run', ALL, 'message', 'input', 110),
    definition('message.reply', '引用消息呈现', '回复消息 {replyId}（{replySentAt}）：\n「{replyText}」', 'run', ALL, 'message', 'reply', 120),
    definition('message.attachments', '附件呈现', '附件：\n{attachments}', 'run', ALL, 'message', 'attachments', 130),
    definition('maintenance.output', '整理与做梦输出格式', 'Output protocol: Return JSON {handoff:string,selectedOriginals:string[],memories:[{id?:string,expectedVersion?:integer,category:"事实与经历"|"认识与判断"|"兴趣"|"黑话与名词解释"|"资料",title:string,body:string,summary?:string,tags?:string[],aliases?:string[],needsReview?:boolean,sourceRefs:string[]}],diary:{title:string,body:string,sourceRefs:string[]},stageSummaries?:{selfUnderstanding:string,ownerUnderstanding:string}}. Each memory must name one of the five parallel categories; 资料 requires an explicit summary. stageSummaries is optional; when updating it supply exactly selfUnderstanding and ownerUnderstanding as complete non-empty strings. Reference IDs must belong to the supplied fixed scope. Revisions require exact snapshot id/expectedVersion. No markdown wrapper.', 'system', ['idle','dream'], 'maintenance', order=900, literal=True),
    definition('maintenance.instructions', '闲时交接说明', '请回顾本批固定范围的对话，整理正在讨论的主题、已确定事项、未完成工作和继续交流所需的交接，并选择必要原话。依据原文，不编造遗漏事实。', 'start', ['idle'], 'maintenance', order=100),
    definition('dream.instructions', '做梦与日记说明', '请回顾 {day} 的固定范围对话，检查已有认识，形成或修订值得保留的记忆并写日记。区分原话事实与理解，不臆造记忆衰减、检索权重或分级算法。', 'start', ['dream'], 'maintenance', order=100),
    definition('dream.stage_summaries', '回顾阶段总结说明与当前内容', '回顾时检查两份当前阶段总结（版本 {stageSummariesVersion}）。若需要更新，请通过结果 stageSummaries 返回 selfUnderstanding 与 ownerUnderstanding 两份完整非空正文；不更新时保留当前版本。支撑事实、判断、兴趣及来源另存图书馆，不将这两份总结另建成图书馆分类。\n当前自我认识：\n{selfUnderstanding}\n\n当前对主人的认识：\n{ownerUnderstanding}', 'start', ['dream'], 'maintenance', order=105),
    definition('dream.recording_contract', '图书馆回顾写入说明', '整理条目使用事实与经历、认识与判断、兴趣、黑话与名词解释、资料五个并列分类，不另设上级或下级目录。形成新条目时填写 category；资料还必须填写独立 summary。修订已有条目使用材料中的编号 id 与版本 expectedVersion，并保留可核对的 sourceRefs。原始对话与日记独立保存；两份阶段总结通过 Core 的 stageSummaries 返回，不作为图书馆条目。', 'start', ['dream'], 'maintenance', order=106),
    definition('maintenance.materials', '整理固定范围材料', '本批范围：记录 {rangeFrom} 至 {rangeTo}，截止 {cutoffAt}\n既有交接：\n{priorHandoff}\n本批原始对话：\n{sourceMessages}\n既有记忆快照：\n{existingMemories}', 'start', ['idle', 'dream'], 'maintenance', order=110),
    definition('dream.existing_diary', '本日已保存日记', '本日已保存日记（{diaryId}，版本 {diaryVersion}）：\n{diaryTitle}\n{diaryBody}', 'start', ['dream'], 'maintenance', 'existing_diary', 115),
    definition('maintenance.retry', '整理校验失败后修正', '上次输出校验未通过：\n{previousError}\n请在同一固定范围内修正，不扩大来源。', 'start', ['idle', 'dream'], 'maintenance', 'previous_error', 120),
    definition('work.instructions', '独立工作说明', '请独立完成以下工作，并如实说明结果、产物、失败原因和尚未完成的部分。\n目标：\n{objective}\n必要上下文引用：\n{contextRefs}', 'start', ['work'], 'work', order=100),
    definition('event.task_result', '工作结果回报', '工作结果：{taskTitle}（{taskId}）\n状态：{taskStatus}\n{taskSummary}\n完整结果引用：{resultRef}\n产物：\n{artifactRefs}\n{truncationNote}', 'run', ['daily'], 'event', 'task_result', 100),
    definition('event.wake', '提醒与唤醒', '系统提醒：{wakeTitle}\n{wakeText}\n计划时间：{dueAt}', 'run', ['daily'], 'event', 'wake', 100),
    definition('event.restart', '重启恢复通知', '服务已恢复：\n{restartSummary}\n{restartDetails}', 'run', ['daily'], 'event', 'restart', 100),
    definition('event.fault', '故障通知', '系统故障提示：\n{faultSummary}\n{faultDetails}', 'run', ['daily'], 'event', 'fault', 100),
    definition('event.recovery', '故障后恢复说明', '系统恢复说明：\n{recoverySummary}\n{recoveryDetails}', 'run', ['daily'], 'event', 'recovery', 100),
    definition('event.notice', '其他系统事件', '系统事件：\n{eventText}', 'run', ['daily'], 'event', 'notice', 100),
    definition('format.sender.owner', '主人称呼呈现', '主人', 'run', ALL, 'format', 'format_only'),
    definition('format.original', '原话条目格式', '{sender}，{sentAt}（{source} / {messageId}）：\n{text}', 'start', ALL, 'format', 'format_only'),
    definition('format.attachment', '附件条目格式', '{attachmentName} {attachmentType} {attachmentRef}', 'run', ALL, 'format', 'format_only'),
    definition('format.memory', '既有记忆条目格式', '{title}\n{body}\n来源：{sourceRefs}\n版本：{itemVersion}', 'start', ['idle', 'dream'], 'format', 'format_only'),
    definition('format.memory_metadata', '整理条目分类与编号', '编号：{itemId}；分类：{itemCategory}；状态：{itemStatus}；待整理：{needsReview}\n标签：{itemTags}\n资料摘要：{itemSummary}', 'start', ['idle','dream'], 'format', 'format_only'),
    definition('format.reference', '引用与产物条目格式', '{reference}', 'run', ALL, 'format', 'format_only'),
    definition('event.truncation', '长结果预览说明', '本轮仅提供简要结果；完整内容保存在 {resultRef}，可按需继续读取。', 'run', ['daily'], 'format', 'format_only'),
]
BY_ID = {item['id']: item for item in DEFAULTS}


class Injections:
    def __init__(self, core):
        self.core, self.store = core, core.store
        self.clock = getattr(core, 'clock', lambda: 0)
        self.store.execute('''CREATE TABLE IF NOT EXISTS injection_templates(
            id TEXT PRIMARY KEY,name TEXT NOT NULL,body TEXT NOT NULL,enabled INTEGER NOT NULL,
            sort_order INTEGER NOT NULL,session_kinds TEXT NOT NULL,placement TEXT NOT NULL,
            version INTEGER NOT NULL,updated REAL NOT NULL)''')
        self.store.execute('''CREATE TABLE IF NOT EXISTS injection_versions(
            id TEXT NOT NULL,version INTEGER NOT NULL,snapshot TEXT NOT NULL,updated REAL NOT NULL,
            PRIMARY KEY(id,version))''')
        config = self._config()
        with self.store.tx():
            for item in DEFAULTS:
                if self.store.one('SELECT id FROM injection_templates WHERE id=?', (item['id'],)):
                    continue
                saved = dict(item)
                if item['id'] in LEGACY:
                    saved['body'] = config.get(LEGACY[item['id']], '')
                self._save(saved, 1)

    def _config(self):
        method = getattr(self.core, 'configuration', None)
        return method()['config'] if method else getattr(self.core, 'config', {})

    def _item(self, row):
        default = BY_ID.get(row['id'], {})
        literal = bool(default.get('literal'))
        variables = [] if literal else sorted({field for _, field, _, _ in string.Formatter().parse(row['body']) if field})
        return {'id': row['id'], 'name': row['name'], 'body': row['body'], 'template': row['body'],
                'enabled': bool(row['enabled']), 'order': row['sort_order'],
                'sessionKinds': decode(row['session_kinds']), 'placement': row['placement'],
                'version': row['version'], 'updatedAt': row['updated'],
                'effectScope': 'next_delivery' if row['placement'] == 'run' else 'new_session',
                'category': default.get('category', 'custom'), 'literal': literal,
                'configKey': LEGACY.get(row['id']), 'variables': variables,
                'availableVariables': sorted(VARIABLES),
                'group':'agent-mode' if row['id'] in MODE_IDS else 'common',
                'agentPreset':MODE_IDS.get(row['id']),
                'source':'core.configurable-agent-mode' if row['id'] in MODE_IDS else ('core.injection-default' if default else 'core.custom-injection'),
                'originalBody':MODE_ORIGINALS[MODE_IDS[row['id']]] if row['id'] in MODE_IDS else default.get('body',''),
                'originalSource':('DSH '+MODE_IDS[row['id']]+' preset reference') if row['id'] in MODE_IDS else ('Core packaged injection default' if default else None),
                'originalMetadataOnly':True}

    def list(self):
        return [self._item(row) for row in self.store.all('SELECT * FROM injection_templates ORDER BY sort_order,id')]

    def get(self, identifier):
        row = self.store.one('SELECT * FROM injection_templates WHERE id=?', (identifier,))
        if not row:
            raise ValueError('注入项不存在')
        return self._item(row)

    def history(self, identifier):
        self.get(identifier)
        return [decode(row['snapshot']) for row in self.store.all('SELECT snapshot FROM injection_versions WHERE id=? ORDER BY version DESC', (identifier,))]

    def _save(self, item, version):
        now = self.clock()
        self.store.execute('''INSERT INTO injection_templates VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,body=excluded.body,enabled=excluded.enabled,
            sort_order=excluded.sort_order,session_kinds=excluded.session_kinds,placement=excluded.placement,
            version=excluded.version,updated=excluded.updated''',
            (item['id'], item['name'], item['body'], int(item['enabled']), item['order'],
             encode(item['sessionKinds']), item['placement'], version, now))
        saved = self.get(item['id'])
        self.store.execute('INSERT INTO injection_versions VALUES(?,?,?,?)', (item['id'], version, encode(saved), now))
        return saved

    def set(self, data):
        if not isinstance(data, dict):
            raise ValueError('注入配置必须为对象')
        allowed = {'id', 'name', 'body', 'template', 'enabled', 'order', 'sessionKinds', 'placement', 'expectedVersion'}
        if set(data) - allowed:
            raise ValueError('未知注入配置字段')
        identifier = data.get('id')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('注入 ID 无效')
        if 'body' in data and 'template' in data and data['body'] != data['template']:
            raise ValueError('body 与 template 不一致')
        with self.store.tx():
            row = self.store.one('SELECT * FROM injection_templates WHERE id=?', (identifier,))
            old = self._item(row) if row else None
            expected = data.get('expectedVersion')
            if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected != (old['version'] if old else 0)):
                raise ValueError('注入版本冲突，请重新读取')
            item = dict(old or {'id': identifier, 'name': identifier, 'body': '', 'enabled': True,
                                'order': 1000, 'sessionKinds': ['daily'], 'placement': 'run'})
            for key in ('name', 'enabled', 'order', 'sessionKinds', 'placement'):
                if key in data:
                    item[key] = data[key]
            if 'body' in data or 'template' in data:
                item['body'] = data.get('body', data.get('template'))
            self._validate(item)
            keys = ('name', 'body', 'enabled', 'order', 'sessionKinds', 'placement')
            changed = not old or any(old[key] != item[key] for key in keys)
            saved = self._save(item, (old['version'] if old else 0) + 1) if changed else old
            if identifier in LEGACY:
                saved = dict(saved, configPatch={LEGACY[identifier]: saved['body']})
            return saved

    @staticmethod
    def _validate(item):
        if not isinstance(item['name'], str) or not item['name'].strip() or len(item['name']) > 120:
            raise ValueError('注入名称必须为 1～120 字符')
        if not isinstance(item['body'], str) or len(item['body']) > MAX_BODY:
            raise ValueError('注入正文最多 100000 字符')
        item['body'].encode('utf-8')
        if not isinstance(item['enabled'], bool):
            raise ValueError('enabled 必须为布尔值')
        if isinstance(item['order'], bool) or not isinstance(item['order'], int) or not -1000000 <= item['order'] <= 1000000:
            raise ValueError('order 必须为 -1000000～1000000 整数')
        if not isinstance(item['sessionKinds'], list) or not item['sessionKinds'] or len(item['sessionKinds']) > 4 or any(not isinstance(kind, str) or kind not in KINDS for kind in item['sessionKinds']) or len(set(item['sessionKinds'])) != len(item['sessionKinds']):
            raise ValueError('sessionKinds 只支持不重复的 daily/work/idle/dream')
        if item['placement'] not in PLACEMENTS:
            raise ValueError('placement 必须为 system/start/run')
        if item['id'] in MODE_IDS and (item['placement']!='system' or set(item['sessionKinds'])!=set(ALL)):
            raise ValueError('模式提示必须保持system位置和全部会话类型；实际装配由agentPreset独立筛选')
        if BY_ID.get(item['id'], {}).get('literal'):
            return
        try:
            for _, field, spec, conversion in string.Formatter().parse(item['body']):
                if field is not None and (field not in VARIABLES or spec or conversion):
                    raise ValueError('只允许已登记的简单变量，不允许属性访问或格式表达式')
        except ValueError as error:
            raise ValueError('注入模板格式无效：' + str(error)) from None

    def sync_legacy(self, patch):
        """Root calls after a successful legacy config write; no Core callback."""
        if not isinstance(patch, dict):
            return
        for identifier, key in LEGACY.items():
            if key in patch:
                self.set({'id': identifier, 'body': patch[key]})

    @staticmethod
    def _utf8_safe(value):
        # Only the final model-facing projection is escaped. Original event JSON,
        # result references and hashes remain untouched in their owning stores.
        return value.encode('utf-8', errors='backslashreplace').decode('utf-8')

    @staticmethod
    def _natural(value):
        if value is None:
            return ''
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return '\n'.join(Injections._natural(item) for item in value)
        if isinstance(value, dict):
            return '\n'.join(str(key) + ': ' + Injections._natural(item) for key, item in value.items())
        return str(value)

    def _format(self, identifier, values, fallback='', snapshot=None, trace=None):
        item = snapshot[identifier] if snapshot is not None else self.get(identifier)
        if trace is not None:
            trace[identifier] = item['version']
        if not item['enabled']:
            return fallback
        return (item['body'] if item.get('literal') else item['body'].format_map({key: self._natural(values.get(key, '')) for key in VARIABLES})).strip()

    def _originals(self, records, snapshot=None, trace=None):
        format_item = lambda identifier, values, fallback='': self._format(identifier, values, fallback, snapshot, trace)
        result = []
        for record in records if isinstance(records, list) else []:
            if isinstance(record, str):
                result.append(record)
                continue
            if not isinstance(record, dict):
                continue
            sender = record.get('sender') or record.get('source', '')
            if sender == 'owner' and record.get('source', 'owner') == 'owner':
                sender = format_item('format.sender.owner', {}, 'owner')
            values = dict(sender=sender, sentAt=record.get('sentAt', record.get('sent_at', '')),
                          source=record.get('source', ''), messageId=record.get('id', record.get('messageId', '')), text=record.get('text', ''))
            result.append(format_item('format.original', values, values['text']))
        return '\n\n'.join(result)

    def _references(self, references, snapshot=None, trace=None):
        format_item = lambda identifier, values, fallback='': self._format(identifier, values, fallback, snapshot, trace)
        return '\n'.join(format_item('format.reference', {'reference': self._natural(ref)}, self._natural(ref))
                         for ref in references if ref is not None) if isinstance(references, list) else self._natural(references)

    def _values(self, kind, input, context, snapshot=None, trace=None):
        format_item = lambda identifier, values, fallback='': self._format(identifier, values, fallback, snapshot, trace)
        config = self._config()
        source = input.get('source', 'owner') if input else ''
        if input and source not in {'owner', 'task_result', 'system'}:
            raise ValueError('input.source 无效')
        text = input.get('text', '') if input else ''
        if not isinstance(text, str):
            raise ValueError('input.text 必须为字符串')
        if input and not isinstance(input.get('attachments', []), list):
            raise ValueError('input.attachments 必须为数组')
        event = input.get('event') if input else None
        if not isinstance(event, dict):
            event = {}
        if input and source != 'owner' and isinstance(text, str):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    event = dict(parsed, **event)
            except (ValueError, TypeError):
                pass
        if not input:
            case = ''
        elif source in {'owner', 'task_result'}:
            case = source
        else:
            case = str(event.get('eventType') or event.get('type') or input.get('eventType', 'notice'))
        case = {'proactive_wake': 'wake', 'restart_recovered': 'restart', 'service_restarted': 'restart', 'restart_complete': 'restart', 'restart_recovery': 'restart', 'service_fault': 'fault', 'input_recovery': 'recovery'}.get(case, case)
        if input and case not in {'owner', 'task_result', 'wake', 'restart', 'fault', 'recovery'}:
            case = 'notice'
        reply = input.get('replyTo') or {} if input else {}
        if not isinstance(reply, dict):
            raise ValueError('input.replyTo 必须为对象')
        values = {key: '' for key in VARIABLES}
        values.update({key: self._natural(value) for key, value in context.items() if key in VARIABLES})
        values.update(runtime=getattr(getattr(self.core, 'runtime', None), 'mode', ''), timezone=config.get('timezone', ''), sessionKind=kind,
                      sender=format_item('format.sender.owner', {}, 'owner') if source == 'owner' and input.get('sender', 'owner') == 'owner' else input.get('sender', '') if input else '',
                      source=source, text=text, sentAt=input.get('sentAt', '') if input else '',
                      receivedAt=input.get('receivedAt', '') if input else '', channel=input.get('channel', '') if input else '',
                      messageId=input.get('id', input.get('inputId', '')) if input else '',
                      replyId=reply.get('messageId', ''), replyText=reply.get('text', ''), replySentAt=reply.get('sentAt', ''))
        attachments = []
        for attachment in input.get('attachments', []) if input else []:
            if isinstance(attachment, str):
                attachments.append(attachment)
            elif isinstance(attachment, dict):
                attachments.append(format_item('format.attachment', {
                    'attachmentName': attachment.get('name', attachment.get('filename', '')),
                    'attachmentType': attachment.get('type', attachment.get('mimeType', '')),
                    'attachmentRef': attachment.get('ref', attachment.get('url', attachment.get('id', '')))}, self._natural(attachment)))
        if not values['environment']:
            values['environment'] = config.get('environmentDescription', '')
        summaries = context.get('currentStageSummaries', {})
        if not isinstance(summaries, dict):
            raise ValueError('currentStageSummaries 必须为对象')
        values.update(selfUnderstanding=summaries.get('selfUnderstanding', ''),
                      ownerUnderstanding=summaries.get('ownerUnderstanding', ''),
                      stageSummariesVersion=summaries.get('version', 0))
        values['attachments'] = '\n'.join(attachments)
        for key in ('selectedOriginals', 'sourceMessages'):
            values[key] = self._originals(context.get(key, []), snapshot, trace)
        values['contextRefs'] = self._references(context.get('contextRefs', []), snapshot, trace)
        memories = []
        for memory in context.get('existingMemories', []) if isinstance(context.get('existingMemories', []), list) else []:
            if isinstance(memory, dict):
                memory_values = dict(title=memory.get('title', ''), body=memory.get('body', ''),
                    sourceRefs=self._references(memory.get('sourceRefs', []), snapshot, trace), itemVersion=memory.get('version', ''),
                    itemId=memory.get('id', ''), itemCategory=memory.get('category', memory.get('kind', '')),
                    itemSummary=memory.get('summary', ''), itemTags=self._references(memory.get('tags', []), snapshot, trace),
                    itemStatus=memory.get('status', ''), needsReview=self._natural(memory.get('needsReview', '')))
                metadata = format_item('format.memory_metadata', memory_values)
                body = format_item('format.memory', memory_values, self._natural(memory))
                memories.append('\n'.join(part for part in (metadata, body) if part))
        values['existingMemories'] = '\n\n'.join(memories)
        diary = context.get('existingDiary') or {}
        if not isinstance(diary, dict):raise ValueError('existingDiary 必须为对象或空值')
        values.update(diaryId=diary.get('id', ''), diaryVersion=diary.get('version', ''),
                      diaryTitle=diary.get('title', ''), diaryBody=diary.get('body', ''))
        fixed = context.get('fixedRange', {})
        if isinstance(fixed, dict):
            values.update(rangeFrom=fixed.get('fromSeq', ''), rangeTo=fixed.get('toSeq', ''), cutoffAt=fixed.get('cutoffAt', ''))
        values.update(taskId=event.get('taskId', context.get('taskId', '')), taskTitle=event.get('title', event.get('taskTitle', context.get('taskTitle', ''))),
                      taskStatus=event.get('status', context.get('taskStatus', '')), taskSummary=event.get('summary', text if case == 'task_result' and not event else context.get('taskSummary', '')), resultRef=event.get('resultRef', ''),
                      artifactRefs=self._references(event.get('artifactRefs', []), snapshot, trace), wakeTitle=event.get('title', ''),
                      wakeText=event.get('text', text if case == 'wake' else ''), dueAt=event.get('dueAt', ''))
        if event.get('summaryTruncated') or event.get('artifactRefsTruncated'):
            values['truncationNote'] = format_item('event.truncation', values)
        details = self._natural(event.get('details', ''))
        for prefix in ('restart', 'fault', 'recovery'):
            values[prefix + 'Summary'] = context.get(prefix + 'Summary', event.get('summary', event.get('message', event.get('text', text if case == prefix else ''))))
            values[prefix + 'Details'] = context.get(prefix + 'Details', details)
        values['eventText'] = self._natural(event) if event else text
        raw = text if case == 'owner' else event.get('summary', event.get('text', self._natural(event) if event else text))
        return values, case, raw

    def render_bundle(self, kind, input=None, context=None, agentPreset='standard'):
        if not isinstance(kind, str) or kind not in KINDS or (input is not None and not isinstance(input, dict)) or (context is not None and not isinstance(context, dict)):
            raise ValueError('kind/input/context 格式无效')
        if not isinstance(agentPreset,str) or agentPreset not in {'standard','minimal'}:raise ValueError('agentPreset只支持standard/minimal')
        context = dict(context or {})
        items = self.list()
        snapshot = {item['id']: item for item in items}
        format_versions = {}
        values, case, raw = self._values(kind, input, context, snapshot, format_versions)
        sections = []
        for item in items:
            if item['id'] in MODE_IDS and MODE_IDS[item['id']]!=agentPreset:continue
            if not item['enabled'] or kind not in item['sessionKinds']:
                continue
            condition = BY_ID.get(item['id'], {}).get('condition', 'always')
            include = condition in {'always','mode_only'} or condition == case and bool(input)
            include = include or condition=='library_deferred' and getattr(self.core,'library_mode',None)=='deferred'
            include = include or condition == 'handoff' and bool(values['handoff']) or condition == 'originals' and bool(values['selectedOriginals'])
            include = include or condition == 'stage_summaries' and bool(values['selfUnderstanding'] or values['ownerUnderstanding'])
            include = include or condition == 'existing_diary' and bool(values['diaryId'])
            include = include or condition == 'previous_error' and bool(values['previousError'])
            include = include or condition == 'input' and bool(input) or condition == 'reply' and bool(values['replyId']) or condition == 'attachments' and bool(values['attachments'])
            if not include:
                continue
            text = item['body'] if item['literal'] else item['body'].format_map({key: self._natural(values.get(key, '')) for key in VARIABLES})
            if text.strip():
                sections.append({key: item[key] for key in ('id', 'name', 'version', 'placement', 'effectScope')} | {'text': text, 'source': 'injection:' + item['id']})
        run = '\n\n'.join(item['text'] for item in sections if item['placement'] == 'run')
        # Original content is data, not a hidden instruction. Never silently drop
        # it when an operator disables a framing template or removes its slot.
        if input and raw and str(raw) not in run:
            sections.append({'id': 'runtime.data', 'name': '原始内容', 'version': 0, 'placement': 'run',
                             'effectScope': 'next_delivery', 'source': source if (source := input.get('source')) else 'owner', 'text': str(raw)})
        for section in sections:
            section['text'] = self._utf8_safe(section['text'])
        by_place = {placement: '\n\n'.join(item['text'] for item in sections if item['placement'] == placement) for placement in PLACEMENTS}
        native_compaction={purpose:self._format(identifier,values,snapshot=snapshot,trace=format_versions) for purpose,identifier in [('instruction','native.compaction.instructions'),('checkpoint','native.compaction.checkpoint')]}
        native_compaction['versions']={purpose:snapshot[identifier]['version'] for purpose,identifier in [('instruction','native.compaction.instructions'),('checkpoint','native.compaction.checkpoint')]}
        return {'nativeCompaction': native_compaction, 'kind': kind, 'promptSections': sections, 'versions': dict(format_versions, **{item['id']: item['version'] for item in sections}), 'formatVersions': format_versions,
                'systemText': by_place['system'], 'startText': by_place['start'], 'runText': by_place['run'],
                'presentationText': by_place['run'], 'compiledText': '\n\n'.join(by_place[p] for p in ('system', 'start', 'run') if by_place[p]),
                'automaticMemoryRecall': False}

    def preview(self, data=None):
        data = data or {}
        if not isinstance(data, dict):
            raise ValueError('preview 必须为对象')
        preset=data.get('agentPreset','standard')
        return dict(self.render_bundle(data.get('kind', 'daily'), data.get('input'), data.get('context'),agentPreset=preset),agentPreset=preset)
