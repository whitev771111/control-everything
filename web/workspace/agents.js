'use strict';

window.WorkspaceAgents = (() => {
  const kinds = { codex: 'Codex', dsh: 'DeepSeek Harness', custom: '自定义 Agent' };
  let fetchJson = null, postJson = null, state = { revision: 0, items: [], tokens: {} }, editing = null, busy = false, bound = false, board = null, timer = null;
  let adapters = [], catalogAvailable = false, adapterId = '', connectionDraft = {}, connectionControls = new Map(), draftItems = null, draftRevision = null;
  const $ = id => document.getElementById(id);
  const editable = row => ({ id: row.id, name: row.name, kind: row.kind, command: row.command, enabled: row.enabled,
    ...(Array.isArray(row.project_ids) ? { project_ids: [...row.project_ids] } : {}),
    ...(Object.hasOwn(row, 'adapter') ? { adapter: row.adapter } : {}),
    ...(row.connection && typeof row.connection === 'object' ? { connection: JSON.parse(JSON.stringify(row.connection)) } : {}),
    permission: row.permission || 'workspace-write',
    network: row.network || (row.adapter === 'dsh' ? 'allowed' : 'denied'),
    approval: row.approval || 'request_user' });
  function renderConnection() {
    const select = $('agent-adapter'), root = $('agent-connection-fields'); if (!select || !root) return;
    select.replaceChildren();
    const add = (value, label) => { const option = document.createElement('option'); option.value = value; option.textContent = label; select.append(option); };
    add('', '普通登记（稍后配置接入）');
    for (const adapter of adapters) add(adapter.id, adapter.name);
    if (adapterId && !adapters.some(row => row.id === adapterId)) add(adapterId, '已保存的接入方式（定义暂不可用）');
    select.value = adapterId;
    const adapter = adapters.find(row => row.id === adapterId);
    $('agent-kind').disabled = !!adapter;
    $('agent-command').required = !!adapter?.fields.find(field => field.key === 'command')?.required;
    $('agent-adapters-status').textContent = catalogAvailable ? '连接参数仅登记在本机；保存后还需在外部 Agent 配置工作台连接。' : '接入方式目录暂不可用；仍可编辑普通登记，已有连接参数会保留。';
    $('agent-setup').textContent = adapter ? `${adapter.description || ''}\n${adapter.setup || ''}` : adapterId ? '保留已有接入方式和连接参数；目录恢复后可继续配置。' : '选择接入方式可填写参数；登记不会启动外部程序。';
    root.replaceChildren(); connectionControls = new Map();
    for (const field of adapter?.fields || []) {
      if (field.key === 'command') continue;
      const label = document.createElement('label'); label.className = 'field full';
      label.textContent = field.type === 'args' ? `${field.label}（JSON 字符串数组）` : field.label;
      const input = document.createElement(field.type === 'args' ? 'textarea' : 'input');
      input.id = 'agent-connection-' + field.key;
      input.required = !!field.required; input.maxLength = field.type === 'args' ? 16384 : 4096;
      input.placeholder = field.type === 'args' ? '["--example", "value"]' : field.placeholder || '';
      const stored = connectionDraft[field.key];
      input.value = stored == null ? '' : typeof stored === 'string' ? stored : JSON.stringify(stored);
      connectionControls.set(field.key, { input, field, initial: input.value });
      label.append(input);
      if (field.type === 'args' && typeof stored === 'string') { const hint = document.createElement('small'); hint.textContent = '已有旧格式未修改时会保留；修改时请使用 JSON 数组。'; label.append(hint); }
      if (field.help) { const help = document.createElement('small'); help.textContent = field.help; label.append(help); }
      root.append(label);
    }
  }
  function collectConnection() {
    const value = { ...connectionDraft };
    for (const [key, { input, field, initial }] of connectionControls) {
      const text = input.value.trim();
      if (!text && field.required) throw new Error(`请填写${field.label}。`);
      if (input.value === initial) continue;
      if (!text) { delete value[key]; continue; }
      if (field.type === 'args') {
        let args; try { args = JSON.parse(text); } catch { throw new Error(`${field.label}须填写 JSON 字符串数组，例如 ["--example"]。`); }
        if (!Array.isArray(args) || args.length > 20 || args.some(row => typeof row !== 'string' || [...row].length > 512 || /[\x00-\x1f]/.test(row))) throw new Error(`${field.label}最多 20 个字符串参数，每个最多 512 字。`);
        value[key] = args;
      } else {
        if ([...text].length > 2048 || /[\x00-\x1f]/.test(text)) throw new Error(`${field.label}最多 2048 字且不能包含控制字符。`);
        if (field.type === 'url') {
          let url; try { url = new URL(text); } catch { throw new Error(`${field.label}须填写有效的本机 HTTP 地址。`); }
          if (!['http:', 'https:'].includes(url.protocol) || !['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname) || url.username || url.password) throw new Error(`${field.label}须使用本机地址，且不能包含用户名或密码。`);
        }
        value[key] = text;
      }
    }
    return value;
  }
  function changeAdapter() {
    const next = $('agent-adapter').value;
    if (next === adapterId) return;
    if ((Object.keys(connectionDraft).length || $('agent-command').value.trim() || [...connectionControls.values()].some(row => row.input.value.trim())) && !window.confirm('切换接入方式将清空当前连接参数并更新命令，确认切换？')) { $('agent-adapter').value = adapterId; return; }
    adapterId = next; connectionDraft = {};
    const adapter = adapters.find(row => row.id === adapterId);
    if (adapter) { $('agent-kind').value = adapter.kind_suggestion || 'custom'; $('agent-command').value = adapter.default_command || ''; }
    $('agent-network').value = next === 'dsh' ? 'allowed' : 'denied';
    renderConnection();
  }
  const note = (text, failed = false) => {
    const node = $('agent-feedback'); node.textContent = text; node.classList.toggle('error', failed);
    if ($('agent-form').hidden) {
      $('agents-status').textContent = text; $('agents-status').classList.toggle('error', failed);
    }
  };
  const button = (text, className, onClick) => {
    const node = document.createElement('button'); node.type = 'button'; node.className = className; node.textContent = text;
    node.addEventListener('click', onClick); return node;
  };
  function render() {
    const root = $('agents-list'); if (!root) return;
    root.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement('p'); empty.className = 'empty-state'; empty.textContent = '还没有注册 Agent。';
      root.append(empty); return;
    }
    for (const item of state.items) {
      const card = document.createElement('article'); card.className = 'agent-card';
      const heading = document.createElement('div'); heading.className = 'section-heading';
      const copy = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = item.name;
      const token = state.tokens[item.id];
      const age = Date.now() - Date.parse(item.last_seen_at);
      const online = item.enabled && item.online === true && Number.isFinite(age) && age >= 0 && age < 60000;
      const last = item.last_seen_at ? `最近心跳 ${new Date(item.last_seen_at).toLocaleTimeString()}` : '尚无心跳';
      const meta = document.createElement('p'); meta.className = 'section-note';
      meta.textContent = `${adapters.find(row => row.id === item.adapter)?.name || kinds[item.kind] || item.kind} · ${item.command || '未填写命令'} · ${item.permission || 'workspace-write'} · ${item.enabled ? '已启用' : '已禁用'} · ${online ? '在线' : '离线 / 联系未知'} · ${last} · ${token?.has_token ? `token ${token.prefix}…` : '未生成 token'}`;
      const scope = document.createElement('p'); scope.className = 'section-note'; scope.textContent = item.project_ids?.length ? `授权范围：${item.project_ids.length} 个项目` : '授权范围：全部项目';
      copy.append(title, meta, scope);
      const actions = document.createElement('div'); actions.className = 'form-actions';
      actions.append(button('编辑', 'button secondary', () => open(item)),
                     button(token?.has_token ? '重置 token' : '生成 token', 'button secondary', () => rotate(item)),
                     button('移除', 'button secondary', () => remove(item.id)));
      heading.append(copy, actions); card.append(heading); root.append(card);
    }
  }
  function open(item = null) {
    if (busy) return;
    editing = item ? item.id : null;
    draftItems = state.items.map(editable); draftRevision = state.revision;
    adapterId = item?.adapter || ''; connectionDraft = item?.connection ? JSON.parse(JSON.stringify(item.connection)) : {};
    $('agent-form-title').textContent = item ? '编辑 Agent' : '注册 Agent';
    $('agent-name').value = item?.name || '';
    $('agent-kind').value = item?.kind || 'codex';
    $('agent-command').value = item?.command || '';
    $('agent-permission').value = item?.permission || 'workspace-write';
    $('agent-network').value = item?.network || (item?.adapter === 'dsh' ? 'allowed' : 'denied');
    $('agent-approval').value = item?.approval || 'request_user';
    $('agent-enabled').checked = item ? item.enabled : true;
    renderConnection();
    $('agent-form').hidden = false;
    $('agent-name').focus();
    note('保存只登记身份与命令，不安装、不启动、不发送任务。');
  }
  function close() {
    editing = null; $('agent-form').hidden = true; $('agent-form').reset(); $('agent-enabled').checked = true;
    draftItems = null; draftRevision = null; adapterId = ''; connectionDraft = {}; connectionControls = new Map();
    $('agent-token').textContent = '';
  }
  async function load() {
    if (!fetchJson) return;
    try {
      const [registry, catalog] = await Promise.allSettled([fetchJson('/api/workspace/agents'), fetchJson('/api/workspace/agent-adapters')]);
      catalogAvailable = catalog.status === 'fulfilled' && Array.isArray(catalog.value?.adapters) && catalog.value.adapters.every(row => row && typeof row.id === 'string' && typeof row.name === 'string' && Array.isArray(row.fields) && row.fields.every(field => field && typeof field.key === 'string' && /^[a-z][a-z0-9_]*$/.test(field.key)));
      adapters = catalogAvailable ? catalog.value.adapters : [];
      if (registry.status === 'rejected') throw registry.reason;
      const value = registry.value;
      state = { revision: value.revision, items: value.items || [], tokens: value.tokens || {} };
      render(); $('agents-status').textContent = `已注册 ${state.items.length} 个 Agent`;
      return true;
    } catch (error) {
      $('agents-status').textContent = `Agent 注册表读取失败：${error.message}`;
      return false;
    }
  }
  async function save(event) {
    event.preventDefault();
    if (busy) return;
    const name = $('agent-name').value.trim();
    if (!name) { note('请填写显示名。', true); $('agent-name').focus(); return; }
    let connection; try { connection = collectConnection(); } catch (error) { note(error.message, true); return; }
    if ($('agent-command').required && !$('agent-command').value.trim()) { note('请填写命令或可执行文件路径。', true); return; }
    const permission = $('agent-permission').value;
    if (permission === 'danger-full-access' && !window.confirm('完全访问会让该 Agent 读写项目外文件并执行系统命令。只应对可信任务开启。确认保存？')) return;
    busy = true; $('agent-fields').disabled = true;
    try {
      const items = (draftItems || state.items).map(editable), previous = items.find(row => row.id === editing);
      const next = { id: editing, name, kind: $('agent-kind').value, command: $('agent-command').value.trim() || null,
                     enabled: $('agent-enabled').checked,
                     permission, network: $('agent-network').value, approval: $('agent-approval').value,
                     ...(Array.isArray(previous?.project_ids) ? { project_ids: [...previous.project_ids] } : {}),
                     ...(adapterId || Object.hasOwn(previous || {}, 'adapter') ? { adapter: adapterId || null } : {}),
                     ...(adapterId || Object.hasOwn(previous || {}, 'connection') ? { connection } : {}) };
      if (editing) items.splice(items.findIndex(row => row.id === editing), 1, next); else items.push(next);
      const value = await postJson('/api/workspace/agents', { expected_revision: draftRevision ?? state.revision, items });
      state = { revision: value.revision, items: value.items || [], tokens: value.tokens || {} };
      close(); render(); $('agents-status').textContent = `已注册 ${state.items.length} 个 Agent`;
      note('Agent 已保存。');
      if (board) void board.load();
    } catch (error) { note(`${error.message}。输入已保留。`, true); }
    finally { busy = false; $('agent-fields').disabled = false; }
  }
  async function rotate(item) {
    if (busy) return;
    if (state.tokens[item.id]?.has_token && !window.confirm('重置后旧 token 立即失效，确认重置？')) return;
    busy = true;
    try {
      const value = await postJson('/api/workspace/agents/token', { id: item.id });
      state = { revision: value.agents.revision, items: value.agents.items, tokens: value.agents.tokens };
      render();
      $('agent-token').textContent = `Token（只显示一次，请立即复制）：${value.token}`;
      if (window.setTimeout) window.setTimeout(() => { $('agent-token').textContent = ''; }, 60000);
      note('Token 已生成；旧 token 已失效。');
    } catch (error) { note(`${error.message}。`, true); }
    finally { busy = false; }
  }
  async function remove(identity) {
    if (busy || !window.confirm('移除这个 Agent 登记？不会结束任何外部进程。')) return;
    busy = true;
    try {
      const items = state.items.filter(row => row.id !== identity)
        .map(editable);
      const value = await postJson('/api/workspace/agents', { expected_revision: state.revision, items });
      state = { revision: value.revision, items: value.items || [], tokens: value.tokens || {} };
      render(); $('agents-status').textContent = `已注册 ${state.items.length} 个 Agent`; note('Agent 登记已移除。');
    } catch (error) { note(`${error.message}。`, true); }
    finally { busy = false; }
  }
  function bind() {
    if (bound) return; bound = true;
    $('agent-new').addEventListener('click', async () => { if (await load()) open(); });
    $('refresh-agents').addEventListener('click', () => load());
    $('cancel-agent').addEventListener('click', close);
    $('agent-form').addEventListener('submit', save);
    $('agent-kind').addEventListener('change', () => {
      $('agent-command').placeholder = '命令或可执行文件路径，例如 C:/example/agent.exe（可留空）';
    });
    $('agent-adapter')?.addEventListener('change', changeAdapter);
  }
  function init(options) {
    fetchJson = options.fetchJson; postJson = options.postJson;
    bind();
    board = window.WorkspaceAgentBoard?.mount({ fetchJson, postJson, sandboxButton: options.sandboxButton,
      onRegister: () => {
        const show = async () => { if (await load() && window.location.hash === '#settings') { open(); $('agent-form').scrollIntoView?.({ block: 'center' }); $('agent-name').focus(); } };
        if (window.location.hash === '#settings') show();
        else { window.addEventListener('hashchange', show, { once: true }); window.location.hash = 'settings'; }
      },
      onAddProject: () => $('new-project')?.click(),
      onProject: value => { window.location.hash = value === '__global__' ? 'global' : value ? `project/${encodeURIComponent(value)}/tasks` : 'overview'; },
    });
    if (board) {
      void board.load();
      for (const identity of ['onboarding-dialog', 'project-dialog']) $(identity)?.addEventListener('close', () => { void board.load(); });
      if (window.setInterval && !timer) timer = window.setInterval(() => {
        if (!$('agent-board').hidden && !document.hidden) void board.load();
        board.refreshPresence();
        if (window.location.hash === '#settings') render();
      }, 15000);
    }
    return { load };
  }
  return { init, load };
})();

// The board consumes browser-authorized management APIs; it never uses Agent tokens.
// Task briefs and manual handoffs use browser permissions, never Agent tokens.
window.WorkspaceTaskDispatch = (() => {
  function create({ root, item, agents, active = false, fetchJson, postJson, isCurrent = () => true, isBusy = () => false, onBusy = () => {}, onDirty = () => {}, onChanged = async () => {} }) {
    const el = (tag, text, identity) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (identity) node.id = identity; return node; };
    const field = (text, control) => { const label = el('label', text); label.className = control.type === 'checkbox' ? 'checkbox-field' : 'field'; label.append(control); return label; };
    const button = (text, fn, identity) => { const node = el('button', text, identity); node.type = 'button'; node.className = 'button secondary'; node.addEventListener('click', fn); return node; };
    const option = (select, value, text) => { const node = el('option', text); node.value = value; select.append(node); };
    const base = '/api/workspace/tasks/' + encodeURIComponent(item.id);
    let revision = null, busy = false, dirty = false, preview = null, unknownWrite = false;
    root.className = 'agent-detail-section task-dispatch';
    const status = el('p', '正在读取事项说明与交接记录…', 'td-status'); status.setAttribute('role', 'status');
    const fields = el('fieldset'), form = el('form');
    const controls = {};
    for (const [key, text, max] of [['request_text', '需求说明（必填）', 20000], ['acceptance_text', '验收标准（建议填写）', 10000], ['constraints_text', '约束与注意事项', 10000], ['references', '参考资料（每行一条，最多20条）', 42000]]) {
      const input = el('textarea', undefined, 'td-' + key); input.rows = key === 'request_text' ? 5 : 3; input.maxLength = max;
      controls[key] = input; fields.append(field(text, input));
      input.addEventListener('input', () => changed());
    }
    const directory = el('select', undefined, 'td-directory'); directory.addEventListener('change', () => changed());
    fields.append(field('执行目录', directory));
    const save = el('button', '保存事项说明', 'td-save'); save.type = 'submit'; save.className = 'button secondary'; fields.append(save); form.append(fields);
    const delivery = el('fieldset'), agent = el('select', undefined, 'td-agent'); option(agent, '', '选择交接给哪个 Agent');
    for (const row of agents.filter(row => row.enabled && (!row.project_ids?.length || row.project_ids.includes(item.project_id)))) option(agent, row.id, row.name);
    agent.value = agents.some(row => row.enabled && row.id === item.assigned_agent_id) ? item.assigned_agent_id : '';
    const auto = el('input', undefined, 'td-auto'); auto.type = 'checkbox'; auto.checked = false;
    function syncAuto() {
      const selected = agents.find(row => row.id === agent.value);
      auto.disabled = !selected || !['dsh', 'codex'].includes(selected.adapter);
      if (auto.disabled) auto.checked = false;
    }
    agent.addEventListener('change', () => { syncAuto(); clearPreview(); });
    auto.addEventListener('change', () => { clearPreview(); message(auto.checked
      ? '自动执行会由工作台在后台启动所选 Agent；项目目录必须已确认。'
      : '当前采用手动交接，不会启动程序。'); });
    const previewButton = button('预览交接内容', () => run(async () => {
      if (dirty) throw new Error('请先保存事项说明，再预览交接。');
      if (!agent.value) throw new Error('请选择 Agent。');
      const body = { agent_id: agent.value, directory_id: directory.value || null };
      if (auto.checked) body.delivery_mode = 'auto';
      const result = await postJson(base + '/dispatch-preview', body);
      if (!isCurrent()) return;
      if (result.status !== 'preview' || typeof result.content_text !== 'string' || !/^[a-f0-9]{64}$/.test(result.brief_sha256 || '') || result.dispatch?.brief_revision !== revision) throw new Error('事项说明已变化或预览不完整，请重新读取核对。');
      preview = { ...result, agent_id: agent.value, directory_id: directory.value || null, delivery_mode: auto.checked ? 'auto' : 'manual' };
      content.value = result.content_text; confirm.checked = false;
      message(`${auto.checked ? '确认后由工作台后台启动所选 Agent。' : '确认后只保存交接记录。'}本次权限：${result.dispatch?.permission || 'workspace-write'}；网络：${result.dispatch?.network || 'allowed'}。`);
    }), 'td-preview');
    const content = el('textarea', undefined, 'td-content'); content.readOnly = true; content.rows = 12;
    const confirm = el('input', undefined, 'td-confirm'); confirm.type = 'checkbox'; confirm.checked = false;
    confirm.addEventListener('change', sync);
    const createButton = button('确认保存交接快照', () => run(async () => {
      if (!preview || !confirm.checked || dirty || unknownWrite) return;
      const expected = preview;
      const body = { agent_id: expected.agent_id, directory_id: expected.directory_id, expected_brief_revision: revision, preview_sha256: expected.brief_sha256, confirmed: true };
      if (expected.delivery_mode === 'auto') body.delivery_mode = 'auto';
      const result = await postJson(base + '/dispatch', body);
      if (!isCurrent()) return;
      if (!['previewed', 'existing'].includes(result.status) || !result.dispatch?.id) throw new Error('交接保存凭据不完整，请重新读取记录核对。');
      const changedContent = result.dispatch.brief_sha256 !== expected.brief_sha256;
      clearPreview();
      await refreshHistory(); await onChanged();
      if (expected.delivery_mode === 'auto') {
        if (changedContent) { message('保存后的材料与预览已变化，请重新核对历史中的实际快照；尚未启动执行。'); return; }
        if (result.dispatch.delivery_mode !== 'auto' || result.dispatch.delivery_state !== 'previewed' || result.dispatch.run_id !== null) {
          message('已有交接快照已关联运行或状态不允许执行，请重新核对历史；本次未启动 Agent。'); return;
        }
        const context = await fetchJson('/api/workspace/dispatches/' + encodeURIComponent(result.dispatch.id) + '/execution-context');
        if (!context?.context_hash) throw new Error('执行上下文未准备完整，请核对交接记录。');
        await postJson('/api/workspace/dispatches/' + encodeURIComponent(result.dispatch.id) + '/execute', { context_hash: context.context_hash, confirmed: true });
        await refreshHistory(); await onChanged();
        message('已确认并由工作台后台启动 Agent。状态与结果会在工作台显示，仍需你验收。');
      } else {
        message(changedContent ? '保存后的材料与预览已变化，请重新核对历史中的实际快照；尚未标记发送。' : '交接记录已保存。请从记录中复制内容，手动交给 Agent 后再标记已发送。');
      }
    }, true), 'td-create');
    delivery.append(field('目标 Agent', agent), field('由工作台后台自动执行（DSH / Codex）', auto),
      el('p', '未勾选时只保存手动交接内容，不启动程序；勾选后由工作台后台启动所选 Agent，不会打开窗口。'), previewButton,
      field('完整交接预览', content), field('我已核对内容，确认保存本次交接快照', confirm), createButton);
    const history = el('fieldset', undefined, 'td-history');
    const reload = button('重新读取说明与交接记录', () => {
      if (dirty && !window.confirm('重新读取会放弃本次事项说明草稿，确认继续？')) return;
      return load();
    });
    root.append(el('h3', '事项说明与手动交接'), el('p', '修改说明用于下一次交接，已有快照保持原样。交接后的结果仍需人工审核。'), status, reload, form, delivery, el('h4', '交接历史'), history);
    function message(text) { status.textContent = text; }
    function clearPreview() { preview = null; confirm.checked = false; content.value = ''; sync(); }
    function changed() { dirty = true; onDirty(true); clearPreview(); }
    function sync() {
      fields.disabled = busy || revision === null;
      delivery.disabled = busy || revision === null || active || ['review', 'done'].includes(item.state) || !item.project_id;
      delivery.hidden = active || ['review', 'done'].includes(item.state);
      content.parentElement.hidden = confirm.parentElement.hidden = createButton.hidden = !preview;
      previewButton.disabled = busy || dirty || unknownWrite;
      createButton.disabled = busy || dirty || !preview || !confirm.checked || unknownWrite;
      history.disabled = busy;
      reload.disabled = busy;
    }
    async function run(fn, mutation = false) {
      if (busy || mutation && isBusy() || !isCurrent()) return;
      if (mutation && unknownWrite) { message('上次结果需要核对，请先重新读取说明与交接记录；不会重复提交。'); return; }
      busy = true; if (mutation) onBusy(true); sync();
      try { await fn(); }
      catch (error) {
        if (!isCurrent()) return;
        if (mutation && !error.local) { unknownWrite = true; clearPreview(); }
        const reason = { brief_changed: '事项说明已被另一处修改。草稿保留，请核对后重新读取。', dispatch_preview_changed: '标题、目录或交接材料已变化，尚未保存快照；请重新读取并预览。', run_conflict: 'Agent 已领取事项，当前不能再交接。', dispatch_state_conflict: '交接状态已变化，请重新读取历史。', project_required: '请先将事项归属到项目。' }[error.reason] || error.message;
        message(`${reason}${mutation && !error.local ? ' 不会自动重试，请重新读取记录核对结果。' : ''}`);
      } finally { busy = false; if (mutation) onBusy(false); if (isCurrent()) sync(); }
    }
    async function refreshHistory() {
      const result = await fetchJson(base + '/dispatches');
      if (!isCurrent()) return;
      if (!Array.isArray(result.items)) throw new Error('交接记录未读完整。');
      history.replaceChildren();
      const labels = { previewed: '已保存 · 尚未标记发送', sent: '已手动发送 · 未确认接收', delivered: '已确认接收', unknown: '送达未知', failed: '交接失败', cancelled: '已取消交接记录' };
      for (const record of result.items) {
        const row = el('section'); row.className = 'dispatch-record';
        row.append(el('p', `${record.run_id ? 'Agent 已领取 · 已关联执行记录' : labels[record.delivery_state] || '状态未知'} · ${record.created_at || '时间未知'}`), el('p', `Agent：${agents.find(a => a.id === record.agent_id)?.name || '原 Agent'} · 执行目录：${record.directory_path || '未提供'}`));
        if (record.run_id) row.append(el('p', `已关联执行：${record.run_id}；发送记录不代表任务已完成。`));
        const details = el('details'), text = el('textarea'); text.readOnly = true; text.rows = 10; text.value = record.content_text || '';
        details.append(el('summary', '查看本次交接快照'), field('已保存的交接文本', text), button('选择交接文本', () => { text.focus(); text.select?.(); })); row.append(details);
        if (record.delivery_state === 'previewed' && !record.run_id) {
          const sentConfirm = el('input'); sentConfirm.type = 'checkbox'; sentConfirm.checked = false;
          const mark = button('我已手动发送给 Agent', () => run(async () => {
            if (!sentConfirm.checked) { message('请先手动发送内容并勾选确认。'); return; }
            await postJson('/api/workspace/dispatches/' + encodeURIComponent(record.id) + '/sent', {});
            await refreshHistory(); message('已记录你确认发送。是否收到与开始执行，需等待 Agent 真实领取和上报。');
          }, true));
          row.append(field('我已将本次快照手动粘贴并发送给目标 Agent', sentConfirm), mark);
        }
        if (['previewed', 'sent', 'delivered'].includes(record.delivery_state) && !record.run_id) row.append(button('取消这条交接记录', () => run(async () => {
          if (!window.confirm('确认取消交接记录？这不会停止已在外部启动的 Agent 或工具。')) return;
          await postJson('/api/workspace/dispatches/' + encodeURIComponent(record.id) + '/cancel', {});
          await refreshHistory(); message('交接记录已取消；外部执行不会因此停止。');
        }, true)));
        history.append(row);
      }
      if (!result.items.length) history.append(el('p', '尚无交接记录。'));
    }
    async function load() {
      return run(async () => {
        const result = await fetchJson(base + '/brief');
        if (!isCurrent()) return;
        if (result.item?.id !== item.id || !Number.isSafeInteger(result.brief?.revision) || !Array.isArray(result.directories)) throw new Error('事项说明未读完整。');
        revision = result.brief.revision;
        for (const key of ['request_text', 'acceptance_text', 'constraints_text']) controls[key].value = result.brief[key] || '';
        controls.references.value = (result.brief.references || []).join('\n');
        directory.replaceChildren(); option(directory, '', result.directories.length > 1 ? '请选择执行目录' : '未选择目录');
        for (const row of result.directories) option(directory, row.binding_id, row.path);
        directory.value = result.brief.execution_directory_id || (result.directories.length === 1 ? result.directories[0].binding_id : '');
        dirty = false; unknownWrite = false; onDirty(false); clearPreview(); await refreshHistory(); message('已读取事项说明与交接记录。');
      });
    }
    form.addEventListener('submit', event => {
      event.preventDefault();
      return run(async () => {
        if (revision === null) return;
        const references = controls.references.value.split(/\r?\n/).map(row => row.trim()).filter(Boolean);
        if (!controls.request_text.value.trim() || references.length > 20 || references.some(row => [...row].length > 2048)) throw Object.assign(new Error('需求说明必填，参考资料最多20条且每条最多2048字。'), { local: true });
        const body = { expected_revision: revision, request_text: controls.request_text.value, acceptance_text: controls.acceptance_text.value, constraints_text: controls.constraints_text.value, references, execution_directory_id: directory.value || null };
        if (new TextEncoder().encode(JSON.stringify(body)).length > 65536) throw Object.assign(new Error('说明提交超过64 KiB，请缩短内容。'), { local: true });
        const result = await postJson(base + '/brief', body);
        if (!isCurrent()) return;
        if (!Number.isSafeInteger(result.brief?.revision)) throw new Error('未收到有效的保存版本。');
        revision = result.brief.revision; dirty = false; onDirty(false); clearPreview(); message('事项说明已保存；已有交接快照不会改变。');
      }, true);
    });
    syncAuto();
    sync();
    const ready = load();
    return { ready };
  }
  return { create };
})();
window.WorkspaceTaskMessages = (() => {
  function create({ root, item, run, reviews = [], agents = [], fetchJson, postJson, isCurrent = () => true, isBusy = () => false, onBusy = () => {}, onDirty = () => {} }) {
    let busy = false, unknown = false, loaded = false, clientToken = null;
    const el = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
    const field = (label, input) => { const node = el('label', label); node.className = 'field'; node.append(input); return node; };
    const option = (select, value, label) => { const node = el('option', label); node.value = value; select.append(node); };
    root.append(el('h3', '补充要求与手动交接'));
    root.append(el('p', '先保存内容，再手动发送到原 Agent 会话。已发送仅表示你的操作记录；工具授权仍需回原 Agent 处理。'));
    const form = el('form'), fields = el('fieldset'), kind = el('select'); kind.id = 'tm-kind';
    for (const [value, label] of [['supplement','补充要求'],['user_input','回复所需信息'],['return','转交退回意见'],['note','事项备注']]) option(kind, value, label);
    kind.value = 'supplement';
    const agent = el('select'); agent.id = 'tm-agent'; option(agent, '', '请选择接收 Agent');
    for (const value of agents.filter(value => value.enabled && (!value.project_ids?.length || value.project_ids.includes(item.project_id)))) option(agent, value.id, value.name);
    agent.value = agents.some(value => value.enabled && value.id === item.assigned_agent_id) ? item.assigned_agent_id : '';
    const body = el('textarea'); body.id = 'tm-body'; body.rows = 5; body.maxLength = 10000; body.required = true;
    const confirm = el('input'); confirm.id = 'tm-confirm'; confirm.type = 'checkbox';
    const consent = field('我已核对接收 Agent 和内容，确认保存本次手动交接记录', confirm); consent.className = 'session-confirm';
    const save = el('button', '确认保存交接内容'); save.id = 'tm-save'; save.type = 'submit'; save.className = 'button primary';
    const discard = el('button', '放弃补充草稿'); discard.type = 'button'; discard.className = 'button secondary';
    const status = el('p', '正在读取交接记录…'); status.id = 'tm-status'; status.setAttribute('role', 'status');
    const refresh = el('button', '重新核对消息记录'); refresh.type = 'button'; refresh.className = 'button secondary';
    const history = el('fieldset'); history.id = 'tm-history';
    fields.append(field('内容用途', kind), field('接收 Agent', agent), field('补充内容（最多 10000 字）', body), consent, save, discard); form.append(fields); root.append(form, status, refresh, history);
    function sync() { fields.disabled = busy || unknown; history.disabled = busy || unknown; refresh.disabled = busy; save.disabled = busy || unknown || !loaded || !confirm.checked; }
    function changed() { clientToken = null; confirm.checked = false; onDirty(true); sync(); }
    body.addEventListener('input', changed); agent.addEventListener('change', changed);
    kind.addEventListener('change', () => { if (kind.value === 'return' && !body.value) { const review = reviews[0]; if (review && ['changes_requested','rejected'].includes(review.verdict)) body.value = review.reason || ''; } changed(); });
    confirm.addEventListener('change', () => { onDirty(true); sync(); });
    function clear() { body.value = ''; confirm.checked = false; clientToken = null; onDirty(false); }
    discard.addEventListener('click', () => { if (busy || isBusy()) return; if (window.confirm('放弃尚未保存的补充内容？')) { clear(); sync(); } });
    function render(rows) {
      history.replaceChildren(el('legend', `最近交接记录 · ${rows.length}（最多 100 条）`));
      if (!rows.length) history.append(el('p', '暂无交接记录。'));
      for (const row of rows) {
        const entry = el('article'); entry.className = 'agent-detail-section';
        const receiver = agents.find(value => value.id === row.agent_id)?.name || row.agent_id || '未指定 Agent';
        entry.append(el('h4', `${receiver} · ${{previewed:'已保存，尚未标记发送',sent:'已手动发送，送达未确认',cancelled:'已取消记录'}[row.delivery_state] || '状态未知'}`));
        const text = el('textarea'); text.readOnly = true; text.rows = 5; text.value = `事项：${item.title}\n事项 ID：${item.id}\n用途：${row.kind}\n${row.run_id ? '运行 ID：' + row.run_id + '\n' : ''}\n${row.body_text}`; text.setAttribute('aria-label', '可复制的补充交接内容'); entry.append(text);
        if (row.delivery_state === 'previewed') {
          const sent = el('input'); sent.type = 'checkbox';
          const consent = field('我已手动复制并发送这条内容给目标 Agent', sent); consent.className = 'session-confirm';
          const send = el('button', '标记这条内容已发送'); send.type = 'button'; send.className = 'button secondary';
          send.addEventListener('click', () => { if (sent.checked) void action(() => postJson('/api/workspace/messages/' + encodeURIComponent(row.id) + '/sent', {})); });
          const cancel = el('button', '取消这条未发送记录'); cancel.type = 'button'; cancel.className = 'button secondary';
          cancel.addEventListener('click', () => { if (window.confirm('确认取消这条未发送记录？这不会停止外部 Agent。')) void action(() => postJson('/api/workspace/messages/' + encodeURIComponent(row.id) + '/cancel', {})); });
          entry.append(consent, send, cancel);
        }
        history.append(entry);
      }
    }
    async function read() {
      const result = await fetchJson('/api/workspace/tasks/' + encodeURIComponent(item.id) + '/messages');
      if (!isCurrent()) return false;
      if (result?.item?.id !== item.id || !Array.isArray(result.items) || result.items.some(value => value.task_id !== item.id)) throw new Error('response mismatch');
      if (unknown && clientToken && result.items.some(value => value.client_token === clientToken && value.body_text === body.value.trim())) clear();
      if (unknown) confirm.checked = false;
      loaded = true; unknown = false; render(result.items); return true;
    }
    async function action(write = null) {
      if (busy || isBusy() || write && unknown) return;
      busy = true; if (write) onBusy(true); sync();
      let committed = false;
      try { if (write) { await write(); committed = true; } if (await read()) status.textContent = '记录已核对；保存不会自动发送，已发送不等于已送达。'; }
      catch { if (isCurrent()) { if (write) unknown = true; status.textContent = committed ? '操作已确认，但记录刷新失败；请重新核对，不会自动重试。' : write ? '操作结果未知；请重新核对消息记录，不会自动重试。' : '消息记录读取失败，请重试。'; } }
      finally { busy = false; if (write) onBusy(false); if (isCurrent()) sync(); }
    }
    refresh.addEventListener('click', () => { void action(); });
    form.addEventListener('submit', event => {
      event.preventDefault(); if (busy || isBusy() || unknown || !loaded || !confirm.checked) return;
      const text = body.value.trim(); if (!text || text.length > 10000 || !agent.value) { status.textContent = '请填写有效内容并选择接收 Agent。'; return; }
      if (!clientToken) clientToken = window.crypto.randomUUID();
      const payload = {kind:kind.value,body_text:text,agent_id:agent.value,client_token:clientToken}; if (run?.id) payload.run_id = run.id;
      if (new TextEncoder().encode(JSON.stringify(payload)).length > 20480) { status.textContent = '内容超过接口字节限制，请缩短后保存。'; return; }
      void action(async () => {
        const result = await postJson('/api/workspace/tasks/' + encodeURIComponent(item.id) + '/messages', payload);
        if (!isCurrent()) return;
        if (result?.message?.task_id !== item.id || result.message.client_token !== clientToken || result.message.body_text !== text) throw new Error('response mismatch');
        clear();
      });
    });
    void action();
    return {};
  }
  return { create };
})();
window.WorkspaceAgentBoard = (() => {
  const labels = { unassigned: '未分配', assigned: '已分配', running: '进行中', waiting_user: '等你处理', waiting_review: '待审核', done: '已完成', blocked: '阻塞', failed: '失败' };
  const states = { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待审核', done: '已完成' };
  let mounted = null, context = { view: 'overview', project: '', tab: 'tasks' };
  function groupItems(items) {
    const groups = Object.fromEntries(Object.keys(labels).map(key => [key, []]));
    for (const item of items) {
      const key = item.state === 'done' ? 'done' : item.state === 'review' ? 'waiting_review' : item.assignment_state === 'waiting_user' ? 'waiting_user' : item.state === 'blocked' ? 'blocked'
        : Object.hasOwn(labels, item.assignment_state) && item.assignment_state !== 'done' ? item.assignment_state : item.assigned_agent_id ? 'assigned' : 'unassigned';
      groups[key].push(item);
    }
    return groups;
  }
  function create({ root, fetchJson, postJson, onRegister = () => {}, onAddProject = () => {}, onProject = null, sandboxButton = null, now = () => Date.now() }) {
    let items = [], projects = [], agents = [], request = 0, detailsRequest = 0, selectedProject = '', complete = false, writing = false, selected = null;
    let loaded = false, detailDirty = false, briefDirty = false, sessionDirty = false, messageDirty = false, registryAvailable = false, disposed = false, attentionOnly = false;
    let approvalPanel = null;
    const shownApprovals = new Set();
    const hasDraft = () => detailDirty || briefDirty || sessionDirty || messageDirty;
    const el = (tag, cls = '', text) => { const node = document.createElement(tag); node.className = cls; if (text !== undefined) node.textContent = text; return node; };
    const id = (node, name) => { node.id = name; return node; };
    const btn = (text, fn, cls = 'button secondary') => { const node = el('button', cls, text); node.type = 'button'; node.addEventListener('click', fn); return node; };
    const option = (select, value, text) => { const node = el('option', '', text); node.value = value; select.append(node); return node; };
    const field = (text, control) => { const node = el('label', 'field', text); node.append(control); return node; };
    const when = value => { const time = Date.parse(value); return Number.isFinite(time) ? new Date(time).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '未上报'; };
    const failureReason = error => ({ item_changed: '事项已被其他窗口更新', agent_run_conflict: 'Agent 已领取或正在等待审核，不能改派', review_already_submitted: 'Agent 已提交审核，请读取最新材料', run_not_found: '本次运行已结束或不可用，请读取最新状态',
      execution_managed_run: '执行仍在运行或清理尚未核验，请先在运行页核对；只有结果未知且清理已核验的执行可人工释放', execution_link_conflict: '执行关联不一致，无法释放；请核对最新运行记录',
      project_busy: '同一项目目录还有任务运行或等待审批，请处理完后再点击执行', capacity_busy: '运行名额已满，请等其他任务结束后再点击执行',
      project_required: '请先为待办选择项目，Agent 需要明确的执行目录', execution_unavailable: '执行服务尚未就绪，请核对服务状态' }[error.reason] || error.message);
    const canExecute = agent => agent?.enabled && ['dsh', 'codex'].includes(agent.adapter)
      && !Object.keys(agent.connection || {}).length && (!agent.command || agent.command === agent.adapter);
    const canAssign = (agent, projectId) => agent?.enabled && (!agent.project_ids?.length || agent.project_ids.includes(projectId));
    async function executeTask(item) {
      const base = '/api/workspace/tasks/' + encodeURIComponent(item.id);
      let loaded = await fetchJson(base + '/brief');
      if (!loaded.brief.request_text?.trim()) loaded = await postJson(base + '/brief', {
        expected_revision: loaded.brief.revision, request_text: item.title,
        acceptance_text: '', constraints_text: '', references: [],
        execution_directory_id: loaded.brief.execution_directory_id || null
      });
      const body = { agent_id: item.assigned_agent_id, directory_id: loaded.brief.execution_directory_id || null, delivery_mode: 'auto' };
      const preview = await postJson(base + '/dispatch-preview', body);
      const saved = await postJson(base + '/dispatch', { ...body, expected_brief_revision: loaded.brief.revision,
        preview_sha256: preview.brief_sha256, confirmed: true });
      const route = '/api/workspace/dispatches/' + encodeURIComponent(saved.dispatch.id);
      const context = await fetchJson(route + '/execution-context');
      if (!context?.context_hash) throw new Error('执行上下文未就绪，请在详情核对交接记录。');
      await postJson(route + '/execute', { context_hash: context.context_hash, confirmed: true });
    }
    const title = el('h2', '', 'Agent 工作台');
    const actions = el('div', 'form-actions');
    const addAgent = btn('＋ 添加 Agent', onRegister), addProject = btn('＋ 添加项目文件夹', onAddProject);
    const refresh = btn('刷新工作台', () => load());
    actions.append(btn('＋ 新建待办', () => openNew()), addAgent, addProject, refresh);
    const management = el('a', 'button secondary', '事项管理'); management.href = '#tasks'; actions.append(management);
    const header = el('div', 'section-heading'); header.append(title, actions);
    const status = id(el('p', 'section-note', '正在读取工作台…'), 'ab-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const agentStrip = id(el('div', 'agent-presence'), 'ab-agents');
    const project = id(el('select'), 'ab-project'); option(project, '', '全部项目');
    const navigation = id(el('nav', 'project-tabs'), 'ab-project-tabs'); navigation.setAttribute('aria-label', '项目内容');
    project.addEventListener('change', () => { if (onProject) onProject(project.value); else setProject(project.value); });
    const groups = id(el('div', 'agent-board-groups'), 'ab-groups');
    const detail = id(el('dialog', 'agent-task-detail'), 'ab-detail'); detail.setAttribute('aria-label', '任务进度与人工审核');
    detail.addEventListener('cancel', event => {
      if (writing || hasDraft() && !window.confirm('尚有未提交内容，关闭并放弃本次编辑？')) event.preventDefault();
      else { detailsRequest++; detailDirty = false; }
    });
    const projectScope = field('项目范围', project);
    const sandboxActions = el('div', 'form-actions');
    root.replaceChildren(header, status, agentStrip, projectScope, navigation, sandboxActions, groups, detail);
    function feedback(text, failed = false) {
      const node = detail.querySelector('#ab-feedback'); if (node) { node.textContent = text; node.classList.toggle('error', failed); }
    }
    function chosenItems() { return items.filter(item => (!selectedProject || (selectedProject === '__global__' ? !item.project_id : item.project_id === selectedProject))
      && (!attentionOnly || ['review', 'blocked'].includes(item.state) || ['waiting_user', 'waiting_review', 'blocked', 'failed'].includes(item.assignment_state))); }
    function renderAgents() {
      agentStrip.replaceChildren();
      if (!registryAvailable) { agentStrip.append(el('p', 'section-note', 'Agent 注册表不可用，暂不能分配。')); return; }
      if (!agents.length) { agentStrip.append(el('p', 'empty-state', '还没有注册 Agent，可从上方添加。')); return; }
      for (const agent of agents) {
        const age = now() - Date.parse(agent.last_seen_at);
        const presence = !agent.enabled ? '已禁用' : agent.online === true && Number.isFinite(age) && age >= 0 && age < 60000 ? '在线' : agent.last_seen_at ? '离线 / 心跳过期' : '尚无心跳';
        const card = el('div', 'agent-presence-card'); card.append(el('strong', '', agent.name), el('span', 'badge', presence), el('small', '', `最近心跳：${when(agent.last_seen_at)}`)); agentStrip.append(card);
      }
    }
    function render() {
      const focusedTask = document.activeElement?.dataset?.taskId;
      renderAgents();
      const inProject = Boolean(selectedProject && selectedProject !== '__global__');
      addAgent.hidden = addProject.hidden = projectScope.hidden = agentStrip.hidden = inProject;
      sandboxActions.replaceChildren();
      const chosenProject = projects.find(item => item.id === selectedProject);
      if (inProject && chosenProject && sandboxButton) {
        const control = sandboxButton(chosenProject);
        if (control) sandboxActions.append(control);
      }
      management.href = inProject ? `#project/${encodeURIComponent(selectedProject)}/manage` : '#tasks';
      management.textContent = inProject ? '本项目事项管理' : '事项管理';
      refresh.textContent = inProject ? '刷新项目' : '刷新工作台';
      if (complete) status.textContent = `已读取 ${chosenItems().length} 项${inProject ? ' · 仅当前项目' : ''} · 分配后由外部 Agent 领取；完成须人工审核。${registryAvailable ? '' : ' Agent 注册表读取失败。'}`;
      const selection = selectedProject; project.replaceChildren(); option(project, '', '全部项目'); option(project, '__global__', '全局事项');
      for (const p of projects) option(project, p.id, p.name); project.value = selection;
      title.textContent = selectedProject === '__global__' ? '全局事项' : selectedProject ? `${projects.find(p => p.id === selectedProject)?.name || '当前项目'} · Agent 工作台` : 'Agent 工作台';
      navigation.replaceChildren(); navigation.hidden = !selectedProject || selectedProject === '__global__';
      if (!navigation.hidden) for (const [tab, label] of [['tasks', '全部事项'], ['attention', '待处理'], ['sessions', '会话']]) {
        const link = el('a', 'project-tab', label); link.href = `#project/${encodeURIComponent(selectedProject)}/${tab}`;
        if (tab === (attentionOnly ? 'attention' : 'tasks')) link.setAttribute('aria-current', 'page'); navigation.append(link);
      }
      const grouped = groupItems(chosenItems()); groups.replaceChildren();
      for (const [key, rows] of Object.entries(grouped)) {
        if (attentionOnly && !['waiting_user', 'waiting_review', 'blocked', 'failed'].includes(key)) continue;
        if ((key === 'blocked' || key === 'failed') && !rows.length) continue;
        const section = el('section', `agent-task-group group-${key}`); section.setAttribute('aria-label', labels[key]);
        section.append(el('h3', '', `${labels[key]} · ${complete ? rows.length : '—'}`));
        if (!rows.length) section.append(el('p', 'section-note', complete ? '暂无事项' : '尚未完整读取'));
        for (const item of rows) {
          const card = el('article', 'agent-task-card');
          const openButton = btn(item.title, () => open(item), 'agent-task-title');
          openButton.dataset.taskId = item.id;
          const p = projects.find(row => row.id === item.project_id), agent = agents.find(row => row.id === item.assigned_agent_id);
          card.append(openButton, el('p', 'section-note', `${p?.name || (item.project_id ? '项目未登记' : '全局事项')} · ${agent?.name || (item.assigned_agent_id ? '原 Agent 不可用' : '未分配 Agent')}`), el('span', 'badge', states[item.state] || '状态未知'));
          if (key === 'assigned') card.append(el('p', 'section-note', !item.project_id ? '尚未派发：打开详情选择项目并执行' : '已分配，尚未确认接收；打开详情核对派发与执行'));
          if (key === 'waiting_user') card.append(el('p', 'section-note', 'Agent 正在等待你的授权、输入或决定'));
          if (item.connection_state === 'unknown' && ['running', 'waiting_user', 'blocked'].includes(key)) card.append(el('p', 'section-note', '最近联系未知 · 运行占用保留'));
          section.append(card);
        }
        groups.append(section);
      }
      if (focusedTask) [...groups.querySelectorAll('button')].find(node => node.dataset.taskId === focusedTask)?.focus({ preventScroll: true });
    }
    async function readItems() {
      let offset = 0, revision = null, result = [];
      while (true) {
        const query = new URLSearchParams({ limit: '100' });
        if (offset) { query.set('offset', String(offset)); query.set('list_revision', String(revision)); }
        const page = await fetchJson('/api/workspace/items?' + query);
        if (!Array.isArray(page.items) || !Number.isSafeInteger(page.total) || page.total < 0 || !Number.isSafeInteger(page.list_revision)) throw new Error('事项列表格式不完整');
        if (revision !== null && revision !== page.list_revision) throw new Error('事项列表已变化，请重新刷新');
        revision = page.list_revision; result.push(...page.items); offset += page.items.length;
        if (offset >= page.total) return result;
        if (!page.items.length) throw new Error('事项列表未完整读取');
      }
    }
    async function load() {
      if (disposed) return;
      const current = ++request; complete = false; status.textContent = loaded ? '正在更新，暂保留上次记录…' : '正在读取工作台…'; render();
      const results = await Promise.allSettled([fetchJson('/api/workspace'), fetchJson('/api/workspace/agents'), readItems()]);
      if (disposed || current !== request) return;
      registryAvailable = results[1].status === 'fulfilled';
      if (registryAvailable) agents = results[1].value.items || [];
      if (results[0].status === 'fulfilled' && results[0].value.projects?.status === 'available' && results[2].status === 'fulfilled') {
        projects = results[0].value.projects.items.filter(p => !p.archived);
        const visible = new Set(projects.map(p => p.id));
        items = [...new Map(results[2].value.filter(item => !item.project_id || visible.has(item.project_id)).map(item => [item.id, item])).values()];
        complete = true; loaded = true;
        status.textContent = `已读取 ${items.length} 项 · 分配后由外部 Agent 领取；完成须人工审核。${registryAvailable ? '' : ' Agent 注册表读取失败。'}`;
      } else status.textContent = '工作台读取失败或未读完整。保留上次记录，数量暂不可确认，请刷新重试。';
      render();
      if (complete && window.WorkspaceExecution?.createTaskApprovals && !detail.open && !writing && !hasDraft()
          && chosenItems().some(item => item.assignment_state === 'waiting_user')) {
        try {
          const listing = await fetchJson('/api/workspace/execution');
          if (disposed || current !== request || detail.open || writing || hasDraft()) return;
          for (const execution of listing.active || []) {
            const task = chosenItems().find(item => item.id === execution.task_id);
            if (!task || execution.state !== 'waiting_approval') continue;
            const pending = await fetchJson('/api/workspace/execution/runs/' + encodeURIComponent(execution.id) + '?after=0');
            if (disposed || current !== request || detail.open || writing || hasDraft()) return;
            if (!chosenItems().some(item => item.id === task.id)) continue;
            if (pending.run?.id !== execution.id || pending.run?.task_id !== task.id) continue;
            const ids = (pending.approvals || []).filter(row => row.status === 'pending').map(row => row.id).sort();
            const key = execution.id + ':' + ids.join(',');
            if (!ids.length || shownApprovals.has(key)) continue;
            shownApprovals.add(key); await open(task); break;
          }
        } catch { /* A failed notification never changes a task or approves a tool. */ }
      }
    }
    function setProject(value, attention = false) {
      if (selectedProject === value && attentionOnly === attention) return;
      selectedProject = value || ''; attentionOnly = attention; project.value = selectedProject; render();
    }
    function showDetail() {
      if (!detail.open) { if (typeof detail.showModal === 'function') detail.showModal(); else if (typeof detail.show === 'function') detail.show(); }
    }
    function detailHeader(item) {
      approvalPanel?.dispose(); approvalPanel = null;
      detail.replaceChildren();
      const close = btn('关闭详情', () => { if (!writing && (!hasDraft() || window.confirm('尚有未提交内容，关闭并放弃本次编辑？'))) { detailsRequest++; detail.close(); detailDirty = false; briefDirty = false; sessionDirty = false; messageDirty = false; } });
      const tools = el('div', 'form-actions');
      if (item.id) tools.append(btn('刷新进度与材料', () => { if (hasDraft()) feedback('有未提交内容，请保留或放弃草稿后再刷新详情。'); else void open(item, true); }));
      tools.append(close);
      const heading = el('div', 'section-heading'); heading.append(el('h2', '', item.title), tools); detail.append(heading);
      const note = id(el('p', 'feedback'), 'ab-feedback'); note.setAttribute('role', 'status'); note.setAttribute('aria-live', 'polite'); detail.append(note);
    }
    function openNew() {
      if (writing || detail.open && hasDraft() && !window.confirm('放弃未提交内容，新建待办？')) return;
      detailDirty = false; briefDirty = false; sessionDirty = false; messageDirty = false; detailsRequest++; detailHeader({ title: '新建待办' });
      const form = el('form'), fields = el('fieldset');
      const name = id(el('textarea'), 'ab-new-title'); name.required = true; name.maxLength = 4000; name.rows = 3;
      const targetProject = selectedProject && selectedProject !== '__global__' ? selectedProject : null;
      const owner = id(el('select'), 'ab-new-project');
      if (!targetProject) option(owner, '', '未归属项目的事项');
      for (const p of projects.filter(row => !targetProject || row.id === targetProject)) option(owner, p.id, p.name);
      owner.value = targetProject || ''; owner.disabled = Boolean(targetProject);
      const agent = id(el('select'), 'ab-new-agent');
      agent.disabled = !registryAvailable;
      const execute = id(el('input'), 'ab-new-execute'); execute.type = 'checkbox'; execute.checked = true;
      const executionNote = el('p', 'section-note');
      function syncExecute() {
        const selected = agents.find(row => row.id === agent.value);
        const invalid = agent.value && !canAssign(selected, targetProject || owner.value || null);
        execute.disabled = invalid || !canExecute(selected);
        executionNote.textContent = invalid ? '所选 Agent 无权访问当前项目；请重新选择或明确取消分配。'
          : execute.disabled ? '选择 DSH / Codex 后可创建并执行。'
          : !(targetProject || owner.value) ? '立即执行需要选择所属项目；若只想登记全局待办，请取消勾选立即执行。'
          : `创建后在项目目录启动 ${selected.name}；权限：${selected.permission || 'workspace-write'}，网络：${selected.network || 'denied'}，工具审批：${selected.approval || 'request_user'}。结果等待你审核。`;
      }
      function renderAgents() {
        const previous = agent.value;
        const available = agents.filter(row => canAssign(row, targetProject || owner.value || null));
        agent.replaceChildren(); option(agent, '', '暂不分配 Agent');
        for (const row of available) option(agent, row.id, row.name);
        if (previous && !available.some(row => row.id === previous)) option(agent, previous, '原选择无权访问当前项目，请重新选择或取消分配').disabled = true;
        agent.value = previous;
        syncExecute();
      }
      agent.addEventListener('change', syncExecute); owner.addEventListener('change', renderAgents); renderAgents();
      for (const control of [name, owner, agent]) control.addEventListener('input', () => { detailDirty = true; });
      const save = el('button', 'button primary', '确认创建待办'); save.type = 'submit';
      fields.append(field('要做什么', name), field('所属项目', owner), field('分配 Agent（可选）', agent), field('创建后立即交给所选 Agent 执行', execute), executionNote, save); form.append(fields); detail.append(form);
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (writing) return;
        if (!name.value.trim() || [...name.value].length > 2000) { feedback('请填写待办，最多 2000 字。', true); return; }
        if (agent.value && !agents.some(row => row.id === agent.value && canAssign(row, targetProject || owner.value || null))) { feedback('所选 Agent 无权访问当前项目；请重新选择或明确取消分配。', true); return; }
        if (execute.checked && !execute.disabled && !(targetProject || owner.value)) { feedback('请选择所属项目再执行；或取消勾选立即执行，仅保存全局待办。', true); return; }
        writing = true; fields.disabled = true;
        try {
          const saved = await postJson('/api/workspace/items', { title: name.value.trim(), project_id: targetProject || owner.value || null, assigned_agent_id: agent.value || null });
          detailDirty = false;
          if (execute.checked && !execute.disabled) {
            try { await executeTask(saved.item); }
            catch (error) { await load(); await open(saved.item, true); feedback(`待办已创建，执行尚未确认：${failureReason(error)}。请核对交接历史与进度，不要重复创建。`, true); return; }
          }
          await load(); detail.close();
        } catch (error) { feedback(`创建结果未确认：${failureReason(error)}。输入已保留，请先刷新核对是否创建成功，再决定是否重试。`, true); }
        finally { writing = false; fields.disabled = false; }
      }); showDetail(); name.focus();
    }
    function renderDetail(data, failure = '') {
      const item = data.item || selected; selected = item; detailHeader(item);
      const run = data.run || null;
      detail.append(el('p', 'section-note', `人工状态：${states[item.state] || '未知'} · ${item.assigned_agent_id ? '已分配 Agent' : '尚未分配 Agent'}`));
      if (failure) detail.append(el('p', 'feedback error', failure));
      const assignment = el('section', 'agent-detail-section'); assignment.append(el('h3', '', '分配 Agent'));
      const select = id(el('select'), 'ab-assignee'); option(select, '', '暂不分配');
      for (const agent of agents.filter(row => canAssign(row, item.project_id))) option(select, agent.id, agent.name);
      if (item.assigned_agent_id && !agents.some(row => row.id === item.assigned_agent_id && canAssign(row, item.project_id))) {
        const original = agents.find(row => row.id === item.assigned_agent_id);
        option(select, item.assigned_agent_id, original?.enabled ? '原 Agent 无权访问当前项目（授权范围已变化）' : '原 Agent 已禁用或移除').disabled = true;
      }
      select.value = item.assigned_agent_id || '';
      const active = run && ['claimed', 'running', 'waiting_review'].includes(run.status) || ['running', 'waiting_user', 'waiting_review'].includes(item.assignment_state);
      const unverified = !!(failure && item.assigned_agent_id);
      select.disabled = !registryAvailable || !!active || unverified || item.state === 'done';
      const assign = id(btn('确认分配', async () => {
        const value = select.value || null;
        if (writing || select.disabled || value === (item.assigned_agent_id || null)) return;
        if (value && !agents.some(row => row.id === value && canAssign(row, item.project_id))) { feedback('所选 Agent 无权访问当前项目；请重新选择或明确取消分配。', true); return; }
        if (briefDirty || sessionDirty || messageDirty) { feedback('请先保存或放弃事项说明、会话关联和补充草稿，再修改分配。'); return; }
        writing = true; assign.disabled = true; select.disabled = true;
        try {
          const saved = await postJson('/api/workspace/items/update', { id: item.id, expected_revision: item.revision, changes: { assigned_agent_id: value } });
          detailDirty = false; feedback('分配已保存，等待外部 Agent 领取。'); await load(); await open(saved.item, true);
        } catch (error) { feedback(`分配未确认：${failureReason(error)}。选择已保留，请刷新核对版本后再提交。`, true); }
        finally { writing = false; assign.disabled = false; select.disabled = !registryAvailable || !!active || unverified || item.state === 'done'; }
      }), 'ab-assign'); assign.disabled = select.disabled;
      select.addEventListener('change', () => { detailDirty = true; });
      assignment.append(field('已注册且启用的 Agent', select), assign, el('p', 'section-note', active ? '当前有活跃运行，暂不能改派。' : unverified ? '运行详情无法核对，暂不能改派。' : '分配不等于启动；Agent 离线时仍可等待其连接领取。')); detail.append(assignment);
      if (!active && !unverified && !['review', 'done'].includes(item.state) && canExecute(agents.find(row => row.id === item.assigned_agent_id))) {
        const launch = el('section', 'agent-detail-section'); launch.append(el('h3', '', '交给 Agent 执行'));
        const owner = id(el('select'), 'ab-start-project'); option(owner, '', '选择本次执行所属项目');
        for (const p of projects) option(owner, p.id, p.name);
        owner.value = item.project_id || ''; owner.disabled = Boolean(item.project_id);
        const start = id(btn('执行此待办', async () => {
          if (writing) return;
          if (hasDraft()) { feedback('请先保存或放弃当前草稿，再执行待办。', true); return; }
          if (!owner.value) { feedback('请选择所属项目，Agent 才能取得明确的执行目录。', true); return; }
          writing = true; start.disabled = true; owner.disabled = true;
          let currentItem = item;
          try {
            if (!item.project_id) currentItem = (await postJson('/api/workspace/items/update', {
              id: item.id, expected_revision: item.revision, changes: { project_id: owner.value }
            })).item;
            await executeTask(currentItem); await load(); await open(currentItem, true);
          } catch (error) {
            await load(); await open(currentItem, true);
            feedback(`待办已保存，执行尚未确认：${failureReason(error)}。请核对交接历史与进度；已有快照会复用，不需要重建待办。`, true);
          } finally { writing = false; start.disabled = false; owner.disabled = Boolean(currentItem.project_id); }
        }), 'ab-start-execution');
        launch.append(el('p', 'section-note', '已分配不代表已经派发。此按钮会将当前需求交给所选 Agent；完成后仍需你审核。'), field('执行所属项目', owner), start);
        detail.append(launch);
      }
      const progress = el('section', 'agent-detail-section'); progress.append(el('h3', '', '工作进度 · Agent 上报'));
      const events = Array.isArray(data.events) ? data.events : [];
      const reports = events.filter(e => (e.kind === 'progress' || e.kind === 'blocked') && (!run?.id || e.run_id === run.id));
      const last = reports.at(-1), payload = last?.payload || last?.payload_json;
      let report = typeof payload === 'object' && payload ? payload : {};
      if (typeof payload === 'string') { try { report = JSON.parse(payload); } catch { report = {}; } }
      const percent = typeof report.percent === 'number' && Number.isFinite(report.percent) && report.percent >= 0 && report.percent <= 100 ? report.percent : null;
      const runLabels = { claimed: '已领取', running: '运行中', waiting_review: '等待人工审核', changes_requested: '等待修改', completed: '已通过审核', failed: '失败', cancelled: '已取消' };
      const stageLabels = { analyzing: '分析', implementing: '实现', testing: '测试', blocked: '阻塞', waiting_user: '等待你处理' };
      progress.append(el('p', '', `运行状态：${runLabels[run?.status] || run?.status || '未上报'} · 阶段：${stageLabels[report.stage] || report.stage || '未上报'} · 进度：${percent === null ? '未上报' : percent + '%'}`));
      if (percent !== null) { const meter = el('progress'); meter.max = 100; meter.value = percent; meter.setAttribute('aria-label', 'Agent 自报进度'); progress.append(meter); }
      const contact = data.last_contact_at ?? run?.heartbeat_at;
      const contactAge = now() - Date.parse(contact);
      const contactUnknown = run && ['claimed', 'running'].includes(run.status) && (data.connection_state === 'unknown' || !data.connection_state && (!Number.isFinite(contactAge) || contactAge < 0 || contactAge >= 60000));
      progress.append(el('p', 'section-note', `运行最近联系：${when(contact)}`));
      if (contactUnknown) progress.append(el('p', 'feedback agent-contact-unknown', '最近没有新的运行消息，连接情况未知；运行和任务占用保留，不代表工具已停止或任务失败。'));
      if (report.message) progress.append(el('p', 'agent-report-text', report.message)); detail.append(progress);
      if (active && report.stage === 'waiting_user') {
        const waiting = id(el('section', 'agent-detail-section'), 'ab-waiting-user');
        const kinds = { tool_authorization: '等待工具授权', user_input: '等待你输入', external_approval: '等待外部批准', unknown: '等待事项未分类' };
        waiting.append(el('h3', '', kinds[report.wait_kind] || '等待你处理'), el('p', 'agent-report-text', report.message || 'Agent 未说明正在等待什么。'));
        if (report.request_id) waiting.append(el('p', 'agent-report-text', `外部请求编号：${report.request_id}`));
        waiting.append(el('p', 'section-note', `请求上报时间：${when(last?.created_at)}`));
        if (report.agent_hint) waiting.append(el('p', 'agent-report-text', `处理位置：${report.agent_hint}`));
        waiting.append(el('p', 'section-note', run?.dispatch_id && window.WorkspaceExecution?.createTaskApprovals
          ? '请在本页工具审批区核对并答复。本次工具授权与任务完成后的结果审核分别处理。'
          : '请回到对应的外部 Agent 处理此请求。这里的结果审核不能替代工具执行前的授权。'));
        detail.insertBefore(waiting, assignment);
      }
      if (run?.dispatch_id && window.WorkspaceExecution?.createTaskApprovals && !failure) {
        const panel = id(el('section', 'agent-detail-section'), 'ab-tool-approvals'), version = detailsRequest;
        detail.insertBefore(panel, assignment);
        approvalPanel = window.WorkspaceExecution.createTaskApprovals({ root: panel, taskId: item.id,
          dispatchId: run.dispatch_id, fetchJson, postJson,
          isCurrent: () => !disposed && version === detailsRequest && detail.open,
          onSettled: async () => { if (!writing && !hasDraft()) { await load(); if (version === detailsRequest && detail.open) await open(item, true); } }
        });
      }
      const output = el('section', 'agent-detail-section'); output.append(el('h3', '', 'Agent 做了什么'));
      output.append(el('p', 'section-note', '以下是 Agent 自述与提交材料，需你核验；不表示系统已验证。'), el('p', 'agent-report-text', run?.summary || 'Agent 未提交说明'));
      const evidence = Array.isArray(run?.evidence) ? run.evidence : [];
      if (!evidence.length) output.append(el('p', 'section-note', '未提交证据或变更文件清单。'));
      for (const entry of evidence) output.append(el('pre', 'agent-evidence', typeof entry === 'string' ? entry : JSON.stringify(entry, null, 2)));
      detail.append(output);
      const timeline = el('ol', 'agent-event-timeline');
      for (const event of events) { const row = el('li'); row.append(el('strong', '', event.kind || '事件'), el('small', '', when(event.created_at)), el('pre', 'agent-evidence', JSON.stringify(event.payload ?? event.payload_json ?? {}, null, 2))); timeline.append(row); }
      const history = el('details'); history.append(el('summary', '', `事件时间线 · ${events.length}`), timeline); detail.append(history);
      if (Array.isArray(data.reviews) && data.reviews.length) {
        const records = el('details'); records.append(el('summary', '', `人工审核历史 · ${data.reviews.length}`));
        for (const record of data.reviews) records.append(el('p', 'agent-report-text', `${when(record.created_at)} · ${record.verdict}\n${record.reason}`));
        detail.append(records);
      }
      if (item.state === 'review') renderReview(item);
      if (item.origin !== 'todo' && window.WorkspaceSessionList) {
        const panel = el('section', 'agent-detail-section'); detail.append(panel); const version = detailsRequest;
        const current = () => !disposed && version === detailsRequest && detail.open;
        const sessions = window.WorkspaceSessionList.create({ root: panel, taskId: item.id, agents,
          postJson, isBusy: () => writing, onBusy: value => { writing = value; },
          read: () => fetchJson('/api/workspace/tasks/' + encodeURIComponent(item.id) + '/sessions'),
          isCurrent: current });
        const editor = el('section', 'agent-detail-section'); detail.append(editor);
        window.WorkspaceSessionList.createEditor({ root: editor, item, agents, postJson, refresh: sessions.refresh,
          isCurrent: current, isBusy: () => writing, onBusy: value => { writing = value; }, onDirty: value => { sessionDirty = value; } });
      }
      if (run && ['claimed', 'running', 'waiting_review'].includes(run.status) && !failure) renderRelease(item);
      if (item.origin !== 'todo' && !failure) {
        const panel = el('section', 'agent-detail-section'); detail.append(panel); const version = detailsRequest;
        window.WorkspaceTaskMessages.create({ root: panel, item, run, reviews: data.reviews || [], agents, fetchJson, postJson,
          isCurrent: () => !disposed && version === detailsRequest && detail.open, isBusy: () => writing,
          onBusy: value => { writing = value; }, onDirty: value => { messageDirty = value; } });
      }
      if (item.origin !== 'todo' && !failure) {
        const panel = el('section'); detail.append(panel); const version = detailsRequest;
        window.WorkspaceTaskDispatch.create({ root: panel, item, agents, active: Boolean(active), fetchJson, postJson,
          isCurrent: () => !disposed && version === detailsRequest && detail.open,
          isBusy: () => writing, onBusy: value => { writing = value; }, onDirty: value => { briefDirty = value; }, onChanged: load });
      }
      showDetail();
    }
    function renderRelease(item) {
      const section = el('details', 'agent-detail-section'); section.append(el('summary', '', '人工释放本次运行'));
      section.append(el('p', 'feedback error', '释放只结束工作台中的运行占用，任务将返回待办，不会终止外部 Agent 或工具。请先在原 Agent 中停止旧执行，避免重复工作。'));
      const form = el('form'), fields = el('fieldset');
      const reason = id(el('textarea'), 'ab-release-reason'); reason.required = true; reason.maxLength = 2000; reason.rows = 2;
      const confirm = id(el('input'), 'ab-release-confirm'); confirm.type = 'checkbox'; confirm.checked = false;
      const submit = id(el('button', 'button secondary', '确认释放运行占用'), 'ab-release-submit'); submit.type = 'submit';
      for (const control of [reason, confirm]) control.addEventListener('input', () => { detailDirty = true; });
      fields.append(field('释放理由（必填）', reason), field('我已核对并停止外部旧执行，确认释放工作台占用', confirm), submit); form.append(fields); section.append(form); detail.append(section);
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (writing) return;
        if (briefDirty || sessionDirty || messageDirty) { feedback('请先保存或放弃事项说明、会话关联和补充草稿，再释放运行。'); return; }
        if (!reason.value.trim() || [...reason.value].length > 1000 || !confirm.checked) { feedback('请填写最多 1000 字的释放理由，并确认已处理外部旧执行。', true); return; }
        if (!window.confirm('确认释放本次运行？任务将返回待办；这不会停止任何外部工具。')) return;
        writing = true; fields.disabled = true;
        try {
          await postJson('/api/workspace/agent-tasks/' + encodeURIComponent(item.id) + '/cancel', { expected_revision: item.revision, reason: reason.value.trim() });
          detailDirty = false; await load(); detail.close();
        } catch (error) { feedback(`释放结果未确认：${failureReason(error)}。理由已保留；请核对最新状态，不会自动重试。`, true); }
        finally { writing = false; fields.disabled = false; }
      });
    }
    function renderReview(item) {
      const form = el('form', 'agent-review-form'), fields = el('fieldset'); fields.append(el('legend', '', '人工审核'));
      const verdict = id(el('select'), 'ab-review-verdict'); option(verdict, '', '请选择审核结论'); option(verdict, 'approved', '通过 → 已完成'); option(verdict, 'changes_requested', '要求修改 → 进行中'); option(verdict, 'rejected', '拒绝 → 待办');
      const reason = id(el('textarea'), 'ab-review-reason'); reason.required = true; reason.maxLength = 4000; reason.rows = 3;
      const evidence = id(el('textarea'), 'ab-review-evidence'); evidence.rows = 2; evidence.placeholder = '人工核验证据，每行一条（可选）';
      const confirm = id(el('input'), 'ab-review-confirm'); confirm.type = 'checkbox'; confirm.checked = false;
      const resume = id(el('input'), 'ab-review-resume'); resume.type = 'checkbox'; resume.checked = true;
      resume.disabled = !canExecute(agents.find(row => row.id === item.assigned_agent_id)) || !item.project_id;
      const impact = el('p', 'section-note', '提交会保存一条人工审核记录，并按所选结论更新事项状态。');
      verdict.addEventListener('change', () => { impact.textContent = `本次影响：${verdict.options?.[verdict.selectedIndex]?.textContent || verdict.value || '请先选择结论'}，同时保存人工审核记录。`; detailDirty = true; });
      for (const control of [reason, evidence, confirm]) control.addEventListener('input', () => { detailDirty = true; });
      const submit = id(el('button', 'button primary', '确认提交审核'), 'ab-review-submit'); submit.type = 'submit';
      fields.append(field('审核结论', verdict), field('核验结论与理由（必填）', reason), field('我的证据说明', evidence), field('驳回或要求修改后，立即交给原 Agent 按理由继续执行', resume), field('我已核对材料，确认上述状态变更', confirm), impact, submit); form.append(fields);
      form.addEventListener('submit', async event => {
        event.preventDefault(); if (writing) return;
        if (briefDirty || sessionDirty || messageDirty) { feedback('请先保存或放弃事项说明、会话关联和补充草稿，再提交人工审核。'); return; }
        if (!Object.hasOwn({ approved: 1, changes_requested: 1, rejected: 1 }, verdict.value) || !reason.value.trim() || !confirm.checked) { feedback('请选择结论、填写核验理由，并明确勾选确认。', true); return; }
        const proof = evidence.value.split(/\r?\n/).filter(row => row.trim());
        if (proof.length > 20 || proof.some(row => [...row].length > 2048) || [...reason.value].length > 2000) { feedback('理由最多 2000 字，证据最多 20 条且每条不超过 2048 字。', true); return; }
        writing = true; fields.disabled = true;
        try {
          const saved = await postJson('/api/workspace/reviews', { task_id: item.id, expected_revision: item.revision, verdict: verdict.value, reason: reason.value, evidence: proof, confirmed: true });
          detailDirty = false;
          if (verdict.value !== 'approved' && resume.checked && !resume.disabled) {
            try { await executeTask(saved.item); }
            catch (error) { await load(); await open(saved.item, true); feedback(`驳回理由已保存，后续执行尚未确认：${failureReason(error)}。请核对交接历史与进度，不要重复提交审核。`, true); return; }
          }
          await load(); detail.close();
        } catch (error) { feedback(`审核结果未确认：${failureReason(error)}。草稿已保留；请核对最新状态后重新打开详情，不会自动重发。`, true); }
        finally { writing = false; fields.disabled = false; }
      }); detail.append(form);
    }
    async function open(item, replace = false) {
      if (!replace && (writing || detail.open && hasDraft() && !window.confirm('放弃未提交内容，打开另一项？'))) return;
      const current = ++detailsRequest; selected = item; detailDirty = false; briefDirty = false; sessionDirty = false; messageDirty = false; detailHeader(item); detail.append(el('p', 'section-note', '正在读取进度与审核材料…')); showDetail();
      try {
        const data = await fetchJson('/api/workspace/agent-tasks/' + encodeURIComponent(item.id));
        if (current !== detailsRequest || disposed) return;
        if (data?.item?.id !== item.id || !Array.isArray(data.events) || data.run?.task_id && data.run.task_id !== item.id) throw new Error('详情与当前事项不一致或尚未完整读取');
        renderDetail(data);
      } catch (error) {
        if (current !== detailsRequest || disposed) return;
        renderDetail({ item, run: null, events: [] }, `运行详情暂不可用：${error.message}。没有可确认的进度或产出。`);
      }
    }
    return { load, setProject, open, dispose() { disposed = true; approvalPanel?.dispose(); request++; detailsRequest++; if (detail.open) detail.close(); },
      refreshPresence: renderAgents };
  }
  function onView(view, project = '', tab = 'tasks') {
    context = { view, project, tab };
    const root = document.getElementById('agent-board'); if (!root) return;
    const active = ['overview', 'project', 'global'].includes(view);
    root.hidden = !active;
    document.body?.classList.toggle('agent-board-active', active);
    mounted?.setProject(view === 'project' ? project : view === 'global' ? '__global__' : '', view === 'project' && tab === 'attention');
  }
  function mount(options) {
    const root = document.getElementById('agent-board'); if (!root) return null;
    mounted = create({ root, ...options }); onView(context.view, context.project, context.tab); return mounted;
  }
  // Extend the existing public view API without taking ownership of workspace.js.
  if (window.WorkspaceProjectSpace?.create) {
    const original = window.WorkspaceProjectSpace.create;
    window.WorkspaceProjectSpace.create = options => {
      const api = original(options); let project = '', currentTab = '';
      return { ...api, route(id, tab) { project = id; currentTab = tab; return api.route(id, tab); },
        show(view) { const result = api.show(view); onView(view === 'project' && ['sessions', 'session-import', 'manage'].includes(currentTab) ? 'legacy-project-content' : view, project, currentTab); return result; } };
    };
  }
  return { create, groupItems, mount, onView };
})();
