const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const phases = { assigned: '等待交付', delivered: '产物已交付，待复核', review_pending: '已请求复核，待接收',
  reviewing: '已接收，复核中', reviewed_pass: '复核通过，等待验收', changes_requested: '需要修改',
  accepted: '复核通过，完成交付', artifact_changed: '文件已变化，请重新交付' };
const roleNames = { worker: '提交者', reviewer: '复核者', coordinator: '协调者' };
const names = { garden_submit_artifact: '交付产物', garden_claim_review: '接收复核', garden_record_review: '记录复核结果' };
const steps = { garden_assign_task: '分配任务', garden_submit_artifact: '交付产物', garden_request_review: '请求复核',
  garden_claim_review: '接收复核', garden_record_review: '记录复核结果', garden_accept_artifact: '验收交付' };
function node(tag, text, className) { const value = document.createElement(tag); if (text !== undefined) value.textContent = text; if (className) value.className = className; return value; }
function publicResponse(value, root, actor) {
  return value?.available === true && value.root_thread_id === root && value.actor_id === actor
    && typeof value.coverage === 'string' && value.coverage.length <= 120 && Array.isArray(value.messages) && value.messages.length <= 6
    && value.messages.every(item => ['commentary', 'final_answer'].includes(item.phase) && typeof item.text === 'string'
      && item.text.length <= 1200 && (item.created_at === null || typeof item.created_at === 'string'));
}
function stamp(value) { const date = value && new Date(value); return date && !Number.isNaN(date.valueOf()) ? date.toLocaleString('zh-CN') : '时间未提供'; }

export function validSnapshot(value, root) {
  if (!value || value.available !== true || value.protocol_version !== 1 || value.root_thread_id !== root
    || !Number.isSafeInteger(value.revision) || value.revision < 0 || !value.actors || !value.tasks || !Array.isArray(value.receipts)) return false;
  for (const [id, actor] of Object.entries(value.actors)) {
    if (!uuid.test(id) || actor.actor_id !== id || actor.root_thread_id !== root || !roleNames[actor.role]
      || typeof actor.nickname !== 'string' || typeof actor.agent_path !== 'string' || !Array.isArray(actor.capabilities)) return false;
  }
  for (const [id, task] of Object.entries(value.tasks)) {
    if (!uuid.test(id) || task.task_id !== id || !value.actors[task.assignee_id] || !phases[task.state]
      || !Number.isSafeInteger(task.version) || task.version < 0 || typeof task.title !== 'string' || typeof task.current_valid !== 'boolean') return false;
    if (task.reviewer_id && !value.actors[task.reviewer_id]) return false;
    if (task.state === 'accepted') {
      const receipt = task.accept_receipt;
      if (!task.current_valid || !receipt || receipt.operation !== 'garden_accept_artifact'
        || receipt.root_thread_id !== root || receipt.actor_id !== root || receipt.task_id !== id
        || receipt.version !== task.version || receipt.artifact_sha256 !== task.artifact_sha256
        || !task.claim_receipt || task.review?.outcome !== 'pass' || task.assignee_id === task.reviewer_id) return false;
    }
  }
  return value.receipts.every(receipt => uuid.test(receipt.id) && receipt.root_thread_id === root && value.actors[receipt.actor_id]);
}

function displayName(actor) {
  if (actor.agent_path.includes('ui_comfort')) return '界面观察员';
  if (actor.agent_path.includes('capability_research')) return '能力调研员';
  if (actor.agent_path.includes('event_research')) return '事件研究员';
  if (actor.role === 'reviewer') return '交付复核员';
  return actor.nickname;
}

function currentReviewPassed(task) {
  return task.current_valid && (task.state === 'reviewed_pass' || task.state === 'accepted') && task.review?.outcome === 'pass';
}

export function mount({ root, query, csrf }) {
  const $ = id => document.getElementById(id);
  let snapshot = null, selected = null, active = true, domain = root, generation = 0, dialogueGeneration = 0;
  let dialogue = { state: 'idle' }, busy = false, received = 0, minimumRevision = 0;
  const seen = new Set(), collapsedClouds = new Set();
  $('garden-tabs').hidden = false;
  $('garden-panel').hidden = false;
  $('garden-root-key').textContent = `codex:${root}`;
  const originalTitle = document.title, originalHeading = document.querySelector('header h1').textContent;
  const subtitle = document.querySelector('header p.muted'), originalSubtitle = subtitle.textContent;

  function owns(key) { return Boolean(active && snapshot && domain === root && key.startsWith('codex:') && key.slice(6) !== root && snapshot.actors[key.slice(6)]); }
  function selectActor(key) {
    if (!owns(key)) return;
    selected = key.slice(6); dialogueGeneration++; dialogue = { state: 'idle', actorId: selected };
    render(); loadPublic(selected);
  }
  function clear(message) {
    dialogueGeneration++; dialogue = { state: 'idle' }; snapshot = null; selected = null; $('garden-announcement').textContent = '';
    $('garden-notice').textContent = message; render();
  }
  function observeDomain(key) {
    generation++; domain = key === `codex:${root}` ? root : null;
    clear(domain ? '选回本会话小院可查看交付记录。' : '已选择其它会话；小院内容已清空，各主会话独立。');
  }
  function visibilityChanged() {
    if (!document.hidden) return;
    dialogueGeneration++; dialogue = { state: 'idle', actorId: selected }; render();
  }
  function switchView(next) {
    generation++; active = next === 'garden';
    document.body.dataset.gardenView = next;
    $('garden-view').setAttribute('aria-pressed', String(active));
    $('observation-view').setAttribute('aria-pressed', String(!active));
    document.title = active ? '子agent 协作小院' : originalTitle;
    document.querySelector('header h1').textContent = active ? '子agent 协作小院' : originalHeading;
    subtitle.textContent = active ? '小院里的伙伴 · 同主会话协作 · 交付结果有据可查' : originalSubtitle;
    domain = active ? root : null;
    clear(active ? '正在读取本会话的交付记录…' : '');
    if (active) refresh();
  }
  function taskFor(actor) {
    const order = snapshot.receipts.filter(receipt => receipt.operation === 'garden_assign_task').map(receipt => receipt.task_id);
    return Object.values(snapshot.tasks).filter(task => task.assignee_id === actor.actor_id || task.reviewer_id === actor.actor_id)
      .sort((a, b) => order.indexOf(a.task_id) - order.indexOf(b.task_id)).at(-1);
  }
  function summary(task) {
    if (!task) return '';
    if (task.state === 'accepted') return task.accept_receipt.summary;
    if (task.state === 'changes_requested' || task.state === 'reviewed_pass') return task.review.summary;
    if (task.state === 'artifact_changed') return '旧结果已失效，重新交付后再复核。';
    return task.submit_receipt?.summary || '';
  }
  async function loadPublic(actorId) {
    if (!active || domain !== root || document.hidden || !owns(`codex:${actorId}`)) return;
    const token = ++dialogueGeneration, viewGeneration = generation;
    dialogue = { state: 'loading', actorId }; render();
    try {
      const result = await query(`/api/garden/actors/${actorId}/public-messages`);
      if (token !== dialogueGeneration || viewGeneration !== generation || !active || domain !== root || selected !== actorId) return;
      dialogue = publicResponse(result, root, actorId) ? { state: 'ready', actorId, result } : { state: 'unavailable', actorId };
    } catch {
      if (token !== dialogueGeneration || viewGeneration !== generation || !active || domain !== root || selected !== actorId) return;
      dialogue = { state: 'unavailable', actorId };
    }
    render();
  }
  async function write(route, payload, success) {
    if (!active || domain !== root || !snapshot || busy || !csrf() || performance.now() - received > 30000) return;
    const attempt = generation;
    busy = true; render();
    try {
      const result = await query(route, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Session-CSRF': csrf() }, body: JSON.stringify(payload) });
      if (attempt !== generation || domain !== root || !active) return;
      if (result.root_thread_id !== root || !result.receipt || result.receipt.root_thread_id !== root) throw new Error('返回收据与本主会话不一致。');
      minimumRevision = Math.max(minimumRevision, result.revision);
      $('garden-notice').textContent = success;
      await refresh();
    } catch (error) {
      if (attempt === generation && domain === root && active) $('garden-notice').textContent = error.result?.reason === 'artifact_changed'
        ? '文件已经变化，请让提交者重新交付。' : `操作未记录：${error.message}`;
    } finally { busy = false; if (active && domain === root) render(); }
  }
  function render() {
    const focused = document.activeElement?.dataset.gardenFocus;
    const list = $('garden-actors'), detail = $('garden-detail'), roster = $('garden-scene-roster');
    const detailsOpen = new Set(Array.from(detail.querySelectorAll('details[open]'), value => value.dataset.recordId));
    list.replaceChildren(); detail.replaceChildren(); roster.replaceChildren();
    const actors = snapshot && domain === root ? Object.values(snapshot.actors).filter(actor => actor.actor_id !== root) : [];
    const priority = actor => { const task = taskFor(actor); return task?.state === 'accepted' ? actor.role === 'worker' ? 0 : 1 : task ? 2 : 3; };
    actors.sort((a, b) => priority(a) - priority(b));
    const tasks = snapshot ? Object.values(snapshot.tasks) : [];
    const accepted = tasks.find(task => task.state === 'accepted' && task.current_valid && task.accept_receipt);
    const attention = tasks.filter(task => ['changes_requested', 'artifact_changed'].includes(task.state)).length;
    const peek = $('garden-peek');
    peek.classList.toggle('has-result', Boolean(accepted));
    $('garden-peek-domain').textContent = `本主会话 · ${actors.length} 个伙伴 · ${tasks.filter(task => task.state === 'accepted' && task.current_valid).length} 项已验收${attention ? ` · ${attention} 项需关注` : ''}`;
    $('garden-peek-title').textContent = accepted ? `${displayName(snapshot.actors[accepted.assignee_id])}的完成摘要` : '等待真实交付';
    $('garden-peek-summary').textContent = accepted ? accepted.accept_receipt.summary : '独立复核并验收后，完成摘要会出现在这里。';
    $('garden-counts').textContent = snapshot ? `${actors.length} 个已登记伙伴 · ${tasks.filter(task => task.state === 'accepted' && task.current_valid).length} 项已验收 · ${tasks.filter(task => ['delivered', 'review_pending', 'reviewing', 'reviewed_pass'].includes(task.state)).length} 项待复核或验收${attention ? ` · ${attention} 项需关注` : ''}`
      : '同主会话子agent · 产物交接与复核';
    if (!selected || !actors.some(actor => actor.actor_id === selected)) selected = actors.find(actor => taskFor(actor))?.actor_id || actors[0]?.actor_id || null;
    const newResults = [];
    for (const actor of actors) {
      const task = taskFor(actor), card = node('article', undefined, 'garden-booth');
      const role = roleNames[actor.role], phase = task && actor.role === 'reviewer' && currentReviewPassed(task) ? '独立复核通过' : task ? phases[task.state] : '已登记，待任务';
      const tile = node('button', undefined, 'garden-roster-card');
      tile.dataset.actorId = actor.actor_id; tile.dataset.gardenFocus = `roster:${actor.actor_id}`;
      tile.setAttribute('aria-pressed', String(selected === actor.actor_id));
      tile.append(node('strong', displayName(actor)), node('span', role, 'garden-role'),
        node('span', task?.title || '已登记，待任务', 'garden-roster-task'), node('span', phase, 'garden-roster-phase'));
      tile.addEventListener('click', () => { selectActor(`codex:${actor.actor_id}`); if (innerWidth <= 900) $('garden-detail').scrollIntoView({ block: 'start', behavior: 'smooth' }); });
      if (roster.childElementCount < 4) roster.append(tile);
      const button = node('button', undefined, 'garden-actor');
      button.dataset.gardenFocus = actor.actor_id; button.dataset.actorId = actor.actor_id;
      button.setAttribute('aria-pressed', String(selected === actor.actor_id));
      const label = node('span', undefined, 'garden-name');
      label.append(node('strong', displayName(actor)), node('span', `${role} · ${phase}`, 'small'));
      button.append(label); button.addEventListener('click', () => selectActor(`codex:${actor.actor_id}`)); card.append(button);
      card.append(node('p', task ? `负责：${task.title}` : '已登记，待任务', 'garden-task-line'));
      const abilities = node('p', actor.capabilities.map(name => names[name] || name).join(' · '), 'small garden-abilities'); card.append(abilities);
      if (task && task.state !== 'assigned') {
        const receipt = task.accept_receipt, cloudId = `${actor.actor_id}:${task.task_id}:${task.version}:${task.state}`;
        const cloud = node('button', undefined, 'garden-cloud');
        cloud.dataset.gardenFocus = cloudId; cloud.dataset.cloudState = task.state;
        cloud.setAttribute('aria-expanded', String(!collapsedClouds.has(cloudId)));
        const reviewed = actor.role === 'reviewer' && currentReviewPassed(task);
        cloud.append(node('strong', reviewed ? '独立复核通过' : phases[task.state]));
        if (!collapsedClouds.has(cloudId)) cloud.append(node('span', reviewed ? `${task.title}，独立复核通过。` : summary(task)));
        cloud.title = '点击收起或展开摘要；完整记录在所选伙伴详情中。';
        cloud.addEventListener('click', () => { if (collapsedClouds.has(cloudId)) collapsedClouds.delete(cloudId); else collapsedClouds.add(cloudId); selected = actor.actor_id; render(); });
        if (task.state === 'accepted' && actor.role === 'worker' && !seen.has(receipt.id)) { newResults.push(receipt); }
        card.append(cloud);
      }
      list.append(card);
    }
    if (actors.length > 4) {
      const more = node('button', `还有 ${actors.length - 4} 位伙伴 · 查看全部工位`, 'garden-roster-more');
      more.dataset.gardenFocus = 'roster:more';
      more.addEventListener('click', () => list.scrollIntoView({ block: 'start', behavior: 'smooth' }));
      roster.append(more);
    }
    for (const receipt of newResults) seen.add(receipt.id);
    if (newResults.length) $('garden-announcement').textContent = newResults[0].summary;
    if (!actors.length) { const message = snapshot ? '小院还没有登记子agent。由本会话协调者登记后，伙伴会出现在这里。' : '等待本主会话的有效记录。'; list.append(node('p', message, 'garden-empty')); roster.append(node('p', message, 'garden-empty')); }

    const actor = actors.find(value => value.actor_id === selected);
    if (actor) {
      detail.append(node('h3', `${displayName(actor)}的工位`));
      detail.append(node('p', `角色：${roleNames[actor.role]} · 负责${actor.role === 'reviewer' ? '独立接收与复核交付产物' : '交付已分配的任务产物'}`, 'garden-responsibility'));
      detail.append(node('p', `已注册能力：${actor.capabilities.map(name => names[name] || name).join(' · ')}`, 'small'));
      const identity = node('details'); identity.dataset.recordId = 'identity';
      identity.open = detailsOpen.has('identity'); identity.append(node('summary', '完整身份与主会话'));
      for (const [label, value] of [['子agent', `codex:${actor.actor_id}`], ['所属主会话', `codex:${root}`], ['直接父会话', actor.parent_thread_id], ['原生名称', actor.nickname], ['路径', actor.agent_path]]) {
        identity.append(node('p', label, 'small'), node('code', value || '未提供', 'wrap'));
      }
      const copy = node('button', '复制完整身份键'); copy.dataset.gardenFocus = 'copy-actor';
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(`codex:${actor.actor_id}`); $('garden-notice').textContent = '完整身份键已复制。'; } catch { $('garden-notice').textContent = '复制不可用，请在完整身份中手动选择。'; } });
      identity.append(copy); detail.append(identity);
      const task = taskFor(actor);
      if (task) {
        const reviewed = actor.role === 'reviewer' && currentReviewPassed(task);
        detail.append(node('h3', task.title), node('p', reviewed ? '独立复核通过' : phases[task.state], 'garden-phase'), node('p', reviewed ? task.review.summary : summary(task)));
        const related = tasks.filter(value => value.assignee_id === actor.actor_id || value.reviewer_id === actor.actor_id);
        if (related.length > 1) detail.append(node('p', `当前显示最近关联事项；另有 ${related.length - 1} 项历史事项。`, 'small'));
        const timeline = node('section', undefined, 'garden-timeline');
        timeline.append(node('h4', '已发生的工作节点'));
        timeline.append(node('p', '节点短号对应下方可展开的完整收据。', 'small'));
        if (!task.current_valid) timeline.append(node('p', '当前文件已变化，以下成功节点仅作历史记录。', 'garden-phase'));
        const events = snapshot.receipts.filter(value => value.task_id === task.task_id && steps[value.operation]);
        const trail = node('ol');
        for (const receipt of events) {
          const item = node('li');
          const doer = snapshot.actors[receipt.actor_id];
          item.append(node('strong', steps[receipt.operation]), node('span', `${doer ? displayName(doer) : '身份未提供'} · ${stamp(receipt.created_at)} · 版本 ${receipt.version ?? '未提供'}`, 'small'),
            node('span', `收据 #${receipt.id.slice(-8)}`, 'small garden-receipt-short'));
          trail.append(item);
        }
        if (events.length) timeline.append(trail); else timeline.append(node('p', '暂无可展示的收据节点。', 'small'));
        detail.append(timeline);
        const records = node('details'); records.dataset.recordId = task.task_id; records.open = detailsOpen.has(task.task_id);
        records.append(node('summary', `产物与完整收据 · 版本 ${task.version}`), node('p', task.artifact_path, 'wrap'),
          node('code', task.artifact_sha256 || '尚未交付', 'wrap'));
        if (!task.current_valid) records.append(node('p', '以下是历史收据，当前文件变化后已失效。', 'garden-phase'));
        const receiptList = snapshot.receipts.filter(value => value.task_id === task.task_id);
        records.append(node('pre', JSON.stringify(receiptList, null, 2))); detail.append(records);
        const actions = node('div', undefined, 'garden-actions');
        const reviewers = actors.filter(value => value.role === 'reviewer' && value.actor_id !== task.assignee_id);
        if (task.state === 'delivered') {
          const choice = node('select'); choice.setAttribute('aria-label', '本主会话复核者');
          for (const reviewer of reviewers) { const option = node('option', `${displayName(reviewer)} · ${reviewer.actor_id.slice(0, 8)}`); option.value = reviewer.actor_id; choice.append(option); }
          const request = node('button', '请求复核'); request.dataset.gardenFocus = 'request-review';
          request.disabled = busy || !csrf() || !reviewers.length || !task.current_valid;
          request.addEventListener('click', () => write('/api/garden/request-review', { task_id: task.task_id, version: task.version, artifact_sha256: task.artifact_sha256, reviewer_id: choice.value }, '请求已记录，待本会话协调者派发；复核者接收后才进入复核。'));
          actions.append(choice, request);
          if (!reviewers.length) actions.append(node('p', '本会话尚未登记独立复核者。', 'small'));
        }
        if (task.state === 'reviewed_pass') {
          const accept = node('button', '验收交付'); accept.dataset.gardenFocus = 'accept-delivery'; accept.disabled = busy || !csrf() || !task.current_valid;
          accept.addEventListener('click', () => write('/api/garden/accept', { task_id: task.task_id, version: task.version, artifact_sha256: task.artifact_sha256, summary: `${task.title}，独立复核通过，完成交付。` }, '当前版本已验收，完成摘要已记录。'));
          actions.append(accept);
        }
        if (task.state === 'review_pending') {
          actions.append(node('p', '请求已记录，等待本会话协调者派发及复核者实际接收。', 'small'));
          const copy = node('button', '复制复核请求'); copy.dataset.gardenFocus = 'copy-review';
          copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(`请在本主会话内派发复核：task ${task.task_id}，version ${task.version}，SHA256 ${task.artifact_sha256}，reviewer ${task.reviewer_id}，request ${task.review_request.id}。`); $('garden-notice').textContent = '复核请求已复制，可交给本会话协调者。'; } catch { $('garden-notice').textContent = '复制不可用，请展开完整收据。'; } }); actions.append(copy);
        }
        detail.append(actions);
      } else detail.append(node('p', '能力已登记；当前还没有关联任务。', 'garden-empty'));
      const conversation = node('section', undefined, 'garden-conversation');
      conversation.append(node('h4', '最近公开发言'), node('p', '仅展示本机记录中该子agent最近的公开 assistant 发言，并非完整对话；任务进度以收据为准。', 'small'));
      const talk = dialogue.actorId === actor.actor_id ? dialogue : { state: 'idle' };
      if (talk.state === 'loading') conversation.append(node('p', '正在读取公开发言…', 'small'));
      else if (talk.state === 'unavailable') conversation.append(node('p', '公开发言暂不可用。', 'small'));
      else if (talk.state === 'ready') {
        conversation.append(node('p', talk.result.coverage, 'small'));
        if (!talk.result.messages.length) conversation.append(node('p', '当前记录没有可展示的公开发言。', 'small'));
        for (const message of talk.result.messages) {
          const bubble = node('article', undefined, 'garden-utterance');
          bubble.append(node('span', `${message.phase === 'final_answer' ? '最终答复' : '工作发言'} · ${stamp(message.created_at)}${message.truncated ? ' · 已截短' : ''}`, 'small'), node('p', message.text));
          conversation.append(bubble);
        }
      } else conversation.append(node('p', '点选伙伴或按下按钮，读取最近公开发言。', 'small'));
      const retry = node('button', talk.state === 'ready' ? '重新读取公开发言' : '查看最近公开发言');
      retry.dataset.gardenFocus = 'public-messages'; retry.disabled = talk.state === 'loading';
      retry.addEventListener('click', () => loadPublic(actor.actor_id)); conversation.append(retry);
      detail.append(conversation);
    } else detail.append(node('p', '选择一个伙伴查看产物、能力和复核记录。', 'garden-empty'));
    if (focused) Array.from(document.querySelectorAll('[data-garden-focus]')).find(value => value.dataset.gardenFocus === focused)?.focus({ preventScroll: true });
  }
  async function refresh() {
    if (!active || domain !== root || document.hidden) return;
    const attempt = generation, start = performance.now();
    try {
      const next = await query('/api/garden');
      if (attempt !== generation || !active || domain !== root) return;
      if (performance.now() - start > 30000 || !validSnapshot(next, root)) { clear('小院记录未能核验，请刷新。'); return; }
      if (next.revision < minimumRevision || (snapshot && next.revision < snapshot.revision)) return;
      snapshot = next; received = start; render();
      if ($('garden-notice').textContent === '正在读取本会话的交付记录…') $('garden-notice').textContent = '';
    } catch (error) { if (attempt === generation && active && domain === root) clear(`小院暂未接入：${error.message}`); }
  }
  const selectGarden = () => switchView('garden'), selectObservation = () => switchView('observations');
  $('garden-view').addEventListener('click', selectGarden); $('observation-view').addEventListener('click', selectObservation);
  $('refresh-garden').addEventListener('click', refresh);
  document.addEventListener('visibilitychange', visibilityChanged);
  switchView('garden');
  return { refresh, observeDomain, dispose() {
    generation++; active = false; clear(''); delete document.body.dataset.gardenView;
    $('garden-view').removeEventListener('click', selectGarden); $('observation-view').removeEventListener('click', selectObservation);
    $('refresh-garden').removeEventListener('click', refresh); $('garden-panel').hidden = true; $('garden-tabs').hidden = true;
    document.removeEventListener('visibilitychange', visibilityChanged);
  } };
}
