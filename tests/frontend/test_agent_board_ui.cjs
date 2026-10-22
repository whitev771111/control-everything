// Synthetic management API and DOM only; no real Agent or user data.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../web/workspace/agents.js'), 'utf8');

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {};
    this.value = ''; this.checked = false; this.disabled = false; this.open = false; this.text = '';
    this.classList = { toggle() {} };
  }
  append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
  insertBefore(node, before) { node.parentElement = this; this.children.splice(this.children.indexOf(before), 0, node); }
  replaceChildren(...nodes) { this.children = []; this.text = ''; this.append(...nodes); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  focus() {}
  removeEventListener(name) { delete this.listeners[name]; }
  get all() { return [this, ...this.children.flatMap(node => node.all)]; }
  querySelector(selector) { return this.all.find(node => selector.startsWith('#') ? node.id === selector.slice(1) : node.tagName === selector) || null; }
  querySelectorAll(selector) { return this.all.filter(node => node.tagName === selector); }
  get options() { return this.children; }
  get selectedIndex() { return this.children.findIndex(node => node.value === this.value); }
  showModal() { this.open = true; }
  close() { this.open = false; }
}
const stamp = '2026-10-19T00:00:00Z';
const agent = { id: 'a', name: 'Synthetic Agent', enabled: true, online: true, last_seen_at: stamp };
const item = (id, extra = {}) => ({ id, title: `Task ${id}`, state: 'todo', project_id: 'p', revision: 7, assigned_agent_id: null, assignment_state: 'unassigned', ...extra });
function fixture(options = {}) {
  const root = new Element('section');
  const context = { window: { confirm: () => true }, document: { createElement: tag => new Element(tag), getElementById: () => null }, URLSearchParams, TextEncoder, console };
  if (options.execution) context.window.WorkspaceExecution = options.execution;
  vm.runInNewContext(source, context);
  const calls = [], posts = [];
  let rows = options.rows || [item('one')], projects = options.projects || [{ id: 'p', name: 'Project' }], agents = options.agents || [agent];
  const state = { failItems: false, failRegistry: false, failProjects: false, failPost: false, detail: options.detail, pageRevision: 9 };
  const fetchJson = async url => {
    calls.push(url);
    if (options.read) { const value = await options.read(url); if (value !== undefined) return value; }
    if (url === '/api/workspace') return { projects: { status: state.failProjects ? 'error' : 'available', items: projects } };
    if (url === '/api/workspace/agents') { if (state.failRegistry) throw new Error('registry unavailable'); return { items: agents, revision: 1 }; }
    if (url.startsWith('/api/workspace/items?')) {
      if (state.failItems) throw new Error('items unavailable');
      if (state.itemsPromise) return state.itemsPromise;
      const query = new URLSearchParams(url.split('?')[1]); const offset = Number(query.get('offset') || 0);
      return { items: state.emptySecondPage && offset ? [] : rows.slice(offset, offset + 100), total: rows.length, list_revision: state.pageRevision + (state.changedSecondPage && offset ? 1 : 0) };
    }
    if (url.startsWith('/api/workspace/agent-tasks/')) {
      if (state.detailPromise) return state.detailPromise;
      if (state.detail instanceof Error) throw state.detail;
      return state.detail || { item: rows.find(row => row.id === decodeURIComponent(url.split('/').at(-1))), run: null, events: [] };
    }
    throw new Error('Unexpected GET ' + url);
  };
  const postJson = async (url, body) => {
    posts.push({ url, body }); if (state.failPost) throw state.postError || new Error('409 revision conflict');
    if (options.post) { const value = await options.post(url, body); if (value !== undefined) return value; }
    if (url.endsWith('/items/update')) { const found = rows.find(row => row.id === body.id); Object.assign(found, body.changes); return { item: found }; }
    return {};
  };
  const callbacks = [];
  const api = context.window.WorkspaceAgentBoard.create({ root, fetchJson, postJson, now: () => Date.parse(stamp), onRegister: () => callbacks.push('register'), onAddProject: () => callbacks.push('project') });
  const node = id => { const found = root.querySelector('#' + id); assert.ok(found, id); return found; };
  const submit = async () => { const form = node('ab-review-submit').parentElement.parentElement; await form.listeners.submit({ preventDefault() {} }); };
  return { api, root, node, calls, posts, state, callbacks, submit, groupItems: context.window.WorkspaceAgentBoard.groupItems };
}

function dispatchFixture(options = {}) {
  const root = new Element('section'), calls = [], posts = [], dirty = [];
  const context = { window: { confirm: () => true }, document: { createElement: tag => new Element(tag) }, URLSearchParams, TextEncoder, console };
  vm.runInNewContext(source, context);
  const task = item('brief'), record = { id: 'dispatch-one', agent_id: 'a', directory_path: 'C:/example/project', content_text: 'Saved request', brief_sha256: 'a'.repeat(64), delivery_state: 'previewed' };
  const state = { current: true, revision: 3, records: [], globalBusy: false };
  const briefResult = () => ({ item: task, brief: { revision: state.revision, request_text: 'Build a small feature', references: [], execution_directory_id: 'main' }, directories: [{ binding_id: 'main', path: 'C:/example/project' }] });
  const api = context.window.WorkspaceTaskDispatch.create({ root, item: task, agents: options.agents || [agent],
    isCurrent: () => state.current, isBusy: () => state.globalBusy, onBusy: value => { state.globalBusy = value; }, onDirty: value => dirty.push(value),
    fetchJson: async url => { calls.push(url); if (options.read) return options.read(url, briefResult()); return url.endsWith('/brief') ? briefResult() : { items: state.records }; },
    postJson: async (url, body) => {
      posts.push({ url, body }); if (options.post) return options.post(url, body);
      if (url.endsWith('/brief')) { state.revision++; return briefResult(); }
      if (url.endsWith('/dispatch-preview')) return { status: 'preview', dispatch: { brief_revision: state.revision }, brief_sha256: 'a'.repeat(64), content_text: 'Preview request' };
      if (url.endsWith('/dispatch')) { state.records = [record]; return { status: 'previewed', dispatch: record }; }
      if (url.endsWith('/sent')) record.delivery_state = 'sent';
      if (url.endsWith('/cancel')) record.delivery_state = 'cancelled';
      return record;
    } });
  return { root, api, calls, posts, dirty, state, node: id => root.querySelector('#' + id) };
}

test('brief edits invalidate the preview and preserve the opening revision on conflict', async () => {
  const f = dispatchFixture({ post: async () => { throw Object.assign(new Error('conflict'), { reason: 'brief_changed' }); } }); await f.api.ready;
  const request = f.node('td-request_text'); request.value = 'Unsaved request'; request.listeners.input();
  assert.equal(f.node('td-preview').disabled, true);
  await f.node('td-save').parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts[0].body.expected_revision, 3); assert.equal(request.value, 'Unsaved request');
  assert.match(f.node('td-status').textContent, /草稿保留/);
  await f.node('td-save').parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 1);
});

test('manual dispatch requires preview confirmation and a separate explicit sent acknowledgement', async () => {
  const f = dispatchFixture(); await f.api.ready;
  f.node('td-agent').value = 'a'; await f.node('td-preview').listeners.click();
  await f.node('td-create').listeners.click(); assert.equal(f.posts.length, 1);
  f.node('td-confirm').checked = true; await f.node('td-create').listeners.click();
  assert.equal(f.posts[1].body.expected_brief_revision, 3); assert.equal(f.posts[1].body.confirmed, true);
  assert.equal(f.posts[1].body.preview_sha256, 'a'.repeat(64));
  const history = f.node('td-history'), sent = history.all.find(n => n.tagName === 'button' && n.textContent === '我已手动发送给 Agent');
  await sent.listeners.click(); assert.equal(f.posts.length, 2);
  history.all.find(n => n.tagName === 'input').checked = true;
  await sent.listeners.click(); assert.ok(f.posts[2].url.endsWith('/sent'));
  assert.match(history.textContent, /已手动发送 · 未确认接收/);
  assert.equal(history.all.some(n => n.tagName === 'button' && n.textContent === '我已手动发送给 Agent'), false);
});

test('dispatch does not send a second create while the first write is pending', async () => {
  let resolveCreate;
  const f = dispatchFixture({ post: (url, body) => url.endsWith('/dispatch-preview')
    ? { status: 'preview', dispatch: { brief_revision: 3 }, brief_sha256: 'a'.repeat(64), content_text: 'Request' }
    : new Promise(resolve => { resolveCreate = resolve; }) });
  await f.api.ready; f.node('td-agent').value = 'a'; await f.node('td-preview').listeners.click();
  f.node('td-confirm').checked = true; const pending = f.node('td-create').listeners.click(); await f.node('td-create').listeners.click();
  assert.equal(f.posts.length, 2);
  resolveCreate({ status: 'previewed', dispatch: { id: 'one', brief_sha256: 'changed' } }); await pending;
  assert.equal(f.node('td-confirm').checked, false); assert.match(f.node('td-status').textContent, /材料与预览已变化/);
});

test('late brief response does not populate a closed task and unsupported endpoints keep the module disabled', async () => {
  let resolve;
  const f = dispatchFixture({ read: (_url, value) => new Promise(done => { resolve = () => done(value); }) });
  f.state.current = false; resolve(); await f.api.ready;
  assert.equal(f.node('td-request_text').value, ''); assert.equal(f.node('td-save').parentElement.disabled, true);
  const old = dispatchFixture({ read: async () => { throw new Error('接口尚未提供'); } }); await old.api.ready;
  assert.match(old.node('td-status').textContent, /接口尚未提供/); assert.equal(old.node('td-save').parentElement.disabled, true);
});

test('UTF8 brief budget is enforced before POST and shortening the draft permits saving without discarding it', async () => {
  const f = dispatchFixture(); await f.api.ready;
  f.node('td-request_text').value = '中'.repeat(20000); f.node('td-acceptance_text').value = '文'.repeat(10000);
  const submit = () => f.node('td-save').parentElement.parentElement.listeners.submit({ preventDefault() {} });
  await submit(); assert.equal(f.posts.length, 0); assert.match(f.node('td-status').textContent, /64 KiB/);
  f.node('td-request_text').value = 'Small request'; f.node('td-acceptance_text').value = 'Small acceptance';
  await submit(); assert.equal(f.posts.length, 1); assert.match(f.node('td-status').textContent, /已保存/);
});

test('preview hash conflict prevents replay and leaves the saved brief and agent selection intact', async () => {
  const f = dispatchFixture({ post: async url => {
    if (url.endsWith('/dispatch-preview')) return { status: 'preview', dispatch: { brief_revision: 3 }, brief_sha256: 'a'.repeat(64), content_text: 'Request' };
    throw Object.assign(new Error('changed'), { reason: 'dispatch_preview_changed' });
  } }); await f.api.ready;
  f.node('td-agent').value = 'a'; await f.node('td-preview').listeners.click(); f.node('td-confirm').checked = true;
  await f.node('td-create').listeners.click(); await f.node('td-create').listeners.click();
  assert.equal(f.posts.length, 2); assert.equal(f.node('td-agent').value, 'a');
  assert.equal(f.node('td-confirm').checked, false); assert.match(f.node('td-status').textContent, /重新读取/);
});

async function previewAutoDispatch(f) {
  f.node('td-agent').value = 'a'; f.node('td-agent').listeners.change();
  f.node('td-auto').checked = true; f.node('td-auto').listeners.change();
  await f.node('td-preview').listeners.click();
}

test('F5 an existing unstarted automatic snapshot retries only after reload and renewed confirmation', async () => {
  let saved = false, executions = 0, contexts = 0;
  const record = { id: 'auto-dispatch', agent_id: 'a', brief_sha256: 'a'.repeat(64), delivery_mode: 'auto', delivery_state: 'previewed', run_id: null };
  const f = dispatchFixture({ agents: [{ ...agent, adapter: 'codex' }], read: async (url, brief) => {
    if (url.endsWith('/brief')) return brief;
    if (url.endsWith('/execution-context')) return { context_hash: `context-${++contexts}` };
    return { items: saved ? [record] : [] };
  }, post: async url => {
    if (url.endsWith('/dispatch-preview')) return { status: 'preview', dispatch: { brief_revision: 3 }, brief_sha256: record.brief_sha256, content_text: 'Request' };
    if (url.endsWith('/dispatch')) { const status = saved ? 'existing' : 'previewed'; saved = true; return { status, dispatch: record }; }
    if (url.endsWith('/execute') && ++executions === 1) throw Object.assign(new Error('capacity_busy'), { status: 409, reason: 'capacity_busy' });
    return { status: 'admitted' };
  } });
  await f.api.ready; await previewAutoDispatch(f); f.node('td-confirm').checked = true;
  await f.node('td-create').listeners.click(); assert.equal(executions, 1);
  assert.match(f.node('td-status').textContent, /capacity_busy.*不会自动重试/);
  await f.node('td-create').listeners.click(); await f.node('td-preview').listeners.click();
  assert.equal(executions, 1); assert.equal(f.posts.filter(row => row.url.endsWith('/dispatch')).length, 1);
  await f.root.all.find(node => node.textContent === '重新读取说明与交接记录').listeners.click();
  assert.equal(executions, 1); await previewAutoDispatch(f);
  await f.node('td-create').listeners.click(); assert.equal(executions, 1);
  f.node('td-confirm').checked = true; await f.node('td-create').listeners.click();
  const writes = f.posts.filter(row => row.url.endsWith('/execute'));
  assert.equal(executions, 2); assert.equal(contexts, 2);
  assert.equal(writes[0].url, writes[1].url); assert.equal(writes[1].body.context_hash, 'context-2');
  assert.equal(writes[1].body.confirmed, true); assert.match(f.node('td-status').textContent, /后台启动 Agent/);
});

test('F5 an automatic execution with unknown outcome cannot be replayed before a record reload', async () => {
  let finish;
  const record = { id: 'pending-auto', brief_sha256: 'a'.repeat(64), delivery_mode: 'auto', delivery_state: 'previewed', run_id: null };
  const f = dispatchFixture({ agents: [{ ...agent, adapter: 'dsh' }], read: async (url, brief) => url.endsWith('/brief') ? brief : url.endsWith('/execution-context') ? { context_hash: 'context' } : { items: [record] }, post: async url => {
    if (url.endsWith('/dispatch-preview')) return { status: 'preview', dispatch: { brief_revision: 3 }, brief_sha256: record.brief_sha256, content_text: 'Request' };
    if (url.endsWith('/dispatch')) return { status: 'previewed', dispatch: record };
    if (url.endsWith('/execute')) return new Promise((_resolve, reject) => { finish = () => reject(new Error('connection closed')); });
  } });
  await f.api.ready; await previewAutoDispatch(f); f.node('td-confirm').checked = true;
  const pending = f.node('td-create').listeners.click(); await new Promise(setImmediate);
  await f.node('td-create').listeners.click(); assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 1);
  finish(); await pending;
  await f.node('td-create').listeners.click(); assert.equal(f.node('td-preview').disabled, true);
  assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 1);
  assert.equal(f.node('td-confirm').checked, false); assert.match(f.node('td-status').textContent, /不会自动重试|不会重复提交/);
});

for (const status of ['previewed', 'existing']) for (const recordState of [
  { delivery_state: 'previewed', run_id: 'known-run' }, { delivery_state: 'cancelled', run_id: null },
  { delivery_state: 'failed', run_id: null }, { delivery_state: 'sent', run_id: null },
  { delivery_state: 'delivered', run_id: null }, { delivery_state: 'previewed', run_id: null, delivery_mode: 'manual' },
  { delivery_state: 'previewed', run_id: null, brief_sha256: 'b'.repeat(64) }
]) test(`F5 ${status} snapshot ${JSON.stringify(recordState)} cannot restart automatically`, async () => {
  const record = { id: 'old-auto', brief_sha256: 'a'.repeat(64), delivery_mode: 'auto', ...recordState };
  const f = dispatchFixture({ agents: [{ ...agent, adapter: 'codex' }], read: async (url, brief) => url.endsWith('/brief') ? brief : { items: [record] }, post: async url => {
    if (url.endsWith('/dispatch-preview')) return { status: 'preview', dispatch: { brief_revision: 3 }, brief_sha256: 'a'.repeat(64), content_text: 'Request' };
    if (url.endsWith('/dispatch')) return { status, dispatch: record };
    throw new Error('Unexpected execution');
  } });
  await f.api.ready; await previewAutoDispatch(f); f.node('td-confirm').checked = true; await f.node('td-create').listeners.click();
  assert.equal(f.calls.filter(url => url.endsWith('/execution-context')).length, 0);
  assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 0);
});

test('groups distinguish assignment from a real run and honor manual review/completion', () => {
  const f = fixture(); const rows = [item('u'), item('a', { assigned_agent_id: 'a', assignment_state: 'assigned' }), item('r', { assignment_state: 'running' }), item('v', { state: 'review', assignment_state: 'running' }), item('d', { state: 'done', assignment_state: 'waiting_review' }), item('b', { state: 'blocked' }), item('f', { assignment_state: 'failed' })];
  const groups = f.groupItems(rows);
  for (const [key, id] of Object.entries({ unassigned: 'u', assigned: 'a', running: 'r', waiting_review: 'v', done: 'd', blocked: 'b', failed: 'f' })) assert.equal(groups[key][0].id, id);
});

test('reads every page with a snapshot token, excludes archived/removed projects and scopes global items', async () => {
  const rows = Array.from({ length: 103 }, (_, i) => item(String(i)));
  rows.push(item('global', { project_id: null }), item('removed', { project_id: 'gone' }), item('archived', { project_id: 'old' }));
  const f = fixture({ rows, projects: [{ id: 'p', name: 'Project' }, { id: 'old', name: 'Archived', archived: true }] });
  await f.api.load();
  assert.ok(f.calls.includes('/api/workspace/items?limit=100&offset=100&list_revision=9'));
  assert.match(f.node('ab-status').textContent, /104/);
  assert.doesNotMatch(f.node('ab-groups').textContent, /Task removed|Task archived/);
  f.api.setProject('__global__'); assert.match(f.node('ab-groups').textContent, /Task global/); assert.doesNotMatch(f.node('ab-groups').textContent, /Task 0/);
  f.api.setProject('p'); assert.doesNotMatch(f.node('ab-groups').textContent, /Task global/);
});

test('home exposes explicit registration and project onboarding callbacks', async () => {
  const f = fixture(); await f.api.load();
  for (const text of ['＋ 添加 Agent', '＋ 添加项目文件夹']) await f.root.all.find(node => node.tagName === 'button' && node.textContent === text).listeners.click();
  assert.deepEqual(f.callbacks, ['register', 'project']);
});

test('project navigation reaches sessions and attention excludes ordinary and completed tasks', async () => {
  const f = fixture({ rows: [item('todo'), item('review', { state: 'review' }), item('done', { state: 'done' }), item('blocked', { state: 'blocked' }), item('failed', { assignment_state: 'failed' }), item('elsewhere', { project_id: 'other', state: 'review' })] });
  await f.api.load(); f.api.setProject('p', true);
  const nav = f.node('ab-project-tabs'); assert.equal(nav.hidden, false);
  assert.equal(nav.children.find(node => node.textContent === '会话').href, '#project/p/sessions');
  assert.equal(nav.children.find(node => node.textContent === '待处理').attributes['aria-current'], 'page');
  assert.match(f.node('ab-groups').textContent, /Task review|Task blocked|Task failed/);
  assert.doesNotMatch(f.node('ab-groups').textContent, /Task todo|Task done|Task elsewhere/);
  f.api.setProject('p'); assert.match(f.node('ab-groups').textContent, /Task todo/);
  assert.equal(f.root.all.find(node => node.tagName === 'a' && node.textContent === '本项目事项管理').href, '#project/p/manage');
});

test('project actions stay scoped and return to global actions on the home page', async () => {
  const f = fixture(); await f.api.load();
  const button = text => f.root.all.find(node => node.tagName === 'button' && node.textContent === text);
  f.api.setProject('p');
  assert.equal(button('＋ 添加 Agent').hidden, true);
  assert.equal(button('＋ 添加项目文件夹').hidden, true);
  assert.equal(f.node('ab-project').parentElement.hidden, true);
  assert.equal(f.node('ab-agents').hidden, true);
  assert.ok(button('刷新项目'));
  f.api.setProject('');
  assert.equal(button('＋ 添加 Agent').hidden, false);
  assert.equal(button('＋ 添加项目文件夹').hidden, false);
  assert.equal(f.node('ab-project').parentElement.hidden, false);
  assert.ok(f.root.all.some(node => node.tagName === 'a' && node.textContent === '事项管理' && node.href === '#tasks'));
});

test('project task creation captures its owner even when navigation changes during a draft', async () => {
  const f = fixture({ projects: [{ id: 'p', name: 'Project' }, { id: 'q', name: 'Other' }] });
  await f.api.load(); f.api.setProject('p');
  await f.root.all.find(node => node.tagName === 'button' && node.textContent === '＋ 新建待办').listeners.click();
  const owner = f.node('ab-new-project');
  assert.equal(owner.disabled, true); assert.deepEqual(owner.children.map(node => node.value), ['p']);
  f.node('ab-new-title').value = 'Keep the original project';
  f.api.setProject('q'); owner.value = 'q';
  await owner.parentElement.parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.at(-1).body.project_id, 'p');
});

test('legacy management and session routes remain visible outside the Agent board', () => {
  const root = new Element('section'); const flags = {};
  const context = { window: {}, document: { getElementById: name => name === 'agent-board' ? root : null, body: { classList: { toggle: (name, value) => { flags[name] = value; } } } } };
  vm.runInNewContext(source, context);
  for (const view of ['tasks', 'attention', 'legacy-project-sessions']) {
    context.window.WorkspaceAgentBoard.onView(view); assert.equal(root.hidden, true); assert.equal(flags['agent-board-active'], false);
  }
  context.window.WorkspaceAgentBoard.onView('overview'); assert.equal(root.hidden, false); assert.equal(flags['agent-board-active'], true);
});

test('a completed run does not show a current heartbeat failure', async () => {
  const row = item('completed', { state: 'done' });
  const f = fixture({ rows: [row], detail: { item: row, run: { status: 'completed', heartbeat_at: '2026-10-18T00:00:00Z' }, events: [] } });
  await f.api.load(); await f.api.open(row);
  assert.match(f.node('ab-detail').textContent, /已通过审核/);
  assert.doesNotMatch(f.node('ab-detail').textContent, /连接情况未知/);
});

test('reported zero is visible, unknown progress stays unknown, and stale heartbeat is honest', async () => {
  const row = item('progress', { assigned_agent_id: 'a', assignment_state: 'running' });
  const f = fixture({ rows: [row], detail: { item: row, run: { status: 'running', heartbeat_at: '2026-10-18T00:00:00Z' }, events: [{ kind: 'progress', payload: { percent: 0, stage: 'starting' } }] } });
  await f.api.load(); await f.api.open(row);
  assert.match(f.node('ab-detail').textContent, /进度：0%/); assert.equal(f.node('ab-detail').querySelector('progress').value, 0);
  assert.match(f.node('ab-detail').textContent, /连接情况未知.*运行和任务占用保留/);
  f.state.detail.events = []; await f.api.open(row);
  assert.match(f.node('ab-detail').textContent, /进度：未上报/); assert.equal(f.node('ab-detail').querySelector('progress'), null);
  assert.equal(f.posts.length, 0);
});

test('waiting for authorization is actionable even with unknown contact, without approval or reassignment controls', async () => {
  const row = item('waiting', { state: 'blocked', assigned_agent_id: 'a', assignment_state: 'waiting_user', connection_state: 'unknown' });
  const f = fixture({ rows: [row], detail: { item: row, run: { id: 'run', status: 'running' }, connection_state: 'unknown', last_contact_at: stamp,
    events: [{ run_id: 'run', kind: 'progress', created_at: stamp, payload: { stage: 'waiting_user', wait_kind: 'tool_authorization', message: '<b>Confirm external tool</b>', request_id: 'request-1', agent_hint: 'Original Agent window' } }] } });
  await f.api.load(); f.api.setProject('p', true);
  assert.equal(f.groupItems([row]).waiting_user.length, 1); assert.match(f.node('ab-groups').textContent, /等你处理 · 1/);
  await f.api.open(row); assert.match(f.node('ab-waiting-user').textContent, /等待工具授权.*<b>Confirm external tool<\/b>.*request-1.*Original Agent window/);
  assert.match(f.node('ab-waiting-user').textContent, /请回到对应的外部 Agent/);
  assert.equal(f.node('ab-assignee').disabled, true); assert.equal(f.root.querySelector('#ab-review-submit'), null);
  assert.equal(f.node('ab-waiting-user').querySelectorAll('button').length, 0); assert.equal(f.posts.length, 0);
  assert.equal(f.groupItems([{ ...row, state: 'done' }]).done.length, 1);
});

test('manual release requires a reason and confirmation, sends one CAS request, and never uses Agent approval', async () => {
  const row = item('release', { state: 'doing', assigned_agent_id: 'a' });
  const f = fixture({ rows: [row], detail: { item: row, run: { id: 'run', status: 'running' }, events: [] } });
  await f.api.load(); await f.api.open(row);
  const submit = () => f.node('ab-release-submit').parentElement.parentElement.listeners.submit({ preventDefault() {} });
  await submit(); f.node('ab-release-reason').value = 'Stopped the external tool myself'; await submit(); assert.equal(f.posts.length, 0);
  f.node('ab-release-confirm').checked = true; await submit(); assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].url, '/api/workspace/agent-tasks/release/cancel');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].body)), { expected_revision: 7, reason: 'Stopped the external tool myself' });
  assert.equal(f.node('ab-detail').open, false);
});

test('conflicting manual release retains its explanation and never retries automatically', async () => {
  const row = item('release', { state: 'doing' }), f = fixture({ rows: [row], detail: { item: row, run: { status: 'running' }, events: [] } });
  await f.api.load(); await f.api.open(row); f.state.failPost = true; f.state.postError = Object.assign(new Error('HTTP 409'), { reason: 'item_changed' });
  f.node('ab-release-reason').value = 'Keep this explanation'; f.node('ab-release-confirm').checked = true;
  await f.node('ab-release-submit').parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 1); assert.equal(f.node('ab-release-reason').value, 'Keep this explanation');
  assert.match(f.node('ab-feedback').textContent, /事项已被其他窗口更新.*理由已保留/); assert.equal(f.node('ab-detail').open, true);
});

test('closed runs cannot show an outstanding authorization from old progress or offer release', async () => {
  const row = item('closed', { state: 'done' }), f = fixture({ rows: [row], detail: { item: row, run: { id: 'run', status: 'completed' }, events: [{ kind: 'progress', run_id: 'run', payload: { stage: 'waiting_user', wait_kind: 'tool_authorization' } }] } });
  await f.api.load(); await f.api.open(row);
  assert.equal(f.root.querySelector('#ab-waiting-user'), null); assert.equal(f.root.querySelector('#ab-release-submit'), null);
});

test('managed release rejection explains why the run cannot be released and retains the reason', async () => {
  for (const [reason, message] of [
    ['execution_managed_run', /执行仍在运行或清理尚未核验/],
    ['execution_link_conflict', /执行关联不一致/]
  ]) {
    const row = item('release', { state: 'blocked' });
    const f = fixture({ rows: [row], detail: { item: row, run: { status: 'running' }, events: [] } });
    await f.api.load(); await f.api.open(row);
    f.state.failPost = true; f.state.postError = Object.assign(new Error('HTTP 409'), { reason });
    f.node('ab-release-reason').value = 'Retain the cleanup evidence'; f.node('ab-release-confirm').checked = true;
    await f.node('ab-release-submit').parentElement.parentElement.listeners.submit({ preventDefault() {} });
    assert.match(f.node('ab-feedback').textContent, message);
    assert.equal(f.node('ab-release-reason').value, 'Retain the cleanup evidence');
    assert.equal(f.posts.length, 1); assert.equal(f.node('ab-detail').open, true);
  }
});

test('404 detail never fabricates progress or output, nor permits unsafe reassignment', async () => {
  const row = item('missing', { assigned_agent_id: 'a', assignment_state: 'assigned' });
  const f = fixture({ rows: [row], detail: new Error('404 not found') }); await f.api.load(); await f.api.open(row);
  assert.match(f.node('ab-detail').textContent, /没有可确认的进度或产出/);
  assert.match(f.node('ab-detail').textContent, /进度：未上报/); assert.equal(f.node('ab-assignee').disabled, true); assert.equal(f.posts.length, 0);
});

test('assignment uses enabled registry entries and optimistic revision without starting a run', async () => {
  const row = item('assign'); const f = fixture({ rows: [row], agents: [agent, { id: 'disabled', name: 'Disabled', enabled: false }] });
  await f.api.load(); await f.api.open(row);
  assert.deepEqual(f.node('ab-assignee').children.map(node => node.value), ['', 'a']);
  f.node('ab-assignee').value = 'a'; await f.node('ab-assign').listeners.click();
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].url, '/api/workspace/items/update');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].body)), { id: 'assign', expected_revision: 7, changes: { assigned_agent_id: 'a' } });
  assert.match(f.node('ab-detail').textContent, /进度：未上报/);
});

const scopeAgents = [agent, { ...agent, id: 'empty', name: 'Unrestricted', project_ids: [] },
  { ...agent, id: 'only-p', name: 'Only P', project_ids: ['p'] }, { ...agent, id: 'only-q', name: 'Only Q', project_ids: ['q'] },
  { ...agent, id: 'off', enabled: false, project_ids: ['p'] }];
for (const projectId of ['p', 'q', null]) test(`F7 assignment options respect project scope ${projectId}`, async () => {
  const row = item('scoped', { project_id: projectId }), f = fixture({ rows: [row], agents: scopeAgents, projects: [{ id: 'p', name: 'P' }, { id: 'q', name: 'Q' }] });
  await f.api.load(); await f.api.open(row);
  assert.deepEqual(f.node('ab-assignee').children.map(node => node.value), ['', 'a', 'empty', ...(projectId ? [`only-${projectId}`] : [])]);
  f.node('ab-assignee').value = projectId === 'p' ? 'only-q' : 'only-p';
  await f.node('ab-assign').listeners.click(); assert.equal(f.posts.length, 0);
  assert.match(f.node('ab-feedback').textContent, /无权访问|授权范围/);
  f.node('ab-assignee').value = projectId ? `only-${projectId}` : 'a'; await f.node('ab-assign').listeners.click();
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].body.changes.assigned_agent_id, projectId ? `only-${projectId}` : 'a');
});

test('F7 an old out-of-scope assignment remains explicit until the user clears or replaces it', async () => {
  const row = item('old-scope', { project_id: 'q', assigned_agent_id: 'only-p' });
  const f = fixture({ rows: [row], agents: scopeAgents, projects: [{ id: 'q', name: 'Q' }] });
  await f.api.load(); await f.api.open(row);
  const select = f.node('ab-assignee'), old = select.children.find(node => node.value === 'only-p');
  assert.equal(select.value, 'only-p'); assert.equal(old.disabled, true); assert.match(old.textContent, /无权访问|授权范围/);
  await f.node('ab-assign').listeners.click(); assert.equal(f.posts.length, 0);
  select.value = ''; await f.node('ab-assign').listeners.click();
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].body.changes.assigned_agent_id, null);
});

test('F7 a new todo retains an invalid selection when switching projects and requires an explicit correction', async () => {
  const f = fixture({ agents: scopeAgents, projects: [{ id: 'p', name: 'P' }, { id: 'q', name: 'Q' }] });
  await f.api.load(); f.root.all.find(node => node.textContent === '＋ 新建待办').listeners.click();
  const owner = f.node('ab-new-project'), select = f.node('ab-new-agent');
  const form = f.node('ab-new-title').parentElement.parentElement.parentElement;
  assert.deepEqual(select.children.map(node => node.value), ['', 'a', 'empty']);
  owner.value = 'p'; owner.listeners.change(); assert.deepEqual(select.children.map(node => node.value), ['', 'a', 'empty', 'only-p']);
  select.value = 'only-p'; select.listeners.change(); f.node('ab-new-title').value = 'Scoped draft';
  owner.value = 'q'; owner.listeners.change(); assert.equal(select.value, 'only-p');
  const old = select.children.find(node => node.value === 'only-p'); assert.equal(old.disabled, true); assert.match(old.textContent, /无权访问|授权范围/);
  await form.listeners.submit({ preventDefault() {} }); assert.equal(f.posts.length, 0); assert.match(f.node('ab-feedback').textContent, /无权访问|授权范围/);
  owner.value = 'p'; owner.listeners.change(); assert.equal(select.value, 'only-p');
  assert.equal(select.children.find(node => node.value === 'only-p').disabled, false);
  owner.value = ''; owner.listeners.change(); assert.equal(select.value, 'only-p');
  await form.listeners.submit({ preventDefault() {} }); assert.equal(f.posts.length, 0);
  select.value = ''; select.listeners.change(); await form.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].body.assigned_agent_id, null); assert.equal(f.posts[0].body.project_id, null);
});

test('F7 creating within a selected project only offers its authorized Agents', async () => {
  const f = fixture({ agents: scopeAgents }); await f.api.load(); f.api.setProject('p');
  f.root.all.find(node => node.textContent === '＋ 新建待办').listeners.click();
  assert.deepEqual(f.node('ab-new-agent').children.map(node => node.value), ['', 'a', 'empty', 'only-p']);
  f.node('ab-new-title').value = 'Project task'; f.node('ab-new-agent').value = 'only-q';
  await f.node('ab-new-title').parentElement.parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 0); assert.match(f.node('ab-feedback').textContent, /无权访问|授权范围/);
});

test('review requires a selected verdict, reason and an explicit confirmation', async () => {
  const row = item('review', { state: 'review', assignment_state: 'waiting_review' }); const f = fixture({ rows: [row] });
  await f.api.load(); await f.api.open(row); await f.submit(); assert.equal(f.posts.length, 0);
  f.node('ab-review-verdict').value = 'approved'; f.node('ab-review-reason').value = 'Checked the synthetic changes';
  await f.submit(); assert.equal(f.posts.length, 0);
  f.node('ab-review-confirm').checked = true; f.node('ab-review-evidence').value = 'fixture test passed\nmanual inspection';
  await f.submit(); assert.equal(f.posts.length, 1);
  assert.equal(f.posts[0].url, '/api/workspace/reviews');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].body)), { task_id: 'review', expected_revision: 7, verdict: 'approved', reason: 'Checked the synthetic changes', evidence: ['fixture test passed', 'manual inspection'], confirmed: true });
});

test('conflicting review keeps draft visible and never retries automatically', async () => {
  const row = item('conflict', { state: 'review' }); const f = fixture({ rows: [row] });
  await f.api.load(); await f.api.open(row); f.state.failPost = true;
  f.node('ab-review-verdict').value = 'changes_requested'; f.node('ab-review-reason').value = 'Please fix the behavior'; f.node('ab-review-confirm').checked = true;
  await f.submit();
  assert.equal(f.posts.length, 1); assert.equal(f.node('ab-review-reason').value, 'Please fix the behavior'); assert.equal(f.node('ab-detail').open, true);
  assert.match(f.node('ab-feedback').textContent, /409.*草稿已保留/);
});

test('source failures preserve records with unknown counts instead of fabricated zeros', async () => {
  const f = fixture(); await f.api.load(); f.state.failItems = true; await f.api.load();
  assert.match(f.node('ab-groups').textContent, /Task one/); assert.match(f.node('ab-status').textContent, /读取失败|未读完整/);
  assert.match(f.node('ab-groups').textContent, /未分配 · —/); assert.doesNotMatch(f.node('ab-groups').textContent, /未分配 · 0/);
  f.state.failItems = false; f.state.failRegistry = true; await f.api.load(); await f.api.open(item('one'));
  assert.equal(f.node('ab-assignee').disabled, true); assert.match(f.node('ab-agents').textContent, /不可用/);
});

test('successful review keeps its modal busy until refresh finishes so another form cannot submit during the previous write', async () => {
  const row = item('review', { state: 'review' }), f = fixture({ rows: [row] });
  await f.api.load(); await f.api.open(row);
  f.node('ab-review-verdict').value = 'approved'; f.node('ab-review-reason').value = 'Verified'; f.node('ab-review-confirm').checked = true;
  let resolve; f.state.itemsPromise = new Promise(done => { resolve = done; });
  const pending = f.submit(); await Promise.resolve(); await Promise.resolve();
  assert.equal(f.node('ab-detail').open, true); assert.equal(f.node('ab-review-submit').parentElement.disabled, true);
  resolve({ items: [row], total: 1, list_revision: 9 }); await pending; assert.equal(f.node('ab-detail').open, false);
});

for (const [reason, message] of [['item_changed', '事项已被其他窗口更新'], ['review_already_submitted', 'Agent 已提交审核，请读取最新材料']]) test(`${reason} explains the conflict without losing the review draft`, async () => {
  const row = item('review', { state: 'review' }), f = fixture({ rows: [row] });
  await f.api.load(); await f.api.open(row); f.state.failPost = true; f.state.postError = Object.assign(new Error('HTTP 409'), { reason });
  f.node('ab-review-verdict').value = 'approved'; f.node('ab-review-reason').value = 'Keep my evidence'; f.node('ab-review-confirm').checked = true;
  await f.submit(); assert.match(f.node('ab-feedback').textContent, new RegExp(message));
  assert.equal(f.node('ab-review-reason').value, 'Keep my evidence'); assert.equal(f.posts.length, 1); assert.equal(f.node('ab-detail').open, true);
});

test('an assignment race explains that the Agent claimed the task and keeps the selected Agent', async () => {
  const f = fixture(); await f.api.load(); await f.api.open(item('one')); f.state.failPost = true;
  f.state.postError = Object.assign(new Error('HTTP 409'), { reason: 'agent_run_conflict' }); f.node('ab-assignee').value = 'a';
  await f.node('ab-assign').listeners.click(); assert.match(f.node('ab-feedback').textContent, /Agent 已领取或正在等待审核，不能改派/);
  assert.equal(f.node('ab-assignee').value, 'a'); assert.equal(f.posts.length, 1);
});

test('out-of-range and string percentages remain unknown; reported material is literal text', async () => {
  const row = item('literal', { title: '<img src=x onerror=alert(1)>', state: 'review' });
  const f = fixture({ rows: [row], detail: { item: row, run: { status: 'waiting_review', summary: '<script>unsafe()</script>', evidence: ['<img src=x>'] }, events: [] } });
  await f.api.load();
  for (const percent of [-1, 101, '50', null]) {
    f.state.detail.events = [{ kind: 'progress', payload: { percent } }]; await f.api.open(row);
    assert.match(f.node('ab-detail').textContent, /进度：未上报/); assert.equal(f.node('ab-detail').querySelector('progress'), null);
  }
  assert.match(f.node('ab-detail').textContent, /<script>unsafe\(\)<\/script>/);
  assert.equal(f.root.all.filter(node => ['img', 'script'].includes(node.tagName)).length, 0);
  assert.equal(f.posts.length, 0);
});

for (const flag of ['emptySecondPage', 'changedSecondPage']) test(`incomplete or changed paginated snapshot ${flag} never reports partial totals`, async () => {
  const f = fixture({ rows: Array.from({ length: 101 }, (_, i) => item(String(i))) });
  f.state[flag] = true; await f.api.load();
  assert.match(f.node('ab-status').textContent, /读取失败|未读完整/);
  assert.match(f.node('ab-groups').textContent, /未分配 · —/);
  assert.doesNotMatch(f.node('ab-status').textContent, /已读取 100/);
});

test('new todo uses selected project and optional enabled agent, retaining inputs on creation failure', async () => {
  const f = fixture({ agents: [agent, { id: 'off', name: 'Disabled', enabled: false }] });
  await f.api.load(); f.api.setProject('p');
  await f.root.all.find(node => node.tagName === 'button' && node.textContent === '＋ 新建待办').listeners.click();
  assert.equal(f.node('ab-new-project').value, 'p');
  assert.deepEqual(f.node('ab-new-agent').children.map(node => node.value), ['', 'a']);
  f.node('ab-new-title').value = '  Build a synthetic feature  '; f.node('ab-new-agent').value = 'a';
  const form = f.node('ab-new-title').parentElement.parentElement.parentElement;
  f.state.failPost = true;
  await form.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].url, '/api/workspace/items');
  assert.deepEqual(JSON.parse(JSON.stringify(f.posts[0].body)), { title: 'Build a synthetic feature', project_id: 'p', assigned_agent_id: 'a' });
  assert.equal(f.node('ab-new-title').value, '  Build a synthetic feature  '); assert.equal(f.node('ab-new-agent').value, 'a');
  assert.equal(f.node('ab-detail').open, true); assert.match(f.node('ab-feedback').textContent, /输入已保留/);
  f.state.failPost = false; await form.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 2); assert.equal(f.node('ab-detail').open, false);
});

for (const action of ['close', 'escape']) test(`${action} during detail fetch prevents a late response reopening the dialog`, async () => {
  const row = item('delayed'); const f = fixture({ rows: [row] }); await f.api.load();
  let resolve; f.state.detailPromise = new Promise(done => { resolve = done; });
  const pending = f.api.open(row); assert.equal(f.node('ab-detail').open, true);
  if (action === 'close') await f.node('ab-detail').all.find(node => node.tagName === 'button' && node.textContent === '关闭详情').listeners.click();
  else {
    let prevented = false;
    f.node('ab-detail').listeners.cancel({ preventDefault() { prevented = true; } });
    assert.equal(prevented, false); f.node('ab-detail').close();
  }
  resolve({ item: row, run: { id: 'late', status: 'running' }, events: [] }); await pending;
  assert.equal(f.node('ab-detail').open, false); assert.equal(f.node('ab-detail').querySelector('progress'), null);
});

test('progress from an old run cannot replace the current run report', async () => {
  const row = item('runs', { assigned_agent_id: 'a', assignment_state: 'running' });
  const f = fixture({ rows: [row], detail: { item: row, run: { id: 'current', status: 'running' }, events: [
    { run_id: 'current', kind: 'progress', payload: { percent: 0, stage: 'Current work' } },
    { run_id: 'previous', kind: 'progress', payload: { percent: 100, stage: 'Old finished work' } }
  ] } });
  await f.api.load(); await f.api.open(row);
  assert.equal(f.node('ab-detail').querySelector('progress').value, 0);
  const progress = f.node('ab-detail').all.find(node => node.tagName === 'p' && node.textContent.startsWith('运行状态：'));
  assert.match(progress.textContent, /Current work.*进度：0%/); assert.doesNotMatch(progress.textContent, /Old finished work|100%/);
});

function messageFixture(options={}) {
  const root=new Element('section'),posts=[],dirty=[];let rows=[],current=true,sequence=0,failRead=false;
  const context={window:{confirm:()=>true,crypto:{randomUUID:()=>`client-${++sequence}`}},document:{createElement:tag=>new Element(tag),getElementById:()=>null},URLSearchParams,TextEncoder,console};vm.runInNewContext(source,context);
  const fetchJson=async()=>{if(failRead)throw Error('offline');return {item:{id:'task-a'},items:rows};};
  const postJson=async(url,body)=>{posts.push({url,body});if(url.endsWith('/messages')){const row={id:'msg-'+posts.length,task_id:'task-a',delivery_state:'previewed',...body};rows=[row,...rows];return {message:row};}const id=url.split('/').at(-2),row=rows.find(row=>row.id===id);row.delivery_state=url.endsWith('/sent')?'sent':'cancelled';return {message:row};};
  context.window.WorkspaceTaskMessages.create({root,item:{id:'task-a',title:'Task',assigned_agent_id:'a',project_id:'p'},run:null,agents:[{id:'a',name:'Agent',enabled:true}],fetchJson,postJson,isCurrent:()=>current,onDirty:value=>dirty.push(value),...options});
  const get=id=>root.all.find(row=>row.id===id),click=text=>root.all.find(row=>row.tagName==='button'&&row.textContent===text).listeners.click();
  const fill=text=>{get('tm-body').value=text;get('tm-body').listeners.input();};
  const confirm=()=>{get('tm-confirm').checked=true;get('tm-confirm').listeners.change();};
  const submit=()=>root.all.find(row=>row.tagName==='form').listeners.submit({preventDefault(){}});
  return {root,posts,dirty,get,click,fill,confirm,submit,rows:()=>rows,close:()=>{current=false;},failReads:()=>{failRead=true;}};
}
test('manual supplements require separate save and sent confirmations and preserve client identity',async()=>{
  const f=messageFixture();await new Promise(setImmediate);f.fill('补充要求');f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,0);
  f.confirm();f.submit();f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,1);assert.equal(f.posts[0].body.client_token,'client-1');assert.equal(f.posts[0].body.kind,'supplement');assert.equal(f.get('tm-body').value,'');
  f.click('标记这条内容已发送');await new Promise(setImmediate);assert.equal(f.posts.length,1);
  f.get('tm-history').all.find(row=>row.tagName==='input').checked=true;f.click('标记这条内容已发送');await new Promise(setImmediate);assert.equal(f.posts.length,2);assert.match(f.root.textContent,/送达未确认/);
});
test('unknown message write preserves draft and blocks replay until human reread',async()=>{
  let writes=0;const f=messageFixture({postJson:async()=>{writes++;throw Error('timeout');}});await new Promise(setImmediate);f.fill('保留内容');f.confirm();f.submit();await new Promise(setImmediate);f.submit();await new Promise(setImmediate);assert.equal(writes,1);assert.equal(f.get('tm-body').value,'保留内容');assert.match(f.root.textContent,/结果未知/);
  f.click('重新核对消息记录');await new Promise(setImmediate);f.submit();await new Promise(setImmediate);assert.equal(writes,1);f.confirm();f.submit();await new Promise(setImmediate);assert.equal(writes,2);
});
test('message byte budget and confirmation invalidation prevent accidental writes',async()=>{
  const f=messageFixture();await new Promise(setImmediate);f.fill('短内容');f.confirm();f.fill('改过的内容');assert.equal(f.get('tm-confirm').checked,false);f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,0);
  f.fill('汉'.repeat(9000));f.confirm();f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,0);assert.match(f.root.textContent,/字节限制/);
});
test('message success followed by read failure cannot silently replay a confirmed write',async()=>{
  const f=messageFixture();await new Promise(setImmediate);f.fill('新内容');f.confirm();f.failReads();f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,1);f.submit();await new Promise(setImmediate);assert.equal(f.posts.length,1);assert.match(f.root.textContent,/操作已确认/);
});

test('manual message cancellation records cancellation without changing task execution',async()=>{
  const f=messageFixture();await new Promise(setImmediate);f.fill('取消前的说明');f.confirm();f.submit();await new Promise(setImmediate);f.click('取消这条未发送记录');await new Promise(setImmediate);assert.equal(f.rows()[0].delivery_state,'cancelled');assert.match(f.root.textContent,/已取消记录/);assert.equal(f.posts.length,2);
});
test('return message uses only the latest review and never revives an older rejection',async()=>{
  const f=messageFixture({reviews:[{verdict:'approved',reason:'已通过'},{verdict:'changes_requested',reason:'旧意见'}]});await new Promise(setImmediate);f.get('tm-kind').value='return';f.get('tm-kind').listeners.change();assert.equal(f.get('tm-body').value,'');assert.equal(f.posts.length,0);
  const g=messageFixture({reviews:[{verdict:'changes_requested',reason:'补充测试'}]});await new Promise(setImmediate);g.get('tm-kind').value='return';g.get('tm-kind').listeners.change();assert.equal(g.get('tm-body').value,'补充测试');assert.equal(g.posts.length,0);
});

for (const adapter of ['dsh', 'codex']) test(`new ${adapter} todo explicitly starts a real execution request`, async () => {
  const row = item('new', { assigned_agent_id: 'a' });
  const f = fixture({ agents: [{ ...agent, adapter }], read: async url => {
    if (url.endsWith('/brief')) return { brief: { revision: 1, request_text: 'Read input.txt', execution_directory_id: 'main' } };
    if (url.endsWith('/execution-context')) return { context_hash: 'b'.repeat(64) };
  }, post: async url => {
    if (url.endsWith('/items')) return { item: row };
    if (url.endsWith('/dispatch-preview')) return { brief_sha256: 'c'.repeat(64) };
    if (url.endsWith('/dispatch')) return { dispatch: { id: 'new-dispatch' } };
  } });
  await f.api.load(); f.api.setProject('p');
  f.root.all.find(node => node.textContent === '＋ 新建待办').listeners.click();
  f.node('ab-new-title').value = 'Read input.txt'; f.node('ab-new-agent').value = 'a';
  f.node('ab-new-agent').listeners.change();
  assert.equal(f.node('ab-new-execute').disabled, false);
  await f.node('ab-new-title').parentElement.parentElement.parentElement.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 1);
  assert.equal(f.posts.find(row => row.url.endsWith('/dispatch')).body.delivery_mode, 'auto');
  assert.equal(f.node('ab-detail').open, false);
});

test('saved rejection followed by execution failure cannot resubmit the review', async () => {
  const row = item('revision', { state: 'review', assigned_agent_id: 'a' });
  const saved = { ...row, state: 'todo', revision: 8 };
  const f = fixture({ rows: [row], agents: [{ ...agent, adapter: 'codex' }], read: async url => {
    if (url.endsWith('/brief')) throw new Error('Execution context unavailable');
  }, post: async url => url.endsWith('/reviews') ? { item: saved } : undefined });
  await f.api.load(); await f.api.open(row);
  f.node('ab-review-verdict').value = 'rejected'; f.node('ab-review-reason').value = '补充工具结果'; f.node('ab-review-confirm').checked = true;
  await f.submit();
  assert.equal(f.posts.filter(row => row.url.endsWith('/reviews')).length, 1);
  assert.match(f.node('ab-feedback').textContent, /驳回理由已保存/);
});

test('a real pending execution opens its task once and embeds the existing approval UI', async () => {
  let pendingId = 'approval-one'; const panels = [];
  const row = item('approval-task', { state: 'doing', assignment_state: 'waiting_user', assigned_agent_id: 'a' });
  const f = fixture({ rows: [row], execution: { createTaskApprovals: options => { panels.push(options); return { dispose() {} }; } },
    detail: { item: row, run: { id: 'agent-run', dispatch_id: 'dispatch-one', task_id: row.id, status: 'running' }, events: [] },
    read: async url => {
      if (url === '/api/workspace/execution') return { active: [{ id: 'exec-one', task_id: row.id, state: 'waiting_approval' }] };
      if (url.includes('/execution/runs/')) return { run: { id: 'exec-one', task_id: row.id }, approvals: [{ id: pendingId, status: 'pending' }] };
    } });
  await f.api.load(); assert.equal(f.node('ab-detail').open, true);
  assert.equal(panels[0].taskId, row.id); assert.equal(panels[0].dispatchId, 'dispatch-one');
  f.node('ab-detail').close(); await f.api.load(); assert.equal(f.node('ab-detail').open, false);
  pendingId = 'approval-two'; await f.api.load(); assert.equal(f.node('ab-detail').open, true);
  assert.equal(f.posts.length, 0);
});

test('a late approval notification cannot open a task outside the newly selected project', async () => {
  let resolve;
  const row = item('old-project-task', { state: 'doing', assignment_state: 'waiting_user' });
  const f = fixture({ rows: [row], execution: { createTaskApprovals() {} }, read: async url => {
    if (url === '/api/workspace/execution') return { active: [{ id: 'exec-one', task_id: row.id, state: 'waiting_approval' }] };
    if (url.includes('/execution/runs/')) return new Promise(done => { resolve = done; });
  } });
  const loading = f.api.load(); await new Promise(setImmediate); f.api.setProject('__global__');
  resolve({ run: { id: 'exec-one', task_id: row.id }, approvals: [{ id: 'approval-one', status: 'pending' }] });
  await loading; assert.equal(f.node('ab-detail').open, false); assert.equal(f.posts.length, 0);
});

for (const adapter of ['codex', 'dsh']) test(`${adapter} auto execution without a project cannot silently save an unstarted todo`, async () => {
  const f = fixture({ agents: [{ ...agent, adapter }] }); await f.api.load();
  f.root.all.find(node => node.textContent === '＋ 新建待办').listeners.click();
  f.node('ab-new-title').value = 'Reply received'; f.node('ab-new-agent').value = 'a'; f.node('ab-new-agent').listeners.change();
  const form = f.node('ab-new-title').parentElement.parentElement.parentElement;
  await form.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 0); assert.match(f.node('ab-feedback').textContent, /请选择所属项目/);
  f.node('ab-new-execute').checked = false; await form.listeners.submit({ preventDefault() {} });
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].body.project_id, null);
});

test('an existing global todo chooses a project and executes the same task without recreating it', async () => {
  const row = item('existing-global', { project_id: null, assigned_agent_id: 'a' });
  const f = fixture({ rows: [row], agents: [{ ...agent, adapter: 'dsh' }], read: async url => {
    if (url.endsWith('/brief')) return { brief: { revision: 1, request_text: 'Reply received', execution_directory_id: 'main' } };
    if (url.endsWith('/execution-context')) return { context_hash: 'b'.repeat(64) };
  }, post: async url => {
    if (url.endsWith('/dispatch-preview')) return { brief_sha256: 'c'.repeat(64) };
    if (url.endsWith('/dispatch')) return { status: 'existing', dispatch: { id: 'saved-dispatch' } };
  } });
  await f.api.load(); await f.api.open(row);
  await f.node('ab-start-execution').listeners.click(); assert.equal(f.posts.length, 0);
  f.node('ab-start-project').value = 'p'; await f.node('ab-start-execution').listeners.click();
  assert.equal(f.posts[0].url, '/api/workspace/items/update'); assert.equal(f.posts[0].body.id, row.id);
  assert.equal(f.posts[0].body.expected_revision, 7); assert.equal(f.posts[0].body.changes.project_id, 'p');
  assert.equal(f.posts.some(row => row.url === '/api/workspace/items'), false);
  assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 1);
});

test('a busy saved dispatch stays explicit and can be retried once without recreating the task', async () => {
  const row = item('busy-task', { assigned_agent_id: 'a' }); let busy = true;
  const f = fixture({ rows: [row], agents: [{ ...agent, adapter: 'codex' }], read: async url => {
    if (url.endsWith('/brief')) return { brief: { revision: 1, request_text: 'Reply received', execution_directory_id: 'main' } };
    if (url.endsWith('/execution-context')) return { context_hash: 'b'.repeat(64) };
  }, post: async url => {
    if (url.endsWith('/dispatch-preview')) return { brief_sha256: 'c'.repeat(64) };
    if (url.endsWith('/dispatch')) return { status: 'existing', dispatch: { id: 'same-dispatch' } };
    if (url.endsWith('/execute') && busy) throw Object.assign(new Error('busy'), { reason: 'project_busy' });
  } });
  await f.api.load(); await f.api.open(row); await f.node('ab-start-execution').listeners.click();
  assert.match(f.node('ab-feedback').textContent, /同一项目目录/); assert.equal(f.posts.filter(row => row.url.endsWith('/execute')).length, 1);
  busy = false; await f.node('ab-start-execution').listeners.click();
  const executions = f.posts.filter(row => row.url.endsWith('/execute'));
  assert.equal(executions.length, 2); assert.equal(executions[0].url, executions[1].url);
  assert.equal(f.posts.some(row => row.url === '/api/workspace/items'), false);
});

test('a submitted reply waiting for human review is not presented as an unresponsive agent', async () => {
  const row = item('submitted', { state: 'review', assignment_state: 'waiting_review', assigned_agent_id: 'a' });
  const f = fixture({ rows: [row], detail: { item: row, run: { id: 'finished-run', status: 'waiting_review',
    summary: '已收到', heartbeat_at: '2026-01-01T00:00:00Z' }, events: [], connection_state: 'unknown' } });
  await f.api.load(); await f.api.open(row);
  assert.match(f.node('ab-detail').textContent, /已收到/);
  assert.doesNotMatch(f.node('ab-detail').textContent, /最近没有新的运行消息/);
});
