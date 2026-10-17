'use strict';
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const assert = require('node:assert/strict');
const { test } = require('node:test');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.text = ''; }
  append(...values) {
    for (const value of values) {
      if (value.parentElement) value.parentElement.children = value.parentElement.children.filter(item => item !== value);
      value.parentElement = this; this.children.push(value);
    }
  }
  replaceChildren(...values) { this.children.forEach(value => { value.parentElement = null; }); this.children = []; this.text = ''; this.append(...values); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(value => value.textContent).join(''); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  get all() { return [this, ...this.children.flatMap(value => value.all)]; }
}
function fixture() {
  const context = vm.createContext({ window: {}, document: { createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag) } });
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../web/workspace/project-space.js'), 'utf8'), context);
  const root = new Element('section'), navigation = new Element('nav'), home = new Element('div'), taskRoot = new Element('div');
  home.append(taskRoot); const contexts = [], routes = [], managed = [], records = [];
  const api = context.window.WorkspaceProjectSpace.create({ root, navigation, taskRoot,
    tasks: { setContext: value => contexts.push(value) }, navigate: value => routes.push(value), manage: value => managed.push(value), record: value => records.push(value) });
  const project = { id: 'p', name: '<img src=x>', note: '真实人工说明', manual_recorded: true, state: '在做', percent: null,
    task_counts: { total: 50, blocked: 2, review: 3, doing: 10 }, directories: [{ path: 'C:/example/project' }], sessions: [], records: [] };
  api.update({ projects: { status: 'available', items: [project] }, sessions: { available: false, items: [] } });
  const click = text => { const button = root.all.find(value => value.tag === 'button' && value.textContent === text); assert.ok(button, text); button.listeners.click(); };
  return { api, root, navigation, home, taskRoot, contexts, routes, project, click };
}
test('overview uses real counts, literal names and does not infer numeric progress', () => {
  const f = fixture(); f.api.show('overview');
  assert.match(f.root.textContent, /5 待处理/); assert.match(f.root.textContent, /50 项任务/);
  assert.doesNotMatch(f.root.textContent, /0%|已交接|自动完成/);
  assert.equal(f.root.all.filter(value => value.tag === 'img').length, 0);
  f.click('<img src=x>'); assert.equal(f.routes.at(-1), 'project/p/tasks');
});
test('project tabs move the same live task node and preserve its draft', () => {
  const f = fixture(); f.taskRoot.draft = 'unsaved'; f.api.route('p', 'tasks'); f.api.show('project');
  assert.equal(f.contexts.at(-1).projectId, 'p'); assert.equal(f.taskRoot.parentElement, f.root);
  f.api.route('p', 'materials'); f.api.show('project'); assert.equal(f.taskRoot.parentElement, f.home);
  assert.ok(f.root.textContent.includes(f.project.directories[0].path));
  f.api.show('attention'); assert.equal(f.contexts.at(-1).scope, 'attention'); assert.equal(f.taskRoot.draft, 'unsaved');
});
test('unavailable sessions and unsupported artifacts have honest empty states', () => {
  const f = fixture(); f.api.route('p', 'sessions'); f.api.show('project'); assert.match(f.root.textContent, /尚未接入/);
  f.api.route('p', 'records'); f.api.show('project'); assert.match(f.root.textContent, /产物文件尚未接入/);
  f.api.route('missing', 'overview'); f.api.show('project'); assert.match(f.root.textContent, /项目已不存在/);
});

test('pending items stay scoped to the selected project and reuse the live editor', () => {
  const f = fixture(); f.taskRoot.draft = 'keep';
  f.api.route('p', 'attention'); f.api.show('project');
  assert.equal(f.contexts.at(-1).projectId, 'p');
  assert.equal(f.contexts.at(-1).scope, 'attention');
  assert.equal(f.taskRoot.parentElement, f.root);
  assert.equal(f.taskRoot.draft, 'keep');
  const pending = f.navigation.all.find(value => value.textContent === '待我处理');
  assert.equal(pending.attributes['aria-current'], 'page');
  f.api.route('p', 'tasks'); f.api.show('project');
  assert.equal(f.contexts.at(-1).scope, 'all');
  assert.equal(f.contexts.at(-1).projectId, 'p');
});
