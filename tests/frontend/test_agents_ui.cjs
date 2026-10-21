// Agent registry module with a synthetic DOM; no browser, process or personal data.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');

const repo = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(repo, 'web/workspace/workspace.html'), 'utf8');
const source = fs.readFileSync(path.join(repo, 'web/workspace/agents.js'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.className = ''; this.text = ''; this.value = '';
    this.checked = false; this.disabled = false; this.hidden = false; this.placeholder = '';
    this.listeners = {}; this.dataset = {}; this.attributes = {};
    this.classList = { toggle() {} };
  }
  append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  focus() {}
  setAttribute(name, value) { this.attributes[name] = String(value); }
  get all() { return [this, ...this.children.flatMap(node => node.all || [])]; }
  querySelector(selector) { return this.all.find(node => selector.startsWith('#') ? node.id === selector.slice(1) : node.tagName === selector) || null; }
  reset() { this.value = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(''); }
}

function fixture() {
  const ids = ['agent-name', 'agent-kind', 'agent-command', 'agent-enabled', 'agent-form', 'agent-form-title',
    'agent-fields', 'agent-feedback', 'agents-status', 'agents-list', 'agent-new', 'cancel-agent',
    'refresh-agents', 'agent-token', 'agent-adapter', 'agent-connection-fields', 'agent-setup', 'agent-adapters-status',
    'agent-permission', 'agent-network', 'agent-approval'];
  const nodes = new Map(ids.map(id => [id, new Element(id === 'agent-form' ? 'form' : 'div')]));
  nodes.get('agent-kind').value = 'codex'; nodes.get('agent-enabled').checked = true;
  const document = { getElementById: id => nodes.get(id) || [...nodes.values()].flatMap(node => node.all).find(node => node.id === id), createElement: tag => new Element(tag) };
  const window = { confirm: () => true };
  vm.runInNewContext(source, { window, document, Promise, Map, Set, URL, console });
  return { window, document, node: id => document.getElementById(id) };
}

const first = { id: 'a'.repeat(32), name: 'Codex', kind: 'codex', command: 'codex', enabled: true,
  online: true, last_seen_at: new Date().toISOString(),
  created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-01-01T00:00:00+00:00' };

test('HTML loads the agent module before workspace and exposes controls', () => {
  assert.ok(html.indexOf('src="/agents.js"') >= 0);
  assert.ok(html.indexOf('src="/agents.js"') < html.indexOf('src="/workspace.js"'));
  for (const id of ['agent-settings', 'agents-list', 'agent-form', 'agent-name', 'agent-kind', 'agent-command', 'agent-adapter', 'agent-connection-fields', 'agent-setup', 'agent-adapters-status']) {
    assert.ok(html.includes(`id="${id}"`), id);
  }
});

test('agent module renders, saves and removes through the profile API', async () => {
  const f = fixture();
  const calls = [];
  const fetchJson = async url => { calls.push(['GET', url]); return { revision: 1, items: [first],
    tokens: { [first.id]: { prefix: 'ag_test', has_token: true } } }; };
  const postJson = async (url, body) => {
    calls.push(['POST', url, body]);
    return { revision: 2, items: [...body.items, { id: 'b'.repeat(32), name: 'Third', kind: 'custom',
      command: null, enabled: true, created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-01-01T00:00:00+00:00' }] };
  };
  f.window.WorkspaceAgents.init({ fetchJson, postJson });
  await f.window.WorkspaceAgents.load();
  assert.equal(calls[0][1], '/api/workspace/agents');
  assert.equal(f.node('agents-list').children.length, 1);
  assert.match(f.node('agents-list').textContent, /在线/);
  assert.match(f.node('agents-list').textContent, /ag_test/);

  f.node('agent-name').value = 'Third';
  f.node('agent-kind').value = 'custom';
  f.node('agent-command').value = 'example-agent';
  await f.node('agent-form').listeners.submit({ preventDefault() {} });
  const body = calls.find(call => call[0] === 'POST')[2];
  assert.equal(body.expected_revision, 1);
  assert.equal(body.items.length, 2);
  assert.equal(body.items[1].name, 'Third');
  assert.equal(body.items[1].command, 'example-agent');
});

test('editing, adding or removing a registration preserves every remaining project authorization', async () => {
  const f = fixture(); let revision = 1, rows = [{ ...first, project_ids: ['project-a'] }, { ...first, id: 'other', name: 'Other', project_ids: ['project-b'] }];
  const writes = [];
  f.window.WorkspaceAgents.init({ fetchJson: async () => ({ revision, items: rows }), postJson: async (_url, body) => {
    writes.push(JSON.parse(JSON.stringify(body))); revision++; rows = body.items.map(row => ({ ...row, id: row.id || 'added' })); return { revision, items: rows };
  } });
  await f.window.WorkspaceAgents.load();
  const actions = index => f.node('agents-list').children[index].children[0].children[1].children;
  await actions(0)[0].listeners.click(); f.node('agent-name').value = 'Renamed';
  await f.node('agent-form').listeners.submit({ preventDefault() {} });
  assert.deepEqual(writes.at(-1).items.map(row => row.project_ids), [['project-a'], ['project-b']]);
  await f.node('agent-new').listeners.click(); f.node('agent-name').value = 'Added';
  await f.node('agent-form').listeners.submit({ preventDefault() {} });
  assert.deepEqual(writes.at(-1).items.slice(0, 2).map(row => row.project_ids), [['project-a'], ['project-b']]);
  await actions(2)[2].listeners.click();
  assert.deepEqual(writes.at(-1).items.map(row => row.project_ids), [['project-a'], ['project-b']]);
});

const adapters = [{ id: 'fixture_cli', name: 'Synthetic CLI', kind_suggestion: 'custom', default_command: 'synthetic-cli', description: 'Synthetic local adapter', setup: 'Configure the external tool manually; no automatic connection.', fields: [
  { key: 'command', label: 'Command', type: 'text', required: false },
  { key: 'args', label: 'Arguments', type: 'args', required: false, placeholder: '["--json"]', help: 'JSON array of strings' },
  { key: 'cwd', label: 'Working directory', type: 'path', required: false }
] }, { id: 'fixture_mcp', name: 'Synthetic MCP', kind_suggestion: 'custom', setup: 'Manual MCP setup', fields: [
  { key: 'endpoint', label: 'Endpoint', type: 'url', required: true, placeholder: 'http://127.0.0.1:1234' }
] }];
function adapterFixture(initialRows = []) {
  const f = fixture(), calls = [], writes = [];
  const state = { rows: structuredClone(initialRows), revision: 3, failCatalog: false, failSave: false };
  f.window.WorkspaceAgents.init({ fetchJson: async url => {
    calls.push(url);
    if (url === '/api/workspace/agent-adapters') { if (state.failCatalog) throw new Error('catalog offline'); return { adapters }; }
    assert.equal(url, '/api/workspace/agents'); return { revision: state.revision, items: state.rows };
  }, postJson: async (url, body) => {
    assert.equal(url, '/api/workspace/agents'); writes.push(JSON.parse(JSON.stringify(body)));
    if (state.failSave) throw new Error('409 item_changed');
    state.rows = body.items.map((row, index) => ({ ...row, id: row.id || `added-${index}` })); state.revision++;
    return { items: state.rows, revision: state.revision };
  } });
  const action = (index, title) => f.node('agents-list').children[index].all.find(node => node.tagName === 'button' && node.textContent === title).listeners.click();
  const submit = () => f.node('agent-form').listeners.submit({ preventDefault() {} });
  const changeAdapter = async value => { f.node('agent-adapter').value = value; await f.node('agent-adapter').listeners.change(); };
  const field = key => { const found = f.node(`agent-connection-${key}`); assert.ok(found, `dynamic connection field ${key}`); return found; };
  return { ...f, state, calls, writes, action, submit, changeAdapter, field };
}

test('adapter catalog drives fields and persists typed arguments without duplicating command', async () => {
  const f = adapterFixture(); await f.window.WorkspaceAgents.load();
  await f.node('agent-new').listeners.click(); await f.changeAdapter('fixture_cli');
  assert.match(f.node('agent-setup').textContent, /external tool manually/);
  f.node('agent-name').value = 'Synthetic CLI'; f.node('agent-command').value = 'custom-cli';
  f.field('args').value = '["--json","two words"]'; f.field('cwd').value = 'C:/example/project';
  await f.submit(); const saved = f.writes.at(-1).items[0];
  assert.equal(saved.adapter, 'fixture_cli'); assert.equal(saved.command, 'custom-cli');
  assert.deepEqual(saved.connection, { args: ['--json', 'two words'], cwd: 'C:/example/project' });
  assert.ok(!Object.hasOwn(saved.connection, 'command'));
  await f.window.WorkspaceAgents.load(); await f.action(0, '编辑');
  assert.deepEqual(JSON.parse(f.field('args').value), ['--json', 'two words']);
  assert.equal(f.field('cwd').value, 'C:/example/project');
  assert.ok(f.calls.every(url => ['/api/workspace/agents', '/api/workspace/agent-adapters'].includes(url)));
});

test('legacy args and unknown typed connection fields survive edit, add and remove', async () => {
  const original = { ...first, adapter: 'fixture_cli', project_ids: ['project-a'], connection: { args: '--old string', cwd: 'C:/example/old', advanced: true, retries: 4, future: { mode: 'keep' } } };
  const other = { ...first, id: 'other', name: 'Other', adapter: 'unknown_future', project_ids: ['project-b'], connection: { endpoint: 'http://127.0.0.1:2345', flag: false } };
  const f = adapterFixture([original, other]); await f.window.WorkspaceAgents.load(); await f.action(0, '编辑');
  f.node('agent-name').value = 'Renamed'; await f.submit();
  assert.deepEqual(f.writes.at(-1).items[0].connection, original.connection);
  assert.deepEqual(f.writes.at(-1).items[1].connection, other.connection);
  await f.node('agent-new').listeners.click(); f.node('agent-name').value = 'Added'; await f.submit();
  await f.action(2, '移除');
  for (const write of f.writes) {
    assert.deepEqual(write.items[0].connection, original.connection); assert.deepEqual(write.items[0].project_ids, ['project-a']);
    assert.equal(write.items[1].adapter, 'unknown_future'); assert.deepEqual(write.items[1].connection, other.connection); assert.deepEqual(write.items[1].project_ids, ['project-b']);
  }
});

test('switching adapters warns before dropping parameters and cancellation restores the draft', async () => {
  const f = adapterFixture([{ ...first, adapter: 'fixture_cli', connection: { args: ['--old'], cwd: 'C:/example/old' } }]);
  await f.window.WorkspaceAgents.load(); await f.action(0, '编辑'); f.field('cwd').value = 'C:/example/draft';
  let confirmations = 0; f.window.confirm = () => { confirmations++; return false; };
  await f.changeAdapter('fixture_mcp'); assert.equal(confirmations, 1); assert.equal(f.node('agent-adapter').value, 'fixture_cli');
  assert.equal(f.field('cwd').value, 'C:/example/draft');
  f.window.confirm = () => true; await f.changeAdapter('fixture_mcp'); f.field('endpoint').value = 'http://127.0.0.1:9876';
  await f.submit(); assert.deepEqual(f.writes.at(-1).items[0].connection, { endpoint: 'http://127.0.0.1:9876' });
});

test('catalog failure leaves unknown registrations editable and preserves their saved connection', async () => {
  const row = { ...first, adapter: 'future_vendor', project_ids: ['project-a'], connection: { args: '--legacy', unknown: true, attempts: 2 } };
  const f = adapterFixture([row]); f.state.failCatalog = true; await f.window.WorkspaceAgents.load();
  await f.action(0, '编辑');
  assert.match(f.node('agent-adapters-status').textContent, /失败|不可用|offline/);
  f.node('agent-name').value = 'Renamed offline'; await f.submit();
  assert.equal(f.writes.at(-1).items[0].adapter, row.adapter); assert.deepEqual(f.writes.at(-1).items[0].connection, row.connection);
  assert.deepEqual(f.writes.at(-1).items[0].project_ids, row.project_ids);
});

test('invalid args are rejected locally and version conflict retains all dynamic draft fields', async () => {
  const f = adapterFixture([{ ...first, adapter: 'fixture_cli', connection: { args: ['--saved'] } }]);
  await f.window.WorkspaceAgents.load(); await f.action(0, '编辑');
  for (const invalid of ['not json', '[1]', '{"x":"y"}']) {
    f.field('args').value = invalid; await f.submit(); assert.equal(f.writes.length, 0);
  }
  f.field('args').value = '["--draft"]'; f.field('cwd').value = 'C:/example/draft'; f.node('agent-name').value = 'Draft name'; f.state.failSave = true;
  await f.submit(); assert.equal(f.writes.length, 1); assert.equal(f.writes[0].expected_revision, 3);
  assert.equal(f.node('agent-name').value, 'Draft name'); assert.equal(f.field('args').value, '["--draft"]'); assert.equal(f.field('cwd').value, 'C:/example/draft');
  assert.equal(f.node('agent-form').hidden, false); assert.match(f.node('agent-feedback').textContent, /409.*保留/);
  f.state.revision = 99; f.state.rows[0].name = 'Changed in another window'; await f.window.WorkspaceAgents.load();
  assert.equal(f.node('agent-name').value, 'Draft name'); assert.equal(f.field('args').value, '["--draft"]');
  await f.submit(); assert.equal(f.writes.at(-1).expected_revision, 3);
});

test('MCP endpoint validation keeps malformed or remote values local and sends no connection request', async () => {
  const f = adapterFixture(); await f.window.WorkspaceAgents.load(); await f.node('agent-new').listeners.click(); await f.changeAdapter('fixture_mcp');
  f.node('agent-name').value = 'Local MCP';
  for (const endpoint of ['invalid', 'file:///example', 'https://example.com', 'http://user:pass@localhost:1234']) {
    f.field('endpoint').value = endpoint; await f.submit(); assert.equal(f.writes.length, 0);
  }
  f.field('endpoint').value = 'http://localhost:1234'; await f.submit();
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].items[0].connection.endpoint, 'http://localhost:1234');
  assert.ok(f.calls.every(url => ['/api/workspace/agents', '/api/workspace/agent-adapters'].includes(url)));
});

test('stale last contact cannot be presented as online even when the saved snapshot says online', async () => {
  const f = adapterFixture([{ ...first, online: true, last_seen_at: '2026-01-01T00:00:00Z' }]);
  await f.window.WorkspaceAgents.load();
  assert.match(f.node('agents-list').textContent, /离线|过期|未知/);
  assert.doesNotMatch(f.node('agents-list').textContent, /· 在线 ·/);
});
