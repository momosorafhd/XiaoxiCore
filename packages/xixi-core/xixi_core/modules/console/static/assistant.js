'use strict';
(() => {
  const base = '/api/modules/assistant/';
  const actions = [...document.querySelectorAll('[data-action]')];
  const states = {running: '运行中', stopping: '停机中', not_started: '未启动'};
  const work = {chat: '处理消息', dream: '做梦整理', handoff: '换聊整理', startup_unread: '启动补收'};
  let busy = false, refreshing = false, promptLoaded = false;
  let actionError = '';
  function describe(error) {
    return typeof error === 'string' ? error : error?.message || error?.code || '';
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const state = await api(base + 'status');
      $('assistant-state').textContent = states[state.state] || state.state;
      $('assistant-session').textContent = '当前会话　' + (state.session_id || '尚无会话');
      $('assistant-work').textContent = (work[state.activity] || state.activity || '空闲') +
        '　·　排队消息 ' + state.queue_depth + ' 条　·　上次做梦 ' + date(state.last_dream_at);
      const error = actionError || describe(state.last_error) || describe(state.last_archive_error);
      $('assistant-error').textContent = error;
      $('assistant-error').hidden = !error;
      const recovery = state.startup_unread || {};
      const labels = {pulling:'正在查询历史', empty:'没有待补收消息', queued:'已按编号去重并入队', failed:'补收失败', cancelled:'已中断', first_start:'首次启动，无停机记录'};
      const phase = recovery.status || recovery.state;
      $('recovery-status').textContent = [labels[phase] || phase || '尚无补收记录',
        recovery.count ? recovery.count + ' 条' : '', recovery.window?.estimated ? '异常停机时间为估计值' : '', describe(recovery.error)].filter(Boolean).join('　·　');
      for (const control of actions) {
        const action = control.dataset.action;
        control.disabled = busy || (action === 'start' ? state.state !== 'not_started' :
          action === 'stop' ? state.state !== 'running' :
          action === 'startup-unread' ? state.state !== 'running' || !state.startup_unread_retry_available || !!state.activity :
          state.state !== 'running' || !!state.activity || state.queue_depth > 0);
      }
      if (!promptLoaded) {
        $('startup-text').value = (await api(base + 'startup-prompt')).text;
        promptLoaded = true;
        $('save-prompt').disabled = false;
      }
    } catch (error) {
      if (error.code === 'handler_unavailable' || error.code === 'module_not_found') {
        $('assistant-state').textContent = '未启动';
        $('assistant-session').textContent = '可在模块管理中启用助手插件。';
        $('assistant-work').textContent = '';
        $('assistant-error').hidden = true;
      } else {
        $('assistant-error').hidden = false;
        $('assistant-error').textContent = error.message;
      }
      actions.forEach(control => control.disabled = true);
      $('save-prompt').disabled = true;
      promptLoaded = false;
    } finally { refreshing = false; }
  }
  actions.forEach(control => control.onclick = async () => {
    if (busy) return;
    busy = true;
    actionError = '';
    actions.forEach(item => item.disabled = true);
    try { await api(base + control.dataset.action, 'POST', {}); }
    catch (error) { actionError = error.message; }
    finally { busy = false; await refresh(); }
  });
  $('startup-form').onsubmit = async event => {
    event.preventDefault();
    try {
      await api(base + 'startup-prompt', 'PUT', {text: $('startup-text').value});
      $('startup-saved').textContent = '已保存，下次补收使用。';
    } catch (error) { $('startup-saved').textContent = error.message; }
  };
  $('refresh-assistant').onclick = refresh;
  perform(async () => {
    if (!await sessionReady()) return;
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 2000);
  });
})();
