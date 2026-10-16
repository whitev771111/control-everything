// Pure Node contracts: no browser, server, Agent, or external state.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'web/workspace/workspace.js'), 'utf8');
const executionSource = fs.readFileSync(path.join(root, 'web/workspace/execution.js'), 'utf8');
const results = [];
const error = (status, reason) => Object.assign(new Error(reason), { status, reason });
const tick = () => new Promise(resolve => setImmediate(resolve));
async function settle() { for (let i = 0; i < 8; i++) await tick(); }
function check(label, condition) { results.push({ label, passed: Boolean(condition) }); assert.ok(condition, label); }
function apiHarness() {
  const sandbox = { document: {}, AbortController, setTimeout, clearTimeout, Error, TypeError, SyntaxError, fetch: null };
  vm.createContext(sandbox);
  vm.runInContext(source.slice(0, source.indexOf('\nfunction showView')) + '\nthis.api=api;this.post=post;this.app=app;', sandbox);
  return sandbox;
}
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.dataset = {}; this.listeners = {}; this.textContent = ''; this.value = ''; this.disabled = false; this.hidden = false; this.classList = { toggle() {} }; }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  insertBefore(child, sibling) { if (child.parent) child.remove(); child.parent = this; const index = sibling ? this.children.indexOf(sibling) : -1; this.children.splice(index < 0 ? this.children.length : index, 0, child); }
  remove() { if (this.parent) { const index = this.parent.children.indexOf(this); if (index >= 0) this.parent.children.splice(index, 1); this.parent = null; } }
  replaceWith(child) { const parent = this.parent; if (parent) { const index = parent.children.indexOf(this); parent.children[index] = child; child.parent = parent; this.parent = null; } }
  setAttribute() {}
  addEventListener(event, callback) { this.listeners[event] = callback; }
  querySelectorAll(selector) { return this.children.flatMap(child => [...((selector === 'p' && child.tag === 'p') || (selector === '[data-run-id]' && child.dataset.runId) ? [child] : []), ...child.querySelectorAll(selector)]); }
  focus() {}
  scrollIntoView() {}
  fire(event = 'click') { return this.listeners[event]?.({ preventDefault() {} }); }
}
function uiHarness(options = {}) {
  const nodes = new Map(), records = [], storage = new Map();
  if (options.saved) storage.set('workspace.execution.receipt', JSON.stringify(options.saved));
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const task = { id: '20000000-0000-4000-8000-000000000001', title: '准备提示词', provider_id: 'codex' };
  const ctx = { task, project: { id: 'alpha', name: 'Alpha', cwd: 'E:\\fixture\\alpha' }, context_hash: 'hash-1', policy: { mode: 'workspace-write', approval_policy: 'on-request', approvals_reviewer: 'user', network_access: false, writable_roots: [], inheritance_note: 'MCP may inherit' } };
  const list = { capability: { implemented: true, available: true, is_owner: true }, runs: [], active: [], total: 0 };
  const sandbox = { window: {}, document: { hidden: false, getElementById: get, createElement: tag => new Element(tag), addEventListener() {} }, sessionStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }, crypto: { randomUUID }, TextEncoder, setTimeout: () => 1, clearTimeout() {}, console };
  vm.createContext(sandbox); vm.runInContext(executionSource, sandbox);
  const ui = sandbox.window.WorkspaceExecution;
  const api = async url => {
    records.push({ method: 'GET', url });
    if (url.includes('/context?')) return options.context ? options.context(ctx) : ctx;
    if (url.includes('?request_id=')) return options.lookup ? options.lookup(url) : Promise.reject(error(404, 'request_not_found'));
    if (url.includes('/runs/')) {
      if (options.detail) return options.detail;
      const id = url.split('/runs/')[1].split('?')[0];
      return { run: { id, task_title: '实际记录', state: 'running' }, approvals: [], events: [], next_cursor: 0 };
    }
    return list;
  };
  ui.init({ api, post: async (url, body) => { records.push({ method: 'POST', url, body }); return options.post(url, body); }, getSnapshot: () => ({ tasks: { items: [task] } }), showView: view => ui.onView(view) });
  return { ui, nodes, get, records, task, ctx, list, storage, open: async () => { await ui.openTask(task.id); await settle(); }, submit: () => get('execution-form').fire('submit') };
}
async function main() {
  const api = apiHarness();
  for (const [status, reason] of [[400, 'invalid_request'], [404, 'task_not_found'], [409, 'context_changed'], [409, 'project_busy'], [409, 'capacity_busy'], [409, 'other_instance'], [409, 'idempotency_conflict'], [404, 'request_not_found'], [503, 'storage_unavailable']]) {
    api.fetch = async () => ({ status, ok: false, json: async () => ({ reason }) });
    await assert.rejects(api.api('/stub'), value => value.status === status && value.reason === reason && Boolean(value.message));
  }
  api.fetch = async () => ({ status: 503, ok: false, json: async () => ({ reason: 'storage_unavailable' }) });
  await assert.rejects(api.api('/stub'), value => value.message === '本地记录暂时无法读取或保存');
  check('HTTP fields survive catch; legacy message remains unchanged', true);
  api.fetch = async () => { throw new TypeError('network'); };
  await assert.rejects(api.api('/stub'), value => value.status === null && value.reason === 'network_error');
  api.fetch = async () => { throw Object.assign(new Error('abort'), { name: 'AbortError' }); };
  await assert.rejects(api.api('/stub'), value => value.status === null && value.reason === 'timeout');
  api.fetch = async () => ({ status: 200, ok: true, json: async () => { throw new SyntaxError('json'); } });
  await assert.rejects(api.api('/stub'), value => value.status === 200 && value.reason === 'invalid_response');
  await assert.rejects(api.post('/stub', {}), value => value.reason === 'csrf_unavailable' && value.status === null);
  check('network, timeout, malformed JSON and local CSRF remain distinguishable', true);
  for (const [status, reason] of [[400, 'invalid_request'], [404, 'task_not_found'], [409, 'project_busy'], [409, 'capacity_busy'], [409, 'other_instance'], [null, 'csrf_unavailable']]) {
    const h = uiHarness({ post: async () => { throw error(status, reason); } });
    await h.open(); h.get('execution-prompt').value = '保留草稿'; await h.submit();
    check(`definitive ${status}/${reason} unlocks draft without lookup`, !h.get('execution-prompt').disabled && !h.records.some(item => item.url.includes('?request_id=')) && h.get('execution-prompt').value === '保留草稿');
  }
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const double = uiHarness({ post: async (url, body) => { await pending; return { run: { id: randomUUID(), request_id: body.request_id } }; } });
  await double.open(); const first = double.submit(); double.submit();
  check('first submit freezes body and locks before await; double click sends once', double.records.filter(item => item.method === 'POST').length === 1 && Object.isFrozen(double.records.find(item => item.method === 'POST').body) && double.get('execution-prompt').disabled);
  release(); await first; await settle();
  check('admitted ordinary submit cannot create another request', (await double.submit(), double.records.filter(item => item.method === 'POST').length === 1));
  for (const lookupReason of ['request_not_found', 'storage_unavailable', 'network_error']) {
    const h = uiHarness({ post: async () => { throw error(null, 'network_error'); }, lookup: async () => { throw error(lookupReason === 'request_not_found' ? 404 : lookupReason === 'storage_unavailable' ? 503 : null, lookupReason); } });
    await h.open(); await h.submit();
    check(`lost response + ${lookupReason} holds key and draft`, h.get('execution-prompt').disabled && h.storage.size === 1);
    const original = h.records.find(item => item.method === 'POST').body;
    await h.get('execution-retry').fire();
    if (lookupReason === 'request_not_found') check('404 explicit retry preserves exact frozen body and key', h.records.filter(item => item.method === 'POST').length === 2 && h.records.filter(item => item.method === 'POST')[1].body === original);
    else check('503/network cannot retry POST or unlock new key', h.records.filter(item => item.method === 'POST').length === 1 && h.get('execution-retry').hidden);
  }
  let body;
  const lost = uiHarness({ post: async (url, value) => { body = value; throw error(409, 'idempotency_conflict'); }, lookup: async () => ({ run: { id: randomUUID(), request_id: body.request_id } }) });
  await lost.open(); await lost.submit();
  check('idempotency conflict looks up existing run rather than releasing key', lost.records.some(item => item.url.includes('?request_id=')) && !lost.get('execution-another').hidden);
  for (const fail of [false, true]) {
    let contexts = 0;
    const h = uiHarness({ context: ctx => { contexts++; if (fail && contexts > 1) throw error(503, 'storage_unavailable'); return { ...ctx, context_hash: `hash-${contexts}` }; }, post: async () => { throw error(409, 'context_changed'); } });
    await h.open(); h.get('execution-prompt').value = '上下文变化也保留'; await h.submit();
    check(`context_changed refresh ${fail ? 'failure blocks' : 'success requires explicit submit'}`, contexts === 2 && h.get('execution-prompt').value === '上下文变化也保留' && h.get('execution-submit').disabled === fail && h.records.filter(item => item.method === 'POST').length === 1);
  }
  const restored = uiHarness({ saved: { request_id: randomUUID(), task_id: '20000000-0000-4000-8000-000000000001' }, post: async () => { throw new Error('must not send'); } });
  await settle(); await restored.open(); await restored.get('execution-retry').fire(); await restored.submit();
  check('reload stores only key/task and never reconstructs or resends body', restored.records.every(item => item.method !== 'POST') && restored.get('execution-retry').hidden && restored.get('execution-prompt').disabled);
  const run = { id: randomUUID(), task_title: '审批焦点', state: 'waiting_approval' };
  const detail = { run, approvals: [{ id: randomUUID(), kind: 'command', detail: { command: 'echo <unsafe>', cwd: 'E:\\fixture', scope_verified: true }, detail_complete: true, can_accept: true, status: 'pending' }, { id: randomUUID(), kind: 'file_change', detail: { changes: [{ path: 'a.txt', diff: '+ exact diff' }] }, detail_complete: false, can_accept: false, status: 'pending' }], events: [], next_cursor: 0 };
  const cards = uiHarness({ detail, post: async () => ({}) }); cards.list.runs = [run]; cards.list.active = [run];
  cards.ui.onView('runs'); await settle(); cards.get('execution-active').children[0].fire(); await settle();
  const firstCard = cards.get('execution-approvals').children[0];
  detail.approvals[1].detail.reason = '另一张卡片更新'; await cards.ui.refresh();
  check('unchanged approval DOM is retained when another approval changes', cards.get('execution-approvals').children[0] === firstCard);
  check('Chinese detail labels preserve exact command and diff text', firstCard.children[2].textContent.includes('命令：echo <unsafe>') && cards.get('execution-approvals').children[1].children[3].textContent.includes('变更内容：+ exact diff'));
  check('execution is loaded before workspace and has guarded bootstrap/view wiring', fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8').indexOf('/execution.js') < fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8').indexOf('/workspace.js') && source.includes('window.WorkspaceExecution?.init') && source.includes('window.WorkspaceExecution?.onView'));
  check('Agent output never uses HTML sinks', !/innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(executionSource));
}
(async () => {
  let failure = null;
  try { await main(); } catch (error) { failure = error.stack; }
  console.log(JSON.stringify({ scope: 'pure Node fetch and DOM stub contracts only', owner: 'S3 impl', checks: results, failure, exit_code: failure ? 1 : 0 }, null, 2));
  process.exitCode = failure ? 1 : 0;
})();
