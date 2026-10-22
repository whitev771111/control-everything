// Project navigation uses the existing snapshot and a single task editor.
'use strict';
window.WorkspaceSessionList = (() => {
  function create({ root, read, taskId = null, projectId = null, agents = [], postJson = null, isBusy = () => false, onBusy = () => {}, isCurrent = () => true }) {
    let request = 0, writing = false, uncertain = false, actions = [];
    const node = (tag, text, cls = '') => {
      const value = document.createElement(tag); value.setAttribute('class', cls);
      if (text !== undefined) value.textContent = text;
      return value;
    };
    const date = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未提供';
    function render(items, message) {
      actions = [];
      root.replaceChildren(node('h3', '事项会话'));
      root.append(node('p', '围绕事项与 Agent 的交流记录。当前仅提供会话信息，未同步聊天正文。', 'section-note'));
      const refresh = node('button', '重新读取会话', 'button secondary'); refresh.type = 'button';
      refresh.disabled = writing;
      actions.push(refresh);
      refresh.addEventListener('click', load); root.append(refresh);
      const status = node('p', message, 'section-note'); status.setAttribute('role', 'status'); root.append(status);
      for (const session of items) {
        const row = node('article', undefined, 'context-row task-session-row');
        const copy = node('div', undefined, 'session-copy');
        copy.append(node('h4', session.title || '未命名会话'));
        const agent = agents.find(value => value.id === session.agent_id);
        copy.append(node('p', `${agent?.name || (session.agent_id ? 'Agent：' + session.agent_id : 'Agent 未登记')} · 来源：${session.source || '未知'}${session.source_instance ? ' · 实例：' + session.source_instance : ''}${session.legacy ? ' · 旧关联' : ''}`, 'section-note'));
        copy.append(node('p', session.external_id || '来源标识未提供', 'context-path'));
        copy.append(node('p', `最近交流：${date(session.last_seen_at)} · 活动状态未知`, 'section-note'));
        if (!taskId && session.task_id) copy.append(node('p', `所属事项：${session.task_id}`, 'context-path'));
        let url = null;
        try { const parsed = new URL(session.url); if (['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password) url = parsed.href; } catch {}
        row.append(copy);
        if (url) {
          const link = node('a', '打开原会话', 'button secondary'); link.setAttribute('href', url); link.setAttribute('target', '_blank'); link.setAttribute('rel', 'noopener noreferrer'); row.append(link);
        } else row.append(node('span', '暂无可用打开入口', 'section-note'));
        if (postJson && taskId && session.id && session.updated_at && !session.legacy) {
          const unlink = node('button', '解除此事项关联', 'button secondary'); unlink.type = 'button'; unlink.disabled = writing || uncertain;
          actions.push(unlink);
          unlink.addEventListener('click', async () => {
            if (writing || uncertain) return;
            if (isBusy()) { status.textContent = '请先完成当前操作，再解除会话关联。'; return; }
            if (!window.confirm(`确认解除“${session.title || session.external_id}”与当前事项的关联？外部聊天及其内容会保留。`)) return;
            writing = true; onBusy(true); render(items, '正在解除关联…');
            try {
              const result = await postJson('/api/workspace/sessions/' + encodeURIComponent(session.id) + '/unlink', {
                expected_task_id: taskId, expected_updated_at: session.updated_at });
              if (!isCurrent()) return;
              if (result?.id !== session.id || result.task_id !== null) throw new Error('response mismatch');
              await load();
            } catch (error) {
              if (!isCurrent()) return;
              uncertain = true;
              render(items, error.reason === 'session_changed' ? '关联已被其他窗口更新，未解除。请重新读取会话后核对。' : '解除结果未确认，请重新读取会话；不会自动重试。');
            } finally {
              writing = false; onBusy(false);
              if (isCurrent()) for (const action of actions) action.disabled = uncertain && action.textContent !== '重新读取会话';
            }
          });
          row.append(unlink);
        }
        root.append(row);
      }
    }
    async function load() {
      const current = ++request;
      render([], '正在读取会话…');
      try {
        const data = await read();
        if (current !== request || !isCurrent()) return;
        if (!Array.isArray(data?.items) || taskId && data.item?.id !== taskId) throw new Error('会话响应与事项不一致');
        const items = data.items.filter(value => value.task_id && (!taskId || value.task_id === taskId) && (!projectId || value.project_id === projectId));
        uncertain = false;
        render(items, items.length ? `${items.length} 条关联会话` : '尚未关联会话。');
        return true;
      } catch {
        if (current === request && isCurrent()) render([], '会话暂不可用，请重新读取。');
        return false;
      }
    }
    void load();
    return { refresh: load, dispose() { request++; } };
  }
  function createEditor({ root, item, agents = [], postJson, refresh, isCurrent = () => true, isBusy = () => false, onBusy = () => {}, onDirty = () => {} }) {
    let busy = false, uncertain = false;
    const node = (tag, text) => { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; return value; };
    const box = node('details'), heading = node('summary', '关联已有 Agent 会话'); box.append(heading); root.append(box);
    box.append(node('p', '登记你与 Agent 围绕本事项交流的会话。来源、来源实例和会话标识共同确定会话；实例留空使用默认来源。不会新建外部聊天或读取正文。'));
    const form = node('form'), fields = node('fieldset'), controls = {};
    for (const [key, label, limit, required] of [['source', '会话来源（如 codex、dsh）', 32, true], ['source_instance', '来源实例（可选）', 64, false], ['external_id', '外部会话标识', 256, true], ['title', '会话标题（可选）', 200, false], ['url', '会话网址（可选）', 2048, false]]) {
      const input = node('input'); input.id = 'sl-' + key; input.maxLength = limit; input.required = required; input.type = 'text';
      const field = node('label', label); field.setAttribute('class', 'field'); field.append(input); fields.append(field); controls[key] = input;
    }
    const agent = node('select'); agent.id = 'sl-agent';
    const empty = node('option', '暂不指定 Agent'); empty.value = ''; agent.append(empty);
    for (const value of agents.filter(value => value.enabled && (!value.project_ids?.length || value.project_ids.includes(item.project_id)))) {
      const choice = node('option', value.name); choice.value = value.id; agent.append(choice);
    }
    const owner = node('label', '交流对象'); owner.setAttribute('class', 'field'); owner.append(agent); fields.append(owner);
    const confirmed = node('input'); confirmed.id = 'sl-confirm'; confirmed.type = 'checkbox';
    const consent = node('label', '我确认这段会话属于当前事项，并将所填信息保存到本机工作台'); consent.setAttribute('class', 'session-confirm'); consent.append(confirmed);
    const submit = node('button', '确认关联会话'); submit.type = 'submit'; submit.setAttribute('class', 'button primary'); submit.disabled = true;
    const discard = node('button', '放弃关联草稿'); discard.type = 'button'; discard.setAttribute('class', 'button secondary');
    const status = node('p', '请填写来源和会话标识。'); status.id = 'sl-status'; status.setAttribute('role', 'status');
    fields.append(consent, submit, discard); form.append(fields, status); box.append(form);
    const sync = () => { fields.disabled = busy; submit.disabled = busy || uncertain || !confirmed.checked; };
    const changed = () => { onDirty(true); confirmed.checked = false; sync(); };
    for (const control of Object.values(controls)) control.addEventListener('input', changed);
    agent.addEventListener('change', changed); confirmed.addEventListener('change', () => { onDirty(true); sync(); });
    function clear() { for (const control of Object.values(controls)) control.value = ''; agent.value = ''; confirmed.checked = false; onDirty(false); sync(); }
    discard.addEventListener('click', () => { if (busy || isBusy()) return; if (window.confirm('放弃尚未保存的会话关联信息？')) { clear(); status.textContent = uncertain ? '上次保存结果未知，请先重新核对。' : '关联草稿已放弃。'; } });
    const check = node('button', '核对关联结果'); check.type = 'button'; check.setAttribute('class', 'button secondary'); box.append(check);
    check.addEventListener('click', async () => {
      if (busy || isBusy()) return;
      busy = true; sync();
      try { if (await refresh()) { uncertain = false; confirmed.checked = false; status.textContent = '已重新读取关联结果，请核对列表后再决定是否保存。'; } else status.textContent = '尚未核对成功，请稍后重试。'; }
      finally { busy = false; if (isCurrent()) sync(); }
    });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy || isBusy() || uncertain || !confirmed.checked) return;
      const body = Object.fromEntries(Object.entries(controls).map(([key, value]) => [key, value.value.trim()]));
      if (!body.source || !body.external_id) { status.textContent = '会话来源和外部会话标识不能为空。'; return; }
      if (Object.entries(controls).some(([key, control]) => body[key].length > control.maxLength || body[key].includes('\0'))) { status.textContent = '会话字段过长或包含无效字符。'; return; }
      if (body.url) {
        try { const url = new URL(body.url); if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw new Error('invalid'); }
        catch { status.textContent = '请输入不含账号密码的 http 或 https 会话网址。'; return; }
      }
      if (agent.value) body.agent_id = agent.value;
      busy = true; onBusy(true); sync();
      try {
        const result = await postJson('/api/workspace/tasks/' + encodeURIComponent(item.id) + '/sessions', body);
        if (!isCurrent()) return;
        if (result?.task_id !== item.id || result?.source !== body.source || result?.external_id !== body.external_id || result?.source_instance !== body.source_instance) throw new Error('response mismatch');
        clear(); status.textContent = '会话关联已保存。';
        if (!await refresh()) status.textContent = '关联已保存，但列表刷新失败，请核对关联结果。';
      } catch (error) {
        if (!isCurrent()) return;
        uncertain = !(error.status >= 400 && error.status < 500);
        status.textContent = error.reason === 'session_conflict' ? '会话已关联其他事项；草稿已保留，请核对来源实例和会话标识。' : uncertain ? '保存结果未知；草稿已保留，请先核对关联结果，不会自动重发。' : '关联未保存；草稿已保留，请检查填写内容。';
      } finally { busy = false; onBusy(false); if (isCurrent()) sync(); }
    });
    return { isUncertain: () => uncertain };
  }
  return { create, createEditor };
})();
(() => {
  const tabs = { tasks: '全部事项', attention: '待处理', sessions: '会话' };
  function create({ root, navigation, taskRoot, tasks, navigate, manage, manual, remove, readSessions, readTaskSessions, onSession, fetchJson, postJson }) {
    let snapshot = null, selected = null, tab = 'tasks', view = 'overview', managementRefresh = false;
    const sandboxStates = new Map(), sandboxRequests = new Map(), sandboxButtons = new Map();
    function sandboxEndpoint(item) {
      return '/api/workspace/projects/' + encodeURIComponent(item.id) + '/sandbox-preparation?directory_id=main';
    }
    function refreshSandbox(item) {
      const key = JSON.stringify(item.directories || []);
      const previous = sandboxStates.get(item.id);
      if (previous && previous.key === key && Date.now() - previous.sampledAt < 15000) return;
      if (sandboxRequests.has(item.id)) return;
      const token = { key };
      sandboxRequests.set(item.id, token);
      Promise.resolve().then(() => fetchJson(sandboxEndpoint(item)))
        .catch(() => ({ status: 'unknown' }))
        .then(value => {
          if (sandboxRequests.get(item.id) !== token) return;
          sandboxRequests.delete(item.id);
          sandboxStates.set(item.id, { ...value, key, sampledAt: Date.now() });
          updateSandboxButtons(item);
        });
    }
    let sessionScope = 'active', sessionKey = '', sessionPage = null, sessionRequest = 0, sessionReading = false, sessionError = '', sampleKey = '';
    const taskHome = taskRoot.parentElement;
    const node = (tag, cls, text) => {
      const value = ['svg', 'rect'].includes(tag) ? document.createElementNS('http://www.w3.org/2000/svg', tag) : document.createElement(tag);
      value.setAttribute('class', cls || '');
      if (text !== undefined) value.textContent = text;
      return value;
    };
    const button = (text, fn, cls = 'button secondary') => {
      const value = node('button', cls, text); value.type = 'button'; value.addEventListener('click', fn); return value;
    };
    const project = () => snapshot?.projects.items.find(item => item.id === selected);
    const blank = text => node('p', 'empty-state', text);
    const attention = item => (item.task_counts?.blocked || 0) + (item.task_counts?.review || 0);
    const origin = item => item.id.startsWith('cl-') ? '来自 Claude Code · 人工管理' : item.id.startsWith('oc-') ? '来自 OpenCode · 人工管理' : item.id.startsWith('cx-') || item.source?.kind === 'codex' ? '来自 Codex' : '手工项目';
    function open(id, next = 'tasks') {
      if (next === 'manage' && (selected !== id || tab !== next)) managementRefresh = true;
      selected = id; tab = next; navigate(`project/${encodeURIComponent(id)}/${next}`);
    }
    function renderNavigation() {
      navigation.replaceChildren();
      for (const item of snapshot?.projects.items || []) {
        if (item.archived && item.id !== selected) continue;
        const value = button(item.name, () => open(item.id), 'project-nav-button');
        value.dataset.initial = [...item.name][0] || 'P';
        value.setAttribute('aria-label', `${item.name} · ${origin(item)}`);
        value.title = `${item.name} · ${origin(item)}`;
        if (item.id.startsWith('cl-') || item.id.startsWith('oc-') || item.id.startsWith('cx-')) value.append(node('small', 'project-nav-origin', origin(item).split(' · ')[0]));
        if (view === 'project' && selected === item.id) value.setAttribute('aria-current', 'page');
        navigation.append(value);
      }
      if (!navigation.children.length) navigation.append(node('p', 'section-note', '尚无项目'));
    }
    function renderOverview() {
      const header = node('div', 'section-heading');
      const heading = node('div', 'project-collection-heading');
      heading.append(node('h2', '', '我的项目'));
      if (snapshot?.projects.status === 'available') heading.append(node('span', 'project-count', String(snapshot.projects.items.filter(item => !item.archived).length)));
      header.append(heading, button('添加项目', () => manage(null), 'button primary'));
      root.append(header);
      if (!snapshot) { root.append(blank('正在读取项目…')); return; }
      if (snapshot.projects.status !== 'available') { root.append(blank('项目读取失败，请刷新重试。')); return; }
      const items = snapshot.projects.items.filter(item => !item.archived);
      if (!items.length) {
        root.append(blank('选择已有项目加入工作台；也可以手工登记目录。'));
        root.append(button('选择已有项目', () => manage(null), 'button primary'));
        if (manual) root.append(button('手工添加目录', manual));
      }
      const grid = node('div', 'project-cards');
      for (const item of items) {
        const card = node('article', 'project-card');
        const heading = node('div', 'section-heading');
        const title = button(item.name, () => open(item.id), 'project-card-title'); title.dataset.initial = [...item.name][0] || 'P';
        title.setAttribute('aria-label', item.name);
        heading.append(title);
        if (attention(item)) heading.append(node('span', 'attention-count', `${attention(item)} 待处理`));
        card.append(heading);
        card.append(node('span', 'project-origin', origin(item)));
        card.append(node('p', 'project-note', item.note || '在这里整理事项，查看关联的开发会话。'));
        const distribution = node('svg', 'project-distribution'); distribution.setAttribute('aria-label', '任务状态分布');
        distribution.setAttribute('viewBox', '0 0 100 6'); distribution.setAttribute('preserveAspectRatio', 'none');
        let position = 0;
        for (const [state, label] of [['done', '完成'], ['doing', '进行中'], ['review', '待验收'], ['blocked', '阻塞'], ['todo', '待办']]) {
          const count = item.task_counts?.[state] || 0;
          if (!count) continue;
          const width = count / Math.max(1, item.task_counts?.total || 0) * 100;
          const segment = node('rect', `distribution-${state}`); segment.setAttribute('x', String(position)); segment.setAttribute('width', String(width)); segment.setAttribute('height', '6');
          segment.setAttribute('aria-label', `${label} ${count}`); distribution.append(segment); position += width;
        }
        if (item.task_counts?.total) card.append(distribution);
        const meta = node('div', 'project-card-meta');
        meta.append(node('span', 'project-task-count', `${item.task_counts?.total || 0} 项任务`));
        const states = [['doing', '进行中'], ['review', '待验收'], ['blocked', '阻塞'], ['done', '完成'], ['todo', '待办']];
        const distributionLabel = states.filter(([key]) => item.task_counts?.[key]).map(([key, label]) => `${label} ${item.task_counts[key]}`).join(' · ');
        if (distributionLabel) meta.append(node('span', 'project-state-caption', distributionLabel));
        if (item.manual_recorded) meta.append(node('span', '', `人工 · ${item.state}${typeof item.percent === 'number' ? ` ${item.percent}%` : ''}`));
        const actions = node('div', 'project-card-actions');
        if (fetchJson && postJson) actions.append(sandboxButton(item));
        actions.append(button('进入项目 →', () => open(item.id), 'text-button'));
        if (remove) actions.append(button('移出工作台', () => remove(item), 'text-button project-remove'));
        card.append(meta, actions); grid.append(card);
      }
      root.append(grid);
      const actions = node('div', 'form-actions project-collection-actions');
      actions.append(button('未归类事项', () => navigate('global')), button('管理与归档', () => navigate('projects')));
      actions.append(button('未归类会话', () => navigate('codex-sessions/projectless')), button('待核对会话', () => navigate('codex-sessions/unknown')));
      root.append(actions);
    }
    function changeSessions(scope) {
      sessionScope = scope; sessionKey = ''; sessionPage = null; sessionError = ''; sessionRequest++; render();
    }
    async function loadSessions(offset = 0) {
      const current = ++sessionRequest, key = sessionKey;
      sessionReading = true; sessionError = ''; render();
      try {
        const result = await readSessions({ ...(view === 'project' ? { project_id: selected } : {}), scope: sessionScope, offset, limit: 20 });
        if (current !== sessionRequest || key !== sessionKey) return;
        if (offset > 0 && sessionPage && (result.source_id !== sessionPage.source_id || result.revision !== sessionPage.revision)) {
          sessionReading = false; return loadSessions(0);
        }
        sessionPage = result;
      } catch (error) {
        if (current !== sessionRequest) return;
        sessionError = error.message || '读取失败';
      } finally {
        if (current === sessionRequest) { sessionReading = false; render(); }
      }
    }
    function renderSessions(item = null) {
      const key = JSON.stringify([item?.id || '', sessionScope]);
      if (key !== sessionKey) {
        sessionKey = key; sessionPage = null; sessionError = ''; sessionReading = false;
        if (readSessions) { void loadSessions(); return; }
      }
      if (!readSessions) { root.append(blank('尚未接入可用的会话来源。')); return; }
      const toolbar = node('div', 'session-toolbar');
      if (item) for (const [scope, label] of [['active', '未归档'], ['archived', '已归档']]) {
        const choice = button(label, () => changeSessions(scope), 'button secondary');
        choice.setAttribute('aria-pressed', String(sessionScope === scope)); toolbar.append(choice);
      }
      const retry = button('重新读取会话', () => loadSessions(0)); retry.disabled = sessionReading; toolbar.append(retry); root.append(toolbar);
      const available = sessionPage?.status === 'available' && sessionPage.complete !== false && !sessionError && !sessionReading && (!item || item.binding_available !== false) && (!snapshot?.codex_desktop || snapshot.codex_desktop.status === 'available');
      const status = node('p', 'section-note session-source-status'); status.setAttribute('role', 'status');
      status.textContent = sessionError ? `会话读取失败：${sessionError}。保留已读内容，暂不能新关联。` : sessionReading ? '正在读取会话…' : sessionPage?.status === 'stale' ? '来源暂不可用，显示上次读取内容；暂不能新关联。' : sessionPage?.status === 'not_configured' ? '尚未启用 Codex 项目与会话来源。' : sessionPage?.status !== 'available' ? '会话来源暂不可用，请检查来源设置。' : sessionPage.complete === false ? '来源读取受限，显示部分结果；暂不能新关联。' : `${sessionPage.total} 条会话 · 活动与完成状态未知`;
      root.append(status);
      if (sessionScope === 'projectless') root.append(node('p', 'section-note', '这些会话在 Codex 中明确未分配项目。'));
      if (sessionScope === 'unknown') root.append(node('p', 'section-note', '这些会话的项目归属未知或有冲突，需要在 Codex 中核对。'));
      for (const session of sessionPage?.items || []) {
        const row = node('article', 'context-row codex-session-row'), copy = node('div', 'session-copy');
        copy.append(node('h3', '', session.title || '未命名会话'));
        if (session.title_truncated) copy.append(node('span', 'badge', '标题节选'));
        const timestamp = Date.parse(session.updated_at);
        const time = Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '时间未知';
        copy.append(node('p', 'section-note', `${time} · ${session.archived ? '已归档' : '未归档'} · 活动未知`), node('p', 'context-path', session.cwd || '目录未知'));
        const choose = button('关联为新事项', () => onSession?.(session, item?.id || null), 'button secondary');
        choose.disabled = !available || session.binding_available === false || ['unknown', 'conflict'].includes(session.assignment_state);
        row.append(copy, choose); root.append(row);
      }
      if (sessionPage && !sessionPage.items.length) root.append(blank('当前范围没有会话。'));
      if (sessionPage) {
        const pager = node('div', 'pager'), previous = button('上一页', () => loadSessions(Math.max(0, sessionPage.offset - sessionPage.limit))), next = button('下一页', () => loadSessions(sessionPage.offset + sessionPage.limit));
        previous.disabled = sessionReading || !!sessionError || sessionPage.offset === 0;
        next.disabled = sessionReading || !!sessionError || sessionPage.status !== 'available' || sessionPage.offset + sessionPage.limit >= sessionPage.total;
        pager.append(previous, node('span', '', `${sessionPage.total ? sessionPage.offset + 1 : 0}–${Math.min(sessionPage.offset + sessionPage.items.length, sessionPage.total)} / ${sessionPage.total} 条`), next); root.append(pager);
      }
      if (!item?.read_only && item?.source?.kind !== 'codex') root.append(button('来源设置', () => navigate('settings')));
    }
    function renderProject() {
      const item = project();
      if (!item) { root.append(blank(snapshot ? '项目已不存在，请从左侧重新选择。' : '正在读取项目…')); return; }
      const header = node('div', 'section-heading'), identity = node('div', 'project-identity'), actions = node('div', 'project-heading-actions');
      identity.append(node('h2', '', item.name), node('span', 'project-origin', origin(item)));
      if (item.read_only || item.source?.kind === 'codex') {
        identity.append(node('span', 'badge', 'Codex · 只读'));
        actions.append(button('来源设置', () => navigate('settings')));
      } else actions.append(button('项目设置', () => manage(item)));
      if (remove) {
        const action = button('移出工作台', () => remove(item), 'text-button project-remove');
        action.title = '保留事项与历史，之后可重新加入';
        actions.append(action);
      }
      header.append(identity, actions);
      header.setAttribute('class', 'section-heading project-heading');
      root.append(header);
      const navigation = node('nav', 'project-tabs'); navigation.setAttribute('aria-label', '项目内容');
      for (const [key, label] of Object.entries(tabs)) {
        const value = button(label, () => open(item.id, key), 'project-tab');
        if (key === tab || tab === 'manage' && key === 'tasks' || tab === 'session-import' && key === 'sessions') value.setAttribute('aria-current', 'page');
        navigation.append(value);
      }
      root.append(navigation);
      if (tab === 'tasks' || tab === 'attention' || tab === 'manage') {
        root.append(taskRoot); taskRoot.hidden = false;
        const reading = tasks.setContext({ projectId: item.id, scope: tab === 'attention' ? 'attention' : 'all' });
        if (managementRefresh) { managementRefresh = false; if (!reading) tasks.refresh?.(0); }
      } else if (tab === 'sessions') {
        root.append(button('从工具导入会话', () => open(item.id, 'session-import')));
        if (!readTaskSessions) { root.append(blank('尚未接入事项会话接口。')); return; }
        const panel = node('section', 'agent-detail-section'); root.append(panel);
        const id = item.id, version = sessionRequest;
        window.WorkspaceSessionList.create({ root: panel, projectId: id,
          read: () => readTaskSessions ? readTaskSessions(id) : Promise.reject(new Error('unavailable')),
          isCurrent: () => view === 'project' && tab === 'sessions' && selected === id && sessionRequest === version && panel.parentElement === root });
      } else if (tab === 'session-import') {
        root.append(node('p', 'section-note', '以下是工具来源中的会话。关联事项前，不计入事项会话列表。'));
        renderSessions(item);
      }
    }
    function updateSandboxButtons(item) {
      const state = sandboxStates.get(item.id);
      for (const control of sandboxButtons.get(item.id) || []) {
        control.textContent = state?.status === 'ready' ? '沙箱已准备' : state?.status === 'required' ? '准备沙箱' : '检查沙箱';
        control.disabled = !state || state.status === 'ready' || sandboxRequests.has(item.id);
      }
    }
    function sandboxButton(item) {
      const control = button('检查沙箱', () => prepareSandbox(item), 'text-button');
      const controls = sandboxButtons.get(item.id) || new Set();
      for (const previous of controls) if (previous.isConnected === false) controls.delete(previous);
      controls.add(control); sandboxButtons.set(item.id, controls);
      updateSandboxButtons(item);
      refreshSandbox(item);
      return control;
    }
    async function prepareSandbox(item) {
      if (!fetchJson || !postJson || sandboxRequests.has(item.id)) return;
      const token = { key: JSON.stringify(item.directories || []) };
      sandboxRequests.set(item.id, token);
      updateSandboxButtons(item);
      try {
        const status = await fetchJson(sandboxEndpoint(item));
        if (!status?.confirmation_hash || !status.canonical_path) throw new Error('无法核对项目目录，请检查后重试。');
        if (status.status === 'ready') {
          sandboxStates.set(item.id, { ...status, key: JSON.stringify(item.directories || []), sampledAt: Date.now() });
          return;
        }
        if (!window.confirm(`将为当前用户补充此目录的沙箱准备权限：${status.canonical_path}。不会修改文件内容。DSH 后续运行还会留下文件权限及低完整性标签，可能允许同用户的其他低权限进程写入项目；恢复能力有限。确认准备？`)) return;
        const endpoint = '/api/workspace/projects/' + encodeURIComponent(item.id) + '/sandbox-preparation';
        const value = await postJson(endpoint, { confirmed: true, path: status.canonical_path,
          directory_id: status.directory_id, confirmation_hash: status.confirmation_hash });
        sandboxStates.set(item.id, { ...value, key: JSON.stringify(item.directories || []), sampledAt: Date.now() });
        window.alert('项目沙箱准备权限已核对。');
      } catch (error) {
        sandboxStates.set(item.id, { status: 'unknown', key: JSON.stringify(item.directories || []), sampledAt: Date.now() });
        window.alert('准备失败：' + error.message);
      } finally {
        sandboxRequests.delete(item.id);
        updateSandboxButtons(item);
      }
    }
    function render() {
      const active = document.activeElement;
      const focusLabel = ['project-tab', 'project-nav-button'].includes(active?.className) ? active.textContent : null;
      const editorFocus = active && taskRoot.contains(active) ? active : null;
      root.dataset.view = view;
      // Move the live editor out before rebuilding its presentation container.
      if (taskRoot.parentElement === root) taskHome.append(taskRoot);
      root.replaceChildren(); renderNavigation();
      if (view === 'overview') renderOverview();
      else if (view === 'project') renderProject();
      else if (view === 'codex-sessions') {
        root.append(node('h2', '', sessionScope === 'unknown' ? '待核对会话' : '未归类会话')); renderSessions();
      }
      else if (['tasks', 'attention', 'global'].includes(view)) {
        root.append(taskRoot); taskRoot.hidden = false;
        tasks.setContext({ scope: view === 'attention' ? 'attention' : view === 'global' ? 'global' : 'all' });
      }
      if (focusLabel) {
        const replacement = [...root.querySelectorAll('button'), ...navigation.querySelectorAll('button')].find(value => value.textContent === focusLabel);
        replacement?.focus({ preventScroll: true });
      } else if (editorFocus && taskRoot.parentElement === root) editorFocus.focus({ preventScroll: true });
    }
    return {
      open, sandboxButton,
      route(id, next) {
        if (next === 'manage' && (selected !== id || tab !== next)) managementRefresh = true;
        if (selected !== id || !['active', 'archived'].includes(sessionScope)) { sessionScope = 'active'; sessionKey = ''; sessionRequest++; }
        selected = id; tab = Object.hasOwn(tabs, next) || ['manage', 'session-import'].includes(next) ? next : 'tasks';
      },
      routeSessions(scope) { if (sessionScope !== scope) { sessionKey = ''; sessionRequest++; } sessionScope = scope === 'unknown' ? 'unknown' : 'projectless'; },
      show(next) { if (view !== next) { sessionRequest++; sessionKey = ''; } view = next; root.hidden = !['overview', 'project', 'tasks', 'attention', 'global', 'codex-sessions'].includes(view); render(); },
      update(value) {
        const stamp = JSON.stringify([value.codex_desktop?.source_id, value.codex_desktop?.sampled_at, value.codex_desktop?.status]);
        const changed = sampleKey !== stamp; sampleKey = stamp;
        for (const item of value.projects?.items || []) {
          const key = JSON.stringify(item.directories || []);
          if ((sandboxStates.has(item.id) && sandboxStates.get(item.id).key !== key)
              || (sandboxRequests.has(item.id) && sandboxRequests.get(item.id).key !== key)) {
            sandboxStates.delete(item.id); sandboxRequests.delete(item.id);
          }
        }
        snapshot = value;
        if (changed && sessionKey && readSessions && (view === 'codex-sessions' || (view === 'project' && tab === 'sessions'))) void loadSessions(0);
        else render();
      },
    };
  }
  function prepareChrome() {
    document.documentElement.dataset.theme = 'dark';
    const toggle = document.getElementById('mobile-menu-toggle'), content = document.getElementById('workspace-navigation');
    const setMenu = open => { toggle.setAttribute('aria-expanded', String(open)); content.dataset.open = String(open); };
    toggle.addEventListener('click', () => setMenu(toggle.getAttribute('aria-expanded') !== 'true'));
    content.addEventListener('click', event => { if (event.target.closest('button[data-view], .project-nav-button, a')) setMenu(false); });
    const nav = document.createElement('nav'); nav.className = 'navigation utilities-navigation'; nav.setAttribute('aria-label', '工具与设置');
    for (const name of ['agents', 'usage', 'news', 'settings']) nav.append(document.querySelector(`[data-view="${name}"]`));
    document.getElementById('project-navigation').after(nav);
    const groups = new Map();
    for (const element of document.querySelectorAll('.form-hint, #project-identity, #profile-path')) {
      const parent = element.parentElement;
      if (!groups.has(parent)) {
        const details = document.createElement('details'), summary = document.createElement('summary');
        details.className = 'technical-details'; summary.textContent = '详细说明';
        parent.append(details); details.append(summary); groups.set(parent, details);
      }
      groups.get(parent).append(element);
    }
  }
  window.WorkspaceProjectSpace = { create, prepareChrome };
})();
