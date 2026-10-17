// Actual workspace history functions with DOM/promise substitutes; no network or browser.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const sourcePath = path.resolve(process.argv[2] || path.join(root, 'web/workspace/workspace.js'));
const source = fs.readFileSync(sourcePath, 'utf8');
const names = ['el', 'message', 'action', 'pager', 'openManual', 'loadHistory', 'saveManual', 'renderIntegrations', 'loadLaunchers'];
const functions = names.map(name => { const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`)); assert.ok(match, name); return match[0]; }).join('\n');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.dataset = {}; this.value = ''; this.text = ''; this.classList = { toggle() {} }; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ''; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
}
function fixture() {
  const nodes = new Map(), requests = [], writes = [];
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = vm.createContext({ document: { createElement: () => new Element() }, $: node,
    api: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })),
    clearFieldErrors: id => { node(id).textContent = ''; }, dateTime: value => value, taskSummary: () => '', observationSummary: () => '',
    post: async (url, body) => { writes.push({ url, body }); }, loadBase: async () => {}, loadCompatibility: async () => {},
    fieldError: (id, error) => { node(id).textContent = error.message; }, console });
  vm.runInContext(source.match(/const app = [^]*?;\r?\n/)[0] + '\n' + functions, context);
  const run = text => vm.runInContext(text, context);
  run("app.profileMode=true; app.csrf='fixture'; app.manualId='a';");
  const open = id => run(`openManual({id:'${id}',name:'Project ${id}',state:'在做',percent:null,note:'draft ${id}',records:[]})`);
  return { node, run, requests, writes, open, context };
}
const response = (offset, note, total = 45) => ({ items: [{ recorded_at: null, origin: 'manual', state: '在做', percent: null, note }], total, offset, limit: 20 });
const tick = () => new Promise(resolve => setImmediate(resolve));
const cases = [];
async function test(name, callback) { try { await callback(); cases.push({ name, passed: true }); } catch (error) { cases.push({ name, passed: false, error: error.stack }); } }
async function race(firstId, secondId, firstOffset, secondOffset, olderFirst = false) {
  const f = fixture(); f.run(`app.manualId='${firstId}'; app.pages.history=${firstOffset}; var older=loadHistory(); app.manualId='${secondId}'; app.pages.history=${secondOffset}; var newer=loadHistory();`);
  if (olderFirst) { f.requests[0].resolve(response(firstOffset, 'OLD')); await f.run('older'); assert.match(f.node('manual-history').textContent, /正在读取/); f.requests[1].resolve(response(secondOffset, 'NEW')); await f.run('newer'); }
  else { f.requests[1].resolve(response(secondOffset, 'NEW')); await f.run('newer'); f.requests[0].resolve(response(firstOffset, 'OLD')); await f.run('older'); }
  assert.match(f.node('manual-history').textContent, /NEW/); assert.ok(!f.node('manual-history').textContent.includes('OLD'));
  assert.match(f.node('history-pager').textContent, new RegExp(`${secondOffset + 1}–${Math.min(secondOffset + 20, 45)} / 45`));
  assert.equal(new URL(f.requests[0].url, 'http://fixture').searchParams.get('offset'), String(firstOffset));
}
(async () => {
  await test('profile snapshot feeds the shared task center once and never dispatches old task/todo reads', async () => {
    const load = source.match(/async function loadBase\([^]*?\n\}/)[0], nodes = new Map(), calls = [];
    const node = id => { if (!nodes.has(id)) nodes.set(id, { value: '', hidden: true, textContent: '' }); return nodes.get(id); };
    const snapshot = { status: 'ok', today: '2026-10-17', sampled_at: null, projects: { status: 'available', items: [] }, tasks: { status: 'available', items: [], total: 0 }, sessions: { available: false, items: [] }, items_page: { items: [], total: 0, offset: 0, limit: 50, list_revision: 1 } };
    const context = vm.createContext({ $: node, api: async url => { calls.push(url); return snapshot; },
      app: { profileMode: true, csrf: 'fixture', taskCenter: { update: value => { assert.equal(value, snapshot); calls.push('task-center'); } } },
      window: {}, dateTime: () => '', message() {}, fillOptions() {}, renderStats() {}, renderProjects() {}, renderProviders() {},
      renderTasks() { throw new Error('legacy tasks dispatched'); }, renderTodos() { throw new Error('legacy todos dispatched'); } });
    vm.runInContext(load, context); await vm.runInContext('loadBase()', context);
    assert.deepEqual(calls, ['/api/workspace', 'task-center']);
    assert.ok(source.includes("if (!app.profileMode) return saveTask(event)"));
  });
  await test('project task distribution uses full supplied counts independently from manual percent', () => {
    const summary = source.match(/function taskSummary\([^]*?\n\}/)[0];
    const context = vm.createContext({ stateNames: { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待验收', done: '完成' } });
    vm.runInContext(summary, context);
    assert.match(vm.runInContext('taskSummary({total:532,todo:500,doing:10,blocked:5,review:7,done:10})', context), /532 条.*待办 500.*完成 10/);
  });
  await test('same project page20 -> reopen page0, late old response ignored', async () => { const f = fixture(); f.run('app.pages.history=20; var older=loadHistory();'); f.open('a'); f.requests[1].resolve(response(0, 'CURRENT')); await tick(); f.requests[0].resolve(response(20, 'STALE')); await f.run('older'); assert.match(f.node('manual-history').textContent, /CURRENT/); assert.ok(!f.node('manual-history').textContent.includes('STALE')); assert.match(f.node('history-pager').textContent, /1–20 \/ 45/); });
  await test('same project page0 slow -> page20 fast', () => race('a', 'a', 0, 20));
  await test('same project page20 slow -> page0 fast', () => race('a', 'a', 20, 0));
  await test('same page newer refresh wins', () => race('a', 'a', 0, 0));
  await test('A slow / B fast', () => race('a', 'b', 0, 0));
  await test('B slow / A fast', () => race('b', 'a', 0, 0));
  await test('older response cannot replace loading for current request', () => race('a', 'b', 0, 0, true));
  await test('loading removes previous history and previous pager', () => { const f = fixture(); f.node('manual-history').textContent = 'OLD'; f.node('history-pager').append(new Element()); f.run('loadHistory();'); assert.match(f.node('manual-history').textContent, /正在读取/); assert.equal(f.node('history-pager').children.length, 0); });
  await test('reopen suppresses old failure feedback', async () => { const f = fixture(); f.open('a'); f.open('a'); f.requests[1].resolve(response(0, 'CURRENT')); await tick(); f.requests[0].reject(new Error('OLD FAILURE')); await tick(); assert.match(f.node('manual-history').textContent, /CURRENT/); assert.equal(f.node('manual-feedback').textContent, ''); });
  await test('current failure remains visible near manual form', async () => { const f = fixture(); f.open('a'); f.requests[0].reject(new Error('CURRENT FAILURE')); await tick(); assert.equal(f.node('manual-feedback').textContent, 'CURRENT FAILURE'); });
  await test('pager resets on current empty history', async () => { const f = fixture(); f.run('var current=loadHistory();'); f.requests[0].resolve({ items: [], total: 0, offset: 0, limit: 20 }); await f.run('current'); assert.match(f.node('manual-history').textContent, /还没有人工历史/); assert.equal(f.node('history-pager').children[0].disabled, true); assert.equal(f.node('history-pager').children[2].disabled, true); });
  await test('history refresh preserves manual draft and NULL/zero save bodies', async () => { const f = fixture(); f.open('a'); f.requests[0].resolve(response(0, 'INITIAL')); await tick(); f.node('manual-note').value = 'edited draft'; f.node('manual-percent').value = ''; f.run('var saving=saveManual({preventDefault(){}});'); await tick(); f.requests[1].resolve(response(0, 'NEW')); await f.run('saving'); assert.equal(f.writes[0].body.percent, null); assert.equal(f.node('manual-note').value, 'edited draft'); f.node('manual-percent').value = '0'; f.run('var savingAgain=saveManual({preventDefault(){}});'); await tick(); f.requests[2].resolve(response(0, 'ZERO')); await f.run('savingAgain'); assert.equal(f.writes[1].body.percent, 0); });
  await test('no-profile pager remains hidden', async () => { const f = fixture(); f.run('app.profileMode=false; var current=loadHistory();'); f.requests[0].resolve(response(0, 'CURRENT')); await f.run('current'); assert.equal(f.node('history-pager').hidden, true); });
  await test('integration summaries expose status and restart without raw settings', () => {
    const f = fixture();
    f.run(`renderIntegrations({data_dir:'C:/example/profile',restart_required:true,integrations:{remote:{status:'configured',reason:'reader_upgrade_recommended'},terminal:{status:'disabled'},workbench:{status:'not_configured'},launchers:{status:'configured'}}});`);
    assert.match(f.node('integration-statuses').textContent, /远端 Claude/); assert.match(f.node('integration-statuses').textContent, /迁移兼容/);
    assert.match(f.node('integration-restart').textContent, /重启/); assert.ok(f.node('integration-import-command').textContent.includes('--data-dir "C:/example/profile"'));
    assert.equal(f.node('integration-statuses').children.length, 4);
  });
  await test('disabled launcher capability makes no desk request', async () => {
    const f = fixture(); await f.run('loadLaunchers()'); assert.equal(f.requests.length, 0); assert.match(f.node('launcher-status').textContent, /未启用/);
  });
  await test('launcher button posts only its registered id and refreshes catalog', async () => {
    const f = fixture(); f.run("app.capabilities={personal_launchers:true}; var loading=loadLaunchers();");
    f.requests[0].resolve({launchers:[{id:'synthetic',group:'tools',name:'Safe <literal>',blurb:'Example',status:'ready',target:'must-not-forward'}]}); await f.run('loading');
    const pending = f.node('configured-launchers').children[0].children[2].listeners.click(); await tick();
    assert.equal(f.writes[0].url, '/api/launch'); assert.deepEqual(JSON.parse(JSON.stringify(f.writes[0].body)), {id:'synthetic'});
    assert.equal(f.requests[1].url, '/api/desk'); f.requests[1].resolve({launchers:[]}); await pending;
  });
  await test('session bootstrap finishes before optional reads and disabled capabilities never poll', async () => {
    const sessionSource = fs.readFileSync(path.join(root,'web/sessions/app.js'),'utf8');
    const calls=[], nodes=new Map(); let release;
    const selected = ['capability','refreshObservations','initializeSessionPage'].map(name => { const match=sessionSource.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`)); assert.ok(match,name); return match[0]; }).join('\n');
    const context=vm.createContext({$:id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},query:()=>new Promise(resolve=>{release=resolve;}),
      renderWorkbench(){},renderCoverage(){},renderSelection(){},refreshCodexThreads:async()=>{calls.push('codex');},refreshSessions:async()=>{calls.push('remote');},refreshWorkbench:async()=>{calls.push('workbench');}});
    vm.runInContext(sessionSource.match(/const state = [^]*?;\r?\n/)[0]+'\n'+selected,context);
    const initializing=vm.runInContext('initializeSessionPage()',context); assert.deepEqual(calls,[]);
    release({csrf_token:'fixture',capabilities:{profile:true,remote_sessions:false,workbench:false}}); await initializing;
    assert.deepEqual(calls,['codex']); await vm.runInContext('refreshObservations()',context); assert.deepEqual(calls,['codex','codex']);
    calls.length=0; vm.runInContext('state.capabilities={remote_sessions:true,workbench:true};',context); await vm.runInContext('refreshObservations()',context);
    assert.deepEqual(calls,['codex','remote','workbench']);
  });
  console.log(JSON.stringify({ scope: 'actual workspace functions / controlled promises only', source: path.relative(process.cwd(), sourcePath), cases, failures: cases.filter(row => !row.passed).length }, null, 2)); process.exitCode = cases.some(row => !row.passed) ? 1 : 0;
})();
