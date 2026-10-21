// Discovery suggests candidates; only explicit user actions select projects.
'use strict';
(() => {
  const MIME = 'application/x-session-panel-project';
  const names = { codex: 'Codex', claude: 'Claude Code', opencode: 'OpenCode' };
  const statuses = { not_configured: '尚未确认读取来源', available: '来源可读', unavailable: '来源暂不可用',
    unsupported_format: '不支持此格式，可新建项目', limited: '读取受限，暂不能加入', stale: '旧样本，请重试' };
  function create({ dialog, fetchJson, postJson, onChanged, manual, manage }) {
    const $ = id => dialog.querySelector(`#${id}`);
    const node = (tag, text, cls = '') => { const value = document.createElement(tag); value.className = cls; if (text !== undefined) value.textContent = text; return value; };
    const button = (text, fn, cls = 'button secondary') => { const value = node('button', text, cls); value.type = 'button'; value.addEventListener('click', fn); return value; };
    let overview = null, provider = 'codex', page = null, requestId = 0, busy = false, dirty = false, sampleNeedsRefresh = false;
    const drafts = new Map(), selected = new Set(), savedExcluded = new Map();
    const feedback = (text, failed = false) => { $('onboarding-feedback').textContent = text; $('onboarding-feedback').classList.toggle('error', failed); };
    let mode = 'existing', verified = null, folderSaved = false;
    const folderInput = () => ({ name: $('onboarding-directory-name').value.trim(), path: $('onboarding-directory-path').value.trim() });
    const folderKey = () => JSON.stringify([mode, folderInput()]);
    const directoryErrors = { directory_exists: '目标已存在，请添加已有文件夹或更换名称。', directory_not_found: '文件夹不存在，请重新选择。',
      invalid_directory: '请输入本机已有文件夹的绝对路径。', invalid_directory_name: '名称不能包含路径分隔符、特殊字符或末尾点号。',
      directory_unreadable: '无法读取该目录，请检查权限。', directory_create_failed: '文件夹创建失败，请检查权限及目标路径。',
      directory_conflict: '该目录已属于一个项目，请打开已有项目。', picker_busy: '文件夹选择窗口已打开，请先完成或取消选择。',
      picker_unavailable: '文件夹选择器不可用，可直接填写本机绝对路径。' };
    const folderError = error => directoryErrors[error.reason] || error.message;
    function syncFolder() {
      $('onboarding-directory-fields').disabled = busy;
      $('onboarding-directory-save').disabled = busy || folderSaved || !verified || verified.key !== folderKey() || !$('onboarding-directory-confirm').checked;
    }
    function invalidateFolder() {
      verified = null; folderSaved = false; $('onboarding-directory-confirm').checked = false;
      $('onboarding-directory-result').textContent = ''; syncFolder();
    }
    async function chooseMode(next) {
      if (busy) return;
      if (dirty && next !== mode && !window.confirm('尚有未保存的输入，确认切换添加方式？')) return;
      requestId++; mode = next; invalidateFolder();
      $('onboarding-tool-import').hidden = mode !== 'import';
      $('onboarding-directory-form').hidden = mode === 'import';
      for (const [identity, value] of [['onboarding-existing', 'existing'], ['onboarding-new', 'new'], ['onboarding-import', 'import']]) $(identity).setAttribute('aria-pressed', String(mode === value));
      if (mode === 'import') {
        feedback('正在读取可选项目来源…');
        const current = requestId;
        try {
          await reloadOverview();
          if (mode !== 'import' || current !== requestId || !dialog.open) return;
          renderSource(); if (await reloadCandidates()) feedback('只加入你选择的项目；不会复制代码或聊天正文。');
        } catch (error) { if (mode === 'import') feedback(`${error.message}。仍可添加本机文件夹。`, true); }
      } else {
        $('onboarding-directory-title').textContent = mode === 'new' ? '新建项目与文件夹' : '添加已有项目文件夹';
        $('onboarding-directory-label').textContent = mode === 'new' ? '父文件夹路径' : '已有文件夹路径';
        $('onboarding-directory-preview').textContent = mode === 'new' ? '预览新文件夹路径' : '核对目录';
        $('onboarding-directory-save').textContent = mode === 'new' ? '确认创建文件夹并添加项目' : '确认添加项目';
        feedback(mode === 'new' ? '选择父目录并填写名称，预览完整路径后再确认创建。' : '选择或填写已有目录。添加项目不会改动目录中的文件。');
      }
    }
    async function pickDirectory() {
      if (busy) return;
      busy = true; syncFolder(); feedback('请在本机窗口选择文件夹；也可取消后手工填写路径。');
      try {
        const result = await postJson('/api/workspace/directories/pick', { title: mode === 'new' ? '选择新项目的父文件夹' : '选择已有项目文件夹' });
        if (result.status === 'cancelled') { feedback('已取消选择，原输入保留。'); return; }
        if (result.status !== 'selected' || typeof result.path !== 'string') throw new Error('未收到有效的文件夹路径。');
        $('onboarding-directory-path').value = result.path; dirty = true; invalidateFolder(); feedback('已选择文件夹，请核对目录。');
      } catch (error) { feedback(folderError(error), true); }
      finally { busy = false; syncFolder(); }
    }
    async function previewDirectory() {
      if (busy) return;
      invalidateFolder();
      const input = folderInput();
      if (!input.path || mode === 'new' && !input.name) { feedback('请填写目录和项目名称。', true); return; }
      busy = true; syncFolder();
      try {
        const result = await postJson(`/api/workspace/projects/${mode === 'new' ? 'create-preview' : 'inspect'}`, mode === 'new' ? { name: input.name, parent_path: input.path } : { path: input.path });
        if (typeof result.path !== 'string' || !['ok', 'preview'].includes(result.status)) throw new Error('未收到有效的目录核对结果。');
        if (mode === 'new' && result.can_create !== true) { feedback('目标已存在，请更换名称或添加已有文件夹。', true); return; }
        if (!input.name && result.name) $('onboarding-directory-name').value = result.name;
        if (!folderInput().name) { feedback('请填写项目名称。', true); return; }
        verified = { key: folderKey(), path: result.path };
        $('onboarding-directory-result').textContent = `${mode === 'new' ? '将创建并添加' : '将添加已有目录'}：${result.path}`;
        feedback('请核对完整路径并勾选确认。');
      } catch (error) { feedback(folderError(error), true); }
      finally { busy = false; syncFolder(); }
    }
    async function saveDirectory(event) {
      event.preventDefault();
      if (busy || folderSaved || !verified || verified.key !== folderKey() || !$('onboarding-directory-confirm').checked) return;
      const input = folderInput(); busy = true; syncFolder(); feedback('正在保存，请勿重复提交…');
      try {
        const result = await postJson(`/api/workspace/projects/${mode === 'new' ? 'create' : 'register'}`, mode === 'new' ? { name: input.name, parent_path: input.path, confirmed: true } : { name: input.name, path: input.path });
        if (result.status === 'partial' && result.directory_created === true && typeof result.path === 'string') {
          mode = 'existing'; $('onboarding-directory-path').value = result.path; verified = null;
          busy = false; dirty = false; await chooseMode('existing'); dirty = true;
          feedback(`文件夹已创建，但项目登记未成功。目录保留在 ${result.path}；请核对后按已有文件夹添加。`, true); return;
        }
        if (!['created', 'registered'].includes(result.status) || !result.project?.id) throw new Error('未收到有效的项目保存凭据。');
        folderSaved = true; dirty = false; verified = null;
        try { await onChanged(); dialog.close(); }
        catch { feedback('项目已保存，但列表刷新失败。请关闭此窗口并刷新工作台，无需再次创建。', true); }
      } catch (error) {
        verified = null; $('onboarding-directory-confirm').checked = false;
        feedback(`${folderError(error)} 保存结果未确认，请先核对目录和工作台记录，再决定是否重新添加。`, true);
      } finally { busy = false; syncFolder(); }
    }
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
    function newProject() { if (busy) return; close(); manual(); }
    function renderManualFallback(text) {
      $('onboarding-candidates').append(node('p', text, 'empty-state'), button('新建项目', newProject));
    }
    function renderSource() {
      const tool = activeTool();
      if (!tool) return;
      const draft = drafts.get(provider) || { enabled: tool.source.enabled, path: tool.source.path || '' };
      $('onboarding-path').value = draft.path;
      $('onboarding-consent').checked = draft.enabled;
      $('onboarding-source-details').open = !tool.source.enabled || drafts.has(provider);
      $('onboarding-candidates-title').textContent = provider === 'codex' ? 'Codex 项目' : `${names[provider]} · 识别到的项目目录`;
      $('onboarding-source-note').textContent = provider === 'claude'
        ? '此全局配置与桌面端、CLI 共用，也包含其他私密设置。确认后只提取 projects 中的目录；不导入完整桌面配置，不保存其他字段，不读取聊天正文。'
        : provider === 'opencode' ? '从受支持的本机 project 表中识别项目目录；不导入完整桌面配置，不读取会话或消息。格式不支持时可新建项目。'
          : '只读所选元数据库及相邻桌面项目配置；候选列表不读取聊天正文。';
      const locations = $('onboarding-locations'); locations.replaceChildren();
      for (const location of tool.locations.items) {
        locations.append(button(`${location.path}${location.exists ? '' : ' · 当前不存在'}`, () => {
          $('onboarding-path').value = location.path; rememberDraft();
        }));
      }
      if (tool.locations.directory_hint) locations.append(node('p', `配置目录提示：${tool.locations.directory_hint}；请填写实际全局配置文件路径。`, 'section-note'));
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
      if (page && !page.items.length) renderManualFallback(page.status === 'available'
        ? '此来源没有可选择的项目。可新建项目，填写名称和本机目录。'
        : '暂时没有可选择的项目。可确认或重试来源，也可直接新建项目。');
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
        return true;
      } catch (error) {
        if (current !== requestId) return;
        feedback(`${error.message}。当前输入已保留，也可直接新建项目。`, true); $('onboarding-retry').hidden = false;
        if (!page) {
          renderCandidates(); $('onboarding-source-status').textContent = '项目来源读取失败'; $('onboarding-retry').hidden = false;
          renderManualFallback('暂时无法读取项目，可直接新建项目，填写名称和本机目录。');
        }
        return false;
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
    async function open({ mode: nextMode = 'existing' } = {}) {
      if (!dialog.open) dialog.showModal();
      return chooseMode(['existing', 'new', 'import'].includes(nextMode) ? nextMode : 'existing');
    }
    $('onboarding-existing').addEventListener('click', () => chooseMode('existing'));
    $('onboarding-new').addEventListener('click', () => chooseMode('new'));
    $('onboarding-import').addEventListener('click', () => chooseMode('import'));
    $('onboarding-directory-pick').addEventListener('click', pickDirectory);
    $('onboarding-directory-preview').addEventListener('click', previewDirectory);
    $('onboarding-directory-confirm').addEventListener('change', syncFolder);
    $('onboarding-directory-form').addEventListener('input', event => {
      if (event.target === $('onboarding-directory-confirm')) return;
      dirty = true; invalidateFolder();
    });
    $('onboarding-directory-form').addEventListener('submit', saveDirectory);
    $('onboarding-tabs').replaceChildren(...Object.keys(names).map(key => {
      const tab = button(names[key], () => {
        if (busy) return; rememberDraft(); provider = key; dirty = drafts.has(provider); page = null; sampleNeedsRefresh = false; selected.clear(); requestId++;
        feedback('请选择此工具的项目来源与候选。');
        for (const child of $('onboarding-tabs').children) child.setAttribute('aria-pressed', String(child.dataset.provider === provider));
        renderSource(); renderCandidates(); reloadCandidates();
      }); tab.dataset.provider = key; tab.setAttribute('aria-pressed', String(key === provider)); return tab;
    }));
    $('onboarding-close').addEventListener('click', close);
    $('onboarding-manual').addEventListener('click', newProject);
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
      async refresh() { if (dialog.open && mode === 'import' && !busy && !dirty) { await reloadOverview(); await reloadCandidates(); } },
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
