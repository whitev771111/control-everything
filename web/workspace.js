'use strict';

const $ = id => document.getElementById(id);
const providerNames = { codex: 'Codex', claude: 'Claude Code', grok: 'Grok', dsh: 'DSH', opencode: 'OpenCode' };
const stateNames = { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待验收', done: '完成' };
const productNames = { subscription: '订阅', cli: 'CLI credits', api: 'xAI API', local: '本地工具' };
const statusNames = { not_checked: '未查询', available: '可用', unavailable: '不可用', stale: '旧缓存' };
const reasons = {
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
  runs: ['运行', '让任务真正开始。', '显式提交给 Codex，查看过程、具体审批与实际结果。', 'OWNED CODEX RUNS'],
  agents: ['Agents', '工具各有所长。', '看清已经发现什么、可以观察什么，以及尚未接入的能力。', 'YOUR AGENT TOOLKIT'],
  usage: ['用量与额度', '每一份消耗，都有来源。', '订阅窗口、每日用量与人工观察，分开记录。', 'USAGE & LIMITS'],
  news: ['AI 资讯', '留一点时间，看向前沿。', '来自公开来源的标题与原文。按发布时间排序，保留抓取状态。', 'A WINDOW INTO AI'],
};
const app = { view: 'overview', csrf: '', snapshot: null, usage: null, news: null, taskId: null, recordId: null };

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
function projectName(id) { return app.snapshot?.projects.items.find(item => item.id === id)?.name || id || '未关联项目'; }
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
  const timer = setTimeout(() => controller.abort(), 40000);
  let status = null;
  try {
    const response = await fetch(path, { ...options, signal: controller.signal, headers: { 'X-Session-Client': 'panel', ...options.headers } });
    status = response.status;
    const result = await response.json();
    if (!response.ok) throw apiError(reason(result?.reason), status, typeof result?.reason === 'string' ? result.reason : 'http_error');
    return result;
  } catch (error) {
    if (Object.hasOwn(error, 'status') && typeof error.reason === 'string') throw error;
    if (error.name === 'AbortError') throw apiError('读取超时，请稍后重试', null, 'timeout');
    if (error instanceof SyntaxError) throw apiError('无法连接工作台服务，请检查服务是否已启动', status, 'invalid_response');
    if (error instanceof TypeError) throw apiError('无法连接工作台服务，请检查服务是否已启动', null, 'network_error');
    throw error;
  } finally { clearTimeout(timer); }
}
function post(path, body) {
  if (!app.csrf) return Promise.reject(apiError('保存凭据尚未就绪，请刷新页面后重试', null, 'csrf_unavailable'));
  return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-CSRF': app.csrf }, body: JSON.stringify(body) });
}

function showView(view) {
  if (!viewCopy[view]) view = 'overview';
  app.view = view;
  const [label, title, description, eyebrow] = viewCopy[view];
  $('breadcrumb').textContent = label; $('page-title').textContent = title;
  $('page-description').textContent = description; $('page-eyebrow').textContent = eyebrow;
  document.querySelectorAll('[data-view]').forEach(button => {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('work-grid').hidden = !['overview', 'tasks'].includes(view);
  $('overview-aside').hidden = view !== 'overview';
  $('overview-stats').hidden = view !== 'overview';
  $('work-grid').classList.toggle('tasks-only', view === 'tasks');
  const projects = $('projects-section'), tasks = $('tasks-section');
  projects.parentElement.insertBefore(view === 'tasks' ? tasks : projects, view === 'tasks' ? projects : tasks);
  for (const id of ['agents', 'usage', 'news', 'runs']) $(`${id}-view`).hidden = view !== id;
  window.WorkspaceExecution?.onView(view);
  $('all-tasks').hidden = view !== 'overview';
  if (app.snapshot) { renderProjects(); renderTasks(); }
  history.replaceState(null, '', `#${view}`);
}
function renderStats() {
  const snapshot = app.snapshot;
  const pending = snapshot.tasks.items.filter(task => task.state !== 'done').length;
  const stats = [
    [snapshot.projects.status === 'available' ? snapshot.projects.items.length : '—', '已登记项目', '来自本机项目注册'],
    [snapshot.tasks.status === 'available' ? pending : '—', '待处理任务', snapshot.tasks.total > snapshot.tasks.limit ? '当前 500 条中的手工状态' : '工作台手工状态'],
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
  if (!items.length) { empty(root, '还没有登记项目', '在原进度页维护项目登记后，这里会展示项目与任务。'); return; }
  for (const [index, project] of items.entries()) {
    const row = el('article', 'project-row'), copy = el('div', 'project-copy'), measure = el('div', 'project-measure');
    copy.append(el('strong', '', project.name));
    const record = project.records?.[0];
    const recordSummary = record ? [record.feature, record.label, record.text || '具体内容未提供'].filter(Boolean).join(' · ') : project.records_available ? '暂无未收口的文档线索' : '文档线索暂不可用';
    copy.append(el('p', '', project.note || recordSummary));
    if (project.manual_recorded && typeof project.percent === 'number') {
      measure.append(el('span', '', `${project.percent}%`), el('small', '', `${project.state} · 手工`));
      const progress = el('progress'); progress.max = 100; progress.value = project.percent;
      progress.setAttribute('aria-label', `${project.name}手工进度 ${project.percent}%`); measure.append(progress);
    } else {
      const label = project.manual_reason === 'invalid_record' ? '记录无效' : project.manual_reason === 'source_unavailable' ? '来源不可用' : '未记录';
      measure.append(el('span', '', label), el('small', '', '手工进度'));
    }
    row.append(el('span', 'project-mark', String(index + 1).padStart(2, '0')), copy, measure); root.append(row);
  }
}
function renderTasks() {
  const segment = app.snapshot.tasks, root = $('tasks-list'); root.replaceChildren();
  if (segment.status !== 'available') { empty(root, '任务暂时不可用', reason(segment.reason)); return; }
  let tasks = segment.items.filter(task => (!$('project-filter').value || task.project_id === $('project-filter').value) && (!$('state-filter').value || task.state === $('state-filter').value));
  const total = tasks.length;
  if (app.view === 'overview') tasks = tasks.slice(0, 5);
  $('tasks-status').textContent = segment.total > segment.limit ? `已载入最近 ${segment.limit} / ${segment.total} 条；当前筛选显示 ${tasks.length} 条。` : `${total} 条任务${tasks.length < total ? ` · 总览展示 ${tasks.length} 条` : ''} · 状态均为手工记录`;
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
    if (task.provider_id === 'codex') {
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
  const items = [...segment.items].sort((a, b) => Number(a.done) - Number(b.done)).slice(0, 4);
  if (!items.length) { empty(root, '清单暂时为空', '旧待办仍可在进度页添加与编辑。'); return; }
  for (const item of items) {
    const row = el('div', `todo-row${item.done ? ' done' : ''}`), copy = el('div');
    copy.append(el('p', '', item.text), el('small', '', projectName(item.project_id)));
    row.append(el('span', 'check', item.done ? '☑' : '□'), copy); root.append(row);
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
      badges.append(el('span', `badge${supported ? ' available' : ''}`, `${label} · ${supported ? '已接入' : '未接入'}`));
    }
    if (provider.id === 'codex') {
      const execution = el('span', 'badge', '执行功能 · 待读取'); execution.id = 'execution-provider-badge'; badges.append(execution);
      const preparation = el('p', '', '本机准备条件待读取；发现命令不代表账号可推理。'); preparation.id = 'execution-provider-status'; detail.append(preparation);
    } else badges.append(el('span', 'badge', '派发 / 审批 / 取消 · 未接入'));
    detail.append(badges, el('p', '', provider.observation_note));
    for (const entry of provider.entrypoints || []) links.append(link(`${entry.label} ↗`, entry.href));
    row.append(heading, detail, links); root.append(row);
  }
  window.WorkspaceExecution?.refresh();
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
    $('today').textContent = `${snapshot.today} · 北京时间`;
    message('workspace-status', `本机信息采样于 ${dateTime(snapshot.sampled_at)} · 额度与资讯独立更新`);
    const selectedFilter = $('project-filter').value;
    fillOptions($('project-filter'), [['', '全部项目'], ...snapshot.projects.items.map(item => [item.id, item.name])], selectedFilter);
    if (!$('project-filter').value) $('project-filter').value = '';
    $('new-task').disabled = !app.csrf || snapshot.projects.status !== 'available' || !snapshot.projects.items.length || snapshot.tasks.status !== 'available';
    renderStats(); renderProjects(); renderTasks(); renderTodos(); renderProviders();
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
  fillOptions($('task-project'), app.snapshot.projects.items.map(item => [item.id, item.name]), task?.project_id || $('project-filter').value || app.snapshot.projects.items[0]?.id);
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
  } catch (error) { message('task-feedback', `${error.message}。输入已保留。`, true); }
  finally { $('task-fields').disabled = false; }
}

function quotaTitle(quota) { return statusNames[quota.status] || '状态未知'; }
function quotaIssue(quota) { return `${reason(quota.reason)}${quota.error_code !== null && quota.error_code !== undefined ? `（${quota.error_code}）` : ''}`; }
function renderQuota(quota) {
  const root = $('quota-detail'), summary = $('quota-summary'); root.replaceChildren(); summary.replaceChildren();
  summary.dataset.status = quota.status;
  const firstBucket = (quota.buckets || []).find(bucket => [bucket.primary, bucket.secondary].some(window => typeof window?.used_percent === 'number' && Number.isFinite(window.used_percent)));
  const firstWindow = firstBucket && [firstBucket.primary, firstBucket.secondary].find(window => typeof window?.used_percent === 'number' && Number.isFinite(window.used_percent));
  const remaining = firstWindow ? Math.max(0, Math.min(100, 100 - firstWindow.used_percent)).toFixed(1).replace(/\.0$/, '') : null;
  summary.append(el('h2', '', firstWindow ? `剩余 ${remaining}%` : quotaTitle(quota)));
  const brief = quota.status === 'not_checked' ? '点击「查询额度」读取官方只读接口。页面不会自动启动查询。' : ['unavailable', 'stale'].includes(quota.status) ? quotaIssue(quota) : '各窗口的已用与剩余比例见用量页。';
  if (firstWindow) {
    const minutes = firstWindow.window_minutes;
    const duration = minutes === null ? '窗口时长未知' : minutes >= 1440 && minutes % 1440 === 0 ? `${minutes / 1440} 天窗口` : `${minutes} 分钟窗口`;
    summary.append(el('p', 'muted', `${firstBucket.id} · ${duration} · 已用 ${firstWindow.used_percent}%${quota.status === 'stale' ? ' · 旧缓存' : ''}`));
    if (quota.status === 'stale') summary.append(el('p', 'muted', brief));
  } else summary.append(el('p', 'muted', brief));
  if (quota.sampled_at) summary.append(el('p', 'muted', `${dateTime(quota.sampled_at)} 采样`));
  root.append(el('p', 'quota-state', quotaTitle(quota)));
  if (quota.status !== 'available') root.append(el('p', ['unavailable', 'stale'].includes(quota.status) ? 'quota-error' : 'section-note', brief));
  if (quota.status === 'stale') root.append(el('p', 'quota-error', '下方保留上次成功结果，不代表当前额度。'));
  for (const bucket of quota.buckets || []) {
    const section = el('div', 'quota-bucket'); section.append(el('h3', '', `额度池 · ${bucket.id}`));
    for (const [key, label] of [['primary', '主窗口'], ['secondary', '次窗口']]) {
      const window = bucket[key]; if (!window) continue;
      const row = el('div', 'quota-window'), heading = el('div', 'window-heading');
      const duration = window.window_minutes === null ? '时长未知' : window.window_minutes >= 1440 && window.window_minutes % 1440 === 0 ? `${window.window_minutes / 1440} 天` : `${window.window_minutes} 分钟`;
      const valid = typeof window.used_percent === 'number' && Number.isFinite(window.used_percent);
      heading.append(el('span', '', `${label} · ${duration}`), el('strong', '', valid ? `剩余 ${Math.max(0, Math.min(100, 100 - window.used_percent)).toFixed(1).replace(/\.0$/, '')}%` : '剩余未知'));
      row.append(heading);
      if (valid) { const bar = el('progress'); bar.max = 100; bar.value = window.used_percent; bar.setAttribute('aria-label', `${label}已用 ${window.used_percent}%`); row.append(bar); }
      row.append(el('p', '', `${valid ? `已用 ${window.used_percent}% · ` : ''}重置：${dateTime(window.resets_at)}（北京时间）`)); section.append(row);
    }
    root.append(section);
  }
  root.append(el('p', 'quota-meta', `来源：Codex 官方 app-server · 最近采样 ${dateTime(quota.sampled_at)}${quota.last_success_at ? ` · 最近成功 ${dateTime(quota.last_success_at)}` : ''}`));
  $('refresh-quota').textContent = quota.status === 'not_checked' ? '查询额度' : '重新查询';
}
function renderRecords() {
  const usage = app.usage, root = $('records-list'); root.replaceChildren();
  $('new-record').disabled = !app.csrf || usage.manual_status !== 'available';
  $('records-status').textContent = usage.manual_status === 'available' ? `手工记录 · 最近 ${usage.manual_records.length} / ${usage.manual_total} 条（最多显示 ${usage.manual_limit} 条）· 不自动汇总` : reason(usage.manual_reason);
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
    app.usage = result; renderQuota(result.codex); renderRecords();
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
  } catch (error) { message('record-feedback', `${error.message}。输入已保留。`, true); }
  finally { $('record-fields').disabled = false; }
}

function renderNews(result) {
  const root = $('news-list'), preview = $('news-preview'), sources = $('news-sources'); root.replaceChildren(); preview.replaceChildren(); sources.replaceChildren();
  $('news-status').textContent = `${statusNames[result.status] || '未知'} · ${dateTime(result.sampled_at)} 采样（北京时间）${result.status === 'stale' ? ' · 部分来源失败，保留旧结果' : ''}`;
  for (const source of result.sources) {
    const chip = el('div', `source-chip ${source.status}`);
    chip.append(el('span', '', `${source.name} · ${statusNames[source.status] || '未知'}`), el('small', '', `${source.reason ? `${reason(source.reason)} · ` : ''}${dateTime(source.sampled_at)} 采样${source.status === 'stale' ? ` · 上次成功 ${dateTime(source.last_success_at)}` : ''}`)); sources.append(chip);
  }
  if (!result.items.length) { empty(root, result.status === 'unavailable' ? '资讯来源暂时不可用' : '当前没有可展示的标题', '可以稍后重新读取。'); preview.append(el('p', 'section-note', result.status === 'unavailable' ? '来源暂不可用，稍后再来看看。' : '暂无公开标题。')); return; }
  for (const [index, item] of result.items.entries()) {
    const story = link('', item.url, 'news-item'), copy = el('div');
    copy.append(el('h3', '', item.title), el('p', '', `${item.source_name} · ${dateTime(item.published_at)} 发布 · ${dateTime(item.fetched_at)} 抓取`));
    story.append(el('span', 'news-number', String(index + 1).padStart(2, '0')), copy, el('span', 'news-arrow', '↗')); root.append(story);
    if (index < 2) { const short = link('', item.url, 'preview-story'); short.append(el('strong', '', item.title), el('small', '', `${item.source_name} · ${dateTime(item.published_at)}`)); preview.append(short); }
  }
  if (result.status === 'stale') preview.append(el('p', 'section-note', '部分来源失败 · 含旧缓存'));
}
async function loadNews() {
  $('refresh-news').disabled = true;
  try { app.news = await api('/api/workspace/news'); renderNews(app.news); }
  catch (error) { $('news-status').textContent = error.message; $('news-preview').replaceChildren(el('p', 'section-note', error.message)); }
  finally { $('refresh-news').disabled = false; }
}

for (const button of document.querySelectorAll('[data-view]')) button.addEventListener('click', () => showView(button.dataset.view));
for (const button of document.querySelectorAll('[data-go]')) button.addEventListener('click', () => showView(button.dataset.go));
$('refresh-workspace').addEventListener('click', loadBase);
$('all-tasks').addEventListener('click', () => showView('tasks'));
$('project-filter').addEventListener('change', () => { if (app.snapshot) renderTasks(); });
$('state-filter').addEventListener('change', () => { if (app.snapshot) renderTasks(); });
$('new-task').addEventListener('click', () => openTask());
$('cancel-task').addEventListener('click', () => { $('task-form').hidden = true; app.taskId = null; $('new-task').focus(); });
$('task-provider').addEventListener('change', () => sessionOptions($('task-session').value));
$('task-form').addEventListener('submit', saveTask);
$('refresh-quota').addEventListener('click', () => loadUsage(true));
$('new-record').addEventListener('click', () => openRecord());
$('cancel-record').addEventListener('click', () => { $('record-form').hidden = true; app.recordId = null; $('new-record').focus(); });
$('record-provider').addEventListener('change', () => recordProducts());
$('record-metric').addEventListener('change', recordMetric);
$('record-form').addEventListener('submit', saveRecord);
$('refresh-news').addEventListener('click', loadNews);
fillOptions($('task-provider'), [['', '暂不分配'], ...Object.entries(providerNames)]);
fillOptions($('record-provider'), Object.entries(providerNames), 'codex');
showView(location.hash.slice(1) || 'overview');
async function bootstrap() {
  try { const value = await api('/api/bootstrap'); app.csrf = typeof value.csrf_token === 'string' ? value.csrf_token : ''; }
  catch { message('workspace-status', '保存凭据不可用，仍可尝试读取信息。', true); }
  window.WorkspaceExecution?.init({ api, post, showView, getSnapshot: () => app.snapshot });
  if (!window.WorkspaceExecution) message('execution-status', '执行页面资源尚未就绪；请在新版服务启动后刷新。现有任务仍可使用。', true);
  await Promise.allSettled([loadBase(), loadUsage(), loadNews()]);
  window.WorkspaceExecution?.onView(app.view);
}
bootstrap();
