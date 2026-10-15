'use strict';

window.WorkspaceExecution = (() => {
  const base = '/api/workspace/execution', storageKey = 'workspace.execution.receipt';
  const activeStates = new Set(['starting', 'running', 'waiting_approval', 'cancel_requested']);
  const stateNames = { starting: '正在准备', running: '运行中', waiting_approval: '等待具体审批', cancel_requested: '已请求取消，等待结果', cancelled_before_send: '发送前已取消', completed: '本次运行已结束，待你验收', interrupted: '本次运行已中断', failed: '本次运行失败', unknown: '本次运行结果未知' };
  const approvalNames = { pending: '等待答复', queued: '答复已登记', sending: '正在发送答复', sent: '答复已发送，等待操作结果', resolved: '审批已结束', expired: '审批已过期', unknown: '答复结果未知' };
  const eventNames = { phase: '运行阶段', agent_message: 'Codex 消息', command: '命令与输出', file_change: '文件变更', approval: '审批', error: '运行错误', token_usage: 'Token 通知', turn_finished: '运行结束通知', cleanup: '进程清理' };
  const reasonNames = { other_instance: '由另一个工作台实例持有，此处只读', platform_unsupported: '当前主机不支持执行', native_cli_not_found: '没有找到可用的 Codex 原生程序', execution_unavailable: '执行服务尚未就绪', storage_unavailable: '本地记录暂时不可用', cleanup_unverified: '所属进程清理尚未确认', policy_mismatch: '有效权限策略未通过核对', config_mismatch: '本次配置未通过核对' };
  const $ = id => document.getElementById(id);
  let deps, view = '', timer, polling = false, refreshPending = false;
  let capability = null, listing = null, context = null, contextReady = false, contextVersion = 0, contextTask = null;
  let attempt = null, looking = false, selected = null, selectionVersion = 0, run = null, cursor = 0, eventCount = 0;
  const actionsInFlight = new Set();
  const node = (tag, cls, text) => {
    const item = document.createElement(tag);
    if (cls) item.className = cls;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  };
  const setText = (id, text) => { if ($(id).textContent !== text) $(id).textContent = text; };
  const note = (id, text, error = false) => { setText(id, text); $(id).classList.toggle('error', error); };
  const stamp = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '时间未知';
  const locked = () => attempt && ['sending', 'unknown', 'not_recorded'].includes(attempt.state);
  const canAct = () => capability?.available === true && capability?.is_owner === true;
  const reasonText = value => reasonNames[value] || (value ? `原因：${value}` : '尚无更多说明');
  function remember() {
    try {
      if (attempt) sessionStorage.setItem(storageKey, JSON.stringify({ request_id: attempt.request_id, task_id: attempt.task_id }));
      else sessionStorage.removeItem(storageKey);
    } catch { /* Receipt recovery remains available in this page if storage is disabled. */ }
  }
  function facts(id, entries) {
    const root = $(id), signature = JSON.stringify(entries);
    if (root.dataset.signature === signature) return;
    root.dataset.signature = signature;
    root.replaceChildren(...entries.map(([label, value]) => {
      const line = node('div'); line.append(node('span', 'muted', label), node('span', '', value ?? '尚未提供')); return line;
    }));
  }
  function renderCapability() {
    const implementation = capability?.implemented ? '执行功能已接入' : capability ? '执行功能未接入' : '执行功能待读取';
    const readiness = capability?.available ? '本机准备条件已满足；账号推理仍以实际运行结果为准。' : `本机暂不可执行：${reasonText(capability?.reason)}。`;
    setText('execution-capability', `${implementation} · ${readiness} 仅操作本工作台创建的运行。`);
    if ($('execution-provider-badge')) setText('execution-provider-badge', implementation);
    if ($('execution-provider-status')) setText('execution-provider-status', `${readiness} 本次启动结果请到「运行」查看；不会接管已有会话。`);
    updateForm();
  }
  function updateForm() {
    const held = Boolean(locked() || attempt?.state === 'admitted');
    $('execution-prompt').disabled = held;
    $('execution-submit').disabled = !contextReady || held || !canAct();
    $('execution-context-refresh').disabled = held;
    $('execution-recovery').hidden = !attempt;
    $('execution-retry').hidden = !(attempt?.state === 'not_recorded' && attempt.body);
    $('execution-retry').disabled = looking || attempt?.state === 'sending';
    $('execution-lookup').hidden = !locked();
    $('execution-lookup').disabled = looking || attempt?.state === 'sending';
    $('execution-another').hidden = attempt?.state !== 'admitted';
    const messages = {
      sending: '正在提交。内容已固定，请等待原请求的回执。',
      unknown: '提交结果未知。请继续查询原回执；此时不能另发一份任务。',
      not_recorded: attempt?.body ? '查询时尚未查到原回执，原提交仍可能在途。可继续查询，或用原内容与原标识显式重试。' : '查询时尚未查到原回执。本页重载后没有原提示词，只能继续查询；不会拼造或自动重发任务。',
      admitted: '主机已接收这次提交。请查看运行的实际结果；若要另发任务，请明确准备另一次运行。',
    };
    setText('execution-recovery-text', attempt ? messages[attempt.state] : '');
  }
  function definitive(error) {
    return (error.status === 400 && error.reason === 'invalid_request') ||
      (error.status === 404 && error.reason === 'task_not_found') ||
      (error.status === 409 && ['context_changed', 'project_busy', 'capacity_busy', 'other_instance'].includes(error.reason));
  }
  async function prepare(taskId) {
    const version = ++contextVersion;
    contextTask = taskId; contextReady = false; context = null;
    $('execution-form').hidden = false;
    note('execution-status', '正在读取已登记目录与本次权限摘要…'); updateForm();
    try {
      const value = await deps.api(`${base}/context?task_id=${encodeURIComponent(taskId)}`);
      if (version !== contextVersion) return;
      if (value.task?.id !== taskId || !value.context_hash || !value.project?.cwd) throw new Error('准备摘要无法识别，请重新读取。');
      context = value; contextReady = true;
      facts('execution-context', [['任务', value.task.title], ['项目', value.project.name], ['真实启动目录', value.project.cwd]]);
      const policy = value.policy || {};
      setText('execution-policy', `权限模式：${policy.mode || '未知'}；审批：${policy.approval_policy || '未知'}；审批人：${policy.approvals_reviewer || '未知'}；工具沙箱网络：${policy.network_access === false ? '关闭' : '未知'}；额外写目录：${policy.writable_roots?.length ? policy.writable_roots.join('、') : '无'}。`);
      setText('execution-inheritance', policy.inheritance_note || '本机独立 MCP 与 managed 设置仍可能继承；外部工具不等同于操作系统文件沙箱。');
      note('execution-status', '请核对任务、目录、权限与提示词，再显式提交。');
    } catch (error) {
      if (version !== contextVersion) return;
      note('execution-status', `${error.message} 草稿已保留，准备摘要读取成功前不能提交。`, true);
    } finally { if (version === contextVersion) updateForm(); }
  }
  async function openTask(taskId) {
    if (!deps) return;
    deps.showView('runs');
    if (locked() || attempt?.state === 'admitted') { updateForm(); $('execution-recovery').scrollIntoView({ block: 'nearest' }); return; }
    if (contextTask !== taskId) $('execution-prompt').value = deps.getSnapshot()?.tasks.items.find(item => item.id === taskId)?.title || '';
    $('execution-form').hidden = false; $('execution-prepare-title').focus();
    await prepare(taskId);
  }
  function acceptReceipt(value) {
    if (!attempt || !value?.run?.id || value.run.request_id !== attempt.request_id) throw new Error('原提交回执无法核对。');
    attempt.state = 'admitted'; attempt.run_id = value.run.id;
    updateForm(); selectRun(value.run.id, false);
  }
  async function lookupAttempt() {
    if (!attempt || looking || attempt.state === 'sending') return;
    looking = true; updateForm();
    try { acceptReceipt(await deps.api(`${base}?request_id=${encodeURIComponent(attempt.request_id)}`)); }
    catch (error) {
      attempt.state = error.status === 404 && error.reason === 'request_not_found' ? 'not_recorded' : 'unknown';
      note('execution-status', `${error.message} 原提交标识仍保留。`, true);
    } finally { looking = false; updateForm(); }
  }
  async function sendFixedBody() {
    if (!attempt?.body) return;
    attempt.state = 'sending'; updateForm();
    try { acceptReceipt(await deps.post(`${base}/runs`, attempt.body)); }
    catch (error) {
      if (definitive(error) || error.reason === 'csrf_unavailable') {
        attempt = null; remember(); updateForm();
        if (error.status === 409 && error.reason === 'context_changed') {
          await prepare(contextTask);
          note('execution-status', contextReady ? '任务或目录已变化。草稿已保留；请核对新的摘要，再显式提交。' : '任务或目录已变化，重新读取摘要失败。草稿已保留，当前不能提交。', true);
        } else note('execution-status', `${error.message} 这次请求未登记，草稿已保留。`, true);
      } else { attempt.state = 'unknown'; updateForm(); await lookupAttempt(); }
    }
  }
  async function submitDraft(event) {
    event.preventDefault();
    if (attempt?.state === 'sending') return;
    if (locked()) return lookupAttempt();
    if (attempt?.state === 'admitted') return selectRun(attempt.run_id, true);
    if (!contextReady || !canAct()) return;
    const prompt = $('execution-prompt').value;
    if (!prompt.trim() || [...prompt].length > 4000 || new TextEncoder().encode(prompt).length > 16384) {
      note('execution-status', '请输入非空提示词，最多 4,000 字符且不超过 16 KiB。', true); return;
    }
    const body = Object.freeze({ request_id: crypto.randomUUID(), task_id: context.task.id, context_hash: context.context_hash, prompt });
    attempt = { state: 'sending', body, request_id: body.request_id, task_id: body.task_id }; remember();
    return sendFixedBody();
  }
  function another(taskId) {
    if (locked()) return;
    taskId = taskId || attempt?.task_id || contextTask;
    attempt = null; remember(); updateForm();
    if (taskId) openTask(taskId); else deps.showView('tasks');
  }
  function renderIndex() {
    const render = (id, items, empty) => {
      const root = $(id), signature = JSON.stringify([items, selected]);
      if (root.dataset.signature === signature) return;
      root.dataset.signature = signature;
      if (!items.length) { root.replaceChildren(node('p', 'section-note', empty)); return; }
      root.querySelectorAll('p').forEach(item => item.remove());
      for (const old of root.querySelectorAll('[data-run-id]')) if (!items.some(item => item.id === old.dataset.runId)) old.remove();
      items.forEach((item, index) => {
        let button = [...root.children].find(child => child.dataset.runId === item.id);
        if (!button) {
          button = node('button', 'execution-run-card'); button.type = 'button'; button.dataset.runId = item.id;
          button.addEventListener('click', () => selectRun(item.id, true));
        }
        button.setAttribute('aria-pressed', String(item.id === selected));
        button.replaceChildren(node('strong', '', item.task_title), node('span', '', `${item.project_name} · ${stateNames[item.state] || '状态未知'}`), node('small', '', `${stamp(item.created_at)} · ${item.model || '模型尚未返回'}`));
        if (root.children[index] !== button) root.insertBefore(button, root.children[index] || null);
      });
    };
    render('execution-active', listing.active || [], '当前没有活动运行。');
    const history = (listing.runs || []).filter(item => !activeStates.has(item.state));
    render('execution-history', history, '还没有历史运行。保存任务后，可从 Codex 任务卡片准备一次运行。');
    setText('execution-history-count', `最近 ${listing.runs?.length || 0} / ${listing.total || 0} 条记录，最多展示 ${listing.limit || 50} 条。`);
  }
  const detailLabels = {
    command: '命令', cwd: '工作目录', reason: '请求原因', output: '输出', text: '消息正文',
    changes: '文件变更', path: '文件路径', diff: '变更内容', kind: '操作类型', move_path: '移动目标路径',
    path_resolved: '路径已解析', scope_verified: '路径信息已核对', network_approval_context: '网络请求范围', host: '目标主机', protocol: '协议',
    status: '操作状态', state: '运行状态', decision: '本次答复', approval_id: '审批记录', item_id: '事件标识',
    final: '完整消息已返回', truncated: '内容已截断', detail_complete: '详情完整', exit_code: '退出码',
    total: '累计用量', last: '最近一次用量', source: '通知来源', usage_inconsistent: '用量通知出现倒退',
    inputTokens: '输入 Token', cachedInputTokens: '缓存输入 Token', outputTokens: '输出 Token', reasoningOutputTokens: '推理输出 Token', totalTokens: '总 Token',
    input_tokens: '输入 Token', output_tokens: '输出 Token', total_tokens: '总 Token',
    mode: '权限模式', sandbox: '工具沙箱', type: '类型', approval_policy: '审批策略', approvalPolicy: '审批策略',
    approvals_reviewer: '审批人', approvalsReviewer: '审批人', network_access: '工具沙箱网络开启', networkAccess: '工具沙箱网络开启',
    writable_roots: '可写目录', writableRoots: '可写目录', excludeTmpdirEnvVar: '排除环境临时目录', excludeSlashTmp: '排除 /tmp 目录',
    cleanup_state: '所属进程清理状态', verified: '回收已确认', cancel_requested: '已请求取消', will_retry: '来源将重试', phase: '阶段',
    features_disabled: '额外功能关闭已核对', notify_disabled: '通知钩子关闭已核对', web_search_disabled: '网络搜索关闭已核对', sandbox_overrides_match: '工具沙箱覆盖已核对', test_mcp_disabled: '测试范围 MCP 关闭已核对',
  };
  function textDetail(value, depth = 0) {
    if (value === null || value === undefined) return '未提供';
    if (typeof value === 'string') return value;
    if (typeof value === 'boolean') return value ? '是' : '否';
    if (typeof value !== 'object') return String(value);
    const indent = '  '.repeat(depth);
    if (Array.isArray(value)) return value.length ? value.map((item, index) => `${indent}第 ${index + 1} 项\n${textDetail(item, depth + 1)}`).join('\n') : '无';
    return Object.entries(value).filter(([key]) => !['native_id_json', 'native_id'].includes(key)).map(([key, item]) => {
      const nested = item && typeof item === 'object';
      return `${indent}${detailLabels[key] || key}：${nested ? '\n' : ''}${textDetail(item, nested ? depth + 1 : depth)}`;
    }).join('\n') || '未提供';
  }
  function renderApproval(approval) {
    const card = node('article', 'execution-approval'); card.dataset.approvalId = approval.id;
    card.append(node('h3', '', approval.kind === 'file_change' ? '文件变更审批' : '命令执行审批'), node('p', 'section-note', approvalNames[approval.status] || '审批状态未知'));
    if (!approval.detail_complete) card.append(node('p', 'execution-warning', '详情不完整，不能允许这次操作。可拒绝或取消当前运行。'));
    const detail = node('pre', 'execution-code', textDetail(approval.detail)); detail.tabIndex = 0; card.append(detail);
    if (approval.status === 'pending') {
      const actions = node('div', 'form-actions');
      for (const [label, decision] of [['仅允许这一次', 'accept'], ['拒绝这次操作', 'decline'], ['取消当前运行', 'cancel']]) {
        const button = node('button', decision === 'accept' ? 'button primary' : 'button secondary', label);
        button.type = 'button'; button.dataset.decision = decision;
        button.disabled = !canAct() || actionsInFlight.has(approval.id) || (decision === 'accept' && (!approval.can_accept || !approval.detail_complete));
        button.addEventListener('click', () => decision === 'cancel' ? cancelRun() : decide(approval.id, decision)); actions.append(button);
      }
      card.append(actions);
    }
    return card;
  }
  function renderRun(value) {
    run = value.run;
    setText('execution-run-title', run.task_title || '运行详情');
    const cleanup = run.cleanup_state === 'verified' ? '所属进程清理已确认' : run.cleanup_state === 'unverified' ? '所属进程清理未确认，该目录仍被占用' : '等待确认所属进程清理';
    setText('execution-run-state', `${stateNames[run.state] || '状态未知'} · ${cleanup}${run.cancel_requested && run.state === 'completed' ? ' · 结束先于取消生效' : ''}${run.reason ? ` · ${reasonText(run.reason)}` : ''}`);
    facts('execution-run-meta', [['项目', run.project_name], ['启动目录', run.cwd], ['实际模型', run.model], ['Thread', run.thread_id], ['Turn', run.turn_id], ['创建时间（北京时间）', stamp(run.created_at)], ['权限模式', run.mode]]);
    setText('execution-run-policy', `本次有效策略：${run.effective_policy ? textDetail(run.effective_policy) : '尚未收到核对结果'}。本机独立 MCP 与 managed 设置可能继承；外部工具有独立权限边界。`);
    const tokens = run.token_usage;
    setText('execution-token', tokens ? `本工作台本次运行官方通知 · ${textDetail(tokens)} · 采样 ${stamp(run.token_observed_at)}。这是最新累计快照，不是每日用量或费用。` : '本工作台本次运行官方 Token 通知：尚未收到；不代表用量为零。');
    $('execution-cancel').hidden = !activeStates.has(run.state);
    $('execution-cancel').disabled = !canAct() || run.cancel_requested || actionsInFlight.has(`cancel:${run.id}`);
    $('execution-unknown').hidden = run.state !== 'unknown';
    $('execution-prepare-again').disabled = Boolean(locked());
    const root = $('execution-approvals'), approvals = value.approvals || [];
    for (const card of [...root.children]) if (!approvals.some(approval => approval.id === card.dataset.approvalId)) card.remove();
    approvals.forEach((approval, index) => {
      let card = [...root.children].find(item => item.dataset.approvalId === approval.id);
      const signature = JSON.stringify([approval, canAct(), actionsInFlight.has(approval.id)]);
      if (!card || card.dataset.signature !== signature) {
        const replacement = renderApproval(approval); replacement.dataset.signature = signature;
        if (card) card.replaceWith(replacement);
        card = replacement;
      }
      if (root.children[index] !== card) root.insertBefore(card, root.children[index] || null);
    });
    if (value.gap) setText('execution-gap', '较早事件已超出保留范围；下面从仍可读取的事件续接。运行摘要与待处理审批仍保留。');
    for (const event of value.events || []) {
      if (event.seq <= cursor) continue;
      const item = node('li', 'execution-event'); item.dataset.seq = event.seq;
      item.append(node('p', 'section-note', `${eventNames[event.kind] || '运行事件'} · Codex / 本地主机通知 · ${stamp(event.observed_at)}`));
      const text = node('pre', `execution-code${event.kind === 'agent_message' ? ' execution-message' : ''}`, textDetail(event.payload)); text.tabIndex = 0; item.append(text); $('execution-events').append(item); eventCount++;
    }
    cursor = Math.max(cursor, value.next_cursor || 0);
    while (eventCount > 2000) { $('execution-events').firstChild.remove(); eventCount--; }
  }
  async function loadRun() {
    const id = selected, version = selectionVersion;
    if (!id) return;
    try {
      const value = await deps.api(`${base}/runs/${encodeURIComponent(id)}?after=${cursor}`);
      if (id !== selected || version !== selectionVersion) return;
      if (value.run?.id !== id) throw new Error('运行回执无法核对。');
      renderRun(value);
    } catch (error) { if (id === selected && version === selectionVersion) note('execution-action-status', `详情暂未更新：${error.message}`, true); }
  }
  function selectRun(id, focus) {
    if (selected !== id) {
      selected = id; selectionVersion++; cursor = 0; eventCount = 0; run = null;
      $('execution-events').replaceChildren(); $('execution-approvals').replaceChildren(); $('execution-approvals').dataset.signature = '';
      setText('execution-gap', ''); setText('execution-action-status', '');
      setText('execution-run-state', '正在读取运行详情…');
      if (listing) renderIndex();
    }
    if (focus) $('execution-run-title').focus();
    refresh();
  }
  async function decide(approvalId, decision) {
    if (!run || !canAct() || actionsInFlight.has(approvalId)) return;
    const id = run.id; actionsInFlight.add(approvalId);
    $('execution-approvals').querySelectorAll(`[data-approval-id="${approvalId}"] button`).forEach(button => { button.disabled = true; });
    note('execution-action-status', '正在登记这一次答复…');
    try { await deps.post(`${base}/runs/${id}/approvals/${approvalId}`, { decision }); if (selected === id) note('execution-action-status', '答复已登记；是否发送与操作结果以最新事件为准。'); }
    catch (error) { if (selected === id) note('execution-action-status', error.message, true); }
    finally { actionsInFlight.delete(approvalId); refresh(); }
  }
  async function cancelRun() {
    if (!run || !canAct() || !activeStates.has(run.state) || run.cancel_requested) return;
    const id = run.id, key = `cancel:${id}`;
    if (actionsInFlight.has(key)) return;
    actionsInFlight.add(key); $('execution-cancel').disabled = true;
    note('execution-action-status', '正在请求取消当前运行…');
    try { await deps.post(`${base}/runs/${id}/cancel`, {}); if (selected === id) note('execution-action-status', '取消请求已登记；收到原生结束通知后才能确认是否中断。'); }
    catch (error) { if (selected === id) note('execution-action-status', error.message, true); }
    finally { actionsInFlight.delete(key); refresh(); }
  }
  async function refresh() {
    if (!deps) return;
    if (polling) { refreshPending = true; return; }
    clearTimeout(timer); polling = true; $('execution-refresh').disabled = true;
    try {
      listing = await deps.api(base); capability = listing.capability; renderCapability(); renderIndex();
      if (view === 'runs' && !document.hidden) await loadRun();
    } catch (error) { note('execution-status', `运行信息暂未更新：${error.message}。已显示的事实保留。`, true); }
    finally {
      polling = false; $('execution-refresh').disabled = false;
      if (refreshPending) { refreshPending = false; if (view === 'runs' && !document.hidden) { refresh(); return; } }
      if (view === 'runs' && !document.hidden) timer = setTimeout(refresh, 1000);
    }
  }
  function onView(next) {
    view = next;
    if (!deps) return;
    clearTimeout(timer);
    if (view === 'runs' && !document.hidden) refresh();
  }
  function init(options) {
    if (deps) return;
    deps = options;
    $('execution-form').addEventListener('submit', submitDraft);
    $('execution-context-refresh').addEventListener('click', () => { if (!locked() && !attempt && contextTask) prepare(contextTask); });
    $('execution-refresh').addEventListener('click', refresh);
    $('execution-lookup').addEventListener('click', lookupAttempt);
    $('execution-retry').addEventListener('click', () => { if (attempt?.state === 'not_recorded' && attempt.body && !looking) sendFixedBody(); });
    $('execution-another').addEventListener('click', () => another());
    $('execution-cancel').addEventListener('click', cancelRun);
    $('execution-back-task').addEventListener('click', () => deps.showView('tasks'));
    $('execution-prepare-again').addEventListener('click', () => another(run?.task_id));
    document.addEventListener('visibilitychange', () => onView(view));
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey));
      const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
      if (saved && uuid.test(saved.request_id) && uuid.test(saved.task_id)) attempt = { state: 'unknown', request_id: saved.request_id, task_id: saved.task_id };
    } catch { /* Invalid local receipt metadata never causes a submission. */ }
    updateForm(); if (attempt) lookupAttempt();
  }
  return { init, openTask, onView, refresh };
})();
