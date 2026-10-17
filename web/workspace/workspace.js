'use strict';

const $ = id => document.getElementById(id);
const providerNames = { codex: 'Codex', claude: 'Claude Code', grok: 'Grok', dsh: 'DSH', opencode: 'OpenCode' };
const stateNames = { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待验收', done: '完成' };
const productNames = { subscription: '订阅', cli: 'CLI credits', api: 'xAI API', local: '本地工具' };
const statusNames = { not_checked: '未查询', available: '可用', unavailable: '不可用', stale: '旧缓存' };
const reasons = {
  directory_conflict: '这个目录已绑定到活动项目，请调整路径或先归档原项目', source_binding_missing: '来源仍引用待删除目录，请先更换来源目录',
  profile_not_empty: '当前 profile 已有记录，请使用另一个空 data-dir 导入', already_imported: '当前 profile 已导入过，请使用另一个空 data-dir',
  invalid_source: '请检查来源类型、绑定目录和相对文件路径', invalid_config: '请检查元数据库绝对路径与启用设置', invalid_import: '旧管理记录不符合导入合同，请检查错误字段',
  body_too_large: '提交超过 16 KiB，请缩短目录或来源路径', store_busy: '管理库正忙，请稍后重试', backup_failed: '备份未生成，导入没有提交，请稍后重试', storage_error: '管理数据保存失败，请保留草稿重试',
  workspace_unavailable: '工作台服务尚未就绪', storage_unavailable: '本地记录暂时无法读取或保存',
  task_not_found: '这条任务已不存在，请刷新后重试', record_not_found: '这条记录已不存在，请刷新后重试',
  session_not_in_sample: '所选会话不在当前样本中，请重新选择；原有草稿已保留',
  session_project_mismatch: '会话目录不属于所选项目，请检查项目与会话',
  invalid_task: '请检查任务名称、项目、工具与状态', invalid_record: '请检查日期、指标、数值与产品范围',
  invalid_request: '提交内容不符合要求，请检查必填项与字段范围',
  invalid_project: '所选项目当前未登记', invalid_body: '提交内容不符合格式要求',
  cli_not_found: '没有发现 Codex 命令', native_cli_not_found: '没有找到可用的 Codex 原生程序',
  probe_timeout: '额度查询超时', initialize_failed: 'Codex 只读接口初始化失败',
  rpc_error: 'Codex 接口未返回可用额度', invalid_response: '来源返回的数据无法识别',
  process_failed: 'Codex 只读查询未正常结束', timeout: '来源读取超时', http_error: '来源暂时无法访问',
  oversize: '来源响应超过读取上限', invalid_feed: '来源内容无法解析',
  codex_index_unavailable: 'Codex 身份索引暂时不可用', not_recorded: '未记录',
  invalid_record_source: '人工记录格式无效', source_unavailable: '来源暂时不可用',
  execution_unavailable: '本机执行暂未就绪，请查看运行准备条件', platform_unsupported: '当前主机不支持执行，仍可查看记录',
  context_changed: '任务或项目目录已变化，请核对新的准备摘要', project_busy: '这个项目目录或其上级、下级目录仍被运行占用',
  capacity_busy: '已有两个运行，请等待其中一个结束并完成清理', other_instance: '执行由另一个工作台实例持有，此处只读',
  idempotency_conflict: '原提交标识已有记录，正在查询原回执', request_not_found: '尚未查到原提交回执',
  run_not_found: '这条运行已不存在', approval_not_found: '这条审批已不存在',
  approval_expired: '这条审批已过期，请查看最新运行事实', decision_conflict: '这条审批已收到其他答复，请查看最新事实',
};
const viewCopy = {
  overview: ['总览', '今天，从这里开始。', '把项目、任务和工具，放回一个清晰的工作空间。', 'YOUR DEVELOPMENT DESK'],
  tasks: ['项目任务', '让下一步清楚一点。', '按项目整理任务，记录负责人、会话关联和手工状态。', 'PROJECTS & TASKS'],
  projects: ['项目与进度', '项目的三种事实，分别看清。', '人工判断、任务分布与文档来源各自保留。', 'PROJECTS & PROGRESS'],
  runs: ['运行', '让任务真正开始。', '显式提交给 Codex，查看过程、具体审批与实际结果。', 'OWNED CODEX RUNS'],
  agents: ['Agents', '工具各有所长。', '看清已经发现什么、可以观察什么，以及尚未接入的能力。', 'YOUR AGENT TOOLKIT'],
  usage: ['用量与额度', '每一份消耗，都有来源。', '订阅窗口、每日用量与人工观察，分开记录。', 'USAGE & LIMITS'],
  news: ['AI 资讯', '留一点时间，看向前沿。', '来自公开来源的标题与原文。按发布时间排序，保留抓取状态。', 'A WINDOW INTO AI'],
  settings: ['来源与导入', '让数据留在自己的空间。', '配置可选的元数据来源，或将旧管理记录导入空 profile。', 'PROFILE & SOURCES'],
};
const app = { view: 'overview', csrf: '', snapshot: null, usage: null, taskId: null, recordId: null, profileMode: false, profile: null, capabilities: {}, projectId: null, restoreId: null, manualId: null, historyRequest: 0, pages: { tasks: 0, records: 0, todos: 0, history: 0, compatibility: 0 } };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}
function empty(root, title, detail = '') {
  const node = el('div', 'empty-state');
  node.append(el('strong', '', title), el('p', '', detail));
  root.append(node);
}
function link(label, href, className = 'text-link') {
  const node = el('a', className, label);
  if (href === '/' || href === '/progress') node.href = href;
  else {
    try {
      const url = new URL(href);
      if (url.protocol !== 'https:' || url.username || url.password) return el('span', className, label);
      node.href = url.href; node.target = '_blank'; node.rel = 'noopener noreferrer';
    } catch { return el('span', className, label); }
  }
  return node;
}
function dateTime(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return '时间未知';
  return new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value));
}
function reason(value) { return reasons[value] || '来源暂时不可用，请稍后重试'; }
function projectName(id) { const project = app.snapshot?.projects.items.find(item => item.id === id); return project ? `${project.name}${project.archived ? '（已归档）' : ''}` : id ? `${app.profileMode ? '未登记项目 · ' : ''}${id}` : '未关联项目'; }
function message(id, text, failed = false) { $(id).textContent = text; $(id).classList.toggle('error', failed); }
function fillOptions(select, options, selected = '') {
  select.replaceChildren(...options.map(([value, label]) => {
    const option = el('option', '', label); option.value = value; return option;
  }));
  select.value = selected;
}
const apiError = (message, status, reason) => Object.assign(new Error(message), { status, reason });
async function api(path, options = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timer = setTimeout(() => controller.abort(), 40000);
  let status = null;
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { 'X-Session-Client': 'panel', ...options.headers } });
    status = response.status;
    const result = await response.json();
    if (!response.ok) throw Object.assign(apiError(reason(result?.reason), status, typeof result?.reason === 'string' ? result.reason : 'http_error'), { field: result.field, detail: result.message });
    return result;
  } catch (error) {
    if (Object.hasOwn(error, 'status') && typeof error.reason === 'string') throw error;
    if (error.name === 'AbortError') throw apiError('读取超时，请稍后重试', null, 'timeout');
    if (error instanceof SyntaxError) throw apiError('无法连接工作台服务，请检查服务是否已启动', status, 'invalid_response');
    if (error instanceof TypeError) throw apiError('无法连接工作台服务，请检查服务是否已启动', null, 'network_error');
    throw error;
  } finally { clearTimeout(timer); options.signal?.removeEventListener('abort', abort); }
}
function post(path, body, options = {}) {
  if (!app.csrf) return Promise.reject(apiError('保存凭据尚未就绪，请刷新页面后重试', null, 'csrf_unavailable'));
  return api(path, { ...options, method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-CSRF': app.csrf }, body: JSON.stringify(body) });
}

function showView(view) {
  if (!viewCopy[view]) view = 'overview';
  if (app.profileMode && view === 'runs') view = 'overview';
  app.view = view;
  const [label, title, description, eyebrow] = viewCopy[view];
  $('breadcrumb').textContent = label; $('page-title').textContent = title;
  $('page-description').textContent = description; $('page-eyebrow').textContent = eyebrow;
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('work-grid').hidden = !['overview', 'tasks', 'projects'].includes(view);
  $('overview-aside').hidden = view !== 'overview';
  $('overview-stats').hidden = view !== 'overview';
  $('work-grid').classList.toggle('tasks-only', view === 'tasks' || view === 'projects');
  const projects = $('projects-section'), tasks = $('tasks-section');
  projects.hidden = app.profileMode && view === 'tasks';
  tasks.hidden = app.profileMode && view === 'projects';
  projects.parentElement.insertBefore(view === 'tasks' ? tasks : projects, view === 'tasks' ? projects : tasks);
  for (const id of ['agents', 'usage', 'news', 'runs']) $(`${id}-view`).hidden = view !== id;
  if (!app.profileMode) window.WorkspaceExecution?.onView(view);
  window.WorkspaceNews?.onView(view);
  $('all-tasks').hidden = view !== 'overview';
  if (app.snapshot) { renderProjects(); if (!app.profileMode) renderTasks(); }
  $('settings-view').hidden = view !== 'settings' || !app.profileMode;
  if (view !== 'settings') $('registration-settings').open = false;
  history.replaceState(null, '', `#${view}`);
}
function renderStats() {
  const snapshot = app.snapshot;
  const pending = app.profileMode ? snapshot.projects.items.reduce((sum, project) => sum + (project.task_counts?.total || 0) - (project.task_counts?.done || 0), 0) : snapshot.tasks.items.filter(task => task.state !== 'done').length;
  const stats = [
    [snapshot.projects.status === 'available' ? snapshot.projects.items.length : '—', '已登记项目', '来自本机项目注册'],
    [snapshot.tasks.status === 'available' ? pending : '—', app.profileMode ? '已登记项目待处理任务' : '待处理任务', app.profileMode ? '全部记录统计 · 手工状态' : snapshot.tasks.total > snapshot.tasks.limit ? '当前 500 条中的手工状态' : '工作台手工状态'],
    [snapshot.providers.filter(item => item.discovery.status === 'found').length, '已发现工具', '命令发现，不代表运行'],
  ];
  $('overview-stats').replaceChildren(...stats.map(([value, title, note]) => {
    const node = el('div', 'stat'), copy = el('span', '', title);
    copy.append(el('small', '', note)); node.append(el('strong', '', value), copy); return node;
  }));
}
function renderProjects() {
  const segment = app.snapshot.projects, root = $('projects-list'); root.replaceChildren();
  if (segment.status !== 'available') { empty(root, '项目信息暂时不可用', reason(segment.reason)); return; }
  const items = app.view === 'overview' ? segment.items.slice(0, 4) : segment.items;
  $('projects-status').textContent = app.view === 'overview' && segment.items.length > items.length ? `展示 ${items.length} / ${segment.items.length} 个项目 · 全部项目见「项目任务」` : '手工进度与文档线索分别保留';
  if (app.profileMode) { $('profile-empty').hidden = segment.items.length > 0; $('compatibility').hidden = false; }
  if (!items.length) { if (!app.profileMode) empty(root, '还没有登记项目', '在原进度页维护项目登记后，这里会展示项目与任务。'); return; }
  for (const [index, project] of items.entries()) {
    const row = el('article', 'project-row'), copy = el('div', 'project-copy'), measure = el('div', 'project-measure');
    row.dataset.projectId = project.id;
    copy.append(el('strong', '', projectName(project.id)));
    const record = project.records?.[0];
    const recordSummary = record ? [record.feature, record.label, record.text || '具体内容未提供'].filter(Boolean).join(' · ') : project.records_available ? '暂无未收口的文档线索' : '文档线索暂不可用';
    copy.append(el('p', '', project.note || recordSummary));
    if (project.manual_recorded && typeof project.percent === 'number') {
      measure.append(el('span', '', `${project.percent}%`), el('small', '', `${project.state} · 手工`));
      const progress = el('progress'); progress.max = 100; progress.value = project.percent;
      progress.setAttribute('aria-label', `${project.name}手工进度 ${project.percent}%`); measure.append(progress);
    } else {
      const label = project.manual_recorded ? '未填写百分比' : project.manual_reason === 'invalid_record' ? '记录无效' : project.manual_reason === 'source_unavailable' ? '来源不可用' : '未记录';
      measure.append(el('span', '', label), el('small', '', project.manual_recorded ? `${project.state} · 手工` : '手工进度'));
    }
    row.append(el('span', 'project-mark', String(index + 1).padStart(2, '0')), copy, measure); root.append(row);
    if (app.profileMode) {
      const actions = el('div', 'project-actions');
      actions.append(action('编辑项目 / 来源', () => openProject(project)), action('人工记录与历史', () => { showView('projects'); openManual(project); }), action('查看项目事项', () => { showView('tasks'); app.taskCenter?.filterProject(project.id); }), action(project.archived ? '恢复项目' : '归档项目', () => archiveProject(project)));
      row.append(actions);
      copy.append(el('p', '', taskSummary(project.task_counts)), el('p', '', observationSummary(project.observation)));
    }
  }
  if (app.profileMode) window.WorkspaceObservation?.renderProjectRefs();
}
function renderTasks() {
  const segment = app.snapshot.tasks, root = $('tasks-list'); root.replaceChildren();
  if (app.profileMode) pager('tasks-pager', segment, 'tasks', () => loadTasks());
  if (segment.status !== 'available') { empty(root, '任务暂时不可用', reason(segment.reason)); return; }
  let tasks = segment.items.filter(task => (!$('project-filter').value || task.project_id === $('project-filter').value) && (!$('state-filter').value || task.state === $('state-filter').value));
  const total = tasks.length;
  if (app.view === 'overview') tasks = tasks.slice(0, 5);
  $('tasks-status').textContent = app.profileMode ? `当前页 ${segment.items.length} / 全部 ${segment.total} 条 · 本页状态筛选显示 ${tasks.length} 条 · 状态均为手工记录` : segment.total > segment.limit ? `已载入最近 ${segment.limit} / ${segment.total} 条；当前筛选显示 ${tasks.length} 条。` : `${total} 条任务${tasks.length < total ? ` · 总览展示 ${tasks.length} 条` : ''} · 状态均为手工记录`;
  if (!tasks.length) { empty(root, segment.items.length ? '当前筛选下没有任务' : '把下一步，记在这里。', segment.items.length ? '试试切换项目或状态。' : '从一个具体任务开始，选好项目与负责工具，进度就有了落点。'); return; }
  const linkLabels = { verified: '会话关联已核验', not_in_current_sample: '关联暂不在当前样本', project_mismatch: '关联与项目不匹配' };
  for (const task of tasks) {
    const row = el('article', 'task-row'); row.dataset.taskId = task.id;
    const copy = el('div', 'task-copy'), meta = el('div', 'task-meta');
    copy.append(el('strong', '', task.title));
    meta.append(el('span', `task-state state-${task.state}`, `${stateNames[task.state] || '未知'} · 手工`), el('span', '', projectName(task.project_id)), el('span', '', providerNames[task.provider_id] || '未分配工具'));
    if (task.session_key) meta.append(el('span', '', linkLabels[task.session_link_state] || '关联状态未知'));
    copy.append(meta);
    const edit = el('button', 'text-button', '编辑'); edit.type = 'button'; edit.disabled = !app.csrf;
    edit.setAttribute('aria-label', `编辑任务 ${task.title}`); edit.addEventListener('click', () => openTask(task));
    const actions = el('div', 'task-actions'); actions.append(edit);
    if (task.provider_id === 'codex' && !app.profileMode) {
      const run = el('button', 'text-button', '交给 Codex'); run.type = 'button'; run.dataset.runTask = task.id;
      run.disabled = !app.csrf || !window.WorkspaceExecution;
      run.addEventListener('click', () => window.WorkspaceExecution?.openTask(task.id)); actions.append(run);
    }
    row.append(el('span', `task-indicator ${task.state}`), copy, actions); root.append(row);
  }
}
function renderTodos() {
  const segment = app.snapshot.todos, root = $('todos-list'); root.replaceChildren();
  if (segment.status !== 'available') { empty(root, '待办来源暂不可用', reason(segment.reason)); return; }
  const items = app.profileMode ? segment.items : [...segment.items].sort((a, b) => Number(a.done) - Number(b.done)).slice(0, 4);
  if (app.profileMode) pager('todos-pager', segment, 'todos', () => loadTodos());
  if (!items.length) { empty(root, '清单暂时为空', '旧待办仍可在进度页添加与编辑。'); return; }
  for (const item of items) {
    const row = el('div', `todo-row${item.done ? ' done' : ''}`), copy = el('div');
    copy.append(el('p', '', item.text), el('small', '', projectName(item.project_id)));
    row.append(el('span', 'check', item.done ? '☑' : '□'), copy);
    if (app.profileMode) row.append(action(item.done ? '恢复待办' : '标记完成', async () => { try { await post('/api/todos/toggle', { id: item.id }); await loadTodos(); } catch (error) { message('workspace-status', error.message, true); } }));
    root.append(row);
  }
  if (segment.items.length > items.length) root.append(el('p', 'section-note', `显示 ${items.length} / ${segment.items.length} 条 · 在旧页查看全部`));
}
function renderProviders() {
  const root = $('providers-list'); root.replaceChildren();
  const discovery = { found: '已发现命令', not_found: '未发现命令', unknown: '发现状态未知' };
  for (const provider of app.snapshot.providers) {
    const row = el('article', 'provider-row'), heading = el('div', 'provider-heading'), name = el('div'), detail = el('div', 'provider-details'), badges = el('div', 'capabilities'), links = el('div', 'provider-links');
    name.append(el('h3', '', provider.name), el('small', `discovery-${provider.discovery.status}`, discovery[provider.discovery.status] || '发现状态未知'));
    heading.append(el('span', 'provider-icon', provider.id === 'claude' ? 'Cl' : provider.id === 'opencode' ? 'Oc' : provider.name.slice(0, 2)), name);
    for (const [key, label] of [['observe_sessions', '会话观察'], ['read_quota', '额度协议']]) {
      const supported = provider.capabilities[key] === true;
      if (app.profileMode && key === 'observe_sessions') {
        const available = provider.id === 'codex' && app.snapshot.sessions.available;
        badges.append(el('span', `badge${available ? ' available' : ''}`, `会话来源 · ${available ? '可读元数据' : '未提供可读来源'}`));
      } else if (app.profileMode) badges.append(el('span', 'badge', `额度查询入口 · ${supported ? '可查询' : '未提供'}`));
      else badges.append(el('span', `badge${supported ? ' available' : ''}`, `${label} · ${supported ? '已接入' : '未接入'}`));
    }
    if (provider.id === 'codex' && !app.profileMode) {
      const execution = el('span', 'badge', '执行功能 · 待读取'); execution.id = 'execution-provider-badge'; badges.append(execution);
      const preparation = el('p', '', '本机准备条件待读取；发现命令不代表账号可推理。'); preparation.id = 'execution-provider-status'; detail.append(preparation);
    } else badges.append(el('span', 'badge', '派发 / 审批 / 取消 · 未接入'));
    detail.append(badges, el('p', '', provider.observation_note));
    if (!app.profileMode) for (const entry of provider.entrypoints || []) links.append(link(`${entry.label} ↗`, entry.href));
    row.append(heading, detail, links); root.append(row);
  }
  if (!app.profileMode) window.WorkspaceExecution?.refresh();
  const sessions = app.snapshot.sessions; $('sessions-list').replaceChildren();
  $('sessions-status').textContent = sessions.available ? `本机身份元数据 · 当前 ${sessions.items.length} / ${sessions.total_visible} 条可见记录（每页最多 ${sessions.limit} 条）· ${dateTime(sessions.sampled_at)} 采样。活动与完成未知。` : reason(sessions.reason);
  if (!sessions.available || !sessions.items.length) { empty($('sessions-list'), sessions.available ? '当前没有可见会话' : '身份索引不可用', '这里不读取会话消息正文。'); return; }
  for (const session of sessions.items) {
    const row = el('div', 'session-row');
    row.append(el('code', '', session.thread_id.slice(0, 8)), el('span', '', session.cwd), el('span', 'muted', `${session.source_kind} · ${dateTime(session.updated_at)}`));
    $('sessions-list').append(row);
  }
}
async function loadBase() {
  $('refresh-workspace').disabled = true;
  try {
    const snapshot = await api('/api/workspace');
    if (snapshot.status !== 'ok') throw new Error('工作空间尚未返回可用数据');
    app.snapshot = snapshot;
    if (app.profileMode && window.WorkspaceObservation) window.WorkspaceObservation.projects = snapshot.projects.items;
    $('today').textContent = `${snapshot.today} · 北京时间`;
    message('workspace-status', `本机信息采样于 ${dateTime(snapshot.sampled_at)} · 额度与资讯独立更新`);
    const selectedFilter = $('project-filter').value;
    fillOptions($('project-filter'), [['', '全部项目'], ...snapshot.projects.items.map(item => [item.id, item.name])], selectedFilter);
    if (!$('project-filter').value) $('project-filter').value = '';
    $('new-task').disabled = !app.csrf || snapshot.projects.status !== 'available' || !snapshot.projects.items.some(item => !item.archived) || snapshot.tasks.status !== 'available';
    if (app.profileMode) app.taskCenter?.update(snapshot);
    renderStats(); renderProjects();
    if (!app.profileMode) { renderTasks(); renderTodos(); }
    renderProviders();
    if (!$('record-form').hidden && !$('record-day').value) $('record-day').value = snapshot.today;
  } catch (error) { message('workspace-status', error.message, true); }
  finally { $('refresh-workspace').disabled = false; }
}

function sessionOptions(selected = '') {
  const select = $('task-session'), isCodex = $('task-provider').value === 'codex';
  const items = app.snapshot?.sessions.available ? app.snapshot.sessions.items : [];
  const options = [['', isCodex ? '不关联会话' : '仅 Codex 支持当前关联']];
  if (isCodex) for (const session of items) options.push([session.session_key, `${session.thread_id.slice(0, 8)} · ${session.cwd}`]);
  if (isCodex && selected && !items.some(item => item.session_key === selected)) options.push([selected, `保留原关联 · ${selected.slice(6, 14)}（当前不可核验）`]);
  fillOptions(select, options, isCodex ? selected : ''); select.disabled = !isCodex;
}
function openTask(task = null) {
  if ($('task-fields').disabled) return;
  app.taskId = task?.id || null;
  $('task-form').hidden = false; $('task-form-title').textContent = task ? '编辑任务' : '新建任务';
  $('task-title').value = task?.title || '';
  const active = app.snapshot.projects.items.filter(item => !app.profileMode || !item.archived);
  const choices = active.map(item => [item.id, item.name]);
  if (task && !active.some(item => item.id === task.project_id)) choices.unshift([task.project_id, `保留原项目 · ${projectName(task.project_id)}`]);
  fillOptions($('task-project'), choices, task?.project_id || active.find(item => item.id === $('project-filter').value)?.id || active[0]?.id);
  $('task-provider').value = task?.provider_id || ''; $('task-state').value = task?.state || 'todo';
  sessionOptions(task?.session_key || ''); message('task-feedback', '');
  $('task-title').focus(); $('task-form').scrollIntoView({ block: 'nearest', behavior: 'auto' });
}
async function saveTask(event) {
  event.preventDefault(); if ($('task-fields').disabled) return;
  const body = { title: $('task-title').value.trim(), project_id: $('task-project').value, provider_id: $('task-provider').value || null, state: $('task-state').value, session_key: $('task-session').value || null };
  if (app.taskId) body.id = app.taskId;
  $('task-fields').disabled = true; message('task-feedback', '正在保存…');
  try {
    const result = await post(app.taskId ? '/api/workspace/tasks/update' : '/api/workspace/tasks', body);
    if (result.status !== 'ok' || !result.task) throw new Error('服务未确认保存，草稿已保留');
    const oldIndex = app.snapshot.tasks.items.findIndex(item => item.id === result.task.id);
    if (oldIndex >= 0) app.snapshot.tasks.items.splice(oldIndex, 1);
    else app.snapshot.tasks.total++;
    app.snapshot.tasks.items.unshift(result.task);
    app.snapshot.tasks.items = app.snapshot.tasks.items.slice(0, app.snapshot.tasks.limit);
    $('task-form').reset(); $('task-form').hidden = true; app.taskId = null;
    renderTasks(); renderStats(); $('new-task').focus();
    message('workspace-status', '任务已保存 · 手工状态，不会向工具发送任务');
    if (app.profileMode) await loadBase();
  } catch (error) { message('task-feedback', `${error.message}。输入已保留。`, true); }
  finally { $('task-fields').disabled = false; }
}

function renderQuota(quota) {
  window.WorkspaceQuota.render(quota, { root: $('quota-detail'), summary: $('quota-summary'),
    refreshButton: $('refresh-quota'), el, dateTime, reason, statusNames });
}
function renderRecords() {
  const usage = app.usage, root = $('records-list'); root.replaceChildren();
  if (app.profileMode) pager('records-pager', { items: usage.manual_records, total: usage.manual_total, offset: app.pages.records, limit: 50 }, 'records', () => loadRecords());
  $('new-record').disabled = !app.csrf || usage.manual_status !== 'available';
  $('records-status').textContent = usage.manual_status === 'available' ? `手工记录 · ${app.profileMode ? '当前页' : '最近'} ${usage.manual_records.length} / ${usage.manual_total} 条（每页最多 ${usage.manual_limit} 条）· 不自动汇总` : reason(usage.manual_reason);
  if (usage.manual_status !== 'available') { empty(root, '人工记录暂时不可用', '原有记录不会因为读取失败被清空。'); return; }
  if (!usage.manual_records.length) { empty(root, '还没有用量记录', '记录原服务上的观察值，并留下日期、窗口和产品范围。'); return; }
  for (const record of usage.manual_records) {
    const row = el('article', 'record-row'), copy = el('div'); row.dataset.recordId = record.id;
    copy.append(el('strong', '', `${providerNames[record.provider_id]} · ${productNames[record.product]}`), el('small', '', `${record.metric === 'quota_used_percent' ? `额度已用 · ${record.window_label}` : record.metric === 'daily_tokens' ? '当日 Token' : '当日费用'} · 手工记录${record.note ? ` · ${record.note}` : ''}`));
    const value = `${Number(record.value).toLocaleString('zh-CN')}${record.metric === 'quota_used_percent' ? '%' : record.metric === 'daily_tokens' ? ' Token' : ` ${record.currency}`}`;
    const edit = el('button', 'text-button', '编辑'); edit.type = 'button'; edit.disabled = !app.csrf; edit.setAttribute('aria-label', `编辑 ${record.day} ${providerNames[record.provider_id]} 记录`); edit.addEventListener('click', () => openRecord(record));
    row.append(el('span', '', record.day), copy, el('span', 'record-value', value), edit); root.append(row);
  }
}
async function loadUsage(refresh = false) {
  if (refresh && $('refresh-quota').disabled) return;
  $('refresh-quota').disabled = true;
  if (refresh) $('refresh-quota').textContent = '查询中…';
  try {
    const result = await (refresh ? post('/api/workspace/usage/refresh', {}) : api('/api/workspace/usage'));
    if (result.status !== 'ok') throw new Error('用量来源未返回可用结果');
    app.usage = result; renderQuota(result.codex);
    if (app.profileMode) await loadRecords(); else renderRecords();
  } catch (error) {
    const note = el('p', 'quota-error', error.message); $('quota-detail').replaceChildren(note);
    $('quota-summary').dataset.status = 'unavailable';
    $('quota-summary').replaceChildren(el('h2', '', '来源不可用'), el('p', 'muted', error.message));
    $('refresh-quota').textContent = '重新查询';
    if (!app.usage) $('records-status').textContent = '人工记录未读取，不能判断是否为空。';
  } finally { $('refresh-quota').disabled = !app.csrf; }
}
function recordProducts(selected) {
  const provider = $('record-provider').value;
  const choices = provider === 'grok' ? ['subscription', 'cli', 'api'] : ['dsh', 'opencode'].includes(provider) ? ['local'] : ['subscription'];
  fillOptions($('record-product'), choices.map(value => [value, productNames[value]]), choices.includes(selected) ? selected : choices[0]);
}
function recordMetric() {
  const metric = $('record-metric').value;
  $('record-window-field').hidden = metric !== 'quota_used_percent'; $('record-window').required = metric === 'quota_used_percent';
  $('record-currency-field').hidden = metric !== 'daily_cost';
  $('record-value').step = metric === 'daily_tokens' ? '1' : 'any';
  if (metric === 'quota_used_percent') $('record-value').max = '100'; else $('record-value').removeAttribute('max');
}
function openRecord(record = null) {
  if ($('record-fields').disabled) return;
  app.recordId = record?.id || null; $('record-form').hidden = false;
  $('record-form-title').textContent = record ? '编辑人工记录' : '新增人工记录';
  $('record-provider').value = record?.provider_id || 'codex'; recordProducts(record?.product);
  $('record-day').value = record?.day || app.snapshot?.today || '';
  if (app.snapshot?.today) $('record-day').max = app.snapshot.today;
  $('record-metric').value = record?.metric || 'quota_used_percent'; $('record-value').value = record?.value ?? '';
  $('record-window').value = record?.window_label || ''; $('record-currency').value = record?.currency || 'USD'; $('record-note').value = record?.note || '';
  recordMetric(); message('record-feedback', ''); $('record-provider').focus(); $('record-form').scrollIntoView({ block: 'nearest', behavior: 'auto' });
}
async function saveRecord(event) {
  event.preventDefault(); if ($('record-fields').disabled) return;
  const metric = $('record-metric').value;
  const value = Number($('record-value').value);
  if (metric === 'daily_tokens' && !Number.isSafeInteger(value)) {
    message('record-feedback', 'Token 必须是浏览器可精确表示的整数（最多 9,007,199,254,740,991）。输入已保留，未提交。', true);
    $('record-value').focus(); return;
  }
  const body = { id: app.recordId, provider_id: $('record-provider').value, product: $('record-product').value, day: $('record-day').value, metric, value, currency: metric === 'daily_cost' ? $('record-currency').value : null, window_label: metric === 'quota_used_percent' ? $('record-window').value.trim() : null, note: $('record-note').value.trim() };
  $('record-fields').disabled = true; message('record-feedback', '正在保存…');
  try {
    const result = await post('/api/workspace/records', body);
    if (result.status !== 'ok' || !result.record) throw new Error('服务未确认保存，草稿已保留');
    const oldIndex = app.usage.manual_records.findIndex(item => item.id === result.record.id);
    if (oldIndex >= 0) app.usage.manual_records.splice(oldIndex, 1); else app.usage.manual_total++;
    app.usage.manual_records.unshift(result.record);
    app.usage.manual_records = app.usage.manual_records.slice(0, app.usage.manual_limit); renderRecords();
    $('record-form').reset(); $('record-form').hidden = true; app.recordId = null; $('new-record').focus();
    message('workspace-status', '人工用量记录已保存 · 未调用外部服务');
    if (app.profileMode) await loadRecords();
  } catch (error) { message('record-feedback', `${error.message}。输入已保留。`, true); }
  finally { $('record-fields').disabled = false; }
}

for (const button of document.querySelectorAll('[data-view]')) button.addEventListener('click', () => showView(button.dataset.view));
for (const button of document.querySelectorAll('[data-go]')) button.addEventListener('click', () => showView(button.dataset.go));
$('refresh-workspace').addEventListener('click', loadBase);
$('all-tasks').addEventListener('click', () => showView('tasks'));
$('project-filter').addEventListener('change', () => { if (!app.profileMode && app.snapshot) renderTasks(); });
$('state-filter').addEventListener('change', () => { if (!app.profileMode && app.snapshot) renderTasks(); });
$('new-task').addEventListener('click', () => { if (!app.profileMode) openTask(); });
$('cancel-task').addEventListener('click', () => { $('task-form').hidden = true; app.taskId = null; $('new-task').focus(); });
$('task-provider').addEventListener('change', () => { if (!app.profileMode) sessionOptions($('task-session').value); });
$('task-form').addEventListener('submit', event => { if (!app.profileMode) return saveTask(event); event.preventDefault(); });
$('refresh-quota').addEventListener('click', () => loadUsage(true));
$('new-record').addEventListener('click', () => openRecord());
$('cancel-record').addEventListener('click', () => { $('record-form').hidden = true; app.recordId = null; $('new-record').focus(); });
$('record-provider').addEventListener('change', () => recordProducts());
$('record-metric').addEventListener('change', recordMetric);
$('record-form').addEventListener('submit', saveRecord);
fillOptions($('task-provider'), [['', '暂不分配'], ...Object.entries(providerNames)]);
fillOptions($('record-provider'), Object.entries(providerNames), 'codex');
window.addEventListener('hashchange', () => showView(location.hash.slice(1) || 'overview'));
showView(location.hash.slice(1) || 'overview');
async function bootstrap() {
  try { const value = await api('/api/bootstrap'); app.csrf = typeof value.csrf_token === 'string' ? value.csrf_token : ''; app.capabilities = value.capabilities || {}; app.profileMode = app.capabilities.profile === true; }
  catch { message('workspace-status', '保存凭据不可用，仍可尝试读取信息。', true); }
  window.WorkspaceNews?.startWorkspace({ fetchJson: api, postJson: post });
  if (app.profileMode) { setupProfile(); setupTaskCenter(); try { await loadProfile(); } catch (error) { message('workspace-status', error.message, true); } }
  else { window.WorkspaceExecution?.init({ api, post, showView, getSnapshot: () => app.snapshot });
    if (!window.WorkspaceExecution) message('execution-status', '执行页面资源尚未就绪；请在新版服务启动后刷新。现有任务仍可使用。', true); }
  if (app.profileMode) window.WorkspaceObservation?.start({ fetchJson: api, postJson: post });
  await Promise.allSettled([loadBase(), loadUsage()]);
  if (!app.profileMode) window.WorkspaceExecution?.onView(app.view);
}
function action(label, callback) {
  const button = el('button', 'text-button', label); button.type = 'button'; button.disabled = !app.csrf;
  button.addEventListener('click', async () => { button.disabled = true; try { await callback(); } catch (error) { message('workspace-status', error.message, true); } finally { button.disabled = !app.csrf; } }); return button;
}
function taskSummary(counts) {
  if (!counts?.total) return '任务统计：没有任务';
  return `任务统计：${counts.total} 条 · ${Object.entries(stateNames).map(([key, label]) => `${label} ${counts[key] || 0}`).join(' / ')}`;
}
function observationSummary(observation) {
  const labels = { not_configured: '未配置', missing: '文件或目录缺失', unrecognized: '没有可识别内容', error: '读取错误', available: '可用' };
  if (!observation) return '来源观察：未配置';
  const report = observation.reported;
  return `来源观察：${labels[observation.status] || observation.status}${observation.reason ? ` · ${observation.reason}` : ''}${report ? ` · 来源报告 ${[report.state, report.stage, Number.isFinite(report.percent) ? `${report.percent}%` : '', report.note].filter(Boolean).join(' · ')}` : ''}`;
}
function pager(id, segment, key, reload) {
  const root = $(id); root.replaceChildren(); root.hidden = !app.profileMode;
  const offset = segment.offset ?? app.pages[key], limit = segment.limit || 50, total = segment.total || 0;
  const previous = action('上一页', () => change(Math.max(0, offset - limit))), next = action('下一页', () => change(offset + limit));
  previous.disabled = offset <= 0; next.disabled = offset + limit >= total;
  const change = async value => { previous.disabled = next.disabled = true; app.pages[key] = value; try { await reload(); } catch (error) { root.append(el('p', 'feedback error', error.message)); } };
  root.append(previous, el('span', '', `${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} / ${total} 条`), next);
}
function fieldError(feedback, error, fields = {}) {
  document.querySelectorAll(`[data-error-for="${feedback}"]`).forEach(node => node.remove());
  const text = `${error.message}${error.field ? ` · ${error.field}` : ''}。输入已保留。`;
  message(feedback, text, true);
  const field = fields[error.field] || (error.field?.startsWith('directories') ? $('project-directories') : null);
  if (field) { const note = el('small', 'feedback error', text); note.dataset.errorFor = feedback; field.parentElement.append(note); field.setAttribute('aria-invalid', 'true'); field.focus(); }
}
function clearFieldErrors(feedback) { document.querySelectorAll(`[data-error-for="${feedback}"]`).forEach(node => node.remove()); $(feedback).parentElement.querySelectorAll('[aria-invalid]').forEach(node => node.removeAttribute('aria-invalid')); message(feedback, ''); }
async function loadTasks() {
  const query = new URLSearchParams({ offset: app.pages.tasks, limit: 50, scope: $('task-scope').value });
  if ($('project-filter').value) query.set('project_id', $('project-filter').value);
  app.snapshot.tasks = { ...app.snapshot.tasks, ...await api(`/api/workspace/tasks?${query}`) }; renderTasks();
}
async function loadRecords() {
  const value = await api(`/api/workspace/records?offset=${app.pages.records}&limit=50`);
  app.usage.manual_records = value.items; app.usage.manual_total = value.total; app.usage.manual_limit = value.limit;
  renderRecords();
}
async function loadTodos() {
  app.snapshot.todos = { ...app.snapshot.todos, ...await api(`/api/workspace/todos?offset=${app.pages.todos}&limit=20`) }; renderTodos();
}
function setupProfile() {
  $('new-project').hidden = false; document.querySelector('[data-view="settings"]').hidden = false;
  document.querySelector('[data-view="runs"]').hidden = true;
  document.querySelector('.sidebar-links').replaceChildren(el('p', 'nav-label', '项目进度'));
  $('provider-sessions-link').hidden = !(app.capabilities.remote_sessions || app.capabilities.workbench);
  document.querySelector('#sessions-status').previousElementSibling.querySelector('a').hidden = !app.capabilities.remote_sessions;
  for (const [after, id] of [['tasks-list', 'tasks-pager'], ['records-list', 'records-pager'], ['todos-list', 'todos-pager']]) { const root = el('div', 'pager'); root.id = id; $(after).after(root); }
  const scope = el('select'); scope.id = 'task-scope'; scope.setAttribute('aria-label', '项目登记状态');
  fillOptions(scope, [['all', '全部项目记录'], ['active', '活动项目'], ['archived', '归档项目'], ['orphan', '未登记项目']], 'all');
  $('project-filter').parentElement.prepend(scope);
  scope.addEventListener('change', () => { app.pages.tasks = 0; loadTasks().catch(error => message('tasks-status', error.message, true)); });
  $('new-project').addEventListener('click', () => openProject()); $('empty-add-project').addEventListener('click', () => openProject());
  $('empty-import').addEventListener('click', () => { showView('settings'); $('import-source-dir').focus(); });
  $('empty-settings').addEventListener('click', () => { showView('settings'); $('codex-db-path').focus(); });
  $('cancel-project').addEventListener('click', () => { $('project-form').hidden = true; });
  $('add-directory').addEventListener('click', () => { if (directoryRows().length < 8) addDirectory(`d-${crypto.randomUUID().replaceAll('-', '')}`, ''); syncSource(); });
  $('project-source-kind').addEventListener('change', syncSource); $('project-form').addEventListener('input', projectBytes);
  $('project-form').addEventListener('submit', saveProject); $('manual-form').addEventListener('submit', saveManual);
  $('clear-percent').addEventListener('click', () => { $('manual-percent').value = ''; });
  $('refresh-launchers').addEventListener('click', () => loadLaunchers().catch(error => message('integrations-feedback', error.message, true)));
  $('settings-form').addEventListener('submit', saveSettings); $('import-form').addEventListener('submit', importLegacy);
  $('open-registration-settings').addEventListener('click', event => {
    event.preventDefault(); showView('settings'); $('registration-settings').open = true;
  });
  if (app.view === 'runs') showView('overview'); else showView(app.view);
}
function setupTaskCenter() {
  document.querySelector('.sidebar-links').append(action('人工记录与历史', () => showView('projects')));
  document.querySelector('[data-view="projects"]').hidden = false;
  const navigation = document.querySelector('[data-view="tasks"]'); navigation.textContent = '☷ 任务中心';
  viewCopy.tasks = ['任务中心', '让下一步清楚一点。', '全局和项目事项共用一份记录，快速记录后再补充细节。', 'TASK CENTER'];
  $('legacy-tasks').hidden = true;
  $('todos-list').closest('section').hidden = true;
  $('projects-section').querySelector('a[href="/progress"]').hidden = true;
  $('profile-empty').querySelector('p').textContent = '可以先记录全局事项；需要项目归属时再添加项目。已有记录可导入当前空 profile。';
  if (!window.WorkspaceTaskCenter) { message('workspace-status', '任务中心资源未就绪，请刷新页面。', true); return; }
  app.taskCenter = window.WorkspaceTaskCenter.create({ root: $('task-center-root'), fetchJson: api, postJson: post,
    canWrite: () => Boolean(app.csrf), onSaved: loadBase });
  showView(app.view);
}
async function loadProfile(preserveDraft = false) {
  app.profile = await api('/api/profile'); const value = app.profile;
  $('profile-path').hidden = false; $('profile-path').textContent = `当前 data-dir：${value.data_dir}`;
  $('settings-profile-path').textContent = `配置与管理记录保存在：${value.data_dir}`;
  $('codex-source-status').textContent = `元数据来源：${value.codex_source?.status || 'not_configured'}${value.codex_source?.reason ? ` · ${value.codex_source.reason}` : ''}`;
  if (!preserveDraft) { $('codex-enabled').checked = value.config.codex.enabled; $('codex-db-path').value = value.config.codex.db_path || ''; }
  $('import-submit').disabled = !value.can_import || !app.csrf;
  $('import-availability').textContent = value.can_import ? `目的目录：${value.data_dir} · 当前可导入` : `${reason(value.import_reason || (value.import_receipt ? 'already_imported' : 'profile_not_empty'))} · 目的目录：${value.data_dir}`;
  if (value.import_receipt) renderImport(value.import_receipt);
  renderIntegrations(value);
  await Promise.all([loadCompatibility(), loadLaunchers()]);
}
function renderIntegrations(value) {
  const root = $('integration-statuses'); root.replaceChildren();
  const names = { remote: '远端 Claude', terminal: '终端观察', workbench: '终端工作台', launchers: '本机启动器' };
  const statuses = { not_configured: '未配置', disabled: '已禁用', configured: '已配置 · 连接与工具状态待核验' };
  for (const [name, label] of Object.entries(names)) {
    const item = value.integrations?.[name] || { status: 'not_configured' };
    const row = el('div', 'source-line'); row.dataset.integration = name;
    row.append(el('strong', '', label), el('p', '', statuses[item.status] || '状态未确认'));
    if (item.reason === 'reader_upgrade_recommended') row.append(el('p', 'section-note', '旧 reader 依赖仅迁移兼容；建议升级并单独核验。'));
    root.append(row);
  }
  $('integration-restart').textContent = value.restart_required ? '配置已保存；请重启同一 data-dir 的服务。当前接入仍使用启动时配置。' : '当前服务使用启动时的接入配置。';
  $('integration-import-command').textContent = `python -m session_panel.profile import-settings --file "<自己的高级配置 JSON 绝对路径>" --data-dir "${value.data_dir}"`;
}
async function loadLaunchers() {
  const root = $('configured-launchers'); root.replaceChildren();
  $('refresh-launchers').disabled = !app.capabilities.personal_launchers;
  if (!app.capabilities.personal_launchers) { message('launcher-status', '当前服务未启用本机启动器；导入配置并重启后可用。'); return; }
  const value = await api('/api/desk');
  message('launcher-status', value.launchers.length ? '只启动已登记的入口；按钮不会修改目标配置。' : '当前没有已登记的启动器。');
  for (const item of value.launchers) {
    const row = el('div', 'source-line');
    row.append(el('strong', '', item.name), el('p', 'section-note', `${item.group} · ${item.blurb} · ${item.status}`));
    row.append(action('启动', async () => { await post('/api/launch', { id: item.id }); message('integrations-feedback', '启动请求已提交；请查看对应窗口或工具。'); await loadLaunchers(); }));
    root.append(row);
  }
}

function directoryRows() { return [...$('project-directories').querySelectorAll('[data-binding-id]')]; }
function addDirectory(id, path) {
  const row = el('div', 'directory-row'); row.dataset.bindingId = id;
  const label = el('label', 'field', id === 'main' ? '主目录（绝对路径）' : '附加目录（仅观察）'), input = el('input');
  input.value = path; input.required = true; input.maxLength = 2048; input.dataset.path = ''; label.append(input); row.append(label);
  if (id !== 'main') row.append(action('移除目录', () => {
    if ($('project-source-kind').value !== 'none' && $('project-source-directory').value === id) { message('project-feedback', '来源仍引用此目录，请先更换来源目录或设为未配置。', true); return; }
    row.remove(); syncSource(); projectBytes();
  }));
  $('project-directories').append(row);
}
function syncSource() {
  const kind = $('project-source-kind').value, selected = $('project-source-directory').value;
  fillOptions($('project-source-directory'), directoryRows().map(row => [row.dataset.bindingId, row.dataset.bindingId === 'main' ? '主目录' : `附加目录 · ${row.querySelector('input').value || row.dataset.bindingId}`]), selected || 'main');
  $('source-directory-field').hidden = kind === 'none'; $('source-path-field').hidden = !['text', 'json'].includes(kind);
  $('project-source-path').required = ['text', 'json'].includes(kind);
  $('add-directory').disabled = directoryRows().length >= 8; projectBytes();
}
function projectBody() {
  const kind = $('project-source-kind').value;
  return { id: app.projectId, name: $('project-name').value.trim(), archived: $('project-archived').checked,
    directories: directoryRows().map(row => ({ id: row.dataset.bindingId, path: row.querySelector('input').value })),
    source: { kind, directory_id: kind === 'none' ? null : $('project-source-directory').value, relative_path: kind === 'none' ? null : kind === 'wfc' ? 'docs/current' : $('project-source-path').value } };
}
function projectBytes() { const size = new TextEncoder().encode(JSON.stringify(projectBody())).length; $('project-body-size').textContent = `提交大小 ${size} / 16384 bytes`; }
function openProject(project = null, restoreId = null) {
  clearFieldErrors('project-feedback');
  app.projectId = project?.id || null; app.restoreId = restoreId; $('project-form').hidden = false;
  $('project-form-title').textContent = restoreId ? '恢复未登记项目' : project ? '编辑项目' : '添加项目';
  $('project-identity').textContent = project?.id || restoreId || '保存后生成稳定项目 ID'; $('project-name').value = project?.name || '';
  $('project-archived').checked = project?.archived || false; $('project-archived').disabled = !project;
  $('project-directories').replaceChildren();
  for (const directory of project?.directories || [{ id: 'main', path: '' }]) addDirectory(directory.id || directory.binding_id, directory.path);
  $('project-source-kind').value = project?.source?.kind || 'none'; $('project-source-path').value = project?.source?.relative_path || '';
  syncSource(); $('project-source-directory').value = project?.source?.directory_id || 'main'; message('project-feedback', '');
  $('project-form').scrollIntoView({ block: 'nearest' }); $('project-name').focus();
}
async function saveProject(event) {
  event.preventDefault(); const body = projectBody(); let route = '/api/workspace/projects/save';
  clearFieldErrors('project-feedback');
  if (app.restoreId) { route = '/api/workspace/projects/restore'; delete body.id; delete body.archived; body.project_id = app.restoreId; }
  if (new TextEncoder().encode(JSON.stringify(body)).length > 16384) { message('project-feedback', reasons.body_too_large, true); return; }
  $('project-fields').disabled = true;
  try { const result = await post(route, body); if (result.status !== 'ok' || !result.project) throw new Error('服务未确认保存');
    $('project-form').hidden = true; await loadBase(); await loadProfile(true); openManual(app.snapshot.projects.items.find(item => item.id === result.project.id));
  } catch (error) { fieldError('project-feedback', error, { name: $('project-name'), source: $('project-source-kind'), 'source.relative_path': $('project-source-path'), 'source.directory_id': $('project-source-directory') }); }
  finally { $('project-fields').disabled = false; }
}
async function archiveProject(project) {
  const body = { id: project.id, name: project.name, archived: !project.archived, directories: project.directories.map(row => ({ id: row.id || row.binding_id, path: row.path })), source: project.source };
  try { await post('/api/workspace/projects/save', body); await loadBase(); if (app.manualId === project.id) openManual(app.snapshot.projects.items.find(item => item.id === project.id)); }
  catch (error) { openProject(project); fieldError('project-feedback', error); }
}
function openManual(project) {
  clearFieldErrors('manual-feedback');
  if (!project) return; app.manualId = project.id || project.project_id; app.pages.history = 0;
  $('manual-panel').hidden = false; $('manual-title').textContent = `人工记录 · ${project.name || app.manualId}`;
  $('manual-state').value = project.state || ''; $('manual-percent').value = Number.isFinite(project.percent) ? project.percent : ''; $('manual-note').value = project.note || '';
  const facts = $('project-facts'); facts.replaceChildren(el('p', '', `人工：${project.state || '未记录'} · ${Number.isFinite(project.percent) ? `${project.percent}%` : '未填写百分比'}`), el('p', '', taskSummary(project.task_counts)), el('p', '', observationSummary(project.observation)));
  for (const record of project.records || []) facts.append(el('p', 'source-record', [record.feature, record.label, record.text].filter(Boolean).join(' · ')));
  if (project.observation?.truncated) facts.append(el('p', '', `来源摘要展示 ${project.records.length} / ${project.observation.total} 条`));
  message('manual-feedback', ''); loadHistory().catch(error => message('manual-feedback', error.message, true));
}
async function loadHistory() {
  const id = app.manualId, offset = app.pages.history, request = ++app.historyRequest;
  const current = () => app.manualId === id && app.pages.history === offset && app.historyRequest === request;
  $('manual-history').replaceChildren(el('p', 'section-note', '正在读取人工历史…'));
  $('history-pager').replaceChildren();
  let value;
  try { value = await api(`/api/workspace/progress/history?project_id=${encodeURIComponent(id)}&offset=${offset}&limit=20`); }
  catch (error) { if (current()) throw error; return; }
  if (!current()) return;
  $('manual-history').replaceChildren(...value.items.map(row => el('p', 'history-row', `${row.recorded_at ? dateTime(row.recorded_at) : '来源未提供时间'} · ${row.origin === 'import' ? '导入快照' : '人工'} · ${row.state} · ${Number.isFinite(row.percent) ? `${row.percent}%` : '未填写百分比'} · ${row.note}`)));
  if (!value.items.length) $('manual-history').append(el('p', 'section-note', '还没有人工历史。'));
  pager('history-pager', { ...value, offset }, 'history', loadHistory);
}
async function saveManual(event) {
  event.preventDefault(); const value = $('manual-percent').value;
  clearFieldErrors('manual-feedback'); message('manual-feedback', '正在保存…');
  $('manual-fields').disabled = true;
  try { await post('/api/workspace/progress', { project_id: app.manualId, state: $('manual-state').value, percent: value === '' ? null : Number(value), note: $('manual-note').value });
    await loadBase(); await loadHistory(); await loadCompatibility(); message('manual-feedback', '人工记录已保存。');
  } catch (error) { fieldError('manual-feedback', error, { state: $('manual-state'), percent: $('manual-percent'), note: $('manual-note') }); }
  finally { $('manual-fields').disabled = false; }
}
async function loadCompatibility() {
  const values = await Promise.all([api(`/api/workspace/compatibility?kind=projects&offset=${app.pages.compatibility}&limit=20`), api('/api/workspace/compatibility?kind=manual&offset=0&limit=100')]);
  const [value, manual] = values, root = $('compatibility-list'); root.replaceChildren();
  for (const item of value.items) {
    const row = el('article', 'compatibility-row'); row.dataset.orphanId = item.project_id;
    row.append(el('strong', '', item.project_id), el('p', '', `任务 ${item.task_count} · 人工 ${item.manual_count} · 待办 ${item.todo_count}`), action('恢复原 ID 登记', () => openProject(null, item.project_id)));
    if (item.manual_count) row.append(action('编辑原人工记录', async () => {
      let record = manual.items.find(entry => entry.project_id === item.project_id);
      // Find the selected row beyond the first compatibility page without changing its ID.
      for (let offset = 100; !record && offset < manual.total; offset += 100) { const page = await api(`/api/workspace/compatibility?kind=manual&offset=${offset}&limit=100`); record = page.items.find(entry => entry.project_id === item.project_id); }
      if (record) openManual(record);
    })); root.append(row);
  }
  if (!value.items.length) root.append(el('p', 'section-note', '没有未登记项目记录。'));
  pager('compatibility-pager', value, 'compatibility', loadCompatibility);
}
async function saveSettings(event) {
  event.preventDefault(); clearFieldErrors('settings-feedback'); message('settings-feedback', '正在保存…'); $('settings-fields').disabled = true;
  try { await post('/api/profile/settings', { codex: { enabled: $('codex-enabled').checked, db_path: $('codex-db-path').value || null } }); await loadProfile(); await loadBase(); message('settings-feedback', '来源设置已保存。'); }
  catch (error) { fieldError('settings-feedback', error, { 'codex.db_path': $('codex-db-path'), 'codex.enabled': $('codex-enabled') }); }
  finally { $('settings-fields').disabled = false; }
}
function renderImport(value) {
  const root = $('import-result'); root.replaceChildren(el('h3', '', '导入收据'));
  for (const [kind, count] of Object.entries(value.counts || {})) root.append(el('p', '', `${kind}：${count}`));
  root.append(el('p', '', `未导入：${(value.excluded_domains || ['execution', 'approvals', 'events', 'private_messages']).join('、')}`));
  if (value.backup_path) root.append(el('p', '', `目的 profile 导入前备份：${value.backup_path}`), el('p', '', '恢复到另一个空目录，再另行启动切换；当前 profile 及导入后新记录仍保留。'), el('code', 'restore-command', `python -m session_panel.profile restore --backup "${value.backup_path}" --data-dir "<另一个空的绝对目录>"`));
}
async function importLegacy(event) {
  event.preventDefault(); clearFieldErrors('import-feedback'); message('import-feedback', '正在导入…'); $('import-fields').disabled = true;
  try { const result = await post('/api/profile/import', { source_dir: $('import-source-dir').value }); renderImport(result); await loadBase(); await loadProfile(true); message('import-feedback', '导入已提交；来源仍保留在旧目录。'); }
  catch (error) { fieldError('import-feedback', error, { source_dir: $('import-source-dir') }); }
  finally { $('import-fields').disabled = false; }
}
bootstrap();
