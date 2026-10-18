// Project navigation uses the existing snapshot and a single task editor.
'use strict';
(() => {
  const tabs = { tasks: '全部事项', attention: '待处理', sessions: '会话' };
  function create({ root, navigation, taskRoot, tasks, navigate, manage, manual, remove, readSessions, onSession }) {
    let snapshot = null, selected = null, tab = 'tasks', view = 'overview';
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
      header.append(node('h2', '', '我的项目'), button('添加项目', () => manage(null), 'button primary'));
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
        if (item.note) card.append(node('p', 'project-note', item.note));
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
        card.append(distribution);
        const meta = node('div', 'project-card-meta');
        meta.append(node('span', 'badge', origin(item)));
        meta.append(node('span', '', `${item.task_counts?.total || 0} 项任务`));
        const states = [['doing', '进行中'], ['review', '待验收'], ['blocked', '阻塞'], ['done', '完成'], ['todo', '待办']];
        const distributionLabel = states.filter(([key]) => item.task_counts?.[key]).map(([key, label]) => `${label} ${item.task_counts[key]}`).join(' · ');
        if (distributionLabel) meta.append(node('span', 'project-state-caption', distributionLabel));
        if (item.manual_recorded) meta.append(node('span', '', `人工 · ${item.state}${typeof item.percent === 'number' ? ` ${item.percent}%` : ''}`));
        meta.append(button('进入项目 →', () => open(item.id), 'text-button'));
        if (remove) meta.append(button('移出工作台', () => remove(item), 'text-button'));
        card.append(meta); grid.append(card);
      }
      root.append(grid);
      const actions = node('div', 'form-actions');
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
      const header = node('div', 'section-heading');
      header.append(node('h2', '', item.name));
      header.append(node('span', 'badge', origin(item)));
      if (item.read_only || item.source?.kind === 'codex') {
        header.append(node('span', 'badge', 'Codex · 只读'), button('来源设置', () => navigate('settings')));
      } else header.append(button('项目设置', () => manage(item)));
      if (remove) header.append(button('移出工作台', () => remove(item)));
      header.setAttribute('class', 'section-heading project-heading');
      root.append(header);
      if (remove) root.append(node('p', 'section-note', '移出保留事项与历史，之后可重新加入。'));
      const navigation = node('nav', 'project-tabs'); navigation.setAttribute('aria-label', '项目内容');
      for (const [key, label] of Object.entries(tabs)) {
        const value = button(label, () => open(item.id, key), 'project-tab');
        if (key === tab) value.setAttribute('aria-current', 'page');
        navigation.append(value);
      }
      root.append(navigation);
      if (tab === 'tasks' || tab === 'attention') {
        root.append(taskRoot); taskRoot.hidden = false;
        tasks.setContext({ projectId: item.id, scope: tab === 'attention' ? 'attention' : 'all' });
      } else if (tab === 'sessions') {
        renderSessions(item);
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
      open,
      route(id, next) { if (selected !== id || !['active', 'archived'].includes(sessionScope)) { sessionScope = 'active'; sessionKey = ''; sessionRequest++; } selected = id; tab = Object.hasOwn(tabs, next) ? next : 'tasks'; },
      routeSessions(scope) { if (sessionScope !== scope) { sessionKey = ''; sessionRequest++; } sessionScope = scope === 'unknown' ? 'unknown' : 'projectless'; },
      show(next) { if (view !== next) { sessionRequest++; sessionKey = ''; } view = next; root.hidden = !['overview', 'project', 'tasks', 'attention', 'global', 'codex-sessions'].includes(view); render(); },
      update(value) {
        const stamp = JSON.stringify([value.codex_desktop?.source_id, value.codex_desktop?.sampled_at, value.codex_desktop?.status]);
        const changed = sampleKey !== stamp; sampleKey = stamp;
        snapshot = value;
        if (changed && sessionKey && readSessions && (view === 'codex-sessions' || (view === 'project' && tab === 'sessions'))) void loadSessions(0);
        else render();
      },
    };
  }
  function prepareChrome() {
    document.documentElement.dataset.theme = 'dark';
    const nav = document.createElement('nav'); nav.className = 'navigation utilities-navigation'; nav.setAttribute('aria-label', '工具与设置');
    for (const name of ['agents', 'usage', 'news', 'settings']) nav.append(document.querySelector(`[data-view="${name}"]`));
    document.getElementById('project-navigation').after(nav);
    document.querySelector('[data-view="projects"]').textContent = '项目管理';
    document.querySelector('[data-view="settings"]').textContent = '设置与导入';
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
