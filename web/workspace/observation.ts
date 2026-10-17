'use strict';

// Native observations have their own polling and short-lived navigation permissions.
(function () {
  type Snapshot = WorkspaceObservationTypes.Snapshot;
  type Settings = WorkspaceObservationTypes.Settings;
  type RegistrationWrite = WorkspaceObservationTypes.RegistrationWrite;
  const names: Readonly<Record<string, string>> = { codex: 'Codex', claude: 'Claude Code', hermes: 'Hermes', pi: 'Pi', grok: 'Grok', dsh: 'DSH', opencode: 'OpenCode' };
  const states: Readonly<Record<string, string>> = { available: '可用', limited: '覆盖受限', unknown: '未知', stale: '样本已过期', unavailable: '观察不可用', not_found: '未发现' };
  const reasons: Readonly<Record<string, string>> = {
    native_missing: '尚未构建本机观察后台，项目与任务仍可使用。', connecting: '正在连接本机观察后台。',
    not_started: '本机观察尚未启动。', scope_pending: '项目目录范围正在同步，归属暂未知。',
    scope_conflict: '另一个管理实例使用了不同目录范围，请关闭重复实例后重新启动。',
    identity_mismatch: '后台身份无法核验。', protocol_mismatch: '后台与页面协议不匹配。',
    sample_timeout: '样本过期，运行数量与返回能力暂未知。', io_cleanup_pending: '系统 I/O 取消仍待完成，观察已暂停。',
    observation_failed: '观察读取失败，以下为旧观察；项目与任务仍可使用。', observation_timeout: '观察读取超时，当前运行状态未知。',
    foreground_denied: '系统未允许切换前台，请手动打开目标窗口。', action_expired: '返回许可已过期，请读取新样本或重新选择窗口。',
    action_busy: '已有操作在处理，请稍后重新选择。', binding_unknown: '无法证明对应的可见终端窗口。',
    binding_changed: '窗口或进程身份已变化，请重新选择。', unsupported_installation: '当前安装形态无法核验。',
    config_pending: '配置已保存，等待后台应用和同版本的新样本。', config_unavailable: '登记配置无法读取，观察关联和操作已暂停。',
    config_conflict: '配置已被另一面板修改，请刷新后重新填写。', config_busy: '另一操作正在使用配置，请稍后保存。',
    duplicate_installation: '存在重复安装，请删除重复登记并复用原登记。',
    invalid_config: '登记格式不符合要求；请检查本机绝对路径、显示名及 Provider 与安装形态的组合。',
    storage_error: '配置保存失败，编辑内容已保留。', backup_failed: '升级前备份失败，配置未保存。',
    registration_disabled: '登记已禁用。', rebind_required: '恢复的登记需要确认本机路径后重新绑定。',
    entry_unsupported: '此入口无法区分脚本或宿主，运行关联与选窗受限。',
    return_unknown: '返回结果未知。请查看当前前台；此操作不会自动重试。',
  };
  const note = (code: string | null | undefined) => (code ? reasons[code] : undefined) || '当前证据不足，状态保持未知。';
  const domTags = {
    'close-window-choices': 'button', 'registration-new': 'button',
    'registration-category': 'select', 'registration-provider': 'select', 'registration-locator': 'select',
    'registration-confirm': 'input', 'registration-enabled': 'input', 'registration-label': 'input', 'registration-path': 'input',
    'registration-fields': 'fieldset', 'registration-form': 'form', 'registration-settings': 'details',
    'native-observations-section': 'section', 'window-choices-picker': 'section',
    'native-installations': 'div', 'native-instances': 'div', 'native-registrations': 'div',
    'registration-candidates': 'div', 'registrations-list': 'div', 'window-choices-list': 'div',
    'native-unassigned': 'p', 'observation-feedback': 'p', 'observation-resource-summary': 'p', 'observation-status': 'p',
    'registrations-feedback': 'p', 'registrations-status': 'p', 'window-choices-status': 'p',
  } as const;
  function $<K extends keyof typeof domTags>(id: K): HTMLElementTagNameMap[typeof domTags[K]] {
    const found = document.getElementById(id);
    if (!found || found.tagName.toLowerCase() !== domTags[id]) throw new Error('observation_dom_unavailable');
    // This assertion follows the closed id/tag map and the checked actual tag.
    return found as HTMLElementTagNameMap[typeof domTags[K]];
  }
  function node<K extends keyof HTMLElementTagNameMap>(tag: K, className: string, text?: unknown): HTMLElementTagNameMap[K] {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (text !== undefined) result.textContent = String(text);
    return result;
  }
  function isRecord(value: unknown): value is Record<string, unknown> { return typeof value === 'object' && value !== null && !Array.isArray(value); }
  function isFiniteNumber(value: unknown): value is number { return typeof value === 'number' && Number.isFinite(value); }
  function isNullableNumber(value: unknown): value is number | null { return value === null || isFiniteNumber(value); }
  function isNullableString(value: unknown): value is string | null { return value === null || typeof value === 'string'; }
  function optionalString(value: Record<string, unknown>, key: string): boolean { return !(key in value) || isNullableString(value[key]); }
  function isStrings(value: unknown): value is string[] { return Array.isArray(value) && value.every(item => typeof item === 'string'); }
  function isCoverage(value: unknown): value is WorkspaceObservationTypes.Coverage { return isRecord(value) && typeof value.status === 'string'; }
  function isPair(value: Record<string, unknown>): boolean { return isNullableNumber(value.config_revision) && isNullableString(value.config_hash); }
  function isValidation(value: unknown): value is WorkspaceObservationTypes.RegistrationValidation {
    return isRecord(value) && typeof value.registration_id === 'string' && typeof value.status === 'string' && optionalString(value, 'reason');
  }
  function isObservedRegistration(value: unknown): value is WorkspaceObservationTypes.ObservedRegistration {
    return isValidation(value) && isRecord(value) && optionalString(value, 'label') && optionalString(value, 'category')
      && (!('can_pick' in value) || value.can_pick === null || typeof value.can_pick === 'boolean');
  }
  function isInstallation(value: unknown): value is WorkspaceObservationTypes.Installation {
    return isRecord(value) && typeof value.provider_id === 'string' && typeof value.status === 'string' && optionalString(value, 'version');
  }
  function isResource(value: unknown): value is WorkspaceObservationTypes.Resource {
    return isRecord(value) && typeof value.resource_id === 'string' && isNullableNumber(value.rss_bytes)
      && isNullableNumber(value.cpu_percent_one_core) && isNullableNumber(value.threads) && isNullableNumber(value.handles) && isNullableNumber(value.metrics_interval_ms);
  }
  function isReturnAction(value: unknown): value is WorkspaceObservationTypes.Grant | WorkspaceObservationTypes.NoAction | null {
    if (value === null) return true;
    if (!isRecord(value)) return false;
    return value.action_id === null ? typeof value.status === 'string' && isNullableString(value.reason)
      : typeof value.action_id === 'string' && isFiniteNumber(value.issued_seq) && isFiniteNumber(value.expires_at_ms) && typeof value.scope === 'string';
  }
  function isInstance(value: unknown): value is WorkspaceObservationTypes.Instance {
    return isRecord(value) && typeof value.instance_id === 'string' && typeof value.kind === 'string' && typeof value.status === 'string'
      && typeof value.resource_id === 'string' && isFiniteNumber(value.pid) && isNullableString(value.provider_id) && isNullableString(value.project_id)
      && (!('classification' in value) || typeof value.classification === 'string')
      && (!('registration_labels' in value) || isStrings(value.registration_labels)) && isReturnAction(value.return_action);
  }
  function isProjectRef(value: unknown): value is { project_id: string; instance_ids: string[] } {
    return isRecord(value) && typeof value.project_id === 'string' && isStrings(value.instance_ids);
  }
  function isSnapshot(value: unknown): value is Snapshot {
    return isRecord(value) && typeof value.status === 'string' && isNullableString(value.reason) && isNullableString(value.generation)
      && isNullableNumber(value.sampled_at_ms) && isPair(value) && isCoverage(value.coverage)
      && Array.isArray(value.installations) && value.installations.every(isInstallation)
      && Array.isArray(value.resources) && value.resources.every(isResource) && Array.isArray(value.instances) && value.instances.every(isInstance)
      && Array.isArray(value.project_refs) && value.project_refs.every(isProjectRef) && isStrings(value.unassigned)
      && (!('registrations' in value) || value.registrations === null || Array.isArray(value.registrations) && value.registrations.every(isObservedRegistration));
  }
  function isRegistration(value: unknown): value is WorkspaceObservationTypes.Registration {
    if (!isRecord(value) || typeof value.id !== 'string' || value.environment_id !== 'windows:local' || typeof value.label !== 'string'
      || typeof value.path !== 'string' || typeof value.enabled !== 'boolean' || (value.binding_state !== 'confirmed' && value.binding_state !== 'rebind_required')) return false;
    if (value.category === 'custom_exe') return value.provider_id === null && value.locator_kind === 'executable';
    if (value.category !== 'builtin') return false;
    return (value.provider_id === 'codex' && (value.locator_kind === 'npm_command' || value.locator_kind === 'codex_desktop'))
      || ((value.provider_id === 'claude' || value.provider_id === 'pi') && value.locator_kind === 'npm_command')
      || (value.provider_id === 'hermes' && value.locator_kind === 'hermes_launcher');
  }
  function isSettings(value: unknown): value is Settings {
    return isRecord(value) && isFiniteNumber(value.revision) && typeof value.hash === 'string'
      && Array.isArray(value.registrations) && value.registrations.every(isRegistration)
      && (!('validation' in value) || value.validation === null || Array.isArray(value.validation) && value.validation.every(isValidation))
      && (!('application' in value) || value.application === null || isRecord(value.application) && optionalString(value.application, 'reason'));
  }
  function isCandidate(value: unknown): value is WorkspaceObservationTypes.Candidate {
    return isRecord(value) && typeof value.provider_id === 'string' && typeof value.locator_kind === 'string' && typeof value.path === 'string' && optionalString(value, 'version');
  }
  function isCandidates(value: unknown): value is WorkspaceObservationTypes.Candidates {
    return isRecord(value) && (!('candidates' in value) || Array.isArray(value.candidates) && value.candidates.every(isCandidate));
  }
  function isChoice(value: unknown): value is WorkspaceObservationTypes.Choices['choices'][number] {
    return isRecord(value) && typeof value.title === 'string' && typeof value.action_id === 'string' && isFiniteNumber(value.issued_seq) && isFiniteNumber(value.expires_at_ms);
  }
  function isChoices(value: unknown): value is WorkspaceObservationTypes.Choices {
    return isRecord(value) && typeof value.selection_id === 'string' && typeof value.generation === 'string'
      && isPair(value) && isFiniteNumber(value.expires_at_ms) && isCoverage(value.coverage) && Array.isArray(value.choices) && value.choices.every(isChoice);
  }
  function isReceipt(value: unknown): value is WorkspaceObservationTypes.Receipt {
    return isRecord(value) && typeof value.status === 'string' && (!('kind' in value) || typeof value.kind === 'string') && optionalString(value, 'reason');
  }
  function invalidResponse(): never { throw new Error('invalid_response'); }
  function requireSnapshot(value: unknown): Snapshot { if (!isSnapshot(value)) invalidResponse(); return value; }
  function requireSettings(value: unknown): Settings { if (!isSettings(value)) invalidResponse(); return value; }
  function requireCandidates(value: unknown): WorkspaceObservationTypes.Candidates { if (!isCandidates(value)) invalidResponse(); return value; }
  function requireChoices(value: unknown): WorkspaceObservationTypes.Choices { if (!isChoices(value)) invalidResponse(); return value; }
  function requireReceipt(value: unknown): WorkspaceObservationTypes.Receipt { if (!isReceipt(value)) invalidResponse(); return value; }
  function errorReason(error: unknown): string | undefined { return isRecord(error) && typeof error.reason === 'string' ? error.reason : undefined; }
  function bytes(value: number | null) { return typeof value === 'number' ? `${(value / 1048576).toFixed(1)} MiB 工作集` : '工作集未知'; }
  function cpu(value: number | null) { return typeof value === 'number' ? `CPU ${value.toFixed(1)}%（单核）` : 'CPU 未知'; }
  let current: Snapshot | null = null;
  let api: WorkspaceObservationTypes.Options['fetchJson'], post: WorkspaceObservationTypes.Options['postJson'];
  let timer: number | undefined, polling = false, started = false;
  let expiryTimer: number | undefined, readTimer: number | undefined, instanceButtons: HTMLButtonElement[] = [];
  let pickerNonce = 0, pickerSelection: WorkspaceObservationTypes.PickerSelection | null = null, pickerTimer: number | undefined, picking = false, actionPending = false;
  let pickerButtons: HTMLButtonElement[] = [], savedPair: WorkspaceObservationTypes.ConfigPair | null = null, settingsValue: Settings | null = null, editId: string | null = null, settingsNonce = 0;
  const samePair = (a: WorkspaceObservationTypes.ConfigPair | null, b: WorkspaceObservationTypes.ConfigPair | null) => !!a && !!b && a.config_revision === b.config_revision && a.config_hash === b.config_hash;

  function feedback(text: string) { $('observation-feedback').textContent = text; }
  function markStale(reason: string) {
    clearPicker();
    if (current) {
      current = { ...current, status: 'stale', reason, coverage: { ...current.coverage, status: 'unknown' },
        instances: current.instances.map(item => ({ ...item, status: 'stale', return_action: null })) };
    } else $('observation-status').textContent = `观察不可用 · ${note(reason)}`;
  }
  function expire() {
    if (!current) return;
    if (!['stale', 'unavailable'].includes(current.status) &&
        (!isFiniteNumber(current.sampled_at_ms) || Date.now() >= current.sampled_at_ms + 12000)) markStale('sample_timeout');
    for (const item of current.instances) {
      const grant = item.return_action;
      if (grant?.action_id && grant.expires_at_ms <= Date.now()) item.return_action = null;
    }
  }
  function scheduleExpiry() {
    clearTimeout(expiryTimer); expiryTimer = undefined;
    if (!started || !current || ['stale', 'unavailable'].includes(current.status) || !isFiniteNumber(current.sampled_at_ms)) return;
    const deadlines = [current.sampled_at_ms + 12000,
      ...current.instances.map(item => item.return_action?.action_id ? item.return_action.expires_at_ms : undefined).filter(isFiniteNumber)];
    expiryTimer = setTimeout(() => { expire(); render(); scheduleExpiry(); }, Math.max(0, Math.min(...deadlines) - Date.now()));
  }
  function renderProjectRefs() {
    for (const row of document.querySelectorAll<HTMLElement>('#projects-list [data-project-id]')) {
      row.querySelector('.native-project-facts')?.remove();
      const reference = current?.project_refs.find(item => item.project_id === row.dataset.projectId);
      const count = reference?.instance_ids.length || 0;
      const old = current !== null && ['stale', 'unavailable'].includes(current.status);
      const complete = current?.status === 'available' && current?.coverage.status === 'complete';
      const text = old ? '旧观察 · 当前本机实例归属未知' : count ? `${count} 个已核验本机实例 · 会话与待处理状态未知` : complete ? '本次覆盖内未发现关联实例' : '本机实例归属未知';
      row.querySelector('.project-copy')?.append(node('p', 'native-project-facts', text));
    }
  }
  function render() {
    for (const button of instanceButtons) button.disabled = true;
    for (const button of pickerButtons) button.disabled = true;
    instanceButtons = [];
    if (!current) return;
    const old = ['stale', 'unavailable'].includes(current.status), prefix = old ? '旧观察 · ' : '';
    if (pickerSelection && (pickerSelection.generation !== current.generation || !samePair(pickerSelection, current) || ['stale', 'unavailable'].includes(current.status))) clearPicker();
    const root = $('native-instances'); root.replaceChildren();
    const coverage = current.coverage.status === 'complete' ? '覆盖完整（仅支持入口）' : current.coverage.status === 'limited' ? '覆盖受限' : '覆盖未知';
    $('observation-status').textContent = `${states[current.status] || '未知'} · 当前用户 Windows 本机 · ${coverage} · ${current.reason ? note(current.reason) : '安装、存活进程、资源与会话来源分别核验。'}`;
    const installations = $('native-installations'); installations.replaceChildren();
    for (const item of current.installations) {
      installations.append(node('span', 'badge', `${prefix}${names[item.provider_id] || item.provider_id} · 安装${states[item.status] || '未知'}${item.version ? ` · ${item.version}` : ''}`));
    }
    const registered = $('native-registrations'); registered.replaceChildren(); pickerButtons = [];
    for (const item of current.registrations ?? []) {
      const row = node('div', 'source-line');
      row.append(node('strong', '', `${item.label || '已登记工具'} · ${item.category === 'custom_exe' ? '自定义程序 / 用户指定' : '内置规则'}`),
        node('p', 'section-note', `${states[item.status] || '安装未核验'}${item.reason ? ` · ${note(item.reason)}` : ''} · 会话来源独立配置`));
      const button = node('button', 'button secondary', '选择工具窗口'); button.type = 'button';
      button.disabled = old || current.status !== 'available' || item.can_pick !== true || actionPending;
      button.addEventListener('click', () => { if (!button.disabled) return openPicker(item.registration_id); });
      pickerButtons.push(button); row.append(button); registered.append(row);
    }
    const resources = new Map(current.resources.map(item => [item.resource_id, item]));
    const rssValues = [...resources.values()].map(item => item.rss_bytes);
    const rss = resources.size && rssValues.every(isFiniteNumber) ? rssValues.reduce((sum, value) => sum + value, 0) : null;
    $('observation-resource-summary').textContent = `${prefix}${resources.size} 份独立资源 · ${bytes(rss)} · 共享宿主只计一次；不按项目分摊。`;
    if (!current.instances.length) root.append(node('p', 'section-note', current.status === 'available' && current.coverage.status === 'complete' ? '本次覆盖内未发现已认领实例。' : '当前实例数量未知，不能据此判定没有运行。'));
    const projectNames = new Map((window.WorkspaceObservation.projects || []).map(item => [item.id, item.name]));
    for (const instance of current.instances) {
      const row = node('article', 'native-instance');
      row.dataset.instanceId = instance.instance_id;
      row.dataset.classification = instance.classification || 'provider';
      const resource = resources.get(instance.resource_id);
      const custom = instance.classification === 'custom_exe';
      row.append(node('strong', '', `${custom ? '自定义程序进程 / 用户指定' : names[instance.provider_id ?? ''] || instance.provider_id} · ${instance.kind === 'desktop' ? '桌面' : custom ? 'EXE' : 'CLI'} · PID ${instance.pid}`),
        node('p', '', `${states[instance.status] || '未知'} · ${instance.project_id ? projectNames.get(instance.project_id) || '已登记项目' : '未归属项目'} · ${custom ? '不提供 Agent/会话/审批语义' : '会话未知 · 待处理未知'}${instance.registration_labels?.length ? ` · ${instance.registration_labels.join(' / ')}` : ''}`),
        node('p', 'section-note', resource ? `${prefix}${cpu(resource.cpu_percent_one_core)} · ${bytes(resource.rss_bytes)} · 线程 ${resource.threads ?? '未知'} · 句柄 ${resource.handles ?? '未知'} · 采样间隔 ${resource.metrics_interval_ms} ms` : '资源未知'));
      const grant = instance.return_action;
      if (!old && grant?.action_id && grant.expires_at_ms > Date.now()) {
        const button = node('button', 'button secondary', grant.scope === 'application_window' ? '切回应用窗口' : '核验并返回终端窗口');
        button.type = 'button'; button.disabled = actionPending;
        button.addEventListener('click', () => {
          if (!current || current.generation === null) return;
          return returnTo({ action_id: grant.action_id, generation: current.generation, seq: grant.issued_seq }, false, grant.expires_at_ms);
        });
        instanceButtons.push(button);
        row.append(button);
      } else row.append(node('p', 'section-note', '返回窗口 · 证据不足或许可已过期'));
      root.append(row);
    }
    $('native-unassigned').textContent = `${prefix}未归属实例 ${current.unassigned.length} · 按已登记目录和当前观察范围关联；手动选窗不会改变关联。`;
    renderProjectRefs();
    if (settingsValue) renderSettings();
  }
  async function poll() {
    if (polling || !started) return;
    polling = true;
    // A pending shared request may take 40 seconds; visible trust never lasts that long.
    readTimer = setTimeout(() => { markStale('observation_timeout'); render(); }, 12000);
    try {
      const received = requireSnapshot(await api('/api/workspace/observations'));
      if (savedPair && !samePair(savedPair, received)) markStale('config_pending');
      else { current = received; savedPair = null; }
      expire(); render(); scheduleExpiry();
    }
    catch { markStale('observation_failed'); render(); scheduleExpiry(); }
    finally { clearTimeout(readTimer); readTimer = undefined; polling = false; if (started) timer = setTimeout(poll, 2000); }
  }
  function clearPicker(sendDismiss = true) {
    pickerNonce += 1;
    clearTimeout(pickerTimer); pickerTimer = undefined;
    const selection = pickerSelection; pickerSelection = null; picking = false;
    $('window-choices-list').replaceChildren(); $('window-choices-status').textContent = '';
    $('window-choices-picker').hidden = true;
    for (const button of pickerButtons) button.disabled = true;
    if (sendDismiss && selection) post('/api/workspace/observations/window-choices/close', { selection_id: selection.selection_id, generation: selection.generation }).catch(() => {});
  }
  async function returnTo(body: WorkspaceObservationTypes.ReturnBody, manual: boolean, expiresAt: number) {
    expire(); render(); scheduleExpiry();
    if (Date.now() >= expiresAt || (manual ? !pickerSelection || pickerSelection.generation !== body.generation :
      !current || ['stale', 'unavailable'].includes(current.status) || current.generation !== body.generation)) {
      feedback(note('action_expired')); return;
    }
    if (actionPending) return;
    actionPending = true;
    if (manual) clearPicker(false);
    render(); feedback('正在核验所选窗口…');
    try {
      const result = requireReceipt(await post('/api/workspace/observations/return', body));
      feedback(result.status === 'available' ? (result.kind === 'manual_navigation' ? '所选工具窗口已在前台；未确认 Agent 或 pane 关联。' : '已核验的窗口已在前台。') : note(result.reason));
    } catch (error) { feedback(note(errorReason(error) || 'return_unknown')); }
    finally { actionPending = false; render(); }
  }
  async function openPicker(registrationId: string) {
    if (picking || actionPending || !current || current.status !== 'available') return;
    clearPicker(); picking = true;
    const nonce = pickerNonce;
    $('window-choices-picker').hidden = false;
    $('window-choices-status').textContent = '正在读取已登记工具的现有窗口…';
    try {
      const raw = await post('/api/workspace/observations/window-choices', { registration_id: registrationId });
      if (nonce !== pickerNonce || document.hidden) return;
      const result = requireChoices(raw);
      if (!samePair(result, current)) { clearPicker(); feedback(note('config_pending')); return; }
      pickerSelection = { selection_id: result.selection_id, generation: result.generation,
        config_revision: result.config_revision, config_hash: result.config_hash };
      const remaining = result.expires_at_ms - Date.now();
      if (remaining <= 0) { clearPicker(); feedback(note('action_expired')); return; }
      pickerTimer = setTimeout(() => { clearPicker(); feedback('窗口选择已过期，请显式重新打开。'); }, remaining);
      $('window-choices-status').textContent = result.choices.length ? `明确选择后才切换窗口；不识别 Agent、项目或 pane。${result.coverage.status === 'limited' ? ' 列表覆盖受限。' : ''}` : '没有可核验的现有窗口；当前安装形态或权限也可能受限。';
      for (const [index, choice] of result.choices.entries()) {
        const row = node('div', 'window-choice');
        // Window titles only exist in this short-lived DOM; never HTML, logs, or app state.
        row.append(node('span', 'window-choice-title', choice.title || `工具窗口 ${index + 1}`));
        const button = node('button', 'button secondary', '选择并返回此窗口'); button.type = 'button';
        const body = { action_id: choice.action_id, generation: result.generation, seq: choice.issued_seq };
        const expiresAt = choice.expires_at_ms;
        button.addEventListener('click', () => returnTo(body, true, expiresAt)); row.append(button);
        $('window-choices-list').append(row);
      }
    } catch (error) { if (nonce === pickerNonce) { clearPicker(); feedback(note(errorReason(error))); } }
    finally { if (nonce === pickerNonce) picking = false; }
  }

  function fillEditor(row: WorkspaceObservationTypes.EditorRow | null = null) {
    editId = row?.id || null;
    $('registration-category').value = row?.category || 'builtin';
    $('registration-provider').value = row?.provider_id || 'codex';
    $('registration-locator').value = row?.locator_kind === 'executable' ? 'npm_command' : row?.locator_kind || 'npm_command';
    $('registration-label').value = row?.label || '';
    $('registration-path').value = row?.path || '';
    $('registration-enabled').checked = row?.enabled ?? true;
    $('registration-confirm').checked = false;
  }
  function renderSettings() {
    const root = $('registrations-list'); root.replaceChildren();
    if (!settingsValue) return;
    const live = current?.status === 'available' && samePair(current, { config_revision: settingsValue.revision, config_hash: settingsValue.hash });
    $('registrations-status').textContent = `已保存版本 ${settingsValue.revision} · ${live ? '后台已应用同版本新样本' : note(settingsValue.application?.reason || 'config_pending')} · 安装、运行、会话与返回分别核验。`;
    const validation = new Map((live && current ? current.registrations ?? [] : settingsValue.validation ?? []).map(row => [row.registration_id, row]));
    for (const item of settingsValue.registrations) {
      const facts = validation.get(item.id), row = node('div', 'source-line');
      row.append(node('strong', '', `${item.label} · ${item.category === 'custom_exe' ? '自定义程序 / 用户指定' : names[item.provider_id]}`),
        node('p', 'section-note', `${item.enabled ? '已启用' : '已禁用'} · ${item.binding_state === 'rebind_required' ? '待重新绑定' : '已确认本机定位'} · ${facts?.status === 'available' ? '安装已核验' : '安装未核验'} · ${facts?.reason ? note(facts.reason) : '运行与会话查看观察页'}`),
        node('p', 'registration-path', item.path));
      const actions: [string, () => void | Promise<void>][] = [
        [item.binding_state === 'rebind_required' ? '确认本机绑定' : '修改', () => fillEditor(item)],
        [item.enabled ? '禁用' : '启用', () => { if (settingsValue) return submitRows(settingsValue.registrations.map(value => value.id === item.id ? { ...value, enabled: !value.enabled } : value)); }],
        ['删除登记', () => { if (settingsValue) return submitRows(settingsValue.registrations.filter(value => value.id !== item.id)); }],
      ];
      for (const [label, handler] of actions) {
        const button = node('button', 'button secondary', label); button.type = 'button'; button.addEventListener('click', handler); row.append(button);
      }
      root.append(row);
    }
  }
  async function loadSettings(withCandidates = false) {
    const nonce = ++settingsNonce;
    try {
      const raw = await api('/api/profile/observations');
      if (nonce !== settingsNonce || !$('registration-settings').open) return;
      const value = requireSettings(raw);
      settingsValue = value; renderSettings();
      if (!withCandidates) return;
      const rawCandidates = await api('/api/profile/observations/candidates');
      if (nonce !== settingsNonce || !$('registration-settings').open) return;
      const result = requireCandidates(rawCandidates);
      const root = $('registration-candidates'); root.replaceChildren();
      if (!result.candidates?.length) root.append(node('p', 'section-note', '暂无已核候选；可以填写非 PATH 的本机安装路径并保存。'));
      for (const candidate of result.candidates || []) {
        const button = node('button', 'button secondary', `${names[candidate.provider_id]} · ${candidate.path}`); button.type = 'button';
        button.addEventListener('click', () => fillEditor({ category: 'builtin', provider_id: candidate.provider_id,
          locator_kind: candidate.locator_kind, label: names[candidate.provider_id] || '', path: candidate.path, enabled: true }));
        root.append(button);
      }
    } catch (error) { if (nonce === settingsNonce) $('registrations-feedback').textContent = note(errorReason(error)); }
  }
  async function submitRows(rows: RegistrationWrite[]) {
    if (!settingsValue || $('registration-fields').disabled) return;
    settingsNonce += 1;
    $('registration-fields').disabled = true;
    try {
      const value = requireSettings(await post('/api/profile/observations', { expected_revision: settingsValue.revision, expected_hash: settingsValue.hash, registrations: rows }));
      settingsValue = $('registration-settings').open ? value : null;
      savedPair = { config_revision: value.revision, config_hash: value.hash };
      markStale('config_pending'); render(); renderSettings(); fillEditor();
      $('registrations-feedback').textContent = '登记已保存；等待后台应用与安装核验。禁用和删除不会结束外部程序或删除项目任务。';
    } catch (error) {
      const reason = errorReason(error);
      $('registrations-feedback').textContent = reason === 'config_conflict' ? '另一面板已修改登记；已刷新列表，请按新版本重新填写后保存。' : note(reason);
      if (reason === 'config_conflict') await loadSettings();
    } finally { $('registration-fields').disabled = false; }
  }
  function initializeSettings() {
    $('registration-settings').addEventListener('toggle', () => {
      if ($('registration-settings').open) { fillEditor(); loadSettings(true); }
      else { settingsNonce += 1; settingsValue = null; fillEditor(); $('registration-candidates').replaceChildren(); $('registrations-list').replaceChildren(); clearPicker(); }
    });
    $('registration-new').addEventListener('click', () => fillEditor());
    $('registration-form').addEventListener('submit', event => {
      event.preventDefault();
      if (!$('registration-confirm').checked) { $('registrations-feedback').textContent = '请确认当前本机路径；保存只登记，不会启动或执行程序。'; return; }
      const custom = $('registration-category').value === 'custom_exe';
      const fields: WorkspaceObservationTypes.RegistrationFields & { id: string | null } = { id: editId,
        environment_id: 'windows:local', label: $('registration-label').value, path: $('registration-path').value,
        enabled: $('registration-enabled').checked, binding_state: 'confirmed' };
      const row: RegistrationWrite = custom ? { ...fields, category: 'custom_exe', provider_id: null, locator_kind: 'executable' }
        : { ...fields, category: 'builtin', provider_id: $('registration-provider').value, locator_kind: $('registration-locator').value };
      return submitRows([...(settingsValue?.registrations || []).filter(value => value.id !== editId), row]);
    });
  }
  window.WorkspaceObservation = {
    projects: [],
    start(options) {
      if (started) return;
      api = options.fetchJson; post = options.postJson; started = true;
      $('native-observations-section').hidden = false;
      initializeSettings();
      $('close-window-choices').addEventListener('click', () => { clearPicker(); render(); });
      document.addEventListener('visibilitychange', () => { if (document.hidden) clearPicker(); });
      window.addEventListener('pagehide', () => { clearPicker(); started = false; clearTimeout(timer); clearTimeout(expiryTimer); clearTimeout(readTimer); });
      window.addEventListener('pageshow', () => { if (!started) { started = true; expire(); render(); scheduleExpiry(); poll(); } });
      poll();
    },
    renderProjectRefs,
  };
})();
