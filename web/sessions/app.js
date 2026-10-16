'use strict';
const $ = id => document.getElementById(id);
const state = { records: [], selected: null, page: null, pageOld: false, csrf: '', epoch: 0, reading: false, focusing: false, refreshing: false, connected: false, received: 0, coverage: {}, snapshotAge: null, sourceReason: null,
  workbench: null, workbenchReceived: 0, workbenchCurrent: false, workbenchReason: null, workbenchError: '', refreshingWorkbench: false, connecting: false, workbenchBlocked: false, workbenchNotice: '' };
const codexState = { items: [], offset: 0, nextOffset: null, total: null, coverage: {}, selected: null, available: false, received: 0, sampledAt: null, refreshing: false, error: '' };
const reasons = {
  reader_empty_unverified: '读取结果未验证：有效空历史与查找或读取失败暂时无法区分',
  reader_scope_unverified: '读取范围未验证：官方 reader 的目录映射无法确认',
  reader_failed: '官方 reader 读取失败', sdk_unavailable: '远端 reader 环境不可用',
  sdk_version_mismatch: '远端 reader 版本不匹配', response_too_large: '消息页超过输出预算',
  messages_changed: '读取期间历史发生变化，请刷新最近消息', revision_mismatch: '历史版本已变化，请刷新最近消息',
  source_mismatch: '来源或 config 与固定来源不一致', source_unknown: '来源证据未确认',
  not_found: '未找到该 UUID', identity_conflict: '会话身份存在冲突', binding_unknown: '当前终端绑定未确认',
  binding_changed: '终端绑定在复核期间发生变化', focus_sent_unverified: '定位命令已发送，但当前焦点未能唯一确认',
  registration_time_unexplained: '注册时间与进程时间关系未解释', stale_observation: '观察已陈旧',
  observation_stale: '观察已陈旧', source_unavailable: '无法连接或核验远端来源', observe_timeout: '远端观察超过时间预算',
  reader_timeout: '消息读取超过时间预算', terminal_unavailable: '无法读取当前 Zellij 清单',
  terminal_version_mismatch: 'Zellij 版本与已核验版本不同', terminal_timeout: '终端清单超过时间预算',
  registration_missing: '本次观察没有该会话的注册', process_unverified: '进程实例未确认', instance_changed: '进程实例已变化，需要重新观察',
  gateway_query_failed: '网关查询失败', gateway_unavailable: '网关不可用', focus_failed: '定位操作失败',
  terminal_action_busy: '另一个终端操作正在核验，请稍后刷新', cleanup_unverified: '终端回收未确认；请核查',
  workbench_disconnected: '工作台尚未关联，请先连接', workbench_association_unknown: '客户端与窗口的关联未确认',
  workbench_ambiguous: '终端关联存在歧义', workbench_exited: '工作台后台已退出', workbench_missing: '工作台后台不存在',
  workbench_unknown: '工作台状态未确认', workbench_invalid_response: '工作台响应未确认', workbench_timeout: '连接观察超过时间预算',
  workbench_not_running: '工作台后台未确认存活', workbench_multiple_sessions: '存在多个后台，无法唯一连接',
  workbench_observation_unknown: '工作台观察未确认', workbench_integrity_failed: '工作台工具或配置与核验版本不同',
  workbench_connect_timeout: '连接观察超过时间预算', workbench_connect_unverified: '连接尚未确认', workbench_launch_failed: '工作台 GUI 启动失败'
};
const reasonText = code => code ? `${reasons[code] || '证据未确认'} (${code})` : '';
const coverageText = text => ({
  'Registry is an internal Claude Code 2.1.278 format; coverage is not a machine-wide total.': '注册来自 Claude Code 2.1.278 内部格式；这里的数量不是整台服务器的会话总数。',
  'claude agents --json omits an observed interactive session; it is not this index\'s source.': 'claude agents 清单遗漏过仍存活的交互会话；本索引采用独立的注册与进程证据。',
  'Process presence and message history do not prove activity or task completion.': '进程存活与历史消息不能证明当前活动或任务完成。'
}[text] || text);
const sourceText = source => source ? `${source.user}@${source.host} · ${source.config_dir}` : '来源未确认';
const timeText = value => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '未观察';
function element(tag, text, className) { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (className) node.className = className; return node; }
async function query(path, options = {}) {
  const response = await fetch(path, { cache: 'no-store', ...options, headers: { 'X-Session-Client': 'panel', ...options.headers } });
  const result = await response.json();
  if (!response.ok) throw Object.assign(new Error(reasonText(result.reason) || '请求失败'), { result });
  return result;
}
function age(record) { return (record.age_ms ?? Infinity) + performance.now() - state.received; }
function workbenchAge() { return (state.workbench?.age_ms ?? Infinity) + performance.now() - state.workbenchReceived; }
function validWorkbench(value) {
  const count = item => item === null || (Number.isInteger(item) && item >= 0);
  return value && typeof value === 'object' && value.session === 'workbench-v4'
    && ['observed', 'unknown'].includes(value.status) && ['running', 'exited', 'missing', 'unknown'].includes(value.server_state)
    && ['absent', 'observed', 'multiple', 'unknown'].includes(value.gui_state)
    && ['disconnected', 'unique_observed', 'ambiguous', 'unknown'].includes(value.association_state)
    && count(value.gui_count) && count(value.interactive_client_count) && count(value.unmanaged_mux_count)
    && value.client_scope === 'interactive_only' && value.watcher_count === null && value.gui_visibility === 'unverified'
    && Number.isFinite(value.age_ms) && value.age_ms >= 0 && typeof value.observed_at === 'string' && Number.isFinite(Date.parse(value.observed_at))
    && typeof value.can_connect === 'boolean' && typeof value.can_focus === 'boolean' && typeof value.actions_blocked === 'boolean'
    && (value.reason === null || typeof value.reason === 'string');
}
function workbenchFresh() { return state.workbenchCurrent && state.workbench?.status === 'observed' && workbenchAge() <= 5000 && !state.refreshingWorkbench && !state.workbenchBlocked && !state.workbench.actions_blocked; }
function terminalAssociated() {
  const value = state.workbench;
  return workbenchFresh() && value.server_state === 'running' && value.gui_state === 'observed' && value.gui_count === 1
    && value.interactive_client_count === 1 && value.unmanaged_mux_count !== null && value.association_state === 'unique_observed'
    && value.can_focus;
}
function terminalReady() { return terminalAssociated() && !state.connecting && !state.focusing; }
function canConnect() {
  const value = state.workbench;
  return workbenchFresh() && value.server_state === 'running' && value.gui_state === 'absent' && value.gui_count === 0
    && value.interactive_client_count === 0 && value.unmanaged_mux_count !== null && value.association_state === 'disconnected'
    && value.can_connect && state.csrf && !state.connecting && !state.focusing;
}
function acceptWorkbench(value) {
  if (!validWorkbench(value)) throw new Error(reasonText('workbench_invalid_response'));
  state.workbench = value; state.workbenchReceived = performance.now(); state.workbenchCurrent = true;
  state.workbenchReason = value.reason; state.workbenchError = ''; if (value.reason === 'cleanup_unverified') state.workbenchBlocked = true;
}
function renderWorkbench() {
  const value = state.workbench, evidenceAge = workbenchAge();
  const currentValue = state.workbenchCurrent && evidenceAge <= 30000;
  const association = { disconnected: '未关联', unique_observed: '唯一关联观察', ambiguous: '关联有歧义', unknown: '关联未知' };
  $('workbench-state').textContent = `workbench-v4 · ${currentValue ? association[value.association_state] : '状态未知 · 上次证据'} · ${Number.isFinite(evidenceAge) ? `证据年龄 ${(evidenceAge / 1000).toFixed(1)} 秒` : '年龄未知'}`;
  const facts = [
    ['后台', ({ running: '存活', exited: '已退出', missing: '不存在', unknown: '未知' }[value?.server_state] || '未知')],
    ['WezTerm GUI 进程', value?.gui_count ?? '未知'], ['交互客户端', value?.interactive_client_count ?? '未知'],
    ['外部独立 mux', value?.unmanaged_mux_count ?? '未知'], ['采样时间', timeText(value?.observed_at)]
  ];
  $('workbench-facts').replaceChildren(...facts.map(([label, text]) => { const item = element('div'); item.append(element('dt', `${currentValue ? '' : '上次证据 · '}${label}`), element('dd', text)); return item; }));
  $('connect-workbench').disabled = !canConnect();
  $('connect-workbench').textContent = state.connecting ? '正在核验连接…' : currentValue && value.association_state === 'unique_observed' ? '已关联' : '连接现有工作台';
  $('refresh-workbench').disabled = state.refreshingWorkbench;
  $('workbench-notice').textContent = state.workbenchBlocked ? reasonText('cleanup_unverified')
    : state.workbenchError || state.workbenchNotice || reasonText(state.workbenchReason) || (!currentValue ? '当前终端证据未知，请刷新终端状态。' : evidenceAge > 5000 ? '动作证据需刷新；状态保留至 30 秒。' : '');
}
async function refreshWorkbench() {
  if (state.refreshingWorkbench) return;
  state.refreshingWorkbench = true; renderWorkbench(); renderSelection();
  try { acceptWorkbench(await query('/api/workbench')); }
  catch (error) { state.workbenchCurrent = false; state.workbenchReason = 'workbench_unknown'; state.workbenchError = error.message; }
  finally { state.refreshingWorkbench = false; renderWorkbench(); renderSelection(); }
}
async function refreshObservations() { await Promise.all([refreshSessions(), refreshWorkbench(), refreshCodexThreads()]); }
function codexFresh() { return codexState.available && performance.now() - codexState.received <= 30000; }
function validCodexPage(value, offset) {
  const count = item => Number.isSafeInteger(item) && item >= 0;
  const date = item => typeof item === 'string' && Number.isFinite(Date.parse(item));
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  if (!value || value.available !== true || value.reason !== null || value.source !== 'codex_local' || !date(value.sampled_at)
    || value.offset !== offset || value.limit !== 12 || !count(value.total_visible) || !Array.isArray(value.items)
    || value.items.length !== Math.min(12, Math.max(0, value.total_visible - offset)) || !value.coverage) return false;
  const coverage = value.coverage;
  if (![coverage.total_count, coverage.archived_count, coverage.excluded_subagent_count, coverage.excluded_other_source_count].every(count)
    || coverage.total_count !== value.total_visible + coverage.archived_count + coverage.excluded_subagent_count + coverage.excluded_other_source_count) return false;
  const keys = new Set();
  for (const item of value.items) {
    if (!item || typeof item.thread_id !== 'string' || !uuid.test(item.thread_id) || item.session_key !== `codex:${item.thread_id}` || keys.has(item.session_key)
      || typeof item.cwd !== 'string' || !date(item.created_at) || !date(item.updated_at) || item.archived !== false
      || !['cli', 'exec', 'vscode'].includes(item.source_kind) || item.activity_state !== 'unknown' || item.task_completion !== 'unknown') return false;
    keys.add(item.session_key);
  }
  const next = value.items.length && offset + value.items.length < value.total_visible ? offset + value.items.length : null;
  return value.next_offset === next;
}
function clearCodex(error) {
  Object.assign(codexState, { items: [], selected: null, available: false, received: 0, sampledAt: null, total: null, coverage: {}, nextOffset: null, error });
}
function renderCodexCoverage() {
  const fresh = codexFresh(), coverage = codexState.coverage;
  $('codex-status').textContent = fresh ? `本机身份索引 · 采样于 ${timeText(codexState.sampledAt)} · 证据年龄 ${((performance.now() - codexState.received) / 1000).toFixed(1)} 秒` : '本机 Codex 身份索引未确认';
  $('codex-coverage').textContent = fresh ? `本页 ${codexState.items.length} / 交互 ${codexState.total} · 全库 ${coverage.total_count} · 排除归档 ${coverage.archived_count}、子代理 ${coverage.excluded_subagent_count}、其他来源 ${coverage.excluded_other_source_count}` : '当前记录不可用；不显示旧记录为当前身份。';
  $('codex-notice').textContent = codexState.error;
  $('refresh-codex').disabled = codexState.refreshing;
  $('codex-previous').disabled = codexState.refreshing || codexState.offset === 0;
  $('codex-next').disabled = codexState.refreshing || !fresh || codexState.nextOffset === null;
  $('codex-page-range').textContent = fresh ? `${codexState.offset + (codexState.items.length ? 1 : 0)}–${codexState.offset + codexState.items.length} / ${codexState.total}` : '';
}
function renderCodexThreads() {
  renderCodexCoverage();
  const root = $('codex-list'), focused = document.activeElement?.dataset.sessionKey;
  root.replaceChildren();
  if (codexFresh()) for (const item of codexState.items) {
    const button = element('button', undefined, 'session codex-card');
    button.dataset.sessionKey = item.session_key; button.dataset.threadId = item.thread_id;
    button.setAttribute('aria-pressed', String(codexState.selected === item.session_key));
    button.append(element('strong', `Codex · ${item.thread_id.slice(0, 8)}`), element('span', item.cwd, 'small wrap'),
      element('span', `${item.source_kind} · 更新于 ${timeText(item.updated_at)} · 活动未知`, 'small muted'), element('code', item.thread_id, 'wrap'));
    button.addEventListener('click', () => selectCodexThread(item.session_key)); root.append(button);
  }
  if (!root.children.length) root.append(element('p', codexFresh() ? '本页没有未归档交互记录。' : '尚无可用 Codex 身份记录。', 'small muted'));
  if (focused) Array.from(root.querySelectorAll('button')).find(node => node.dataset.sessionKey === focused)?.focus();
  const item = codexFresh() && codexState.items.find(row => row.session_key === codexState.selected);
  $('codex-detail').replaceChildren();
  if (item) {
    const facts = element('dl', undefined, 'facts');
    for (const [label, value] of [['来源', `本机 Codex · ${item.source_kind}`], ['创建时间', timeText(item.created_at)], ['更新时间', timeText(item.updated_at)], ['归档', '未归档'], ['活动 / 完成', '未知 / 未判断'], ['采样时间', timeText(codexState.sampledAt)]]) {
      const pair = element('div'); pair.append(element('dt', label), element('dd', value)); facts.append(pair);
    }
    $('codex-detail').append(element('h3', 'Codex 身份元数据'), element('code', item.thread_id, 'wrap'), element('p', item.cwd, 'path wrap'), facts,
      element('p', '只读身份索引；未读取正文，不提供实时工作状态。', 'small muted'));
  } else $('codex-detail').append(element('p', '从 Codex 列表选择一条真实身份记录。', 'small muted'));
  renderEvidenceFlow();
}
async function refreshCodexThreads(offset = codexState.offset) {
  if (codexState.refreshing) return;
  if (offset !== codexState.offset) { clearCodex(''); codexState.offset = offset; renderCodexThreads(); }
  codexState.refreshing = true; renderCodexCoverage();
  const started = performance.now();
  try {
    const result = await query(`/api/codex/threads?offset=${offset}`);
    if (performance.now() - started > 30000) throw new Error('Codex sampling expired');
    if (!validCodexPage(result, offset)) throw new Error('Codex index unavailable');
    Object.assign(codexState, { items: result.items, offset, nextOffset: result.next_offset, total: result.total_visible, coverage: result.coverage,
      available: true, received: started, sampledAt: result.sampled_at, error: '' });
    const linked = new URLSearchParams(location.search).get("codex");
    if (linked && result.items.some(item => item.thread_id === linked)) codexState.selected = "codex:" + linked;
    else if (!result.items.some(item => item.session_key === codexState.selected)) codexState.selected = null;
  } catch { clearCodex(performance.now() - started > 30000 ? 'Codex 身份采样已过期，请刷新。' : '本机 Codex 身份索引不可用，请刷新。'); }
  finally { codexState.refreshing = false; renderCodexThreads(); }
}
function expireCodexThreads() {
  if (codexState.available && !codexFresh()) { clearCodex('Codex 身份采样已过期，请刷新。'); renderCodexThreads(); }
  else renderCodexCoverage();
}
function selectCodexThread(key) {
  if (!codexFresh() || !codexState.items.some(item => item.session_key === key)) return;
  codexState.selected = key;
  state.epoch++; state.selected = null; state.page = null; state.pageOld = false; state.reading = false;
  $('messages').replaceChildren();
  for (const id of ['read-time', 'message-notice', 'message-metadata-notice', 'action-notice']) $(id).textContent = '';
  updatePaging(); $('focus-session').disabled = true; $('refresh-messages').disabled = true;
  $('detail-heading').textContent = '远端 Claude · 等待选择';
  renderSessions(); renderSelection(); renderCodexThreads();
}
function current(record) { return state.connected && age(record) <= 30000; }
function eligible(record) { return current(record) && age(record) <= 5000 && recordView(record).category === 'confirmed' && record.identity_state === 'verified' && record.terminal_binding === 'confirmed'; }
function badge(text, verified = false) { return element('span', text, 'badge' + (verified ? ' verified' : '')); }
function registrationFromCurrentProvenance(record) {
  const rows = record.provenance?.registrations;
  return current(record) && Array.isArray(rows) ? (rows.length ? 'present' : 'missing') : 'unknown';
}
function recordView(record) {
  const registration = record.registration_state ?? registrationFromCurrentProvenance(record);
  const category = !current(record) || !['present', 'missing'].includes(registration) ? 'unknown'
    : registration === 'missing' ? 'retained'
    : record.identity_state === 'verified' && record.terminal_binding === 'confirmed' ? 'confirmed' : 'pending';
  const label = category === 'retained' ? `保留记录 · ${record.slot ? '上次槽位 ' + record.slot : '上次槽位未知'}`
    : category === 'unknown' ? '当前未观察 · 上次证据'
    : `${record.slot || '槽位未知'} · ${category === 'confirmed' ? '位置已确认' : '待关联'}`;
  return { sessionId: record.session_id, label, category };
}
function currentReasons(record) {
  if (!current(record)) return [state.sourceReason || 'observation_stale'];
  if (recordView(record).category === 'retained') return ['registration_missing'];
  if (recordView(record).category === 'unknown') return ['source_unknown'];
  return record.reason_codes || [];
}
function renderCoverage() {
  const fresh = state.connected && state.snapshotAge !== null && state.snapshotAge + performance.now() - state.received <= 30000;
  const coverage = state.coverage;
  $('coverage').textContent = `${fresh ? '' : '上次采样 · '}注册 ${coverage.registration_count ?? '未知'} · 进程 ${coverage.process_count ?? '未知'} · 位置确认 ${coverage.confirmed_binding_count ?? '未知'} · 索引 ${state.records.length}（含保留/未知）`;
  $('connection-dot').classList.toggle('ok', fresh);
  if (state.connected && !fresh) $('connection-state').textContent = '来源观察已陈旧 · 上次证据';
  renderEvidenceFlow();
}
function renderEvidenceFlow() {
  const codex = codexFresh() && codexState.items.find(item => item.session_key === codexState.selected);
  const record = state.records.find(item => item.session_id === state.selected);
  const fresh = state.connected && state.snapshotAge !== null && state.snapshotAge + performance.now() - state.received <= 30000;
  const verified = record && current(record) && record.identity_state === 'verified';
  const canFocus = record && eligible(record) && terminalReady() && state.csrf;
  const key = codex ? codex.session_key : state.selected || '';
  if ($('mapping-key').textContent !== key) $('mapping-copy-notice').textContent = '';
  $('mapping-key').textContent = key;
  $('mapping-summary').hidden = !key;
  $('copy-mapping-key').disabled = !key;
  $('flow-heading').textContent = codex ? '本机 Codex · 所选身份映射' : key ? '远端 Claude · 所选会话映射' : '会话证据链 · 等待选择';
  const labels = codex ? ['本机来源', '身份索引', '完整身份键', '历史能力', '终端位置'] : ['固定来源', '真实 UUID 索引', '身份核验', '按需历史', '终端位置'];
  const nodes = codex ? [
    ['source', '本机 Codex 身份元数据', true], ['index', `本页 ${codexState.items.length} 条真实身份`, true],
    ['identity', '所选身份已索引；位置证据未提供', true], ['message', '正文未接入', false], ['terminal', '终端绑定未接入', false]
  ] : [
    ['source', fresh ? '本次来源已观察' : '来源未确认 / 陈旧', fresh],
    ['index', `${state.records.length} 条${fresh ? '' : '留存'}记录`, fresh && state.records.length > 0],
    ['identity', !record ? '等待选中 UUID' : verified ? '所选身份已核验' : '所选身份未确认', Boolean(verified)],
    ['message', state.reading ? '按需读取中' : !state.page ? '尚未读取' : state.pageOld ? '上次读取结果' : '已读取所选历史页', Boolean(state.page && !state.pageOld)],
    ['terminal', !record ? '等待选中 UUID' : canFocus ? '所选位置可定位' : '所选位置未确认', Boolean(canFocus)]
  ];
  nodes.forEach(([id, text, available], index) => { const node = $(`flow-${id}`); node.dataset.available = String(available); node.querySelector('strong').textContent = labels[index]; node.querySelector('.flow-value').textContent = text; });
  if (!key) return;
  $('mapping-detail-link').href = codex ? '#codex-detail' : '#selected';
  $('mapping-refresh-link').href = codex ? '#refresh-codex' : '#refresh-sessions';
  $('mapping-history-link').hidden = Boolean(codex);
  $('mapping-workbench-link').hidden = Boolean(codex);
  if (codex) {
    $('mapping-source').textContent = `本机 Codex · ${codex.source_kind} · 采样 ${timeText(codexState.sampledAt)} · 证据年龄 ${((performance.now() - codexState.received) / 1000).toFixed(1)} 秒`;
    $('mapping-identity').textContent = '完整身份键与本页记录一致；身份已索引，位置证据未提供。活动 / 完成：未知 / 未判断。';
    $('mapping-history').textContent = '正文未接入；当前只读身份元数据。';
    $('mapping-terminal').textContent = '终端绑定未接入；不会按目录猜测位置。';
    $('mapping-workbench').textContent = '所选 Codex 没有定位能力。';
    $('mapping-next').textContent = '可复制完整身份键、查看身份详情或刷新 Codex 索引。';
    return;
  }
  const valid = record && current(record), evidenceAge = record ? age(record) : Infinity;
  $('mapping-source').textContent = `${valid ? '本次' : '上次'}来源 · ${sourceText(record?.source)}${record?.source?.id ? ` · ${record.source.id}` : ''} · 采样 ${timeText(record?.observed_at)} · 证据年龄 ${Number.isFinite(evidenceAge) ? (evidenceAge / 1000).toFixed(1) + ' 秒' : '未知'}`;
  $('mapping-identity').textContent = `${record ? recordView(record).label : reasonText('not_found')} · ${verified ? '身份已核验' : '身份未确认'}；记录使用此完整 UUID。活动 / 完成：未知 / 未判断。`;
  $('mapping-history').textContent = state.reading ? '正在按需读取所选历史页。' : state.page ? `${state.pageOld ? '上次读取结果' : '已读取所选历史页'} · 读取 ${timeText(state.page.read_at)}；reader 结果独立于当前位置证据。` : $('message-notice').textContent || '尚未读取；可在历史区刷新最近消息。';
  const position = record && (valid && record.terminal_binding === 'confirmed' ? `本次位置 · ${record.slot || '槽位未知'} → ${record.pane_id}`
    : `位置未确认${record.pane_id ? ` · 上次位置 ${record.slot || '槽位未知'} → ${record.pane_id}` : ''}`);
  const recordReasons = record ? currentReasons(record).map(reasonText).join('；') : reasonText('not_found');
  $('mapping-terminal').textContent = `${position || '位置未确认'}${record?.instance_key ? ` · 实例 ${record.instance_key}` : ''}${recordReasons ? ` · ${recordReasons}` : ''}${evidenceAge > 5000 ? ' · 动作需要 5 秒内的唯一绑定；请刷新观察。' : ''}`;
  const workbenchReasons = state.workbenchError || reasonText(state.workbenchBlocked ? 'cleanup_unverified' : state.workbenchReason);
  $('mapping-workbench').textContent = canFocus ? '当前所选位置可定位；切换已有 Zellij pane。' : `${workbenchReasons ? workbenchReasons + ' · ' : ''}${state.connecting || state.focusing ? '终端动作正在核验。' : !terminalReady() ? '客户端或归属未确认，或终端证据需刷新。' : !state.csrf ? '动作凭据未确认。' : '所选位置尚不满足原定位条件。'}${workbenchAge() > 5000 ? ' 工作台动作证据也需在 5 秒内，请刷新终端状态。' : ''}`;
  $('mapping-next').textContent = canFocus ? '查看身份与历史后，可在详情中定位原终端。' : '可复制完整 UUID、查看独立历史页或刷新观察；定位条件见上方原因与工作台详情。';
}
function renderSessions() {
  const root = $('sessions');
  const focusedId = document.activeElement?.dataset.sessionId;
  root.replaceChildren();
  const views = state.records.map(record => ({ record, view: recordView(record) }));
  for (const [category, heading] of [['confirmed', '位置已确认'], ['pending', '待关联'], ['retained', '保留记录'], ['unknown', '当前未观察']]) {
    const rows = views.filter(item => item.view.category === category).sort((a, b) => a.view.sessionId.localeCompare(b.view.sessionId));
    if (!rows.length) continue;
    const group = element('section', undefined, 'session-group'); group.dataset.category = category;
    group.append(element('h3', `${heading} · ${rows.length}`, 'session-group-heading'));
    for (const { record, view } of rows) {
      const valid = current(record);
      const button = element('button', undefined, 'session'); button.dataset.sessionId = record.session_id;
      button.setAttribute('aria-pressed', String(state.selected === record.session_id));
      const title = element('div', undefined, 'session-title'); title.append(element('strong', view.label), element('code', record.session_id.slice(0, 8)));
      const status = element('div', undefined, 'status-row');
      status.append(badge(valid && record.identity_state === 'verified' ? '身份已核验' : '身份未知', valid && record.identity_state === 'verified'), badge(valid && record.process_state === 'observed_running' ? '进程已观察' : '进程未知'), badge(valid && record.terminal_binding === 'confirmed' ? '位置已确认' : '位置未知'));
      const codes = currentReasons(record);
      button.append(title, element('span', record.cwd || '目录未知', 'small'), status,
        element('span', codes.length ? codes.map(reasonText).join('；') : '本次观察未发现关联异常', 'small session-reason'),
        element('span', `${valid ? '本次采样' : '上次采样'}：${timeText(record.observed_at)} · 证据年龄 ${Number.isFinite(age(record)) ? (age(record) / 1000).toFixed(1) + ' 秒' : '未知'}`, 'small muted'),
        element('span', record.session_id, 'small muted'));
      button.addEventListener('click', () => selectSession(record.session_id)); group.append(button);
    }
    root.append(group);
  }
  if (!state.records.length) root.append(element('p', '尚无会话证据；不会按目录猜测绑定。', 'small muted'));
  if (focusedId) Array.from(root.querySelectorAll('button[data-session-id]')).find(node => node.dataset.sessionId === focusedId)?.focus();
  renderCoverage();
}
function renderSelection() {
  renderEvidenceFlow();
  const record = state.records.find(item => item.session_id === state.selected);
  $('selection-empty').hidden = Boolean(state.selected); $('selected').hidden = !state.selected;
  if (!state.selected) return;
  $('detail-heading').textContent = record ? `${recordView(record).label} · 对话` : '会话详情 · 未找到该 UUID';
  $('session-id').textContent = state.selected; $('cwd').textContent = record?.cwd || '目录未确认';
  const valid = record && current(record);
  const facts = [
    ['当前注册', valid ? ({ present: '本次存在', missing: '本次未观察到注册', unknown: '未知' }[record.registration_state ?? registrationFromCurrentProvenance(record)] || '未知') : '未知'],
    ['身份', valid && record.identity_state === 'verified' ? '已核验' : '未知'],
    ['进程', valid && record.process_state === 'observed_running' ? '观察到存活' : '未知'],
    ['终端位置', valid && record.terminal_binding === 'confirmed' ? record.pane_id : '未知'],
    ['任务活动 / 完成', '未知 / 未判断'], ['最后成功观察', timeText(record?.last_success_at)],
    ['证据年龄', record && Number.isFinite(age(record)) ? `${(age(record) / 1000).toFixed(1)} 秒` : '未知']
  ];
  $('facts').replaceChildren(...facts.map(([label, value]) => { const item = element('div'); item.append(element('dt', label), element('dd', value)); return item; }));
  const canFocus = record && eligible(record) && terminalReady() && state.csrf;
  $('focus-session').disabled = !canFocus;
  $('focus-hint').textContent = canFocus ? '切换已有 Zellij pane；请切到 WezTerm 查看' : state.workbenchBlocked ? '终端回收未确认；请核查'
    : state.workbenchCurrent && state.workbench?.server_state === 'running' && state.workbench.association_state === 'disconnected' && state.workbench.can_connect ? '先连接现有工作台，再刷新观察'
    : !terminalReady() ? '客户端或归属未确认，或终端证据需刷新' : '需要 5 秒内的唯一绑定；请刷新观察';
  $('selection-notice').textContent = !record ? reasonText('not_found') : `${!valid ? '当前证据未知；保留上次观察，不据此定位。' : '本次原因：'}${currentReasons(record).map(reasonText).join('；') || '未发现关联异常'}`;
  const previous = record?.last_evidence;
  $('last-evidence-notice').textContent = previous ? `上次采样留存原因（采样于 ${timeText(previous.observed_at)}）：${(previous.reason_codes || []).map(reasonText).join('；') || '未提供原因'}。历史原因发生时间未核验。` : '尚无上次采样留存证据；历史原因发生时间未核验。';
  $('message-metadata-notice').textContent = state.page && !valid ? '元数据未确认；本页为上次读取。消息分页仍按 reader 独立结果。' : '';
  const evidence = record ? { current: { source: record.source, observed_at: record.observed_at, last_success_at: record.last_success_at, registration_state: valid ? record.registration_state ?? registrationFromCurrentProvenance(record) : 'unknown', pid: record.pid, process_start_ticks: record.process_start_ticks, registered_started_at: record.registered_started_at, instance_key: record.instance_key, pane_id: valid ? record.pane_id : null, reason_codes: currentReasons(record), provenance: record.provenance }, last_evidence: previous || null, history_time_note: '上次采样留存；历史原因发生时间未核验' } : { status: 'unknown', reason: 'not_found' };
  $('evidence').textContent = JSON.stringify(evidence, null, 2);
}
async function refreshSessions() {
  if (state.refreshing) return;
  state.refreshing = true; $('refresh-sessions').disabled = true;
  try {
    const result = await query('/api/sessions');
    if (!Array.isArray(result.sessions) || !['observed', 'unknown'].includes(result.status)) throw new Error(reasonText('gateway_query_failed'));
    state.received = performance.now(); state.records = result.sessions; state.connected = result.status === 'observed';
    state.coverage = result.coverage || {}; state.snapshotAge = result.age_ms ?? null; state.sourceReason = result.reason || null;
    $('source').textContent = sourceText(result.source);
    const coverage = result.coverage || {};
    $('coverage-warnings').replaceChildren(...(coverage.warnings || []).map(item => element('li', coverageText(item))));
    $('connection-state').textContent = state.connected ? `来源观察于 ${timeText(result.observed_at)}` : '来源未确认 · 上次证据';
    $('list-notice').textContent = result.reason ? reasonText(result.reason) : '';
  } catch (error) {
    state.connected = false; state.sourceReason = 'gateway_unavailable'; $('connection-state').textContent = '断连 · 显示上次证据'; $('list-notice').textContent = error.message;
  } finally {
    $('connection-dot').classList.toggle('ok', state.connected); state.refreshing = false; $('refresh-sessions').disabled = false; renderSessions(); renderSelection();
  }
}
function selectSession(sessionId) {
  if (!state.records.some(record => record.session_id === sessionId) || state.selected === sessionId) return;
  codexState.selected = null; renderCodexThreads();
  state.epoch++; state.selected = sessionId; state.page = null; state.pageOld = false; state.reading = false;
  $('messages').replaceChildren(); $('read-time').textContent = ''; $('message-notice').textContent = ''; $('action-notice').textContent = ''; updatePaging(); renderSessions(); renderSelection();
  readMessages();
}
function updatePaging() {
  const page = state.page;
  $('older').disabled = state.reading || state.pageOld || !page || page.offset === 0;
  $('newer').disabled = state.reading || state.pageOld || !page || page.next_offset === null;
  $('refresh-messages').disabled = state.reading;
  $('page-range').textContent = page ? `${page.offset + (page.items.length ? 1 : 0)}–${page.offset + page.items.length} / ${page.total_visible}` : '';
}
function renderMessages(page) {
  $('messages').replaceChildren(...page.items.map(item => {
    const card = element('article', undefined, 'message');
    const heading = element('div', undefined, 'message-head'); heading.append(element('strong', item.type === 'assistant' ? 'Claude' : item.type === 'user' ? '用户' : item.type), element('span', item.uuid)); card.append(heading);
    const message = item.message;
    const content = message?.content ?? message;
    const blocks = Array.isArray(content) ? content : [content];
    for (const block of blocks) {
      const text = typeof block === 'string' ? block : block?.type === 'text' ? block.text : JSON.stringify(block, null, 2);
      card.append(element('div', text, 'message-content'));
    }
    const details = element('details'); details.append(element('summary', '消息证据'), element('pre', JSON.stringify({ type: item.type, uuid: item.uuid, session_id: item.session_id, parent_tool_use_id: item.parent_tool_use_id, parent_agent_id: item.parent_agent_id }, null, 2))); card.append(details); return card;
  }));
  $('read-time').textContent = `本页读取时间：${timeText(page.read_at)} · reader ${page.reader_version}；单条消息时间未提供`;
}
async function readMessages(offset = null, revision = null, limit = 20) {
  if (!state.selected || state.reading) return;
  const epoch = state.epoch; const sessionId = state.selected;
  state.reading = true; updatePaging(); $('message-notice').textContent = '正在读取所选对话…';
  const params = new URLSearchParams({ limit: String(limit) }); if (offset !== null) params.set('offset', offset); if (revision) params.set('revision', revision);
  try {
    const page = await query(`/api/sessions/${sessionId}/messages?${params}`);
    if (epoch !== state.epoch) return;
    if (!page.available) throw new Error(reasonText(page.reason) || '消息未验证');
    state.page = page; state.pageOld = false; renderMessages(page); $('message-notice').textContent = ''; renderSelection();
  } catch (error) {
    if (epoch !== state.epoch) return;
    state.pageOld = Boolean(state.page); $('message-notice').textContent = `${error.message}${state.page ? ' · 下方为上次结果' : ''}`;
  } finally { if (epoch === state.epoch) { state.reading = false; updatePaging(); renderEvidenceFlow(); } }
}
async function focusSession() {
  const sessionId = state.selected, record = state.records.find(item => item.session_id === sessionId);
  if (!sessionId || !record || !eligible(record) || !terminalReady() || !state.csrf || $('focus-session').disabled) return;
  state.focusing = true; renderWorkbench();
  $('focus-session').disabled = true; $('action-notice').textContent = '正在重新核验身份和终端位置…';
  try {
    const result = await query(`/api/sessions/${sessionId}/focus`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-CSRF': state.csrf }, body: '{}' });
    if (result.reason === 'cleanup_unverified') state.workbenchBlocked = true;
    if (state.selected === sessionId) $('action-notice').textContent = result.focused ? `已确认 Zellij 选中 ${result.pane_id}；请切到 WezTerm 查看` : reasonText(result.reason) || '定位未确认';
  } catch (error) { if (error.result?.reason === 'cleanup_unverified') state.workbenchBlocked = true; if (state.selected === sessionId) $('action-notice').textContent = error.message; }
  finally { state.focusing = false; await refreshObservations(); }
}
async function connectWorkbench() {
  if (!canConnect() || $('connect-workbench').disabled) return;
  state.connecting = true; state.workbenchNotice = '正在核验现有工作台与新 GUI…'; renderWorkbench(); renderSelection();
  try {
    const result = await query('/api/workbench/connect', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-CSRF': state.csrf }, body: '{}' });
    if (result.reason === 'cleanup_unverified' || result.cleanup === 'unverified') state.workbenchBlocked = true;
    if (typeof result.connected !== 'boolean' || typeof result.launched !== 'boolean' || !['unique_observed', 'unknown'].includes(result.status)
      || !['verified', 'unverified', 'not_needed'].includes(result.cleanup) || !(result.reason === null || typeof result.reason === 'string')) throw new Error(reasonText('workbench_invalid_response'));
    acceptWorkbench(result.workbench);
    state.workbenchNotice = result.connected === true && typeof result.launched === 'boolean' && result.status === 'unique_observed' && terminalAssociated()
      ? `${result.launched ? '已连接现有工作台' : '工作台已关联，未重复创建窗口'} · 唯一关联观察；可见性未核验`
      : reasonText(result.reason) || '连接尚未确认';
  } catch (error) {
    state.workbenchCurrent = false; state.workbenchNotice = error.message;
    if (error.result?.reason === 'cleanup_unverified' || error.result?.cleanup === 'unverified') state.workbenchBlocked = true;
  } finally { state.connecting = false; await refreshObservations(); }
}
$('refresh-sessions').addEventListener('click', refreshObservations);
$('refresh-codex').addEventListener('click', () => refreshCodexThreads());
$('codex-previous').addEventListener('click', () => refreshCodexThreads(Math.max(0, codexState.offset - 12)));
$('codex-next').addEventListener('click', () => { if (codexState.nextOffset !== null) refreshCodexThreads(codexState.nextOffset); });
$('refresh-workbench').addEventListener('click', () => { state.workbenchNotice = ''; refreshWorkbench(); });
$('connect-workbench').addEventListener('click', connectWorkbench);
$('refresh-messages').addEventListener('click', () => readMessages());
$('older').addEventListener('click', () => readMessages(Math.max(0, state.page.offset - 20), state.page.revision, Math.min(20, state.page.offset)));
$('newer').addEventListener('click', () => readMessages(state.page.next_offset, state.page.revision));
$('focus-session').addEventListener('click', focusSession);
$('copy-id').addEventListener('click', async () => { try { await navigator.clipboard.writeText(state.selected); $('action-notice').textContent = '完整 UUID 已复制'; } catch { $('action-notice').textContent = '复制不可用，请手动选择上方完整 UUID'; } });
$('copy-mapping-key').addEventListener('click', async () => {
  const codex = codexFresh() && codexState.items.find(item => item.session_key === codexState.selected);
  const key = codex ? codex.session_key : state.selected;
  if (!key) return;
  try { await navigator.clipboard.writeText(key); $('mapping-copy-notice').textContent = '完整身份键已复制'; }
  catch { $('mapping-copy-notice').textContent = '复制不可用，请手动选择上方完整身份键'; }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) { refreshObservations(); } });
setInterval(() => { if (!document.hidden) { refreshObservations(); } }, 10000);
setInterval(() => { if (!document.hidden) { renderWorkbench(); renderSelection(); renderCoverage(); expireCodexThreads(); if (state.records.some(record => age(record) > 30000)) renderSessions(); } }, 1000);
query('/api/bootstrap').then(result => {
  state.csrf = result.csrf_token; renderWorkbench(); renderSelection();
}).catch(error => { $('list-notice').textContent = error.message; });
refreshObservations();
