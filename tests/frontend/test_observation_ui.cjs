'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'web/workspace/observation.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

class Element {
  constructor() { this.value = ''; this.checked = false; this.open = false; this.children = []; this.listeners = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this.text = ''; }
  append(...items) { for (const item of items) item.parent = this; this.children.push(...items); }
  replaceChildren(...items) { this.children = items; this.text = ''; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  setAttribute(name, value) { this[name] = String(value); }
  set textContent(text) { this.text = String(text); this.children = []; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  set innerHTML(_text) { throw new Error('HTML injection forbidden'); }
  querySelector(selector) { return this.children.find(item => `.${item.className}` === selector) || this.children.map(item => item.querySelector(selector)).find(Boolean) || null; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(item => item !== this); }
}
function fixture() {
  const nodes = new Map(), reads = [], writes = [], timers = new Map(), events = {}, documentEvents = {};
  let timerId = 0, clock = Date.now();
  const project = new Element(), projectCopy = new Element(); project.dataset.projectId = 'alpha'; projectCopy.className = 'project-copy'; project.append(projectCopy);
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = { hidden: false, getElementById: node, createElement: () => new Element(), querySelectorAll: () => [project],
    addEventListener: (name, listener) => { documentEvents[name] = listener; } };
  const window = { addEventListener: (name, listener) => { events[name] = listener; } };
  const context = vm.createContext({ window, document, Date: class extends Date { static now() { return clock; } }, Map, console,
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay, due: clock + delay }); return id; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(source, context);
  window.WorkspaceObservation.start({ fetchJson: url => new Promise((resolve, reject) => reads.push({ url, resolve, reject })),
    postJson: (url, body) => new Promise((resolve, reject) => writes.push({ url, body: JSON.parse(JSON.stringify(body)), resolve, reject })) });
  async function advance(milliseconds) {
    const target = clock + milliseconds;
    while (true) {
      const next = [...timers.entries()].filter(([_id, timer]) => timer.due <= target).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      timers.delete(next[0]); clock = next[1].due; next[1].callback(); await tick();
    }
    clock = target; await tick();
  }
  return { node, reads, writes, timers, events, documentEvents, document, window, advance, projectCopy, now: () => clock };
}
const registrationId = 'a'.repeat(32);
const pickerButton = f => f.node('native-registrations').children[0].children[2];
const snapshot = () => ({ config_revision: 1, config_hash: 'hash', registrations: [{ registration_id: registrationId, category: 'custom_exe', label: 'Example host', enabled: true, binding_state: 'confirmed', can_pick: true, status: 'available', reason: null }], status: 'available', reason: null, sampled_at_ms: Date.now(), generation: 'g', seq: 5, coverage: { status: 'complete' },
  installations: [], resources: [{ resource_id: 'resource', rss_bytes: 1048576, cpu_percent_one_core: null, threads: 2, handles: 3, metrics_interval_ms: 1000 }],
  instances: [{ instance_id: 'instance', provider_id: 'codex', kind: 'cli', pid: 123, project_id: null, status: 'available', resource_id: 'resource',
    return_action: { action_id: 'action', issued_seq: 1, scope: 'console_window', expires_at_ms: Date.now() + 10000 } }], project_refs: [], unassigned: ['instance'] });
const choices = () => ({ config_revision: 1, config_hash: 'hash', status: 'available', reason: null, generation: 'g', selection_id: 'selection', expires_at_ms: Date.now() + 9000,
  coverage: { status: 'complete', omitted: 0 }, choices: [{ action_id: 'manual', issued_seq: 1, title: '<img src=x onerror=attack()>', expires_at_ms: Date.now() + 9000 }] });

test('actual HTML loads observation between existing scripts and actual bootstrap starts independently', () => {
  assert.ok(html.indexOf('src="/execution.js"') < html.indexOf('src="/observation.js"'));
  assert.ok(html.indexOf('src="/observation.js"') < html.indexOf('src="/workspace.js"'));
  for (const id of ['native-instances', 'native-unassigned', 'native-registrations', 'window-choices-picker']) assert.ok(html.includes(`id="${id}"`));
  const workspace = fs.readFileSync(path.join(root, 'web/workspace/workspace.js'), 'utf8');
  assert.match(workspace, /WorkspaceObservation\?\.start\(\{ fetchJson: api, postJson: post \}\)/);
  assert.ok(workspace.indexOf('WorkspaceObservation?.start(') < workspace.indexOf('await Promise.allSettled([loadBase()'));
});

test('profile capability setup keeps local registration navigation visible when remote access is disabled', () => {
  const workspace = fs.readFileSync(path.join(root, 'web/workspace/workspace.js'), 'utf8');
  const setup = workspace.match(/function setupProfile\([^]*?\n\}/)[0];
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      const value = new Element(); value.after = () => {}; value.parentElement = { prepend() {} };
      value.previousElementSibling = { querySelector: () => node('session-index-link') };
      nodes.set(id, value);
    }
    return nodes.get(id);
  };
  const context = vm.createContext({ $: node, app: { capabilities: {}, view: 'overview', pages: {} },
    document: { querySelector(selector) {
      if (selector === '#agents-view .section-heading a') return node('open-registration-settings');
      return node(selector);
    } }, el: () => new Element(), link: () => new Element(), fillOptions() {}, showView() {},
    saveProject() {}, saveManual() {}, saveSettings() {}, importLegacy() {}, syncSource() {}, projectBytes() {} });
  vm.runInContext(setup + '\nsetupProfile();', context);
  assert.equal(node('open-registration-settings').hidden, false);
  assert.equal(node('provider-sessions-link').hidden, true);
});

test('default path only reads cache, 2 second polling never overlaps and renders unknown session independently', async () => {
  const f = fixture(); assert.equal(f.reads[0].url, '/api/workspace/observations'); assert.equal(f.writes.length, 0);
  f.reads[0].resolve(snapshot()); await tick();
  assert.match(f.node('native-instances').textContent, /会话未知.*待处理未知/);
  assert.match(f.node('observation-resource-summary').textContent, /1 份独立资源.*1.0 MiB/);
  const timer = [...f.timers.values()].find(item => item.delay === 2000);
  assert.ok(timer); timer.callback(); timer.callback(); assert.equal(f.reads.length, 2);
  assert.equal(f.writes.length, 0);
});

test('explicit picker displays literal title and close clears DOM, dismissal and late response are safe', async () => {
  const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
  const opening = pickerButton(f).listeners.click();
  assert.deepEqual(f.writes[0].body, { registration_id: registrationId });
  f.writes[0].resolve(choices()); await opening;
  assert.match(f.node('window-choices-list').textContent, /<img src=x onerror=attack\(\)>/);
  f.node('close-window-choices').listeners.click();
  assert.equal(f.node('window-choices-list').textContent, '');
  assert.deepEqual(f.writes[1].body, { selection_id: 'selection', generation: 'g' });
  f.writes[1].resolve({ status: 'available' });
  const late = pickerButton(f).listeners.click();
  f.node('close-window-choices').listeners.click(); f.writes[2].resolve(choices()); await late;
  assert.equal(f.node('window-choices-list').textContent, ''); assert.equal(f.node('window-choices-picker').hidden, true);
});

test('hidden, pagehide and TTL clear titles without re-listing', async () => {
  for (const mode of ['hidden', 'pagehide', 'ttl']) {
    const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
    const opening = pickerButton(f).listeners.click(); f.writes[0].resolve(choices()); await opening;
    if (mode === 'hidden') { f.document.hidden = true; f.documentEvents.visibilitychange(); }
    if (mode === 'pagehide') f.events.pagehide();
    if (mode === 'ttl') await f.advance(10000);
    assert.equal(f.node('window-choices-list').textContent, '');
    assert.equal(f.writes.filter(item => item.url.endsWith('/window-choices')).length, 1);
  }
});

test('return uses issued sequence and opaque IDs, manual selection does not change associations or retry lost response', async () => {
  const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
  const opening = pickerButton(f).listeners.click(); f.writes[0].resolve(choices()); await opening;
  const returning = f.node('window-choices-list').children[0].children[1].listeners.click();
  assert.equal(f.node('window-choices-list').textContent, '');
  assert.deepEqual(f.writes[1].body, { action_id: 'manual', generation: 'g', seq: 1 });
  f.writes[1].reject({ reason: 'return_unknown' }); await returning;
  assert.match(f.node('observation-feedback').textContent, /不会自动重试/);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 1);
  assert.match(f.node('native-instances').textContent, /未归属项目/);
  assert.equal(f.window.WorkspaceObservation.projects.length, 0);
});

test('rejected poll immediately marks retained facts stale, clears picker and revokes old button handlers', async () => {
  const f = fixture(), value = snapshot();
  value.project_refs = [{ project_id: 'alpha', instance_ids: ['instance'] }];
  f.reads[0].resolve(value); await tick();
  const oldButton = f.node('native-instances').children[0].children.at(-1);
  const opening = pickerButton(f).listeners.click(); f.writes[0].resolve(choices()); await opening;
  await f.advance(2000); f.reads[1].reject(new Error('offline')); await tick();
  assert.match(f.node('observation-status').textContent, /过期|不可用/);
  assert.doesNotMatch(f.node('native-instances').textContent, /可用/);
  assert.match(f.node('observation-resource-summary').textContent, /旧观察/);
  assert.match(f.projectCopy.textContent, /旧观察/);
  assert.doesNotMatch(f.projectCopy.textContent, /已核验本机实例/);
  assert.equal(oldButton.disabled, true);
  assert.equal(f.node('window-choices-list').textContent, '');
  assert.equal(f.node('window-choices-picker').hidden, true);
  await oldButton.listeners.click();
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
  await f.advance(2000);
  const recovered = snapshot(); recovered.sampled_at_ms = f.now(); recovered.seq = 6;
  recovered.instances[0].return_action.expires_at_ms = f.now() + 10000;
  recovered.project_refs = [{ project_id: 'alpha', instance_ids: ['instance'] }];
  f.reads[2].resolve(recovered); await tick();
  assert.match(f.projectCopy.textContent, /1 个已核验本机实例/);
  assert.doesNotMatch(f.projectCopy.textContent, /旧观察/);
  const button = f.node('native-instances').children[0].children.at(-1);
  assert.equal(button.disabled, false); assert.equal(oldButton.disabled, true);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
  const returning = button.listeners.click();
  const request = f.writes.find(item => item.url.endsWith('/return'));
  request.resolve({ status: 'available', kind: 'observed_instance', reason: null }); await returning;
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 1);
});

test('pending read independently expires the 10 second grant then the 12 second sample and fresh response recovers', async () => {
  const f = fixture(), value = snapshot();
  value.sampled_at_ms = f.now(); value.instances[0].return_action.expires_at_ms = f.now() + 10000;
  value.project_refs = [{ project_id: 'alpha', instance_ids: ['instance'] }];
  f.reads[0].resolve(value); await tick();
  const oldButton = f.node('native-instances').children[0].children.at(-1);
  await f.advance(10000);
  assert.equal(f.reads.length, 2); assert.equal(oldButton.disabled, true);
  assert.doesNotMatch(f.node('native-instances').textContent, /核验并返回/);
  assert.match(f.node('native-instances').textContent, /可用/);
  await f.advance(2000);
  assert.doesNotMatch(f.node('native-instances').textContent, /可用/);
  assert.match(f.node('observation-status').textContent, /过期/);
  assert.match(f.projectCopy.textContent, /旧观察/);
  assert.match(f.node('observation-resource-summary').textContent, /旧观察/);
  await f.advance(8000); assert.equal(f.reads.length, 2);
  const recovered = snapshot(); recovered.sampled_at_ms = f.now(); recovered.seq = 6;
  recovered.instances[0].return_action.expires_at_ms = f.now() + 10000;
  f.reads[1].resolve(recovered); await tick();
  assert.match(f.node('native-instances').textContent, /可用/);
  assert.match(f.node('native-instances').textContent, /核验并返回/);
  assert.doesNotMatch(f.node('observation-resource-summary').textContent, /旧观察/);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
});

test('first pending read becomes unavailable without a sample and delayed stale response cannot restore availability', async () => {
  const f = fixture(); await f.advance(12000);
  assert.match(f.node('observation-status').textContent, /超时|不可用/);
  assert.equal(f.reads.length, 1);
  const expired = snapshot(); expired.sampled_at_ms = f.now() - 20000;
  expired.instances[0].return_action.expires_at_ms = f.now() - 10000;
  f.reads[0].resolve(expired); await tick();
  assert.doesNotMatch(f.node('native-instances').textContent, /可用/);
  assert.doesNotMatch(f.node('native-instances').textContent, /核验并返回/);
});

test('sample expiry dismisses an in-flight picker and discards its late title response', async () => {
  const f = fixture(), value = snapshot(); value.sampled_at_ms = f.now();
  f.reads[0].resolve(value); await tick();
  const opening = pickerButton(f).listeners.click();
  await f.advance(12000);
  f.writes[0].resolve(choices()); await opening;
  assert.equal(f.node('window-choices-list').textContent, '');
  assert.equal(f.node('window-choices-picker').hidden, true);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
});

const settingsSnapshot = (rows = [], revision = 0) => ({ revision, hash: `hash-${revision}`, registrations: rows,
  application: { status: 'pending', reason: 'config_pending' }, validation: [] });
const configuredRow = () => ({ id: registrationId, category: 'custom_exe', provider_id: null, environment_id: 'windows:local',
  label: 'Example host', locator_kind: 'executable', path: 'C:/example/host.exe', enabled: true, binding_state: 'confirmed' });
async function openSettings(f, value = settingsSnapshot()) {
  f.node('registration-settings').open = true;
  f.node('registration-settings').listeners.toggle();
  assert.equal(f.reads.at(-1).url, '/api/profile/observations');
  f.reads.at(-1).resolve(value); await tick();
  assert.equal(f.reads.at(-1).url, '/api/profile/observations/candidates');
  f.reads.at(-1).resolve({ status: 'unavailable', reason: 'native_missing' }); await tick();
}
function editFields(f) {
  f.node('registration-category').value = 'custom_exe';
  f.node('registration-label').value = 'Example host';
  f.node('registration-path').value = 'C:/example/host.exe';
  f.node('registration-confirm').checked = true;
}
const submit = f => f.node('registration-form').listeners.submit({ preventDefault() {} });

test('actual settings form has fixed fields, explicit candidates and offline CAS save revokes old UI trust', async () => {
  for (const id of ['registration-settings', 'registration-form', 'registration-category', 'registration-provider', 'registration-path', 'registration-confirm']) assert.ok(html.includes(`id="${id}"`));
  assert.ok(!html.includes('type="file"'));
  const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
  const oldButton = pickerButton(f);
  await openSettings(f); editFields(f);
  const saving = submit(f);
  assert.deepEqual(f.writes[0].body, { expected_revision: 0, expected_hash: 'hash-0', registrations: [{ ...configuredRow(), id: null }] });
  f.writes[0].resolve(settingsSnapshot([configuredRow()], 1)); await saving;
  assert.equal(oldButton.disabled, true);
  assert.match(f.node('registrations-feedback').textContent, /已保存.*不会结束外部程序/);
  assert.match(f.node('registrations-status').textContent, /等待后台应用/);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
});

test('edit disable delete and rebind use one complete CAS domain without starting or killing tools', async () => {
  const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
  const restored = { ...configuredRow(), binding_state: 'rebind_required' };
  await openSettings(f, settingsSnapshot([restored], 1));
  f.node('registrations-list').children[0].children[3].listeners.click();
  assert.equal(f.node('registration-confirm').checked, false);
  submit(f); assert.equal(f.writes.length, 0);
  f.node('registration-confirm').checked = true;
  const binding = submit(f);
  assert.equal(f.writes[0].body.registrations[0].binding_state, 'confirmed');
  f.writes[0].resolve(settingsSnapshot([configuredRow()], 2)); await binding;
  const disabling = f.node('registrations-list').children[0].children[4].listeners.click();
  assert.equal(f.writes[1].body.registrations[0].enabled, false);
  f.writes[1].resolve(settingsSnapshot([{ ...configuredRow(), enabled: false }], 3)); await disabling;
  const deleting = f.node('registrations-list').children[0].children[5].listeners.click();
  assert.deepEqual(f.writes[2].body.registrations, []);
  f.writes[2].resolve(settingsSnapshot([], 4)); await deleting;
  assert.ok(f.writes.every(item => item.url === '/api/profile/observations'));
});

test('failed save preserves draft, CAS conflict refreshes without overwriting and closing discards candidate paths', async () => {
  const f = fixture(); f.reads[0].resolve(snapshot()); await tick();
  await openSettings(f); editFields(f);
  const failing = submit(f); f.writes[0].reject({ reason: 'storage_error' }); await failing;
  assert.equal(f.node('registration-path').value, 'C:/example/host.exe');
  const conflict = submit(f); f.writes[1].reject({ reason: 'config_conflict' }); await tick();
  f.reads.at(-1).resolve(settingsSnapshot([configuredRow()], 5)); await conflict;
  assert.match(f.node('registrations-feedback').textContent, /已刷新列表.*重新填写/);
  assert.equal(f.node('registration-path').value, 'C:/example/host.exe');
  f.node('registration-settings').open = false; f.node('registration-settings').listeners.toggle();
  assert.equal(f.node('registration-path').value, '');
  assert.equal(f.node('registrations-list').textContent, '');
  assert.equal(f.node('registration-candidates').textContent, '');
});

test('config pair changes clear picker and separate custom program processes from Agent facts', async () => {
  const f = fixture(), value = snapshot();
  value.instances[0].classification = 'custom_exe'; value.instances[0].provider_id = null; value.instances[0].kind = 'custom_exe';
  f.reads[0].resolve(value); await tick();
  assert.match(f.node('native-instances').textContent, /自定义程序进程.*不提供 Agent\/会话\/审批语义/);
  const opening = pickerButton(f).listeners.click(); f.writes[0].resolve(choices()); await opening;
  await f.advance(2000);
  f.reads[1].resolve({ ...snapshot(), config_revision: 2, config_hash: 'other' }); await tick();
  assert.equal(f.node('window-choices-list').textContent, '');
  assert.equal(f.node('window-choices-picker').hidden, true);
  assert.equal(f.writes.filter(item => item.url.endsWith('/return')).length, 0);
});
