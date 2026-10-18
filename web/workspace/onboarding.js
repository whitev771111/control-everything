// Discovery suggests candidates; only explicit user actions select projects.
'use strict';
(() => {
  const MIME = 'application/x-session-panel-project';
  const names = { codex: 'Codex', claude: 'Claude Code', opencode: 'OpenCode' };
  const statuses = { not_configured: '尚未确认读取来源', available: '来源可读', unavailable: '来源暂不可用',
    unsupported_format: '不支持此格式，请手工添加目录', limited: '读取受限，暂不能加入', stale: '旧样本，请重试' };
  function create({ dialog, fetchJson, postJson, onChanged, manual, configure, manage }) {
    const $ = id => dialog.querySelector(`#${id}`);
    const node = (tag, text, cls = '') => { const value = document.createElement(tag); value.className = cls; if (text !== undefined) value.textContent = text; return value; };
    const button = (text, fn, cls = 'button secondary') => { const value = node('button', text, cls); value.type = 'button'; value.addEventListener('click', fn); return value; };
    let overview = null, provider = 'codex', page = null, requestId = 0, busy = false, dirty = false, sampleNeedsRefresh = false;
    const drafts = new Map(), selected = new Set(), savedExcluded = new Map();
    let programs = null, programCandidates = [], programError = '';
    const feedback = (text, failed = false) => { $('onboarding-feedback').textContent = text; $('onboarding-feedback').classList.toggle('error', failed); };
    const activeTool = () => overview?.tools.find(tool => tool.provider === provider);
    async function reloadOverview() {
      overview = await fetchJson('/api/workspace/onboarding');
      renderPrevious();
    }
    function renderPrevious() {
      const root = $('onboarding-previous'); root.replaceChildren();
      const values = overview?.excluded_projects || [];
      if (values.length) root.append(node('h3', '之前管理过'));
      for (const item of values) root.append(button(`${item.name} · 重新加入${item.archived ? '（已归档）' : ''}`, () => rejoin(item.project_id)));
    }
    function renderPrograms() {
      const root = $('onboarding-program'); root.replaceChildren();
      const registered = programs?.registrations.filter(row => row.provider_id === provider) || [];
      const candidates = programCandidates.filter(row => row.provider_id === provider);
      root.append(node('strong', `程序 · ${registered.length ? '已登记' : candidates.length ? '发现候选，待确认' : '未发现受支持入口'}`));
      const details = node('details'); details.append(node('summary', '程序登记与安装候选'));
      details.append(node('p', programError || '程序登记与项目来源分别确认；未发现入口仍可选择项目来源。', 'section-note'));
      for (const candidate of candidates) details.append(button(`${candidate.locator_kind === 'native_executable' ? '用户确认的程序' : '安装候选'} · ${candidate.path}`, () => { close(); configure(provider, candidate); }));
      details.append(button('登记程序路径', () => { close(); configure(provider); })); root.append(details);
    }
    function renderSource() {
      const tool = activeTool();
      if (!tool) return;
      const draft = drafts.get(provider) || { enabled: tool.source.enabled, path: tool.source.path || '' };
      $('onboarding-path').value = draft.path;
      $('onboarding-consent').checked = draft.enabled;
      $('onboarding-source-details').open = !tool.source.enabled || drafts.has(provider);
      $('onboarding-source-note').textContent = provider === 'claude'
        ? '此全局配置也包含其他私密设置。确认后只提取 projects 中的目录，不保存其他字段，不读取聊天正文。'
        : provider === 'opencode' ? '只读受支持 project 表的项目目录；不读取会话或消息。其他格式可手工添加目录。'
          : '只读所选元数据库及相邻桌面项目配置；候选列表不读取聊天正文。';
      const locations = $('onboarding-locations'); locations.replaceChildren();
      for (const location of tool.locations.items) {
        locations.append(button(`${location.path}${location.exists ? '' : ' · 当前不存在'}`, () => {
          $('onboarding-path').value = location.path; rememberDraft();
        }));
      }
      if (tool.locations.directory_hint) locations.append(node('p', `配置目录提示：${tool.locations.directory_hint}；请填写实际全局配置文件路径。`, 'section-note'));
      renderPrograms();
    }
    function rememberDraft() {
      const draft = { path: $('onboarding-path').value, enabled: $('onboarding-consent').checked };
      const saved = activeTool()?.source;
      if (saved && draft.path === (saved.path || '') && draft.enabled === saved.enabled) { drafts.delete(provider); dirty = false; }
      else { drafts.set(provider, draft); dirty = true; }
    }
    function renderCandidates() {
      const root = $('onboarding-candidates'); root.replaceChildren();
      $('onboarding-source-status').textContent = statuses[page?.status] || '正在读取候选…';
      $('onboarding-retry').hidden = !page || page.status === 'available';
      const query = $('onboarding-search').value.trim().toLocaleLowerCase();
      const items = (page?.items || []).filter(item => `${item.name} ${item.directories.map(row => row.path).join(' ')}`.toLocaleLowerCase().includes(query));
      for (const item of items) {
        const row = node('article', undefined, 'onboarding-candidate'); row.dataset.candidateId = item.candidate_id;
        const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.setAttribute('aria-label', `选择 ${item.name}`);
        checkbox.checked = selected.has(item.candidate_id); checkbox.disabled = busy || item.selected || !page.complete;
        checkbox.addEventListener('change', () => { checkbox.checked ? selected.add(item.candidate_id) : selected.delete(item.candidate_id); });
        const copy = node('div'); copy.append(node('strong', item.name), node('p', item.directories.map(binding => binding.path).join(' · '), 'context-path'), node('small', `${names[provider]} · ${item.selected ? '已加入' : '未加入'}${item.directory_available === false ? ' · 目录当前不可用，仍可保留管理' : ''}`));
        const add = button(item.selected ? '已加入' : savedExcluded.has(item.project_id) ? '重新加入' : '加入', () => join(item.candidate_id));
        add.disabled = busy || item.selected || !page.complete;
        row.draggable = !add.disabled;
        row.addEventListener('dragstart', event => {
          if (!row.draggable) { event.preventDefault(); return; }
          event.dataTransfer.setData(MIME, JSON.stringify({ candidate_id: item.candidate_id, snapshot_token: page.snapshot_token }));
          event.dataTransfer.effectAllowed = 'copy';
        });
        row.append(checkbox, copy, add); root.append(row);
      }
      if (page?.status === 'available' && !page.items.length) root.append(node('p', '此来源没有项目，可手工添加目录。', 'empty-state'));
      if (page && page.total > page.items.length) root.append(button(`加载更多（${page.items.length}/${page.total}）`, () => reloadCandidates(true)));
      $('onboarding-add-selected').disabled = busy || !page?.complete;
    }
    async function reloadCandidates(more = false) {
      const current = ++requestId, currentProvider = provider;
      try {
        const offset = more ? page?.items.length || 0 : 0;
        const value = await fetchJson(`/api/workspace/project-candidates?provider=${provider}&offset=${offset}&limit=100`);
        if (current !== requestId || provider !== currentProvider || !dialog.open) return;
        if (more && page?.snapshot_token !== value.snapshot_token) { feedback('来源已变化，请重新选择项目。', true); selected.clear(); return reloadCandidates(); }
        if (!more && page && page.source_id !== value.source_id) { selected.clear(); feedback('来源已变化，请重新选择项目。', true); }
        page = more ? { ...value, items: [...page.items, ...value.items] } : value;
        sampleNeedsRefresh = false;
        if (overview) overview.selection_token = value.selection_token;
        renderCandidates();
      } catch (error) {
        if (current !== requestId) return;
        feedback(`${error.message}。当前输入已保留。`, true); $('onboarding-retry').hidden = false;
      }
    }
    async function refreshJoinSample() {
      const previous = page, currentProvider = provider;
      let fresh = null;
      try {
        for (let offset = 0; offset < Math.max(1, previous.items.length); offset += 100) {
          const value = await fetchJson(`/api/workspace/project-candidates?provider=${currentProvider}&offset=${offset}&limit=100`);
          if (provider !== currentProvider || value.source_id !== previous.source_id || value.status !== 'available' || !value.complete
              || fresh && fresh.snapshot_token !== value.snapshot_token) throw new Error('来源已变化或暂不可用，请重新读取并选择项目。');
          fresh = fresh ? { ...value, items: [...fresh.items, ...value.items] } : value;
          if (offset + value.items.length >= value.total) break;
        }
        page = fresh; overview.selection_token = fresh.selection_token; sampleNeedsRefresh = false; renderCandidates();
      } catch (error) {
        page = { ...previous, status: 'stale', complete: false }; renderCandidates(); throw error;
      }
    }
    async function consume(result) {
      overview.selection_token = result.selection_token;
      if (result.requires_rejoin) {
        savedExcluded.set(result.project_id, true);
        feedback('项目已保存，正在加入…');
        try {
          result = await postJson('/api/workspace/project-selection', { action: 'rejoin', project_id: result.project_id, selection_token: result.selection_token });
          overview.selection_token = result.selection_token;
        } catch (error) {
          feedback('项目已保存，尚未加入。请使用“重新加入”继续。', true);
          await reloadOverview();
          throw error;
        }
      }
      if (!result.selected) throw new Error('项目尚未加入');
      savedExcluded.delete(result.project_id);
      feedback(result.archived ? '已加入 · 已归档。可在项目管理中取消归档。' : '已加入工作台。');
      if (result.archived) $('onboarding-previous').append(button('打开项目管理', () => { close(); manage(); }));
      await onChanged();
      return result;
    }
    async function joinOne(id) {
      if (sampleNeedsRefresh) await refreshJoinSample();
      const item = page?.items.find(row => row.candidate_id === id);
      if (!item || !page.complete) throw new Error('候选已变化，请重新读取并选择项目。');
      if (item.selected) { selected.delete(id); return false; }
      const rejoining = savedExcluded.has(item.project_id) || overview.excluded_projects.some(row => row.project_id === item.project_id);
      const result = savedExcluded.has(item.project_id)
        ? await postJson('/api/workspace/project-selection', { action: 'rejoin', project_id: item.project_id, selection_token: overview.selection_token })
        : await postJson('/api/workspace/project-selection', { action: 'join', provider, candidate_id: id,
            snapshot_token: page.snapshot_token, selection_token: overview.selection_token });
      await consume(result); sampleNeedsRefresh = rejoining || result.requires_rejoin;
      item.selected = true; selected.delete(id); return true;
    }
    async function join(id) {
      if (busy) return;
      busy = true; renderCandidates();
      try { await joinOne(id); }
      catch (error) {
        if (!savedExcluded.size) feedback(`${error.message}。选择已保留，请重试。`, true);
        await reloadOverview();
        if (error.reason === 'candidate_changed' || error.status === 409) { sampleNeedsRefresh = true; try { await refreshJoinSample(); } catch (refreshError) { feedback(refreshError.message, true); } }
      }
      finally { busy = false; renderCandidates(); }
    }
    async function joinSelected() {
      if (busy) return;
      busy = true; renderCandidates();
      let success = 0, failed = 0;
      for (const id of [...selected]) {
        try { if (await joinOne(id)) success++; }
        catch (error) {
          failed++; try { await reloadOverview(); } catch { /* Keep selected items when offline. */ }
          if (error.reason === 'candidate_changed' || error.status === 409) { sampleNeedsRefresh = true; try { await refreshJoinSample(); } catch { /* Explicit reread remains available. */ } }
        }
      }
      busy = false; renderCandidates(); feedback(`已加入 ${success} 项，${failed} 项未加入${failed ? '；未成功项仍保留选择，可重试。' : '。'}`, failed > 0);
    }
    async function rejoin(id) {
      if (busy) return;
      busy = true;
      try { await consume(await postJson('/api/workspace/project-selection', { action: 'rejoin', project_id: id, selection_token: overview.selection_token })); sampleNeedsRefresh = true; await reloadOverview(); }
      catch (error) { feedback(`${error.message}。项目和历史仍保留。`, true); await reloadOverview(); }
      finally { busy = false; }
    }
    async function saveSource(event) {
      event.preventDefault(); if (busy) return;
      rememberDraft(); busy = true;
      $('onboarding-source-fields').disabled = true;
      try {
        await postJson('/api/workspace/sources', { expected_revision: overview.workbench_revision, provider,
          enabled: $('onboarding-consent').checked, path: $('onboarding-path').value || null });
        drafts.delete(provider); dirty = false; selected.clear(); await reloadOverview();
        $('onboarding-source-details').open = !$('onboarding-consent').checked;
        await reloadCandidates(); await onChanged(); feedback('来源设置已保存。请明确选择要加入的项目。');
      } catch (error) { $('onboarding-source-details').open = true; feedback(`${error.message}。输入已保留。`, true); await reloadOverview(); }
      finally { busy = false; $('onboarding-source-fields').disabled = false; renderCandidates(); }
    }
    function close() { if (busy) return; requestId++; dialog.close(); }
    async function open() {
      if (!dialog.open) dialog.showModal();
      feedback('正在查找来源位置…');
      try {
        await reloadOverview(); renderSource(); await reloadCandidates(); feedback('只加入你选择的项目；不会复制代码或聊天正文。');
        const values = await Promise.allSettled([fetchJson('/api/profile/observations'), fetchJson('/api/profile/observations/candidates')]);
        if (!dialog.open) return;
        programs = values[0].status === 'fulfilled' ? values[0].value : null;
        programCandidates = values[1].status === 'fulfilled' ? values[1].value.candidates || [] : [];
        programError = values.some(value => value.status === 'rejected') ? '程序发现暂不可用，仍可手填登记和配置项目来源。' : '';
        renderPrograms();
      } catch (error) { feedback(`${error.message}。仍可手工添加目录。`, true); }
    }
    $('onboarding-tabs').replaceChildren(...Object.keys(names).map(key => {
      const tab = button(names[key], () => {
        if (busy) return; rememberDraft(); provider = key; dirty = drafts.has(provider); page = null; sampleNeedsRefresh = false; selected.clear(); requestId++;
        feedback('请选择此工具的项目来源与候选。');
        for (const child of $('onboarding-tabs').children) child.setAttribute('aria-pressed', String(child.dataset.provider === provider));
        renderSource(); renderCandidates(); reloadCandidates();
      }); tab.dataset.provider = key; tab.setAttribute('aria-pressed', String(key === provider)); return tab;
    }), button('手工目录', () => { close(); manual(); }));
    $('onboarding-close').addEventListener('click', close);
    $('onboarding-manual').addEventListener('click', () => { close(); manual(); });
    $('onboarding-custom').addEventListener('click', () => { close(); configure(null); });
    $('onboarding-source-form').addEventListener('submit', saveSource);
    $('onboarding-source-form').addEventListener('input', rememberDraft);
    $('onboarding-search').addEventListener('input', renderCandidates);
    $('onboarding-add-selected').addEventListener('click', joinSelected);
    $('onboarding-retry').addEventListener('click', () => reloadCandidates());
    const zone = $('onboarding-drop');
    zone.addEventListener('dragover', event => { if ([...event.dataTransfer.types].includes(MIME)) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; } });
    zone.addEventListener('drop', event => {
      event.preventDefault();
      if (![...event.dataTransfer.types].includes(MIME)) { feedback('请填写本机目录路径；这里只接收本页项目候选。', true); return; }
      try {
        const value = JSON.parse(event.dataTransfer.getData(MIME));
        if (value.snapshot_token !== page?.snapshot_token || !page.items.some(row => row.candidate_id === value.candidate_id)) throw new Error('stale');
        join(value.candidate_id);
      } catch { feedback('候选已变化，请从当前列表重新选择。', true); }
    });
    dialog.addEventListener('dragenter', event => {
      if ([...event.dataTransfer.types].includes('Files')) feedback('请填写本机目录路径；这里只接收本页项目候选。', true);
    });
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); else requestId++; });
    return { open, join, rejoin, joinSelected,
      async refresh() { if (dialog.open && !busy && !dirty) { await reloadOverview(); await reloadCandidates(); } },
      async remove(project) {
        await reloadOverview();
        const value = await postJson('/api/workspace/project-selection', { action: 'remove', project_id: project.id, selection_token: overview.selection_token });
        overview.selection_token = value.selection_token; await onChanged(); return value;
      },
      isEditing: () => dialog.open && (dirty || busy),
    };
  }
  window.WorkspaceOnboarding = { create, MIME };
})();
