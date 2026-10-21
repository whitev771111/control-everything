'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.text = ''; }
  append(...values) {
    for (const value of values) {
      if (value.parentElement) value.parentElement.children = value.parentElement.children.filter(item => item !== value);
      value.parentElement = this; this.children.push(value);
    }
  }
  replaceChildren(...values) { this.children.forEach(value => { value.parentElement = null; }); this.children = []; this.text = ''; this.append(...values); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(value => value.textContent).join(''); }
  setAttribute() {}
  addEventListener(key, fn) { this.listeners[key] = fn; }
  get all() { return [this, ...this.children.flatMap(value => value.all)]; }
}

function fixture(fetchJson, postJson = async () => ({ status: 'ready' })) {
  const context = vm.createContext({ window: { alert() {}, confirm: () => true }, document: { createElement: tag => new Element(tag),
    createElementNS: (_namespace, tag) => new Element(tag) } });
  vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../web/workspace/project-space.js'), 'utf8'), context);
  const root = new Element('section'), navigation = new Element('nav');
  const taskRoot = new Element('div'), home = new Element('div'); home.append(taskRoot);
  const api = context.window.WorkspaceProjectSpace.create({ root, navigation, taskRoot,
    tasks: { setContext() {} }, navigate() {}, fetchJson, postJson });
  const snapshot = { projects: { status: 'available', items: [{ id: 'sample', name: 'Sample',
    task_counts: { total: 0 }, directories: [], sessions: [], records: [] }] },
    sessions: { available: false, items: [] } };
  api.update(snapshot);
  return { root, api, snapshot };
}

test('overview renders an asynchronously loaded ready state', async () => {
  const { root } = fixture(async () => ({ status: 'ready' }));
  await new Promise(resolve => setImmediate(resolve));
  const button = root.all.find(value => value.tag === 'button' && value.textContent === '沙箱已准备');
  assert.ok(button, 'The loaded sandbox state should replace the loading button');
  assert.equal(button.disabled, true);
});

test('overview enables preparation after loading required state', async () => {
  const { root } = fixture(async () => ({ status: 'required' }));
  await new Promise(resolve => setImmediate(resolve));
  const button = root.all.find(value => value.tag === 'button' && value.textContent === '准备沙箱');
  assert.ok(button, 'Required preparation must be actionable without another navigation');
  assert.equal(button.disabled, false);
});

test('a failed status request leaves an enabled retry action', async () => {
  const { root } = fixture(async () => { throw new Error('temporary unavailable'); });
  await new Promise(resolve => setImmediate(resolve));
  const button = root.all.find(value => value.tag === 'button' && value.textContent === '检查沙箱');
  assert.ok(button);
  assert.equal(button.disabled, false, 'A failed request must not leave the initial disabled state');
});

test('successful preparation sends the bound plan and refreshes ready state', async () => {
  const calls = [];
  const plan = { status: 'required', canonical_path: 'C:/example/project', directory_id: 'main', confirmation_hash: 'a'.repeat(64) };
  const { root } = fixture(async () => plan, async (endpoint, body) => {
    calls.push({ endpoint, ...body }); return { status: 'ready' };
  });
  await new Promise(resolve => setImmediate(resolve));
  await root.all.find(value => value.tag === 'button' && value.textContent === '准备沙箱').listeners.click();
  assert.equal(calls.length, 1);
  assert.equal(calls[0].confirmation_hash, plan.confirmation_hash);
  assert.equal(calls[0].directory_id, 'main');
  assert.equal(calls[0].path, plan.canonical_path);
  assert.ok(root.all.some(value => value.tag === 'button' && value.textContent === '沙箱已准备' && value.disabled));
});

test('an old response cannot overwrite the state for a changed project directory', async () => {
  let completeOld;
  let calls = 0;
  const { root, api, snapshot } = fixture(() => ++calls === 1
    ? new Promise(resolve => { completeOld = resolve; }) : Promise.resolve({ status: 'required' }));
  await new Promise(resolve => setImmediate(resolve));
  snapshot.projects.items[0].directories = [{ path: 'C:/example/new-project' }];
  api.update(snapshot);
  await new Promise(resolve => setImmediate(resolve));
  completeOld({ status: 'ready' });
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(root.all.some(value => value.tag === 'button' && value.textContent === '准备沙箱' && !value.disabled));
  assert.ok(!root.all.some(value => value.textContent === '沙箱已准备'));
});
