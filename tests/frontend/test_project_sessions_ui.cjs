'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.text = ''; this.value = ''; this.className = ''; this.classList = { toggle() {} }; }
  append(...rows) { for (const row of rows) { if (row.parentElement) row.parentElement.children = row.parentElement.children.filter(value => value !== row); row.parentElement = this; this.children.push(row); } }
  replaceChildren(...rows) { for (const row of this.children) row.parentElement = null; this.children = []; this.text = ''; this.append(...rows); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(row => row.textContent).join(''); }
  get lastElementChild() { return this.children.at(-1); }
  get all() { return [this, ...this.children.flatMap(row => row.all)]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { (this.listeners[name] ??= []).push(fn); }
  querySelectorAll(selector) { return this.all.filter(row => row !== this && row.tag === selector); }
  contains(node) { return this.all.includes(node); }
  show() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  async fire(name) { for (const fn of this.listeners[name] || []) await fn({ preventDefault() {} }); }
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const session = (n, extra = {}) => ({ thread_id: `thread-${n}`, session_key: `codex:${n}`, title: `真实标题 ${n}`, cwd: 'C:/example/shared', project_id: 'cx-a', assignment_state: 'assigned', binding_available: true, updated_at: '2026-10-18T01:00:00Z', archived: false, ...extra });
const result = (items, extra = {}) => ({ status: 'available', source_id: 'source', sampled_at: 'first', revision: 'same', complete: true, items, total: 35, offset: 0, limit: 20, ...extra });
function fixture() {
  const document = { createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag) }, context = vm.createContext({ document, window: {}, URLSearchParams, TextEncoder });
  for (const file of ['task-center.js', 'project-space.js']) vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../web/workspace', file), 'utf8'), context);
  const root = new Element('section'), navigation = new Element('nav'), home = new Element('div'), taskRoot = new Element('div'), reads = [], itemReads = [], writes = [], routes = [];
  home.append(taskRoot);
  const tasks = context.window.WorkspaceTaskCenter.create({ root: taskRoot, canWrite: () => true,
    fetchJson: url => new Promise(resolve => itemReads.push({ url, resolve })), postJson: async (_url, body) => { writes.push(body); return { item: { id: '00000000-0000-4000-8000-000000000001', revision: 1 } }; } });
  const project = { id: 'cx-a', name: '真实项目', read_only: true, binding_available: true, source: { kind: 'codex' } };
  const snapshot = { projects: { status: 'available', items: [project, { id: 'cx-b', name: '同目录另一项目', read_only: true, binding_available: true }] }, codex_desktop: { status: 'available', source_id: 'source', sampled_at: 'first' }, sessions: { available: true, items: Array.from({ length: 12 }, (_, n) => session(n)) }, items_page: { items: [], total: 0, offset: 0, limit: 50 } };
  tasks.update(snapshot);
  const api = context.window.WorkspaceProjectSpace.create({ root, navigation, taskRoot, tasks, navigate: route => routes.push(route), manage: () => { throw Error('read-only settings opened'); }, readSessions: params => new Promise((resolve, reject) => reads.push({ params, resolve, reject })), onSession: (value, id) => tasks.openSession(value, id) });
  api.update(snapshot); api.route('cx-a', 'sessions'); api.show('project');
  const find = (text, tag = 'button') => { const row = root.all.find(row => row.tag === tag && row.textContent === text); assert.ok(row, text); return row; };
  const get = id => { const row = taskRoot.all.find(row => row.id === id); assert.ok(row, id); return row; };
  return { api, root, taskRoot, tasks, reads, itemReads, writes, routes, snapshot, project, find, get };
}
test('real project sessions paginate beyond legacy 12 and fill an explicit unsaved editor', async () => {
  const f = fixture(); assert.deepEqual(JSON.parse(JSON.stringify(f.reads[0].params)), { project_id: 'cx-a', scope: 'active', offset: 0, limit: 20 });
  f.reads[0].resolve(result(Array.from({ length: 20 }, (_, n) => session(n)))); await tick();
  assert.match(f.root.textContent, /真实标题 19/); assert.doesNotMatch(f.root.textContent, /当前样本/);
  void f.find('下一页').fire('click'); assert.equal(f.reads[1].params.offset, 20);
  f.reads[1].resolve(result([session(25)], { offset: 20, sampled_at: 'later' })); await tick();
  assert.equal(f.reads.length, 2); assert.match(f.root.textContent, /真实标题 25/);
  void f.find('关联为新事项').fire('click');
  assert.equal(f.get('item-editor').open, true); assert.equal(f.get('item-title').value, '真实标题 25');
  assert.equal(f.get('item-project_id').value, 'cx-a'); assert.equal(f.get('item-provider_id').value, 'codex'); assert.equal(f.get('item-session_key').value, 'codex:25'); assert.equal(f.writes.length, 0);
  f.tasks.update({ ...f.snapshot, sessions: { available: true, items: [] } }); assert.equal(f.get('item-session_key').value, 'codex:25');
  const saving = f.get('item-form').fire('submit'); await tick(); assert.equal(f.writes[0].session_key, 'codex:25');
  f.itemReads.at(-1).resolve({ items: [], total: 0, offset: 0, limit: 50 }); await saving;
});
test('session editor cancellation makes no write and existing detailed or quick draft cannot be replaced', async () => {
  const f = fixture(); assert.equal(f.tasks.openSession(session(30), 'cx-a'), true);
  await f.get('item-editor').querySelectorAll('button').find(row => row.textContent === '取消').fire('click'); assert.equal(f.get('item-editor').open, false); assert.equal(f.writes.length, 0);
  f.get('item-quick-title').value = 'quick draft'; assert.equal(f.tasks.openSession(session(31), 'cx-a'), false); assert.equal(f.get('item-quick-title').value, 'quick draft');
  assert.match(f.get('item-quick-feedback').textContent, /先保存或取消/);
  f.get('item-quick-title').value = ''; f.tasks.open(); f.get('item-title').value = 'detail draft';
  assert.equal(f.tasks.openSession(session(31), 'cx-a'), false); assert.equal(f.get('item-title').value, 'detail draft'); assert.equal(f.writes.length, 0);
});
test('switching project and archive filter ignores late requests', async () => {
  const f = fixture(); f.api.route('cx-b', 'sessions'); f.api.show('project');
  f.reads[1].resolve(result([session(40, { project_id: 'cx-b' })])); await tick(); f.reads[0].resolve(result([session(0)])); await tick();
  assert.match(f.root.textContent, /真实标题 40/); assert.doesNotMatch(f.root.textContent, /真实标题 0/);
  void f.find('已归档').fire('click'); assert.equal(f.reads[2].params.scope, 'archived');
  void f.find('未归档').fire('click'); f.reads[3].resolve(result([session(50)])); await tick(); f.reads[2].resolve(result([session(60, { archived: true })])); await tick();
  assert.match(f.root.textContent, /真实标题 50/); assert.doesNotMatch(f.root.textContent, /真实标题 60/);
});
test('read failures retain content, prevent associations and allow retry; stale and disabled remain honest', async () => {
  const f = fixture(); f.reads[0].resolve(result([session(25)])); await tick();
  void f.find('重新读取会话').fire('click'); f.reads[1].reject(Error('network lost')); await tick();
  assert.match(f.root.textContent, /真实标题 25/); assert.match(f.root.textContent, /保留已读内容/); assert.equal(f.find('关联为新事项').disabled, true);
  void f.find('重新读取会话').fire('click'); f.reads[2].resolve(result([session(25)], { status: 'stale' })); await tick(); assert.equal(f.find('关联为新事项').disabled, true);
  f.tasks.update({ ...f.snapshot, codex_desktop: { status: 'not_configured' } }); assert.equal(f.tasks.openSession(session(25), 'cx-a'), false);
  f.api.update({ ...f.snapshot, codex_desktop: { status: 'not_configured' } }); assert.match(f.root.textContent, /真实标题 25/); assert.equal(f.find('关联为新事项').disabled, true);
});
test('source membership revision changes reset pagination while sampling alone does not', async () => {
  const f = fixture(); f.reads[0].resolve(result([session(1)])); await tick(); void f.find('下一页').fire('click');
  f.reads[1].resolve(result([session(2)], { offset: 20, revision: 'changed' })); await tick(); assert.equal(f.reads[2].params.offset, 0);
  f.reads[2].resolve(result([session(3)], { revision: 'changed' })); await tick(); assert.match(f.root.textContent, /真实标题 3/);
});
test('readonly project hides editing and global projectless and unknown routes are separate', async () => {
  const f = fixture(); assert.match(f.root.textContent, /Codex · 只读/); assert.ok(!f.root.all.some(row => row.tag === 'button' && row.textContent === '项目设置'));
  void f.find('来源设置').fire('click'); assert.equal(f.routes.at(-1), 'settings');
  f.api.routeSessions('projectless'); f.api.show('codex-sessions'); assert.equal(f.reads.at(-1).params.scope, 'projectless'); assert.equal(f.reads.at(-1).params.project_id, undefined);
  f.reads.at(-1).resolve(result([session(70, { project_id: null, assignment_state: 'projectless' })])); await tick(); assert.match(f.root.textContent, /明确未分配项目/);
  f.api.routeSessions('unknown'); f.api.show('codex-sessions'); f.reads.at(-1).resolve(result([session(80, { project_id: null, assignment_state: 'conflict', binding_available: false })])); await tick(); assert.match(f.root.textContent, /归属未知或有冲突/); assert.equal(f.find('关联为新事项').disabled, true);
});
test('moving an existing associated item preserves mismatched selection and asks for explicit reselection', async () => {
  const f = fixture(); f.tasks.openSession(session(30), 'cx-a'); f.get('item-project_id').value = 'cx-b'; await f.get('item-project_id').fire('change');
  assert.equal(f.get('item-session_key').value, 'codex:30'); assert.match(f.get('item-feedback').textContent, /重新选择/); await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0);
});
test('manual project session page can save a projectless association for service directory validation', async () => {
  const f = fixture(), manual = { id: 'manual', name: '手工项目', directories: [{ path: 'C:/example/shared' }] };
  const snapshot = { ...f.snapshot, projects: { status: 'available', items: [...f.snapshot.projects.items, manual] } };
  f.tasks.update(snapshot); f.api.update(snapshot); f.api.route('manual', 'sessions'); f.api.show('project');
  f.reads.at(-1).resolve(result([session(90, { project_id: null, assignment_state: 'projectless', binding_available: true })])); await tick();
  assert.equal(f.find('关联为新事项').disabled, false); void f.find('关联为新事项').fire('click');
  assert.equal(f.get('item-project_id').value, 'manual'); assert.equal(f.get('item-session_key').value, 'codex:90');
  const saving = f.get('item-form').fire('submit'); await tick();
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].project_id, 'manual'); assert.equal(f.writes[0].session_key, 'codex:90');
  f.itemReads.at(-1).resolve({ items: [], total: 0, offset: 0, limit: 50 }); await saving;
});
test('projectless manual draft moved to an external project keeps selection and blocks the mismatch', async () => {
  const f = fixture(), manual = { id: 'manual', name: '手工项目' };
  f.tasks.update({ ...f.snapshot, projects: { items: [...f.snapshot.projects.items, manual] } });
  assert.equal(f.tasks.openSession(session(91, { project_id: null, assignment_state: 'projectless' }), 'manual'), true);
  f.get('item-project_id').value = 'cx-a'; await f.get('item-project_id').fire('change');
  assert.equal(f.get('item-session_key').value, 'codex:91'); assert.match(f.get('item-feedback').textContent, /重新选择/);
  await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0);
});
