// Actual quota module with explicit DOM dependencies; no browser or account calls.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(repo, 'web/workspace/quota.js'), 'utf8');
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.dataset = {}; this.text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(''); }
  set innerHTML(_value) { throw new Error('HTML sink forbidden'); }
  setAttribute(name, value) { this[name] = value; }
}
function fixture() {
  const window = {}, forbidden = () => { throw new Error('Quota rendering cannot query an account'); };
  vm.runInNewContext(source, { window, fetch: forbidden, document: { getElementById: forbidden } });
  const el = (tag, className = '', text = '') => { const node = new Element(tag); node.className = className; node.textContent = text; return node; };
  const dependencies = { root: new Element(), summary: new Element(), refreshButton: new Element('button'),
    el, dateTime: value => value || '未知', reason: value => value || 'unknown',
    statusNames: { not_checked: '未查询', available: '可用', stale: '旧缓存' } };
  return { window, dependencies, render: quota => window.WorkspaceQuota.render(quota, dependencies) };
}
const quota = overrides => ({ status: 'not_checked', reason: null, error_code: null, buckets: [],
  sampled_at: null, last_success_at: null, ...overrides });
const descendants = node => [node, ...node.children.flatMap(descendants)];

test('not-checked renders without an implicit account query and keeps a single public API', () => {
  const f = fixture(); f.render(quota({}));
  assert.deepEqual(Object.keys(f.window), ['WorkspaceQuota']);
  assert.deepEqual(Object.keys(f.window.WorkspaceQuota), ['render']);
  assert.equal(f.dependencies.summary.dataset.status, 'not_checked');
  assert.match(f.dependencies.summary.textContent, /不会自动启动查询/);
  assert.equal(f.dependencies.refreshButton.textContent, '查询额度');
});
test('stale multi-window values preserve unknowns, finite checks and literal text', () => {
  const f = fixture();
  f.render(quota({ status: 'stale', reason: '<unsafe>', error_code: 0, sampled_at: 'sample', last_success_at: 'success',
    buckets: [{ id: '<img onerror=bad>', primary: { used_percent: 25, window_minutes: 1440, resets_at: null },
      secondary: { used_percent: null, window_minutes: null, resets_at: 'later' } },
    { id: 'other', primary: { used_percent: Infinity, window_minutes: 60, resets_at: null }, secondary: null }] }));
  assert.match(f.dependencies.summary.textContent, /剩余 75%/);
  assert.match(f.dependencies.summary.textContent, /<unsafe>（0）/);
  assert.match(f.dependencies.root.textContent, /不代表当前额度/);
  assert.match(f.dependencies.root.textContent, /<img onerror=bad>/);
  assert.match(f.dependencies.root.textContent, /剩余未知/);
  assert.match(f.dependencies.root.textContent, /最近成功 success/);
  const bars = descendants(f.dependencies.root).filter(node => node.tagName === 'progress');
  assert.equal(bars.length, 1); assert.equal(bars[0].value, 25);
  assert.equal(f.dependencies.refreshButton.textContent, '重新查询');
});
test('zero is a real value and a second render clears all earlier quota data', () => {
  const f = fixture();
  f.render(quota({ status: 'available', buckets: [{ id: 'zero', primary: { used_percent: 0, window_minutes: 60, resets_at: null } }] }));
  assert.match(f.dependencies.summary.textContent, /剩余 100%/);
  assert.equal(descendants(f.dependencies.root).find(node => node.tagName === 'progress').value, 0);
  f.render(quota({}));
  assert.doesNotMatch(f.dependencies.root.textContent, /zero|100%/);
  assert.equal(descendants(f.dependencies.root).filter(node => node.tagName === 'progress').length, 0);
});
test('the real controller injects dependencies and the page loads quota before the controller', () => {
  const f = fixture(), controller = fs.readFileSync(path.join(repo, 'web/workspace/workspace.js'), 'utf8');
  const wrapper = controller.match(/function renderQuota\([^]*?\n\}/)[0];
  const nodes = { 'quota-detail': f.dependencies.root, 'quota-summary': f.dependencies.summary, 'refresh-quota': f.dependencies.refreshButton };
  const context = vm.createContext({ window: f.window, $: id => nodes[id], ...f.dependencies });
  vm.runInContext(wrapper + '\nrenderQuota({ status: "not_checked", buckets: [] });', context);
  assert.equal(nodes['quota-summary'].dataset.status, 'not_checked');
  const html = fs.readFileSync(path.join(repo, 'web/workspace/workspace.html'), 'utf8');
  assert.ok(html.indexOf('src="/quota.js" defer') < html.indexOf('src="/workspace.js" defer'));
  assert.equal((html.match(/src="\/quota.js"/g) || []).length, 1);
});
