// Actual workspace history functions with DOM/promise substitutes; no network or browser.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../..');
const sourcePath = path.resolve(process.argv[2] || path.join(root, 'web/workspace/workspace.js'));
const source = fs.readFileSync(sourcePath, 'utf8');
const names = ['el', 'message', 'action', 'pager', 'openManual', 'loadHistory', 'saveManual'];
const functions = names.map(name => { const match = source.match(new RegExp(`(?:async )?function ${name}\\([^]*?\\n\\}`)); assert.ok(match, name); return match[0]; }).join('\n');
class Element {
  constructor() { this.children = []; this.listeners = {}; this.value = ''; this.text = ''; this.classList = { toggle() {} }; }
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
  console.log(JSON.stringify({ scope: 'actual workspace functions / controlled promises only', source: path.relative(process.cwd(), sourcePath), cases, failures: cases.filter(row => !row.passed).length }, null, 2)); process.exitCode = cases.some(row => !row.passed) ? 1 : 0;
})();
