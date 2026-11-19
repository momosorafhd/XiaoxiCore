'use strict';
(() => {
  if (document.body.dataset.page !== 'core-persona') return;
  const url = '/api/modules/dsh_bridge/prompt-policy';
  const form = document.getElementById('prompt-policy-form');
  const fields = document.getElementById('prompt-policy-fields');
  const tabs = document.getElementById('prompt-policy-tabs');
  const editor = document.getElementById('prompt-policy-editor');
  const status = document.getElementById('prompt-policy-state');
  const feedback = document.getElementById('prompt-policy-save-state');
  const reload = document.getElementById('prompt-policy-reload');
  const save = form.querySelector('[type=submit]');

  const standardNames = {
    'harness:identity': 'Harness 身份',
    'deployment:persona-prefix': '角色前缀',
    'deployment:persona-suffix': '角色后缀',
    'plan:policy': '计划模式指引',
  };
  const runtimeNames = {
    time: '时间', agentInstructions: '工作区指令', planMode: '计划模式',
    runtimeContext: '运行环境', modelSelection: '模型切换', workingDirectory: '工作目录', sessionTitle: '会话标题',
  };
  const partNames = {
    intro: '首次说明', replacementIntro: '替换说明', emptyReplacementIntro: '清空后的说明', compactIntro: '压缩后的说明',
    fileHeader: '文件标题', additionalHeader: '补充文件标题', applicabilityGlobal: '全局适用范围', applicabilityProject: '项目适用范围',
    additionalGuidance: '补充指导', updatedHeader: '更新标题', updatedGuidance: '更新指导', removedHeader: '移除标题',
    removedGuidance: '移除指导', budgetOmitted: '省略文件说明', budgetTruncatedItem: '单文件截断说明',
    budgetTruncated: '截断列表', budgetSummary: '预算汇总', entered: '进入计划模式', exited: '退出计划模式',
    initialIntro: '初始框架', cleared: '清空框架', changed: '变化说明', recovered: '目录恢复说明',
    system: '标题系统文案', inputIntro: '标题材料引导',
  };
  const groupNotes = {
    standard: '标准预设在相应场景下组合这些分段。计划指引只在会话进入计划模式时出现。',
    minimal: '极简预设使用一段完整的系统提示。角色卡放在系统提示中时，请使用自定义正文，或将角色卡移到会话首轮后再继承原文。',
    time: '原版按浏览器时区和时间间隔动态组合。编辑框从兼容模板起步，继承可恢复完整的原版行为。',
    agentInstructions: '这些说明围绕 DSH 读到的 AGENTS 指令文件，文件内容仍由 DSH 读取。',
    planMode: '文案随实际会话模式切换触发。',
    runtimeContext: '这里可改说明文字，运行环境的实际数据继续由 DSH 填入。',
    modelSelection: '这里可改切换提示，实际模型仍由 DSH 选择。',
    workingDirectory: '路径变量由 DSH 填入。{{from}} 和 {{to}} 带 JSON 引号，{{raw_from}} 和 {{raw_to}} 为原路径。',
    sessionTitle: '这两段用于自动生成标题。DSH 自行提供原始消息和标题目标长度。',
    compaction: '压缩指令用于生成会话摘要，检查点说明放在摘要之前。检查点的协议标记由 DSH 添加。',
  };

  let snapshot, readDraft, activeGroup = 'standard', intent, dirty = false, busy = false;
  function element(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function button(text, handler) {
    const item = element('button', text, 'secondary small');
    item.type = 'button';
    item.addEventListener('click', handler);
    return item;
  }
  function originalText(meta) {
    return meta.originalBody ?? meta.originalDefault ?? meta.originalText;
  }
  function own(object, key) {
    return object && Object.hasOwn(object, key) ? object[key] : undefined;
  }
  function originalPanel(meta) {
    const box = element('details', undefined, 'core-original-compare');
    box.append(element('summary', '查看 DSH 原文'));
    if (meta.originalSource) box.append(element('p', meta.originalSource, 'core-caption'));
    const original = originalText(meta);
    box.append(element('pre', original === undefined ? '此段由宿主提供原文。' : original || '（原文为空）'));
    if (meta.originalVariants) {
      const variants = element('details');
      variants.append(element('summary', '查看原文的动态分支'), element('pre', JSON.stringify(meta.originalVariants, null, 2)));
      box.append(variants);
    }
    return box;
  }
  function setBusy(value) {
    busy = value;
    fields.disabled = value;
    reload.disabled = value;
    form.setAttribute('aria-busy', String(value));
    save.disabled = value || !dirty;
  }
  function markDirty() {
    dirty = true;
    if (!busy) save.disabled = false;
    feedback.textContent = '有未保存的修改';
  }
  function showError(error) {
    const message = document.getElementById('message');
    if (message) message.hidden = false;
    notify(error.message, true);
  }

  // Inherited text is a visible starting point. Only an explicit edit or use
  // action creates an override, so untouched original text never gets frozen.
  function promptField(parent, {key, title, value, savedDraft, meta = {}, syntax, locked = false, lockReason, identify, onEdit}) {
    const card = element('section', undefined, 'prompt-policy-row prompt-editor-item');
    card.dataset.promptItem = key;
    const caption = element('div', undefined, 'prompt-editor-caption');
    const label = element('label', title);
    label.htmlFor = 'prompt-text-' + key;
    if (identify) label.append(element('small', identify));
    const badge = element('span', undefined, 'core-caption');
    caption.append(label, badge);
    const input = element('textarea');
    input.id = label.htmlFor;
    input.rows = 4;
    input.maxLength = 100000;
    input.readOnly = locked;
    const original = originalText(meta);
    const starter = meta.editorText ?? original ?? '';
    const variables = meta.variables || [];
    const used = [...starter.matchAll(/\{\{([a-z][a-z0-9_]*)\}\}/g)].map(match => match[1]);
    const syntaxDefault = meta.editorSyntax ?? (syntax && used.length && used.every(name => variables.includes(name)) ? syntax : 'literal');
    const kind = syntax && element('select');
    if (kind) {
      kind.setAttribute('aria-label', title + '的文字语法');
      for (const [id, name] of [['literal', '原样文字'], [syntax, '替换模板变量']]) {
        const option = element('option', name);
        option.value = id;
        kind.append(option);
      }
      kind.disabled = locked;
      kind.addEventListener('change', changed);
    }
    const actions = element('div', undefined, 'actions prompt-editor-actions');
    const useOriginal = button(meta.editorText === undefined ? '填入原文' : '填入起始模板', () => {
      input.value = starter;
      if (kind) kind.value = syntaxDefault;
      changed();
      input.focus();
    });
    useOriginal.disabled = locked || original === undefined && meta.editorText === undefined;
    const inherit = button('恢复继承', () => {
      reset();
      markDirty();
    });
    actions.append(useOriginal, inherit);
    const useDraft = savedDraft && button('填入已保存的草稿', () => {
      input.value = savedDraft.text;
      if (kind) kind.value = savedDraft.templateSyntax;
      changed();
      input.focus();
    });
    if (useDraft && !locked) actions.append(useDraft);
    if (kind) actions.append(kind);
    let custom = value !== undefined;
    input.value = value?.text ?? starter;
    if (kind) kind.value = value?.templateSyntax ?? syntaxDefault;
    if (!starter && !custom) input.placeholder = '直接输入自定义正文';
    function update() {
      badge.textContent = custom ? '自定义正文' : '继承 DSH 原文';
      inherit.disabled = !custom;
    }
    function changed() {
      custom = true;
      onEdit?.();
      update();
      markDirty();
    }
    function reset() {
      custom = false;
      input.value = starter;
      if (kind) kind.value = syntaxDefault;
      update();
    }
    input.addEventListener('input', changed);
    card.append(caption, input, actions);
    if (variables.length) card.append(element('p', '可用变量 ' + variables.map(name => '{{' + name + '}}').join('、'), 'core-caption'));
    if (locked) card.append(element('p', lockReason || '当前 DSH 缺少此项渲染接口。已有覆盖可恢复继承。', 'core-caption'));
    card.append(originalPanel(meta));
    parent.append(card);
    update();
    return {
      read() {
        return custom ? {text: input.value, templateSyntax: kind?.value ?? 'literal'} : undefined;
      },
      reset,
    };
  }

  function render(data) {
    const {policy, compaction} = data;
    const catalog = data.capabilities?.catalog || {};
    const caps = data.capabilities?.hostCapabilities || {};
    tabs.replaceChildren();
    editor.replaceChildren();
    const groups = new Map();
    const addGroup = (id, name) => {
      const pane = element('section', undefined, 'prompt-editor-group');
      pane.id = 'prompt-group-' + id;
      pane.dataset.promptGroup = id;
      pane.append(element('p', groupNotes[id], 'core-caption'));
      const trigger = button(name, () => selectGroup(id));
      trigger.dataset.promptTab = id;
      trigger.setAttribute('aria-controls', pane.id);
      groups.set(id, {pane, trigger});
      tabs.append(trigger);
      editor.append(pane);
      return pane;
    };
    function selectGroup(id) {
      activeGroup = id;
      for (const [key, {pane, trigger}] of groups) {
        pane.hidden = key !== id;
        trigger.setAttribute('aria-pressed', String(key === id));
      }
    }

    const standard = addGroup('standard', '标准系统提示');
    const standardMetadata = catalog.system || {};
    const savedRows = new Map(policy.standard.overrides.map(row => [row.id, row]));
    const standardFields = new Map();
    const editableIds = standardMetadata.editableNativeIds ?? Object.keys(standardNames);
    const readOnlySections = new Set(['ui:deliverable-file-references', 'harness:source', 'app:web-surface']);
    function protectedSection(id) {
      return readOnlySections.has(id) || /^(tool:|tools:|mcp:|team:|working-directory:|sandbox:|approval:|runtime-context:)/.test(id) ||
        ['mcp-resource-servers', 'context:file-reference', 'xixi:core:configured'].includes(id);
    }
    function addStandardField(parent, id) {
      const row = savedRows.get(id);
      const title = own(standardNames, id);
      const originalBody = id === 'deployment:persona-prefix' ?
        standardMetadata.originalPresets?.standard?.prefix : own(standardMetadata.originalSections, id);
      standardFields.set(id, promptField(parent, {
        key: id, title: title || id, identify: title && id, value: row?.enabled ? row : undefined,
        savedDraft: row?.enabled === false ? row : undefined,
        meta: {originalBody, originalSource: own(standardMetadata.sectionSources, id) ?? standardMetadata.originalSource,
          variables: standardMetadata.nativeVariableNames ?? []},
        syntax: 'dsh', locked: protectedSection(id), lockReason: '此项由 DSH 管理。已有覆盖可恢复继承。',
      }));
    }
    for (const id of editableIds) addStandardField(standard, id);

    const extra = element('details', undefined, 'core-original-compare');
    extra.append(element('summary', '其他已注册分段'));
    extra.append(element('p', '分段 ID 由 DSH 或安装的插件注册，并须出现在目标会话中。输入未知标识会导致会话装配报错。', 'core-caption'));
    const additionalRows = element('div');
    for (const id of savedRows.keys()) {
      if (!standardFields.has(id)) addStandardField(additionalRows, id);
    }
    const customId = element('input');
    customId.type = 'text';
    customId.maxLength = 200;
    customId.setAttribute('aria-label', '已注册的分段 ID');
    customId.placeholder = '已注册的分段 ID';
    function addCustomSection() {
      const id = customId.value.trim();
      if (!id || /[\u0000-\u001f]/.test(id)) {
        showError(new Error('请输入有效的分段 ID。'));
      } else if (protectedSection(id) || standardFields.has(id)) {
        showError(new Error('该分段已列出，或由 DSH 管理。'));
      } else {
        addStandardField(additionalRows, id);
        additionalRows.lastElementChild.querySelector('textarea').focus();
        customId.value = '';
      }
    }
    customId.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      addCustomSection();
    });
    const extraActions = element('div', undefined, 'actions prompt-editor-actions');
    extraActions.append(customId, button('添加已注册分段', addCustomSection));
    extra.append(extraActions, additionalRows);
    standard.append(extra);

    const fixed = element('details', undefined, 'core-original-compare');
    fixed.append(element('summary', 'DSH 管理的运行事实与协议'));
    for (const id of readOnlySections) fixed.append(element('p', id, 'core-caption'));
    standard.append(fixed);

    const minimal = addGroup('minimal', '极简系统提示');
    const minimalField = promptField(minimal, {
      key: 'minimal', title: '极简系统正文', syntax: 'dsh',
      meta: {...catalog.minimal, variables: standardMetadata.nativeVariableNames ?? []},
      value: policy.minimal.mode === 'override' ? policy.minimal : undefined,
    });

    const runtimeReaders = {};
    for (const [name, title] of Object.entries(runtimeNames)) {
      const group = addGroup(name, title);
      const current = policy.runtime[name] ?? {mode: 'inherit'};
      const meta = catalog[name] ?? {};
      const originalParts = meta.parts ?? {};
      const canDisable = ['time', 'agentInstructions', 'planMode'].includes(name);
      const isTime = name === 'time';
      let off = current.mode === 'off';
      const groupFields = new Map();
      const actions = element('div', undefined, 'actions');
      const offButton = button('停用本组提示', () => {
        off = true;
        for (const field of groupFields.values()) field.reset();
        updateGroup();
        markDirty();
      });
      const resume = button('恢复本组继承', () => {
        off = false;
        updateGroup();
        markDirty();
      });
      const groupState = element('span', undefined, 'core-caption');
      function updateGroup() {
        if (!canDisable) return;
        offButton.hidden = off;
        resume.hidden = !off;
        groupState.textContent = off ? '本组已停用，输入文字可重新启用' : '';
      }
      if (canDisable) {
        actions.append(offButton, resume, groupState);
        group.append(actions);
        updateGroup();
      }
      const parts = isTime ? ['time'] : [...new Set([...Object.keys(originalParts), ...Object.keys(current.templates || {})])];
      if (!parts.length) group.append(element('p', '宿主暂无可编辑的原文目录。', 'core-caption'));
      for (const part of parts) {
        const value = isTime ? current.mode === 'template' ? current : undefined : current.templates?.[part];
        const info = originalParts[part] ?? {};
        groupFields.set(part, promptField(group, {
          key: name + '-' + part, title: isTime ? '时间提示正文' : partNames[part] || part,
          value, meta: info, syntax: 'core', locked: caps[name] !== 1,
          onEdit() {
            off = false;
            updateGroup();
          },
        }));
      }
      runtimeReaders[name] = () => {
        if (off) return {mode: 'off'};
        if (isTime) {
          const custom = groupFields.get('time').read();
          return custom ? {mode: 'template', ...custom} : {mode: 'inherit'};
        }
        const templates = {};
        for (const [part, field] of groupFields) {
          const text = field.read();
          if (text !== undefined) templates[part] = text;
        }
        return Object.keys(templates).length ? {mode: 'template', templates} : {mode: 'inherit'};
      };
    }

    const compact = addGroup('compaction', '上下文压缩');
    const compactFields = {};
    for (const [key, title] of [['instruction', '压缩指令'], ['checkpoint', '检查点说明']]) {
      compactFields[key] = promptField(compact, {
        key: 'compaction-' + key, title, locked: caps.compaction !== 1,
        meta: catalog.compaction?.parts?.[key],
        value: Object.hasOwn(compaction, key) ? {text: compaction[key]} : undefined,
      });
    }
    selectGroup(groups.has(activeGroup) ? activeGroup : 'standard');

    readDraft = () => {
      const overrides = [];
      for (const [id, field] of standardFields) {
        const custom = field.read();
        if (custom) overrides.push({id, enabled: true, ...custom});
        else if (savedRows.get(id)?.enabled === false) overrides.push(savedRows.get(id));
      }
      const chosenMinimal = minimalField.read();
      const runtime = Object.fromEntries(Object.entries(runtimeReaders).map(([name, read]) => [name, read()]));
      const chosenCompact = {};
      for (const [key, field] of Object.entries(compactFields)) {
        const value = field.read();
        if (value !== undefined) chosenCompact[key] = value.text;
      }
      return {
        policy: {version: 1, standard: {overrides}, minimal: chosenMinimal ? {mode: 'override', ...chosenMinimal} : {mode: 'inherit'}, runtime},
        compaction: chosenCompact,
      };
    };
    form.hidden = false;
  }

  function showRevision() {
    status.textContent = '配置版本 ' + snapshot.revision;
  }
  async function load() {
    setBusy(true);
    try {
      const result = await api(url);
      if (!Number.isInteger(result.revision) || result.policy?.version !== 1) throw Error('未能读取有效的提示配置。');
      snapshot = {...result, compaction: result.compaction || {}};
      render(snapshot);
      showRevision();
      dirty = false;
      intent = null;
      feedback.textContent = '';
    } finally {
      setBusy(false);
    }
  }
  async function submit(event) {
    event.preventDefault();
    if (!snapshot || busy || !dirty) return;
    setBusy(true);
    try {
      const payload = {expectedRevision: snapshot.revision, ...readDraft()};
      const fingerprint = JSON.stringify(payload);
      if (intent?.fingerprint !== fingerprint) {
        intent = {fingerprint, requestId: globalThis.crypto?.randomUUID?.() || 'prompt-' + Date.now() + '-' + Math.random().toString(16).slice(2)};
      }
      const result = await api(url, 'POST', {...payload, requestId: intent.requestId});
      if (!Number.isInteger(result.revision)) throw Error('未收到有效的保存回执。');
      snapshot = {...snapshot, ...result, policy: result.policy ?? payload.policy, compaction: result.compaction ?? payload.compaction};
      render(snapshot);
      showRevision();
      dirty = false;
      intent = null;
      feedback.textContent = '已保存。新会话将采用这些提示。';
    } catch (error) {
      showError(new Error(error.message + ' 草稿仍在编辑框中。'));
    } finally {
      setBusy(false);
    }
  }
  async function start() {
    if (!await sessionReady()) return;
    form.addEventListener('submit', submit);
    reload.addEventListener('click', () => {
      if (dirty && !confirm('重新读取会清除未保存的修改，继续吗？')) return;
      load().catch(showError);
    });
    await load();
  }
  start().catch(error => {
    status.textContent = '读取提示配置失败';
    showError(error);
  });
})();
