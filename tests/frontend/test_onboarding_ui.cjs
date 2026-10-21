'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'web/workspace/onboarding.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.value = ''; this.checked = false; this.text = ''; this.open = false; this.classList = { toggle() {} }; }
  append(...values) { this.children.push(...values); }
  replaceChildren(...values) { this.children = values; this.text = ''; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  setAttribute(key, value) { this[key] = value; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(value => value.textContent).join(''); }
}
function fixture(options = {}) {
  const nodes = new Map(), calls = [], writes = [], changes = [], manual = [];
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const dialog = node('dialog'); dialog.querySelector = selector => node(selector.slice(1));
  const item = { candidate_id: 'cl-' + '1'.repeat(24), project_id: 'cl-' + '1'.repeat(24), name: '<script>literal</script>', directories: [{ id: 'main', path: 'C:/example/project' }], selected: false };
  const page = { status: 'available', complete: true, snapshot_token: 'snapshot', selection_token: 'selection', items: [item], total: 1 };
  const overview = { workbench_revision: 0, selection_token: 'selection', excluded_projects: [], tools: ['codex', 'claude', 'opencode'].map(provider => ({ provider, source: { enabled: false, path: null }, locations: { items: [] } })) };
  const fetchJson = async url => {
    calls.push(url);
    if (url.includes('/onboarding')) return overview;
    if (url.includes('project-candidates')) return options.read ? options.read(url) : page;
    return url.endsWith('/candidates') ? { candidates: [] } : { registrations: [] };
  };
  const postJson = async (url, body) => {
    writes.push({ url, body: JSON.parse(JSON.stringify(body)) });
    if (options.post) return options.post(url, body, writes.length);
    return { selected: true, changed: true, requires_rejoin: false, project_id: item.project_id, selection_token: 'new-token' };
  };
  const window = { confirm: () => true };
  vm.runInNewContext(source, { window, document: { createElement: tag => new Element(tag) }, Map, Set, console });
  const controller = window.WorkspaceOnboarding.create({ dialog, fetchJson, postJson, onChanged: async () => changes.push(true), manual: () => manual.push(true), configure() {}, manage() {} });
  return { controller, window, dialog, node, calls, writes, changes, manual, page, overview, item };
}
test('HTML resource and consent controls precede workspace and default native diagnostics are folded', () => {
  assert.ok(html.indexOf('/onboarding.js') < html.indexOf('/workspace.js'));
  assert.match(html, /id="onboarding-consent" type="checkbox"/);
  assert.match(html, /<details[^>]+id="observation-diagnostics"/);
  assert.doesNotMatch(html.match(/<details[^>]+id="observation-diagnostics"[^>]*>/)[0], /\bopen\b/);
  assert.match(html, /id="refresh-workspace"[^>]+hidden/);
  const css = fs.readFileSync(path.join(root, 'web/workspace/workspace.css'), 'utf8');
  assert.match(css, /\.onboarding-picker\{display:grid/);
  assert.doesNotMatch(css, /\.onboarding-drop\{[^}]*position:sticky/);
});

test('opening project addition defaults to local folders without reading a tool source', async () => {
  const f = fixture(); await f.controller.open();
  assert.equal(f.calls.length, 0); assert.equal(f.writes.length, 0);
  assert.equal(f.node('onboarding-tool-import').hidden, true);
  assert.equal(f.node('onboarding-directory-form').hidden, false);
  await f.node('onboarding-import').listeners.click();
  assert.equal(f.node('onboarding-tool-import').hidden, false);
  assert.ok(f.calls.some(url => url.includes('/onboarding')));
});

test('new folder creation requires a matching preview and explicit confirmation, with no duplicate submission', async () => {
  let complete;
  const f = fixture({ post: async (url, body) => {
    if (url.endsWith('/create-preview')) return { status: 'preview', path: 'C:/example/New', can_create: true };
    return new Promise(resolve => { complete = resolve; });
  } });
  await f.controller.open({ mode: 'new' });
  f.node('onboarding-directory-name').value = 'New'; f.node('onboarding-directory-path').value = 'C:/example/parent';
  const submit = () => f.node('onboarding-directory-form').listeners.submit({ preventDefault() {} });
  await submit(); assert.equal(f.writes.length, 0);
  await f.node('onboarding-directory-preview').listeners.click();
  assert.ok(f.node('onboarding-directory-result').textContent.includes('C:/example/New'));
  await submit(); assert.equal(f.writes.length, 1);
  f.node('onboarding-directory-confirm').checked = true;
  f.node('onboarding-directory-confirm').listeners.change();
  f.node('onboarding-directory-name').value = 'Changed';
  await submit(); assert.equal(f.writes.length, 1);
  f.node('onboarding-directory-name').value = 'New';
  const pending = submit(); await submit(); assert.equal(f.writes.length, 2);
  assert.deepEqual(f.writes[1].body, { name: 'New', parent_path: 'C:/example/parent', confirmed: true });
  complete({ status: 'created', project: { id: 'p' } }); await pending;
  assert.equal(f.dialog.open, false); assert.equal(f.changes.length, 1);
});

test('existing folders are inspected and registered without creating a directory', async () => {
  const f = fixture({ post: async url => url.endsWith('/inspect')
    ? { status: 'ok', path: 'C:/example/Existing', name: 'Existing' }
    : { status: 'registered', project: { id: 'p' } } });
  await f.controller.open(); f.node('onboarding-directory-path').value = 'C:/example/Existing';
  await f.node('onboarding-directory-preview').listeners.click();
  assert.equal(f.node('onboarding-directory-name').value, 'Existing');
  f.node('onboarding-directory-confirm').checked = true;
  await f.node('onboarding-directory-form').listeners.submit({ preventDefault() {} });
  assert.deepEqual(f.writes.map(row => row.url), ['/api/workspace/projects/inspect', '/api/workspace/projects/register']);
});

test('cancelled native picker preserves input and partial creation switches to explicit existing-folder recovery', async () => {
  const f = fixture({ post: async url => url.endsWith('/pick') ? { status: 'cancelled' } : url.endsWith('/create-preview')
    ? { status: 'preview', path: 'C:/example/New', can_create: true }
    : { status: 'partial', path: 'C:/example/New', directory_created: true } });
  await f.controller.open({ mode: 'new' });
  f.node('onboarding-directory-name').value = 'New'; f.node('onboarding-directory-path').value = 'C:/example/parent';
  await f.node('onboarding-directory-pick').listeners.click(); assert.equal(f.node('onboarding-directory-path').value, 'C:/example/parent');
  await f.node('onboarding-directory-preview').listeners.click(); f.node('onboarding-directory-confirm').checked = true;
  await f.node('onboarding-directory-form').listeners.submit({ preventDefault() {} });
  assert.equal(f.node('onboarding-directory-path').value, 'C:/example/New');
  assert.equal(f.node('onboarding-directory-save').disabled, true);
  assert.equal(f.node('onboarding-directory-confirm').checked, false);
  assert.match(f.node('onboarding-feedback').textContent, /文件夹已创建，但项目登记未成功/);
  assert.equal(f.writes.filter(row => row.url.endsWith('/create')).length, 1); assert.equal(f.dialog.open, true);
});
test('three tools are present, literal candidates start unselected, click writes opaque IDs only', async () => {
  const f = fixture(); await f.controller.open({ mode: 'import' });
  assert.deepEqual(f.node('onboarding-tabs').children.slice(0, 3).map(node => node.textContent), ['Codex', 'Claude Code', 'OpenCode']);
  assert.equal(f.node('onboarding-candidates').children[0].children[0].checked, false);
  assert.match(f.node('onboarding-candidates').textContent, /<script>literal<\/script>/);
  await f.controller.join(f.item.candidate_id);
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].body.action, 'join');
  assert.equal(Object.hasOwn(f.writes[0].body, 'directories'), false);
  assert.equal(f.changes.length, 1);
  await f.controller.join(f.item.candidate_id); assert.equal(f.writes.length, 1);
});

test('project picker separates existing projects from new projects and never discovers programs', async () => {
  const f = fixture(); await f.controller.open({ mode: 'import' });
  const picker = html.match(/<dialog id="onboarding-dialog"[^]*?<\/dialog>/)[0];
  assert.match(picker, /从已有项目选择/);
  assert.match(picker, /id="onboarding-manual"[^>]*>手工登记已有目录/);
  assert.doesNotMatch(picker, /onboarding-program|onboarding-custom|EXE|程序登记/);
  assert.equal(f.node('onboarding-tabs').children.length, 3);
  assert.equal(f.calls.some(url => url.includes('/api/profile/observations')), false);
  assert.match(html, /id="open-registration-settings">配置工具/);
  f.node('onboarding-manual').listeners.click();
  assert.equal(f.dialog.open, false); assert.equal(f.manual.length, 1);
  assert.equal(f.writes.length, 0);
});

for (const status of ['available', 'not_configured', 'unsupported_format', 'unavailable', 'limited']) {
  test(`${status} without candidates offers direct new-project creation without a source write`, async () => {
    const f = fixture({ read: () => ({ status, complete: status === 'available', items: [], total: 0 }) });
    await f.controller.open({ mode: 'import' });
    const fallback = f.node('onboarding-candidates').children.find(node => node.tagName === 'button');
    assert.equal(fallback?.textContent, '新建项目');
    fallback.listeners.click();
    assert.equal(f.dialog.open, false); assert.equal(f.manual.length, 1); assert.equal(f.writes.length, 0);
  });
}

test('candidate read failure preserves a direct new-project fallback', async () => {
  const f = fixture({ read: () => { throw new Error('fixture offline'); } });
  await f.controller.open({ mode: 'import' });
  assert.match(f.node('onboarding-feedback').textContent, /fixture offline/);
  assert.equal(f.node('onboarding-retry').hidden, false);
  f.node('onboarding-candidates').children.find(node => node.tagName === 'button').listeners.click();
  assert.equal(f.manual.length, 1); assert.equal(f.writes.length, 0);
});

test('Codex preserves source names and paths while Claude and OpenCode describe recognized directories', async () => {
  const f = fixture(); f.item.name = 'mianban-develop'; f.item.directories[0].path = 'C:/example/mianban-develop';
  await f.controller.open({ mode: 'import' });
  const copy = f.node('onboarding-candidates').children[0].children[1];
  assert.equal(copy.children[0].textContent, 'mianban-develop');
  assert.equal(copy.children[1].textContent, 'C:/example/mianban-develop');
  assert.equal(f.node('onboarding-candidates-title').textContent, 'Codex 项目');
  for (const tab of f.node('onboarding-tabs').children.slice(1)) {
    tab.listeners.click(); await tick();
    assert.match(f.node('onboarding-candidates-title').textContent, /识别到的项目目录/);
    assert.match(f.node('onboarding-source-note').textContent, /不导入完整桌面配置/);
  }
});
test('two-step join never announces success before profile rejoin completes', async () => {
  let resolve;
  const f = fixture({ post: async (_url, body) => body.action === 'join'
    ? { project_id: 'saved', selected: false, requires_rejoin: true, selection_token: 'after-db' }
    : new Promise(done => { resolve = done; }) });
  await f.controller.open({ mode: 'import' }); const joining = f.controller.join(f.item.candidate_id); await tick();
  assert.match(f.node('onboarding-feedback').textContent, /已保存，正在加入/);
  assert.equal(f.changes.length, 0); assert.equal(f.writes[1].body.selection_token, 'after-db');
  resolve({ project_id: 'saved', selected: true, requires_rejoin: false, selection_token: 'after-profile' }); await joining;
  assert.equal(f.changes.length, 1); assert.match(f.node('onboarding-feedback').textContent, /已加入工作台/);
});
test('failed second step retains selection and explicit retry uses stored rejoin', async () => {
  let fail = true;
  const f = fixture({ post: async (_url, body) => {
    if (body.action === 'join') return { project_id: 'cl-' + '1'.repeat(24), selected: false, requires_rejoin: true, selection_token: 'db' };
    if (fail) throw new Error('fixture conflict');
    return { selected: true, project_id: body.project_id, selection_token: 'final' };
  } });
  await f.controller.open({ mode: 'import' }); await f.controller.join(f.item.candidate_id);
  assert.equal(f.changes.length, 0); assert.match(f.node('onboarding-feedback').textContent, /尚未加入/);
  fail = false; await f.controller.join(f.item.candidate_id);
  assert.deepEqual(f.writes.map(row => row.body.action), ['join', 'rejoin', 'rejoin']); assert.equal(f.changes.length, 1);
});
test('drop and keyboard-click share join; stale or external data never writes', async () => {
  const f = fixture(); await f.controller.open({ mode: 'import' }); const zone = f.node('onboarding-drop');
  const drop = (types, payload) => zone.listeners.drop({ preventDefault() {}, dataTransfer: { types, getData: () => payload } });
  drop(['Files'], ''); assert.match(f.node('onboarding-feedback').textContent, /请填写本机目录路径/);
  drop([f.window.WorkspaceOnboarding.MIME], JSON.stringify({ candidate_id: f.item.candidate_id, snapshot_token: 'old' })); assert.equal(f.writes.length, 0);
  drop([f.window.WorkspaceOnboarding.MIME], JSON.stringify({ candidate_id: f.item.candidate_id, snapshot_token: 'snapshot' })); await tick();
  assert.equal(f.writes[0].body.action, 'join');
});
test('source save preserves draft on error and no project write precedes consent', async () => {
  const f = fixture({ post: async () => { throw new Error('storage_error'); } }); await f.controller.open({ mode: 'import' });
  f.node('onboarding-path').value = 'C:/example/source.json'; f.node('onboarding-consent').checked = true;
  await f.node('onboarding-source-form').listeners.submit({ preventDefault() {} });
  assert.equal(f.writes[0].url, '/api/workspace/sources'); assert.equal(f.node('onboarding-path').value, 'C:/example/source.json');
  assert.match(f.node('onboarding-feedback').textContent, /输入已保留/);
  assert.equal(f.node('onboarding-source-details').open, true);
});
test('enabled sources open on candidates, tab changes clear the previous success feedback', async () => {
  const f = fixture(); f.overview.tools[0].source.enabled = true;
  await f.controller.open({ mode: 'import' }); assert.equal(f.node('onboarding-source-details').open, false);
  await f.controller.join(f.item.candidate_id); assert.match(f.node('onboarding-feedback').textContent, /已加入/);
  f.node('onboarding-tabs').children[1].listeners.click(); await tick();
  assert.doesNotMatch(f.node('onboarding-feedback').textContent, /已加入/);
  assert.equal(f.node('onboarding-source-details').open, true);
});
test('batch success is per selected result; failure stays selected and next request uses latest token', async () => {
  const f = fixture({ post: async () => { throw new Error('fixture'); } }); await f.controller.open({ mode: 'import' });
  const checkbox = f.node('onboarding-candidates').children[0].children[0]; checkbox.checked = true; checkbox.listeners.change();
  await f.controller.joinSelected(); assert.match(f.node('onboarding-feedback').textContent, /已加入 0 项，1 项未加入/);
  assert.equal(f.node('onboarding-candidates').children[0].children[0].checked, true);
});
test('mixed excluded rejoin and new selection refreshes the revision sample before the next write', async () => {
  let revision = 0, f;
  const post = async (_url, body) => {
    if (body.action === 'rejoin') { revision++; return { project_id: body.project_id, selected: true, selection_token: `selection-${revision}` }; }
    assert.equal(body.snapshot_token, `sample-${revision}`);
    assert.equal(body.selection_token, `selection-${revision}`);
    if (body.candidate_id === f.item.candidate_id) return { project_id: f.item.project_id, selected: false, requires_rejoin: true, selection_token: `selection-${revision}` };
    return { project_id: 'new', selected: true, selection_token: `selection-${revision}` };
  };
  const options = { read: () => ({ ...f.page, source_id: 'same-source', snapshot_token: `sample-${revision}`, selection_token: `selection-${revision}`,
    items: [f.item, { ...f.item, candidate_id: 'new', project_id: 'new' }].map(item => ({ ...item })) }), post };
  f = fixture(options); f.overview.excluded_projects = [{ project_id: f.item.project_id, name: 'Excluded' }];
  await f.controller.open({ mode: 'import' });
  for (const row of f.node('onboarding-candidates').children) { row.children[0].checked = true; row.children[0].listeners.change(); }
  await f.controller.joinSelected();
  assert.deepEqual(f.writes.map(row => row.body.action), ['join', 'rejoin', 'join']);
  assert.equal(f.writes.at(-1).body.snapshot_token, 'sample-1');
  assert.equal(f.calls.filter(url => url.includes('project-candidates')).length, 2);
  assert.match(f.node('onboarding-feedback').textContent, /已加入 2 项，0 项未加入/);
});
test('409 refresh preserves remaining choices and explicit retry uses the fresh sample', async () => {
  let reads = 0, f;
  f = fixture({ read: () => ({ ...f.page, source_id: 'same-source', snapshot_token: `sample-${++reads}`, items: [{ ...f.item }] }),
    post: async (_url, body, count) => {
      if (count === 1) throw Object.assign(new Error('candidate_changed'), { reason: 'candidate_changed', status: 409 });
      assert.equal(body.snapshot_token, 'sample-2'); return { selected: true, project_id: f.item.project_id, selection_token: 'fresh' };
    } });
  await f.controller.open({ mode: 'import' }); const box = f.node('onboarding-candidates').children[0].children[0]; box.checked = true; box.listeners.change();
  await f.controller.joinSelected();
  assert.equal(f.writes.length, 1); assert.equal(reads, 2);
  assert.equal(f.node('onboarding-candidates').children[0].children[0].checked, true);
  await f.controller.joinSelected(); assert.equal(f.writes.length, 2); assert.equal(f.changes.length, 1);
});
test('source changes cannot silently reuse old selections during a conflict retry', async () => {
  let reads = 0, f;
  f = fixture({ read: () => ({ ...f.page, source_id: ++reads === 1 ? 'original' : 'replacement', items: [{ ...f.item }] }),
    post: async () => { throw Object.assign(new Error('candidate_changed'), { status: 409 }); } });
  await f.controller.open({ mode: 'import' }); const box = f.node('onboarding-candidates').children[0].children[0]; box.checked = true; box.listeners.change();
  await f.controller.joinSelected(); await f.controller.joinSelected();
  assert.equal(f.writes.length, 1); assert.equal(f.node('onboarding-retry').hidden, false);
  await f.node('onboarding-retry').listeners.click();
  assert.equal(f.node('onboarding-candidates').children[0].children[0].checked, false);
  await f.controller.joinSelected(); assert.equal(f.writes.length, 1);
});
test('late candidates from a previous tool cannot replace current candidates', async () => {
  let first;
  const f = fixture({ read: url => url.includes('provider=codex') ? new Promise(resolve => { first = resolve; }) : Promise.resolve({ status: 'available', complete: true, snapshot_token: 'new', selection_token: 'selection', items: [], total: 0 }) });
  const opening = f.controller.open({ mode: 'import' }); await tick();
  f.node('onboarding-tabs').children[1].listeners.click(); await tick();
  first({ status: 'available', complete: true, snapshot_token: 'old', items: [f.item], total: 1 }); await opening;
  assert.doesNotMatch(f.node('onboarding-candidates').textContent, /literal/);
});

const workspaceSource = fs.readFileSync(path.join(root, 'web/workspace/workspace.js'), 'utf8');
function refreshFixture() {
  const nodes = new Map(), reads = [], applied = [];
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  let editing = false;
  const app = { profileMode: true, csrf: 'fixture', taskCenter: { isEditing: () => editing, update: value => applied.push(value.marker) } };
  const context = vm.createContext({ app, $: node, window: {},
    api: () => new Promise((resolve, reject) => reads.push({ resolve, reject })),
    dateTime: value => value, message(id, text) { node(id).textContent = text; }, fillOptions() {},
    renderStats() {}, renderProjects() {}, renderProviders() {} });
  vm.runInContext(workspaceSource.match(/async function loadBase\([^]*?\n\}/)[0], context);
  const snapshot = marker => ({ marker, status: 'ok', today: '', sampled_at: '', projects: { status: 'available', items: [] }, tasks: { status: 'available' } });
  return { app, reads, applied, node, snapshot, setEditing(value) { editing = value; }, run: code => vm.runInContext(code, context) };
}
test('workspace refresh coalesces concurrent requests and only applies the final response', async () => {
  const f = refreshFixture(); const first = f.run('loadBase()'), second = f.run('loadBase()');
  assert.equal(f.reads.length, 1);
  f.reads[0].resolve(f.snapshot('old')); await tick();
  assert.equal(f.reads.length, 2); assert.deepEqual(f.applied, []);
  f.reads[1].resolve(f.snapshot('new')); await Promise.all([first, second]);
  assert.deepEqual(f.applied, ['new']);
});
test('background refresh defers presentation during a draft and failure keeps the last successful snapshot', async () => {
  const f = refreshFixture(); f.setEditing(true);
  const pending = f.run('loadBase({background:true})'); f.reads[0].resolve(f.snapshot('deferred')); await pending;
  assert.deepEqual(f.applied, []); assert.match(f.node('workspace-status').textContent, /有更新/);
  f.setEditing(false); const next = f.run('loadBase({background:true})'); f.reads[1].resolve(f.snapshot('current')); await next;
  const failed = f.run('loadBase({background:true})'); f.reads[2].reject(new Error('fixture offline')); await failed;
  assert.equal(f.app.snapshot.marker, 'current'); assert.equal(f.node('refresh-workspace').hidden, false);
});
test('automatic refresh merges focus events and stops timers while hidden or on pagehide', () => {
  const timers = new Map(), intervals = new Map(), events = {}, documentEvents = {}; let id = 0, refreshes = 0;
  const document = { hidden: false, addEventListener: (name, fn) => { documentEvents[name] = fn; } };
  const context = vm.createContext({ app: {}, document, window: { addEventListener: (name, fn) => { events[name] = fn; } },
    loadBase: () => refreshes++, setTimeout: fn => { timers.set(++id, fn); return id; }, clearTimeout: value => timers.delete(value),
    setInterval: (fn, delay) => { assert.equal(delay, 30000); intervals.set(++id, fn); return id; }, clearInterval: value => intervals.delete(value) });
  vm.runInContext(workspaceSource.match(/function setupAutomaticRefresh\([^]*?\n\}/)[0] + '\nsetupAutomaticRefresh();', context);
  events.focus(); events.focus(); assert.equal(timers.size, 1); assert.equal(intervals.size, 1);
  document.hidden = true; documentEvents.visibilitychange(); assert.equal(timers.size, 0); assert.equal(intervals.size, 0);
  document.hidden = false; documentEvents.visibilitychange(); assert.equal(intervals.size, 1);
  [...timers.values()][0](); assert.equal(refreshes, 1);
  events.pagehide(); assert.equal(intervals.size, 0);
});
test('manual directory conflict preserves draft and offers the existing project or rejoin picker', async () => {
  const nodes = new Map(), opened = [];
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  node('project-name').value = 'Unsaved name';
  const app = { snapshot: { projects: { items: [{ id: 'existing', name: 'Existing', directories: [{ path: 'C:/example/project' }] }] } },
    projectSpace: { open: id => opened.push(id) }, onboarding: { open: () => opened.push('picker') } };
  const context = vm.createContext({ app, $: node, TextEncoder, projectBody: () => ({ id: null, directories: [{ path: 'C:/example/project' }] }),
    clearFieldErrors() {}, fieldError() {}, post: async () => { throw Object.assign(new Error('conflict'), { reason: 'directory_conflict' }); },
    action: (label, fn) => { const button = new Element('button'); button.textContent = label; button.click = fn; return button; } });
  vm.runInContext(workspaceSource.match(/async function saveProject\([^]*?\n\}/)[0], context);
  await vm.runInContext('saveProject({preventDefault(){}})', context);
  assert.equal(node('project-name').value, 'Unsaved name');
  assert.match(node('project-feedback').textContent, /打开已有项目.*重新加入/);
  node('project-feedback').children[0].click(); assert.deepEqual(opened, ['existing']);
});
