// Project navigation uses the existing snapshot and a single task editor.
'use strict';
(() => {
  const tabs = { overview: '概览', tasks: '任务', sessions: '会话', records: '成果与记录', materials: '资料' };
  function create({ root, navigation, taskRoot, tasks, navigate, manage, record }) {
    let snapshot = null, selected = null, tab = 'overview', view = 'overview';
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
    function open(id, next = 'overview') {
      selected = id; tab = next; navigate(`project/${encodeURIComponent(id)}/${next}`);
    }
    function renderNavigation() {
      navigation.replaceChildren();
      for (const item of snapshot?.projects.items || []) {
        if (item.archived && item.id !== selected) continue;
        const value = button(item.name, () => open(item.id), 'project-nav-button');
        value.dataset.initial = [...item.name][0] || 'P';
        value.setAttribute('aria-label', item.name);
        value.title = item.name;
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
      if (!items.length) root.append(blank('添加一个项目，或先记下一条全局事项。'));
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
        meta.append(node('span', '', `${item.task_counts?.total || 0} 项任务`));
        const states = [['doing', '进行中'], ['review', '待验收'], ['blocked', '阻塞'], ['done', '完成'], ['todo', '待办']];
        const distributionLabel = states.filter(([key]) => item.task_counts?.[key]).map(([key, label]) => `${label} ${item.task_counts[key]}`).join(' · ');
        if (distributionLabel) meta.append(node('span', 'project-state-caption', distributionLabel));
        if (item.manual_recorded) meta.append(node('span', '', `人工 · ${item.state}${typeof item.percent === 'number' ? ` ${item.percent}%` : ''}`));
        meta.append(button('进入项目 →', () => open(item.id), 'text-button'));
        card.append(meta); grid.append(card);
      }
      root.append(grid);
      const actions = node('div', 'form-actions');
      actions.append(button('全部事项', () => navigate('tasks')), button('未归类事项', () => navigate('global')), button('管理与归档', () => navigate('projects')));
      root.append(actions);
    }
    function renderProject() {
      const item = project();
      if (!item) { root.append(blank(snapshot ? '项目已不存在，请从左侧重新选择。' : '正在读取项目…')); return; }
      const header = node('div', 'section-heading');
      header.append(node('h2', '', item.name), button('项目设置', () => manage(item)));
      root.append(header);
      const navigation = node('nav', 'project-tabs'); navigation.setAttribute('aria-label', '项目内容');
      for (const [key, label] of Object.entries(tabs)) {
        const value = button(label, () => open(item.id, key), 'project-tab');
        if (key === tab) value.setAttribute('aria-current', 'page');
        navigation.append(value);
      }
      root.append(navigation);
      if (tab === 'tasks') {
        root.append(taskRoot); taskRoot.hidden = false;
        tasks.setContext({ projectId: item.id });
      } else if (tab === 'overview') {
        const panel = node('section', 'project-summary');
        panel.append(node('h3', '', '当前记录'), node('p', 'project-note', item.note || '还没有记录当前进展。'));
        if (item.manual_recorded) panel.append(node('p', 'section-note', `人工状态：${item.state}${typeof item.percent === 'number' ? ` · ${item.percent}%` : ''}`));
        panel.append(button('更新记录', () => record(item), 'text-button')); root.append(panel);
        const counts = node('div', 'project-counts');
        for (const [key, label] of [['todo', '待办'], ['doing', '进行中'], ['blocked', '阻塞'], ['review', '待验收'], ['done', '完成']]) {
          counts.append(button(`${label} ${item.task_counts?.[key] || 0}`, () => {
            open(item.id, 'tasks'); tasks.filterState(key);
          }));
        }
        root.append(counts);
      } else if (tab === 'sessions') {
        root.append(node('p', 'section-note', '当前接入来源中，工作目录属于本项目的会话。'));
        for (const hit of item.sessions || []) {
          const session = snapshot.sessions.items.find(row => row.session_key === hit.session_key);
          const row = node('article', 'context-row');
          row.append(node('h3', '', session?.title || hit.tool), node('p', 'context-path', hit.place));
          root.append(row);
        }
        if (!item.sessions?.length) root.append(blank(snapshot.sessions.available ? '当前样本中没有项目会话。' : '尚未接入可用的会话来源。'));
        root.append(button('工具与接入', () => navigate('agents')));
      } else if (tab === 'records') {
        root.append(button('人工记录与历史', () => record(item)));
        for (const entry of item.records || []) {
          const row = node('article', 'context-row');
          row.append(node('h3', '', [entry.feature, entry.label].filter(Boolean).join(' · ') || '来源记录'), node('p', '', entry.text || '来源未提供内容'));
          root.append(row);
        }
        if (!item.records?.length) root.append(blank('暂无来源记录；产物文件尚未接入。'));
        if (item.observation?.truncated) root.append(node('p', 'section-note', `来源摘要 ${item.records.length} / ${item.observation.total} 条`));
      } else {
        root.append(node('h3', '', '项目目录'));
        for (const directory of item.directories || []) {
          const row = node('article', 'context-row');
          row.append(node('p', 'context-path', directory.path)); root.append(row);
        }
        root.append(node('p', 'section-note', '展示已登记的目录；不扫描或上传本地文档。'), button('管理目录与来源', () => manage(item)));
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
      route(id, next) { selected = id; tab = Object.hasOwn(tabs, next) ? next : 'overview'; },
      show(next) { view = next; root.hidden = !['overview', 'project', 'tasks', 'attention', 'global'].includes(view); render(); },
      update(value) { snapshot = value; render(); },
    };
  }
  function prepareChrome() {
    const choice = document.getElementById('theme-choice');
    const apply = value => {
      document.documentElement.dataset.theme = ['light', 'dark'].includes(value) ? value : 'auto';
      choice.value = document.documentElement.dataset.theme;
    };
    try { apply(localStorage.getItem('workspace-theme')); } catch { apply('auto'); }
    choice.addEventListener('change', () => {
      apply(choice.value);
      try { localStorage.setItem('workspace-theme', choice.value); } catch { /* Preference storage is optional. */ }
    });
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
