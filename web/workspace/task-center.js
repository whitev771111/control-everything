// Profile-only task management; legacy task/todo consumers keep their own adapters.
'use strict';
(() => {
  const states = { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待验收', done: '完成' };
  const providers = { codex: 'Codex', claude: 'Claude Code', grok: 'Grok', dsh: 'DSH', opencode: 'OpenCode', hermes: 'Hermes', pi: 'Pi' };
  const issues = { item_changed: '这条事项已被另一处修改。草稿已保留；请核对列表，取消后重新打开最新事项。',
    list_changed: '记录或项目登记已变化，已重新读取第一页。编辑草稿仍保留。',
    body_too_large: '提交超过 256 KiB，请缩短修改内容。', text_budget: '内容超过可编辑预算，请缩短修改内容。',
    invalid_item: '请检查事项名称、项目、工具和状态。', session_not_in_sample: '所选会话暂不在当前样本中，请重新核对关联。',
    session_project_mismatch: '所选会话与项目不匹配，请核对关联。' };
  const fields = ['project_id', 'provider_id', 'state', 'session_key'];
  const codepoints = text => [...text].length;
  const bytes = body => new TextEncoder().encode(JSON.stringify(body)).byteLength;
  function create({ root, fetchJson, postJson, canWrite, onSaved }) {
    const node = (tag, className = '', text) => {
      const value = document.createElement(tag); value.className = className;
      if (text !== undefined) value.textContent = text;
      return value;
    };
    const named = (tag, id, className = '') => { const value = node(tag, className); value.id = id; return value; };
    const button = (label, callback, className = 'text-button') => {
      const value = node('button', className, label); value.type = 'button'; value.addEventListener('click', callback); return value;
    };
    const options = (select, rows, selected = '') => {
      select.replaceChildren(...rows.map(([value, label]) => { const option = node('option', '', label); option.value = value; return option; }));
      select.value = selected;
    };
    const label = (text, control) => { const value = node('label', 'field', text); value.append(control); return value; };
    const feedback = (target, text, failed = false) => { target.textContent = text; target.classList.toggle('error', failed); };
    let projects = [], sessions = [], page = null, request = 0, reading = false, writing = false;
    let original = null, editingId = null, initialTitle = '', initialFields = {};
    const heading = node('div', 'section-heading'), title = node('h2', '', '任务中心');
    heading.append(title, button('详细新建', () => open(null), 'button secondary'));
    const hint = node('p', 'section-note', '快速记下一个下一步，也可以稍后关联项目和工具。状态均为人工记录，不会发送给工具。');
    const quick = named('form', 'item-quick-form', 'item-quick'), quickFields = node('fieldset');
    const quickTitle = named('textarea', 'item-quick-title'); quickTitle.rows = 2; quickTitle.maxLength = 4000; quickTitle.required = true;
    quickTitle.placeholder = '写下一个具体的下一步（可不选项目）';
    const quickSave = node('button', 'button primary', '快速记录'); quickSave.type = 'submit';
    quickFields.append(label('快速记录', quickTitle), quickSave); quick.append(quickFields);
    const quickFeedback = named('p', 'item-quick-feedback', 'feedback'); quickFeedback.setAttribute('role', 'status');
    const toolbar = node('div', 'task-toolbar item-toolbar');
    const scope = named('select', 'item-scope'), project = named('select', 'item-project-filter');
    const provider = named('select', 'item-provider-filter'), state = named('select', 'item-state-filter');
    options(scope, [['all', '全部事项'], ['active', '活动项目'], ['archived', '归档项目'], ['orphan', '未登记项目'], ['global', '全局事项']], 'all');
    options(provider, [['', '全部工具'], ...Object.entries(providers)]);
    options(state, [['', '全部状态'], ...Object.entries(states)]);
    toolbar.append(label('范围', scope), label('项目', project), label('工具', provider), label('状态', state));
    const status = named('p', 'items-status', 'section-note'); status.setAttribute('role', 'status');
    const list = named('div', 'items-list', 'task-list'), pager = named('div', 'items-pager', 'pager');
    const refreshButton = button('重新读取列表', () => refresh(0));
    const dialog = named('dialog', 'item-editor', 'item-editor'); dialog.setAttribute('aria-labelledby', 'item-editor-title');
    const form = named('form', 'item-form'), formTitle = named('h2', 'item-editor-title');
    const editorFields = node('fieldset'), editorTitle = named('textarea', 'item-title'); editorTitle.rows = 6; editorTitle.required = true;
    const identity = named('p', 'item-identity', 'section-note'), meter = named('p', 'item-size', 'form-hint');
    const controls = {}, grid = node('div', 'form-grid');
    for (const [key, text] of [['project_id', '所属项目（可留空）'], ['provider_id', '计划负责工具'], ['state', '人工状态'], ['session_key', '关联 Codex 会话']]) {
      controls[key] = named('select', `item-${key}`); grid.append(label(text, controls[key]));
    }
    const save = node('button', 'button primary', '保存事项'); save.type = 'submit';
    const cancel = button('取消', () => { if (!writing) dialog.close(); }, 'button secondary');
    const actions = node('div', 'form-actions'); actions.append(save, cancel);
    const editorFeedback = named('p', 'item-feedback', 'feedback'); editorFeedback.setAttribute('role', 'status');
    editorFields.append(label('事项名称', editorTitle), grid, meter, actions);
    form.append(formTitle, identity, editorFields, editorFeedback); dialog.append(form);
    root.replaceChildren(heading, hint, quick, quickFeedback, toolbar, status, list, pager, refreshButton, dialog);
    root.hidden = false;

    function projectName(id) {
      if (id === null || id === '') return '全局事项';
      const found = projects.find(row => row.id === id);
      return found ? `${found.name}${found.archived ? '（已归档）' : ''}` : `未登记项目 · ${id}`;
    }
    function syncBusy() {
      quickFields.disabled = editorFields.disabled = writing || !canWrite();
      heading.lastElementChild.disabled = writing || !canWrite();
      refreshButton.disabled = reading;
      for (const control of [scope, project, provider, state]) control.disabled = reading;
      for (const control of list.querySelectorAll('button')) control.disabled = writing || !canWrite();
      for (const control of pager.querySelectorAll('button')) control.disabled = reading || control.dataset.unavailable === 'true';
    }
    function render() {
      list.replaceChildren(); pager.replaceChildren();
      if (!page) { list.append(node('p', 'empty-state', '列表尚未读取，请重新读取。')); syncBusy(); return; }
      feedback(status, `${page.total} 条匹配事项 · 当前 ${page.items.length} 条 · 全部记录统计，不影响人工百分比`);
      if (!page.items.length) list.append(node('p', 'empty-state', '当前范围没有事项。可以直接快速记录全局事项。'));
      for (const item of page.items) {
        const row = node('article', 'task-row item-row'); row.dataset.itemId = item.id;
        const copy = node('div', 'task-copy'), meta = node('div', 'task-meta');
        copy.append(node('strong', 'item-title-copy', item.title));
        meta.append(node('span', `task-state state-${item.state}`, `${states[item.state] || '未知'} · 手工`),
          node('span', '', projectName(item.project_id)), node('span', '', providers[item.provider_id] || item.provider_id || '未分配工具'),
          node('span', 'item-origin', `${item.origin === 'todo' ? '原待办' : '原任务'} · ${item.origin_id === '' ? '空原ID' : item.origin_id}`));
        if (item.session_key) meta.append(node('span', '', { verified: '会话关联已核验', not_in_current_sample: '关联暂不在当前样本', project_mismatch: '关联与项目不匹配' }[item.session_link_state] || '关联状态未知'));
        const rowActions = node('div', 'task-actions'); rowActions.append(button('编辑详情', () => open(item)));
        copy.append(meta); row.append(node('span', `task-indicator ${item.state}`), copy, rowActions);
        list.append(row);
      }
      const previous = button('上一页', () => refresh(Math.max(0, page.offset - page.limit)));
      const next = button('下一页', () => refresh(page.offset + page.limit));
      previous.dataset.unavailable = String(page.offset === 0); next.dataset.unavailable = String(page.offset + page.limit >= page.total);
      pager.append(previous, node('span', '', `${page.total ? page.offset + 1 : 0}–${Math.min(page.offset + page.limit, page.total)} / ${page.total} 条`), next);
      syncBusy();
    }
    async function refresh(offset = 0, changed = false) {
      const current = ++request, revision = page?.list_revision;
      const query = new URLSearchParams({ offset, limit: 50, scope: scope.value });
      if (offset > 0 && revision !== undefined) query.set('list_revision', revision);
      for (const [key, control] of [['project_id', project], ['provider_id', provider], ['state', state]]) if (control.value) query.set(key, control.value);
      reading = true; page = null; render(); feedback(status, '正在读取事项…');
      try {
        const result = await fetchJson(`/api/workspace/items?${query}`);
        if (current !== request) return;
        page = result; reading = false; render();
        if (changed) feedback(status, issues.list_changed);
      } catch (error) {
        if (current !== request) return;
        reading = false;
        if (error.status === 409 && error.reason === 'list_changed' && offset > 0) return refresh(0, true);
        render(); feedback(status, `事项读取失败：${error.message}。编辑草稿已保留，可重新读取。`, true);
      } finally { if (current === request) { reading = false; syncBusy(); } }
    }
    function sessionOptions(selected = controls.session_key.value) {
      const select = controls.session_key, codex = controls.provider_id.value === 'codex';
      const rows = [['', codex ? '不关联会话' : '仅 Codex 支持当前关联']];
      if (codex) for (const session of sessions) rows.push([session.session_key, `${session.thread_id.slice(0, 8)} · ${session.cwd}`]);
      if (codex && original?.session_key && !rows.some(([id]) => id === original.session_key)) rows.push([original.session_key, '保留原关联（当前不可核验）']);
      options(select, rows, codex ? selected : ''); select.disabled = !codex;
    }
    function open(item = null) {
      if (writing) return;
      original = item ? { ...item } : null; editingId = item === null ? null : item.id;
      editorTitle.value = item?.title ?? '';
      // Textarea normalizes CRLF. Compare with the DOM value, never rewrite untouched text.
      initialTitle = editorTitle.value;
      editorTitle.maxLength = 2 * Math.max(2000, codepoints(item?.title ?? ''));
      formTitle.textContent = item ? '编辑事项' : '详细新建';
      identity.textContent = item ? `${item.origin === 'todo' ? '原待办' : '原任务'} · ${item.origin_id === '' ? '空原ID' : item.origin_id}` : '保存后生成事项标识；项目和工具可留空。';
      const projectRows = [['', '全局事项'], ...projects.filter(row => !row.archived).map(row => [row.id, row.name])];
      if (item?.project_id && !projectRows.some(([id]) => id === item.project_id)) projectRows.push([item.project_id, `保留原项目 · ${projectName(item.project_id)}`]);
      options(controls.project_id, projectRows, item?.project_id ?? '');
      const providerRows = [['', '暂不分配'], ...Object.entries(providers)];
      if (item?.provider_id && !providers[item.provider_id]) providerRows.push([item.provider_id, `保留原工具 · ${item.provider_id}`]);
      options(controls.provider_id, providerRows, item?.provider_id ?? '');
      options(controls.state, Object.entries(states), item?.state ?? 'todo');
      sessionOptions(item?.session_key ?? '');
      initialFields = Object.fromEntries(fields.map(key => [key, controls[key].value]));
      feedback(editorFeedback, ''); updateMeter(); syncBusy();
      // A modeless editor keeps pagination usable without replacing its draft.
      if (!dialog.open) dialog.show(); editorTitle.focus();
    }
    function body() {
      const changes = {};
      if (editingId === null || editorTitle.value !== initialTitle) changes.title = editingId === null ? editorTitle.value.trim() : editorTitle.value;
      for (const key of fields) if (editingId === null || controls[key].value !== initialFields[key]) changes[key] = controls[key].value || null;
      return editingId === null ? changes : { id: editingId, expected_revision: original.revision, changes };
    }
    function updateMeter() {
      meter.textContent = `${codepoints(editorTitle.value)} / ${Math.max(2000, codepoints(original?.title ?? ''))} 个字符 · 提交 ${bytes(body())} / 262144 bytes`;
    }
    function validate(payload, text, maximum) {
      if (text !== undefined && (!text.trim() || codepoints(text) > maximum || /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(text))) throw new Error(`名称须为非空文字，最多 ${maximum} 个字符。`);
      if (bytes(payload) > 262144) throw new Error(issues.body_too_large);
    }
    async function write(payload, target, success) {
      if (writing || !canWrite()) return;
      writing = true; syncBusy(); feedback(target, '正在保存…');
      let confirmed = false;
      try {
        const result = await postJson(editingId !== null && target === editorFeedback ? '/api/workspace/items/update' : '/api/workspace/items', payload);
        if (!result.item || typeof result.item.id !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(result.item.id) || !Number.isSafeInteger(result.item.revision) || result.item.revision < 1) throw new Error('服务没有返回确认回执');
        confirmed = true; success(); feedback(target, '事项已保存。');
        await refresh(0);
        if (onSaved) await onSaved();
      } catch (error) {
        if (confirmed) feedback(target, '事项保存已确认；项目信息刷新失败，请重新读取。', true);
        else {
          feedback(target, `${issues[error.reason] || error.message}。输入已保留；未自动重试。请先重新读取并核对列表。`, true);
          await refresh(0);
        }
      } finally { writing = false; syncBusy(); }
    }
    quick.addEventListener('submit', async event => {
      event.preventDefault(); if (writing) return;
      const payload = { title: quickTitle.value.trim() };
      try { validate(payload, payload.title, 2000); }
      catch (error) { feedback(quickFeedback, `${error.message} 输入已保留。`, true); return; }
      await write(payload, quickFeedback, () => { quickTitle.value = ''; });
    });
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (writing) return;
      const payload = body();
      if (editingId !== null && !Object.keys(payload.changes).length) { feedback(editorFeedback, '没有修改；原值已保留。'); return; }
      try { validate(payload, editingId === null ? payload.title : payload.changes.title, Math.max(2000, codepoints(original?.title ?? ''))); }
      catch (error) { feedback(editorFeedback, `${error.message} 输入已保留。`, true); return; }
      await write(payload, editorFeedback, () => { dialog.close(); original = null; editingId = null; });
    });
    dialog.addEventListener('cancel', event => { if (writing) event.preventDefault(); });
    editorTitle.addEventListener('input', updateMeter);
    for (const control of Object.values(controls)) control.addEventListener('change', updateMeter);
    controls.provider_id.addEventListener('change', () => { sessionOptions(); updateMeter(); });
    for (const control of [scope, project, provider, state]) control.addEventListener('change', () => refresh(0));
    function update(snapshot) {
      projects = snapshot.projects.items; sessions = snapshot.sessions.available ? snapshot.sessions.items : [];
      const selected = project.value;
      options(project, [['', '全部项目'], ...projects.map(row => [row.id, `${row.name}${row.archived ? '（已归档）' : ''}`])], selected);
      if (selected && !projects.some(row => row.id === selected)) { const option = node('option', '', `未登记项目 · ${selected}`); option.value = selected; project.append(option); project.value = selected; }
      // A snapshot refresh never touches an open editor or quick draft.
      if (scope.value === 'all' && !project.value && !provider.value && !state.value) {
        request++; reading = false; page = snapshot.items_page ?? null; render();
      } else refresh(0);
      syncBusy();
    }
    render();
    return { update, refresh, open, filterProject(id) { scope.value = 'all'; project.value = id; return refresh(0); } };
  }
  window.WorkspaceTaskCenter = { create };
})();
