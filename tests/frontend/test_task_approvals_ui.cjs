// Task approval UI contracts against synthetic execution snapshots only.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.resolve(__dirname, '../../web/workspace/execution.js'), 'utf8');
class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.text = ''; this.disabled = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(n => n.textContent).join(''); }
  setAttribute() {}
  addEventListener(name, fn) { this.listeners[name] = fn; }
  get all() { return [this, ...this.children.flatMap(n => n.all)]; }
  querySelectorAll(tag) { return this.all.filter(n => n.tagName === tag); }
}
const flush = () => new Promise(setImmediate);
function fixture(options = {}) {
  const context = { window: {}, document: { createElement: tag => new Element(tag) }, setTimeout: () => 1, clearTimeout() {} };
  vm.runInNewContext(source, context);
  const root = new Element('section'), posts = [];
  const run = { id: 'exec-a', task_id: 'task-a', request_id: 'dispatch-a', state: 'waiting_approval', cancel_requested: false };
  const approval = { id: 'approval-a', kind: 'file_change', status: 'pending', can_accept: true,
    detail_complete: true, detail: { changes: [{ path: 'C:/example/synthetic/check.md', diff: '+<script>literal</script>' }] } };
  const state = { current: true, owner: true, run, approvals: [approval], settled: 0, reads: 0 };
  const api = context.window.WorkspaceExecution.createTaskApprovals({ root, taskId: 'task-a', dispatchId: 'dispatch-a',
    isCurrent: () => state.current, onSettled: () => { state.settled++; },
    fetchJson: async url => {
      state.reads++;
      if (options.read) { const value = await options.read(url); if (value !== undefined) return value; }
      if (url.includes('?request_id=')) return { run: state.run };
      if (url.includes('/runs/')) return { run: state.run, approvals: state.approvals };
      return { capability: { available: state.owner, is_owner: state.owner } };
    }, postJson: async (url, body) => { posts.push({ url, body }); if (options.post) await options.post(url, body); }
  });
  const button = text => root.all.find(n => n.tagName === 'button' && n.textContent === text);
  return { root, posts, state, api, button, approval };
}
test('bound task shows literal diff and sends only one exact approval while pending', async () => {
  let resolve;
  const f = fixture({ post: () => new Promise(done => { resolve = done; }) }); await flush();
  assert.match(f.root.textContent, /文件变更审批/); assert.match(f.root.textContent, /<script>literal<\/script>/);
  assert.equal(f.root.all.some(n => n.tagName === 'script'), false);
  f.button('仅允许这一次').listeners.click(); f.button('仅允许这一次').listeners.click();
  assert.equal(f.posts.length, 1); assert.equal(f.posts[0].url, '/api/workspace/execution/runs/exec-a/approvals/approval-a');
  assert.equal(f.posts[0].body.decision, 'accept');
  assert.ok(f.root.querySelectorAll('button').every(n => n.disabled));
  f.approval.status = 'queued'; resolve(); await flush();
  assert.equal(f.posts.length, 1); assert.equal(f.button('仅允许这一次'), undefined);
});
test('incomplete approval blocks accept but permits explicit decline; read-only blocks every action', async () => {
  const f = fixture(); f.approval.detail_complete = false; f.approval.can_accept = false; await flush();
  assert.equal(f.button('仅允许这一次').disabled, true); assert.equal(f.button('拒绝这次操作').disabled, false);
  f.button('拒绝这次操作').listeners.click(); await flush(); assert.equal(f.posts[0].body.decision, 'decline');
  f.state.owner = false; await f.api.refresh();
  assert.ok(f.root.querySelectorAll('button').every(n => n.disabled));
});
test('cancel uses the owned run cancel endpoint and terminal approvals cannot act again', async () => {
  const f = fixture(); await flush(); f.button('取消当前运行').listeners.click(); await flush();
  assert.equal(f.posts[0].url, '/api/workspace/execution/runs/exec-a/cancel');
  assert.deepEqual(Object.keys(f.posts[0].body), []);
  f.state.run = { ...f.state.run, state: 'interrupted' }; f.approval.status = 'expired'; await f.api.refresh();
  assert.equal(f.root.querySelectorAll('button').length, 0); assert.equal(f.state.settled, 1);
});
test('mismatched task identity never exposes actionable approvals', async () => {
  const f = fixture(); f.state.run.task_id = 'other-task'; await flush();
  assert.match(f.root.textContent, /不一致/); assert.equal(f.root.querySelectorAll('button').length, 0); assert.equal(f.posts.length, 0);
});
test('late reads after closing do not populate the old task panel', async () => {
  let resolve;
  const f = fixture({ read: url => url.includes('?request_id=') ? new Promise(done => { resolve = done; }) : undefined });
  await flush(); f.state.current = false; resolve({ run: f.state.run }); await flush();
  assert.equal(f.root.querySelectorAll('button').length, 0); assert.equal(f.posts.length, 0);
});
test('a failed decision is not automatically resent and failed refresh disables stale buttons', async () => {
  let failRead = false;
  const f = fixture({ post: () => { failRead = true; throw new Error('reply timeout'); }, read: () => { if (failRead) throw new Error('offline'); } });
  await flush(); f.button('仅允许这一次').listeners.click(); await flush();
  assert.equal(f.posts.length, 1); assert.match(f.root.textContent, /offline/);
  assert.ok(f.root.querySelectorAll('button').every(n => n.disabled));
});
