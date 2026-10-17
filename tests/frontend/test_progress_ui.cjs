// Pure DOM and controlled-promise regression; no browser, server, or real sources.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const sourcePath = process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, 'web/progress/progress.js');
const source = fs.readFileSync(sourcePath, 'utf8').replace(/load\(\)\.catch\(\(\) => \{ statusLine\.textContent = "工作台没有读出来。"; \}\);\s*$/, '');
class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.dataset = {}; this.value = ''; this.disabled = false; this.text = '';
    this.style = { values: {}, setProperty(key, value) { this.values[key] = value; }, removeProperty(key) { delete this.values[key]; } };
    this.classList = { add() {}, remove() {} };
  }
  append(...nodes) { for (const node of nodes) { if (node?.parent) node.parent.children = node.parent.children.filter(child => child !== node); if (node && typeof node === 'object') node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.text = ''; this.append(...nodes); }
  insertBefore(node, before) { if (node.parent) node.parent.children = node.parent.children.filter(child => child !== node); node.parent = this; this.children.splice(this.children.indexOf(before), 0, node); }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener(key, callback) { this.listeners[key] = callback; }
  get textContent() { return this.text + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); }
  set textContent(value) { this.text = String(value); this.children = []; }
}
function fixture() {
  const nodes = new Map(), requests = [];
  const node = selector => { if (!nodes.has(selector)) nodes.set(selector, new Element()); return nodes.get(selector); };
  const context = vm.createContext({ document: { querySelector: node, createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag), createTextNode: text => text }, window: { matchMedia: () => ({ matches: true }) }, location: { origin: 'http://127.0.0.1:14401' }, Intl, Date, Number, console,
    fetch: url => new Promise((resolve, reject) => requests.push({ url, resolve, reject })) });
  vm.runInContext(source, context);
  const run = value => vm.runInContext(value, context);
  run(`profileMode=true; csrf='fixture'; projects=[{id:'archived',name:'Archived',archived:true,state:null,percent:null,note:'',records:[],sessions:[]},{id:'active',name:'Active',archived:false,state:null,percent:null,note:'',records:[],sessions:[]}]; selected='archived'; ensureStage(); paintSide({todos:[],todos_total:0,news:[]}); paintRail(); paintBoard();`);
  return { nodes, requests, node, run, context };
}
const cases = [];
async function test(name, callback) { try { await callback(); cases.push({ name, passed: true }); } catch (error) { cases.push({ name, passed: false, error: error.stack }); } }
const response = (note, total = 41) => ({ ok: true, json: async () => ({ items: [{ recorded_at: null, origin: 'manual', state: '在做', percent: null, note }], total }) });
function clickProject(f, surface, index) { f.node(surface).children[index].listeners.click(); }
function todoDisabled(f) { return f.node('#todo-zone').children[0].children[0].disabled; }
function historyText(f) { return f.run('built.history.textContent'); }
async function historyOrder(firstId, secondId, firstOffset, secondOffset, firstReturnsFirst) {
  const f = fixture();
  f.run(`selected='${firstId}'; historyOffset=${firstOffset}; var firstHistory=loadHistory(); selected='${secondId}'; historyOffset=${secondOffset}; var secondHistory=loadHistory();`);
  const newer = response(`${secondId.toUpperCase()} PAGE ${secondOffset}`, 62), older = response(`${firstId.toUpperCase()} PAGE ${firstOffset}`, 41);
  if (firstReturnsFirst) { f.requests[0].resolve(older); await f.run('firstHistory'); assert.ok(!historyText(f).includes(`${firstId.toUpperCase()} PAGE ${firstOffset}`), 'superseded response must not replace loading state'); f.requests[1].resolve(newer); await f.run('secondHistory'); }
  else { f.requests[1].resolve(newer); await f.run('secondHistory'); f.requests[0].resolve(older); await f.run('firstHistory'); }
  assert.ok(historyText(f).includes(`${secondId.toUpperCase()} PAGE ${secondOffset}`));
  assert.ok(historyText(f).includes(`${secondOffset + 1}–${Math.min(secondOffset + 20, 62)} / 62`), 'pagination belongs to the requested page and response');
  assert.equal(new URL(f.requests[0].url, 'http://fixture').searchParams.get('project_id'), firstId);
  assert.equal(new URL(f.requests[1].url, 'http://fixture').searchParams.get('offset'), String(secondOffset));
}
(async () => {
  await test('profile progress redirects to the sole workspace manual editor; legacy resources remain mapped', () => {
    const gateway = fs.readFileSync(path.join(root, 'session_panel/http/gateway.py'), 'utf8');
    assert.match(gateway, /path\.path == "\/progress"[^\n]*self\.profile\(\) is not None/);
    assert.ok(gateway.includes('self.send_header("Location", "/workspace#projects")'));
    for (const resource of ['/progress.js', '/progress.css']) assert.ok(gateway.includes(`"${resource}"`));
    const html = fs.readFileSync(path.join(root, 'web/workspace/workspace.html'), 'utf8');
    assert.equal((html.match(/id="manual-form"/g) || []).length, 1);
  });
  for (const [surface, label] of [['#progress-rail', 'rail'], ['#progress-board', 'board']]) await test(`${label}: archived -> active -> archived -> active`, () => {
    const f = fixture(); assert.equal(todoDisabled(f), true);
    for (const [index, expected] of [[1, false], [0, true], [1, false]]) { clickProject(f, surface, index); assert.equal(todoDisabled(f), expected); assert.equal(f.node('#todo-zone').children[0].children[1].disabled, expected); }
  });
  await test('legacy selection leaves original launcher behavior available', () => { const f = fixture(); f.run('profileMode=false; paintSide({todos:[],news:[],launchers:[]}); paintRail(); paintBoard();'); clickProject(f, '#progress-rail', 1); assert.equal(todoDisabled(f), false); assert.equal(f.node('#launch-zone').children.length, 4); });
  await test('unknown rail uses explicit text and no numeric fill', () => { const f = fixture(), row = f.node('#progress-rail').children[0]; assert.equal(row.children[1].children[1].textContent, '未记录 · 无记录'); assert.equal(row.children[2].children[0].style.values['--fill'], undefined); assert.equal(row.children[2].children[0].hidden, true); });
  await test('zero rail remains genuine numeric progress', () => { const f = fixture(); f.run("projects[0].percent=0; projects[0].state='未开始'; paintRail(); showProject(false);"); assert.equal(f.node('#progress-rail').children[0].children[2].children[0].style.values['--fill'], '0%'); assert.equal(f.run('built.label.textContent'), '0'); });
  await test('A slow / B fast keeps B history', () => historyOrder('archived', 'active', 0, 0, false));
  await test('A fast / B slow keeps loading until B arrives', () => historyOrder('archived', 'active', 0, 0, true));
  await test('B slow / A fast keeps A history', () => historyOrder('active', 'archived', 0, 0, false));
  await test('same project page 0 slow / page 20 fast', () => historyOrder('active', 'active', 0, 20, false));
  await test('same project page 20 slow / page 0 fast', () => historyOrder('active', 'active', 20, 0, false));
  await test('same project and page newer refresh wins', async () => { const f = fixture(); f.run("selected='active'; var firstHistory=loadHistory(); var secondHistory=loadHistory();"); f.requests[1].resolve(response('NEW VALUE')); await f.run('secondHistory'); f.requests[0].resolve(response('OLD VALUE')); await f.run('firstHistory'); assert.ok(historyText(f).includes('NEW VALUE')); assert.ok(!historyText(f).includes('OLD VALUE')); });
  await test('stale failed request cannot overwrite current success', async () => { const f = fixture(); f.run("selected='archived'; var firstHistory=loadHistory(); selected='active'; var secondHistory=loadHistory();"); f.requests[1].resolve(response('ACTIVE SUCCESS')); await f.run('secondHistory'); f.requests[0].reject(new Error('old request failure')); await f.run('firstHistory'); assert.ok(historyText(f).includes('ACTIVE SUCCESS')); });
  await test('current failed request still reports failure', async () => { const f = fixture(); f.run("selected='active'; var firstHistory=loadHistory();"); f.requests[0].reject(new Error('current request failure')); await assert.rejects(f.run('firstHistory'), /current request failure/); });
  await test('legacy todo read and write complete while independent news is pending; late news preserves the draft', async () => {
    const f = fixture(), turn = () => new Promise(resolve => setImmediate(resolve));
    f.context.window.WorkspaceNews = { startLegacy({ fetchJson }) { void fetchJson('/api/desk/news', {}).then(() => { f.node('#news-zone').textContent = 'late news only'; }); } };
    f.run('var loading=load();');
    f.requests[0].resolve({ ok: true, json: async () => ({ csrf_token: 'synthetic', capabilities: {} }) }); await turn();
    f.requests[1].resolve({ ok: true, json: async () => ({ projects: [{ id: 'active', name: 'Synthetic', state: '在做', percent: null, note: '', records: [], sessions: [] }] }) }); await turn();
    assert.equal(f.requests[2].url, '/api/desk/news'); assert.equal(f.requests[3].url, '/api/desk');
    f.requests[3].resolve({ ok: true, json: async () => ({ todos: [{ id: 'one', text: 'local todo', done: false }], launchers: [] }) }); await f.run('loading');
    assert.match(f.node('#todo-zone').textContent, /local todo/);
    f.run("var savingTodo=createTodo('new local todo','active');");
    f.requests[4].resolve({ ok: true }); await turn();
    assert.equal(f.requests[5].url, '/api/desk'); f.requests[5].resolve({ ok: true, json: async () => ({ todos: [{ id: 'two', text: 'saved todo', done: false }], launchers: [] }) }); await f.run('savingTodo');
    assert.match(f.node('#todo-zone').textContent, /saved todo/);
    const input = f.node('#todo-zone').children[0].children[0]; input.value = 'unfinished draft';
    f.requests[2].resolve({ ok: true, json: async () => ({}) }); await turn();
    assert.equal(f.node('#todo-zone').children[0].children[0], input); assert.equal(input.value, 'unfinished draft');
    assert.equal(f.requests.filter(row => row.url === '/api/desk/news').length, 1);
  });
  console.log(JSON.stringify({ scope: 'pure DOM / controlled promises only', source: sourcePath, cases, failures: cases.filter(item => !item.passed).length }, null, 2)); process.exitCode = cases.some(item => !item.passed) ? 1 : 0;
})();
