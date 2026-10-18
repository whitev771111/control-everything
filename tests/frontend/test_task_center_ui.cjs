// Actual ordinary-script module with synthetic DOM and controlled API promises only.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'web/workspace/task-center.js'), 'utf8');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {}; this.text = ''; this._value = ''; this.disabled = false; this.open = false; this.classList = { toggle() {} }; }
  append(...rows) { this.children.push(...rows); }
  replaceChildren(...rows) { this.children = rows; this.text = ''; }
  addEventListener(name, callback) { (this.listeners[name] ??= []).push(callback); }
  setAttribute(name, value) { this.attributes[name] = value; }
  get textContent() { return this.text + this.children.map(row => row.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get value() { return this._value; }
  set value(value) { this._value = this.tag === 'textarea' ? String(value).replace(/\r\n?/g, '\n') : String(value); }
  get lastElementChild() { return this.children.at(-1); }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.tag === selector ? [child] : []), ...child.querySelectorAll(selector)]); }
  show() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  async fire(name) { for (const callback of this.listeners[name] ?? []) await callback({ preventDefault() {} }); }
}
const item = (fields = {}) => ({ id: '00000000-0000-4000-8000-000000000001', origin: 'task', origin_id: 'same', title: '事项', project_id: null, provider_id: null, state: 'todo', session_key: null, source: null, created_at: null, updated_at: null, todo_position: null, sort_index: 0, resume_state: 'todo', revision: 1, session_link_state: 'none', ...fields });
const page = (items = [item()], fields = {}) => ({ items, total: items.length, offset: 0, limit: 50, list_revision: 1, ...fields });
function fixture(seed = page(), config = {}) {
  const host = new Element('div'), reads = [], writes = [];
  const context = vm.createContext({ document: { createElement: tag => new Element(tag) }, window: {}, URLSearchParams, TextEncoder, console });
  vm.runInContext(source, context);
  const api = context.window.WorkspaceTaskCenter.create({ root: host,
    fetchJson: url => new Promise((resolve, reject) => reads.push({ url, resolve, reject })),
    postJson: (url, payload) => new Promise((resolve, reject) => writes.push({ url, payload: JSON.parse(JSON.stringify(payload)), resolve, reject })),
    canWrite: () => config.writable !== false, onSaved: config.onSaved });
  const snapshot = { projects: { items: config.projects ?? [] }, sessions: { available: !!config.sessions, items: config.sessions ?? [] }, items_page: seed };
  api.update(snapshot);
  const all = () => [host, ...descend(host)];
  function descend(node) { return node.children.flatMap(child => [child, ...descend(child)]); }
  const get = id => { const found = all().find(row => row.id === id); assert.ok(found, id); return found; };
  const button = text => { const found = all().find(row => row.tag === 'button' && row.textContent === text); assert.ok(found, text); return found; };
  return { api, host, get, button, reads, writes, snapshot };
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const error = (status, reason) => Object.assign(new Error(reason), { status, reason });
async function settleWrite(f, pending, result = { item: item() }) {
  f.writes.at(-1).resolve(result); await tick(); f.reads.at(-1).resolve(page()); await pending;
}
async function rejectWrite(f, pending, failure) {
  f.writes.at(-1).reject(failure); await tick(); f.reads.at(-1).resolve(page()); await pending;
}
test('HTML keeps original scripts and loads the task module before workspace', () => {
  const html = fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8');
  const order = ['/execution.js', '/observation.js', '/task-center.js', '/workspace.js'].map(url => html.indexOf(`src="${url}"`));
  assert.ok(order.every((value, index) => value >= 0 && (!index || value > order[index - 1])));
  for (const id of ['task-center-root', 'legacy-tasks', 'manual-form', 'task-form', 'todos-list']) assert.ok(html.includes(`id="${id}"`));
  assert.equal((html.match(/id="manual-form"/g) ?? []).length, 1);
});
test('empty projects: quick creation is title-only and succeeds without a project', async () => {
  const f = fixture(page([])); f.get('item-quick-title').value = ' 全局下一步 ';
  const pending = f.get('item-quick-form').fire('submit');
  assert.deepEqual(f.writes[0].payload, { title: '全局下一步' }); assert.equal(f.writes[0].url, '/api/workspace/items');
  await settleWrite(f, pending); assert.equal(f.get('item-quick-title').value, '');
});
test('literal titles, empty raw IDs and same raw ID from separate origins stay distinct', () => {
  const f = fixture(page([item({ title: '<img onerror=attack()>', origin_id: '' }), item({ id: '00000000-0000-4000-8000-000000000002', origin: 'todo' }), item({ id: '00000000-0000-4000-8000-000000000003' })]));
  assert.match(f.get('items-list').textContent, /<img onerror=attack\(\)>/);
  assert.doesNotMatch(f.get('items-list').textContent, /空原ID/);
  f.api.open(item({ origin_id: '' })); assert.match(f.get('item-identity').textContent, /空原ID/);
  assert.equal(f.get('items-list').children.length, 3); assert.equal(f.host.querySelectorAll('img').length, 0);
});
for (const state of ['todo', 'doing', 'blocked', 'review', 'done']) test(`state ${state} updates by internal UUID, never raw origin ID`, async () => {
  const f = fixture(); f.api.open(item({ origin: 'todo', origin_id: '', state: state === 'todo' ? 'doing' : 'todo' }));
  f.get('item-state').value = state; const pending = f.get('item-form').fire('submit');
  assert.deepEqual(f.writes[0].payload, { id: item().id, expected_revision: 1, changes: { state } });
  assert.equal(f.writes[0].url, '/api/workspace/items/update'); await settleWrite(f, pending);
});
test('unchanged CRLF, whitespace, history NULLs, empty association and wide title remain omitted', async () => {
  const f = fixture(); const original = item({ title: '  旧值\r\n' + '😀'.repeat(2100) + '  ', project_id: '', origin_id: '' });
  f.api.open(original); assert.equal(f.get('item-title').maxLength, 2 * [...original.title].length);
  await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0);
  f.get('item-state').value = 'review'; const pending = f.get('item-form').fire('submit');
  assert.deepEqual(f.writes[0].payload.changes, { state: 'review' }); await settleWrite(f, pending);
});
test('history wider than 2000 can be edited up to original codepoint count and shortened', async () => {
  const f = fixture(); f.api.open(item({ title: '中'.repeat(2100) })); f.get('item-title').value = '😀'.repeat(2100);
  const pending = f.get('item-form').fire('submit'); assert.equal([...f.writes[0].payload.changes.title].length, 2100); await settleWrite(f, pending);
  f.api.open(item({ title: '中'.repeat(2100) })); f.get('item-title').value = '较短'; const shorter = f.get('item-form').fire('submit'); await settleWrite(f, shorter);
});
test('2000 astral codepoints are accepted; 2001 are rejected without UTF16 confusion', async () => {
  const f = fixture(); f.get('item-quick-title').value = '😀'.repeat(2000);
  const pending = f.get('item-quick-form').fire('submit'); assert.equal(f.writes[0].payload.title.length, 4000); await settleWrite(f, pending);
  f.get('item-quick-title').value = '😀'.repeat(2001); await f.get('item-quick-form').fire('submit'); assert.equal(f.writes.length, 1); assert.match(f.get('item-quick-feedback').textContent, /2000/);
});
test('changed history whitespace and CRLF use the actual new DOM value without trim', async () => {
  const f = fixture(); f.api.open(item({ title: 'old\r\nvalue' })); f.get('item-title').value = '  new\r\nvalue  ';
  const pending = f.get('item-form').fire('submit'); assert.equal(f.writes[0].payload.changes.title, '  new\nvalue  '); await settleWrite(f, pending);
});
test('JSON worst escaping is measured using real bytes and blocks oversized history', async () => {
  const f = fixture(); f.api.open(item({ title: 'x'.repeat(44000) })); f.get('item-title').value = '\u0001'.repeat(44000);
  await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0); assert.match(f.get('item-feedback').textContent, /256 KiB/);
  f.get('item-title').value = ('"\\\u0001😀').repeat(2000); const pending = f.get('item-form').fire('submit');
  assert.ok(Buffer.byteLength(JSON.stringify(f.writes[0].payload)) <= 262144); await settleWrite(f, pending);
});
test('lone surrogate and blank new title are rejected locally', async () => {
  const f = fixture(); for (const text of ['  \n ', '\ud800', '\udc00']) { f.get('item-quick-title').value = text; await f.get('item-quick-form').fire('submit'); }
  assert.equal(f.writes.length, 0);
});
for (const failure of [error(413, 'body_too_large'), error(409, 'item_changed'), new Error('network lost'), new Error('timeout')]) test(`${failure.message}: detail draft survives, GET reconciles and no POST replay`, async () => {
  const f = fixture(); f.api.open(item()); f.get('item-title').value = '我的草稿'; const pending = f.get('item-form').fire('submit');
  assert.equal(f.get('item-editor').open, true); await rejectWrite(f, pending, failure);
  assert.equal(f.get('item-editor').open, true); assert.equal(f.get('item-title').value, '我的草稿'); assert.equal(f.writes.length, 1);
  assert.match(f.get('item-feedback').textContent, /未自动重试/); assert.equal(f.reads.length, 1);
});
test('quick failure preserves text and disable prevents duplicate submissions while pending', async () => {
  const f = fixture(); f.get('item-quick-title').value = '草稿'; const pending = f.get('item-quick-form').fire('submit');
  await f.get('item-quick-form').fire('submit'); assert.equal(f.writes.length, 1);
  await rejectWrite(f, pending, new Error('lost')); assert.equal(f.get('item-quick-title').value, '草稿');
});
test('unconfirmed POST receipt is unknown and preserves the draft', async () => {
  const f = fixture(); f.get('item-quick-title').value = '草稿'; const pending = f.get('item-quick-form').fire('submit');
  await settleWrite(f, pending, {}); assert.equal(f.get('item-quick-title').value, '草稿'); assert.match(f.get('item-quick-feedback').textContent, /没有返回确认/);
});
for (const projectChange of ['archive', 'unarchive', 'restore-orphan']) test(`project-only ${projectChange}: page 409 resets revision and preserves editor`, async () => {
  const f = fixture(page([item()], { total: 101, list_revision: 8 })); f.api.open(item()); f.get('item-title').value = '分页期间草稿';
  const pending = f.button('下一页').fire('click'); assert.match(f.reads[0].url, /list_revision=8/);
  f.reads[0].reject(error(409, 'list_changed')); await tick();
  assert.equal(new URL(f.reads[1].url, 'http://fixture').searchParams.get('offset'), '0'); assert.ok(!f.reads[1].url.includes('list_revision'));
  f.reads[1].resolve(page([item({ title: projectChange })], { list_revision: 9 })); await pending;
  assert.equal(f.writes.length, 0); assert.equal(f.get('item-title').value, '分页期间草稿'); assert.match(f.get('items-status').textContent, /第一页/);
});
test('server filters carry all scopes/provider/state and filtered total rather than page count', async () => {
  const f = fixture(); f.get('item-scope').value = 'global'; f.get('item-provider-filter').value = 'pi'; f.get('item-state-filter').value = 'blocked';
  const pending = f.get('item-scope').fire('change'); const query = new URL(f.reads[0].url, 'http://fixture').searchParams;
  assert.equal(query.get('scope'), 'global'); assert.equal(query.get('provider_id'), 'pi'); assert.equal(query.get('state'), 'blocked');
  f.reads[0].resolve(page([item()], { total: 501 })); await pending; assert.match(f.get('items-status').textContent, /501 条/);
});
test('late page response cannot replace newer page or updated snapshot; drafts stay untouched', async () => {
  const f = fixture(); f.api.open(item()); f.get('item-title').value = 'draft'; f.get('item-quick-title').value = 'quick draft';
  const older = f.api.refresh(0); const newer = f.api.refresh(0);
  f.reads[1].resolve(page([item({ title: 'NEW' })])); await newer; f.reads[0].resolve(page([item({ title: 'OLD' })])); await older;
  assert.match(f.get('items-list').textContent, /NEW/); assert.equal(f.get('item-title').value, 'draft');
  const stale = f.api.refresh(0); f.api.update({ ...f.snapshot, items_page: page([item({ title: 'SNAPSHOT' })]) });
  f.reads[2].resolve(page([item({ title: 'LATE' })])); await stale; assert.match(f.get('items-list').textContent, /SNAPSHOT/); assert.equal(f.get('item-quick-title').value, 'quick draft');
});
test('read failure is visible, keeps draft, removes old page and recovers', async () => {
  const f = fixture(); f.api.open(item()); f.get('item-title').value = 'draft'; const pending = f.api.refresh(0);
  f.reads[0].reject(new Error('read lost')); await pending; assert.match(f.get('items-status').textContent, /读取失败/); assert.ok(!f.get('items-list').textContent.includes('原任务'));
  const recovered = f.api.refresh(0); f.reads[1].resolve(page()); await recovered; assert.match(f.get('items-list').textContent, /编辑详情/); assert.equal(f.get('item-title').value, 'draft');
});
test('archived/orphan original associations and unavailable sessions are preserved on state-only edit', async () => {
  const f = fixture(page(), { projects: [{ id: 'archived', name: '旧项目', archived: true }] });
  f.api.open(item({ project_id: 'archived', provider_id: 'codex', session_key: 'codex:old' }));
  assert.equal(f.get('item-session_key').value, 'codex:old'); assert.match(f.get('item-project_id').textContent, /保留原项目/);
  f.get('item-state').value = 'done'; const pending = f.get('item-form').fire('submit'); assert.deepEqual(f.writes[0].payload.changes, { state: 'done' }); await settleWrite(f, pending);
});
test('Hermes and Pi are task labels only; explicit project unlink sends null', async () => {
  const f = fixture(); f.api.open(item({ project_id: 'orphan' })); assert.match(f.get('item-provider_id').textContent, /Hermes.*Pi/);
  f.get('item-project_id').value = ''; f.get('item-provider_id').value = 'hermes'; const pending = f.get('item-form').fire('submit');
  assert.deepEqual(f.writes[0].payload.changes, { project_id: null, provider_id: 'hermes' }); await settleWrite(f, pending);
});
test('confirmed save is not reported as unknown if project refresh fails', async () => {
  const f = fixture(page(), { onSaved: async () => { throw new Error('refresh failed'); } }); f.get('item-quick-title').value = 'confirmed';
  const pending = f.get('item-quick-form').fire('submit'); await settleWrite(f, pending); assert.equal(f.get('item-quick-title').value, ''); assert.match(f.get('item-quick-feedback').textContent, /保存已确认/);
});

test('project context defaults new items without reassigning existing global items', async () => {
  const f = fixture(page(), { projects: [{ id: 'p', name: 'Project' }] });
  const loading = f.api.setContext({ projectId: 'p' });
  assert.equal(new URL('http://local' + f.reads.at(-1).url).searchParams.get('project_id'), 'p');
  f.reads.at(-1).resolve(page()); await loading;
  f.api.open(); assert.equal(f.get('item-project_id').value, 'p');
  f.api.open(item()); assert.equal(f.get('item-project_id').value, '');
  f.get('item-quick-title').value = 'project task'; await f.get('item-quick-title').fire('input');
  const saving = f.get('item-quick-form').fire('submit');
  assert.equal(f.writes[0].payload.project_id, 'p'); await settleWrite(f, saving);
});

test('quick and detail drafts retain identity across project and attention navigation', async () => {
  const f = fixture(page(), { projects: [{ id: 'p', name: 'Project' }] });
  let pending = f.api.setContext({ projectId: 'p' }); f.reads.at(-1).resolve(page()); await pending;
  f.get('item-quick-title').value = 'draft'; await f.get('item-quick-title').fire('input');
  f.api.open(item()); f.get('item-title').value = 'detail draft';
  pending = f.api.setContext({ scope: 'attention' });
  assert.equal(new URL('http://local' + f.reads.at(-1).url).searchParams.get('scope'), 'attention');
  f.reads.at(-1).resolve(page()); await pending;
  assert.equal(f.get('item-title').value, 'detail draft');
  const reads = f.reads.length; f.api.setContext({ scope: 'attention' }); assert.equal(f.reads.length, reads);
  const saving = f.get('item-quick-form').fire('submit');
  assert.equal(f.writes[0].payload.project_id, 'p'); await settleWrite(f, saving);
});

for (const entry of ['quick', 'detail']) test(`${entry} permits an unlinked manual item in a selected Codex project while its source is unavailable`, async () => {
  const projectId = 'cx-' + '1'.repeat(24);
  const f = fixture(page([]), { projects: [{ id: projectId, name: 'Codex project', read_only: true, binding_available: false }] });
  f.snapshot.codex_desktop = { status: 'unavailable' }; f.api.update(f.snapshot);
  const loading = f.api.setContext({ projectId }); f.reads.at(-1).resolve(page([])); await loading;
  let saving;
  if (entry === 'quick') {
    f.get('item-quick-title').value = 'Manual next step'; await f.get('item-quick-title').fire('input');
    saving = f.get('item-quick-form').fire('submit');
  } else {
    f.api.open(); f.get('item-title').value = 'Manual next step';
    assert.equal(f.get('item-session_key').value, ''); saving = f.get('item-form').fire('submit');
  }
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].payload.project_id, projectId);
  assert.ok(!f.writes[0].payload.session_key);
  await settleWrite(f, saving, { item: item({ title: 'Manual next step', project_id: projectId }) });
});

test('unavailable Codex source still blocks a new nonempty session association and openSession', async () => {
  const projectId = 'cx-' + '1'.repeat(24), key = 'codex:00000000-0000-4000-8000-000000000009';
  const session = { session_key: key, thread_id: key.slice(6), cwd: 'C:/example/project', assignment_state: 'assigned', project_id: projectId, binding_available: true };
  const f = fixture(page([]), { projects: [{ id: projectId, name: 'Codex', read_only: true, binding_available: false }], sessions: [session] });
  f.snapshot.codex_desktop = { status: 'stale' }; f.api.update(f.snapshot);
  assert.equal(f.api.openSession(session, projectId), false);
  const loading = f.api.setContext({ projectId }); f.reads.at(-1).resolve(page([])); await loading;
  f.api.open(); f.get('item-title').value = 'Linked next step'; f.get('item-provider_id').value = 'codex';
  await f.get('item-provider_id').fire('change'); f.get('item-session_key').value = key;
  await f.get('item-form').fire('submit');
  assert.equal(f.writes.length, 0); assert.match(f.get('item-feedback').textContent, /Codex 来源当前不能建立新关联/);
});
