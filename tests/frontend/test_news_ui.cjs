// Actual generated ordinary script, synthetic DOM/clock and controlled promises only.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const assert = require('node:assert/strict');
const repo = path.resolve(__dirname, '../..');
const html = fs.readFileSync(path.join(repo, 'web/workspace/workspace.html'), 'utf8');
const legacyHtml = fs.readFileSync(path.join(repo, 'web/progress/progress.html'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.dataset = {}; this.text = ''; this.value = ''; this.disabled = false; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ''; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(''); }
  set innerHTML(_value) { throw new Error('HTML sink forbidden'); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
function fixture(options = {}) {
  const source = fs.readFileSync(path.join(repo, 'web/workspace/news.js'), 'utf8');
  const tags = new Map([...(options.legacy ? legacyHtml : html).matchAll(/<([a-z][a-z0-9-]*)\b[^>]*\bid="([^"]+)"/g)].map(row => [row[2], row[1]]));
  const nodes = new Map(), reads = [], writes = [], timers = new Map(), events = {}, documentEvents = {};
  let wall = Date.parse(options.now || '2026-10-17T00:30:00Z'), monotonic = 0, timerId = 0, zone = options.zone || 'Asia/Shanghai';
  const node = id => { if (!nodes.has(id)) nodes.set(id, new Element(options.wrongTag === id ? 'span' : tags.get(id) || 'div')); return nodes.get(id); };
  const document = { hidden: false, getElementById: id => options.missing === id ? null : node(id), createElement: tag => new Element(tag), addEventListener: (name, callback) => { documentEvents[name] = callback; } };
  const window = { addEventListener: (name, callback) => { events[name] = callback; } };
  const context = vm.createContext({ document, window, URL, AbortController, console,
    Date: class extends Date { constructor(...args) { super(...(args.length ? args : [wall])); } static now() { return wall; } },
    Intl: { DateTimeFormat: class { constructor(locale, settings) { if (zone === 'unavailable') throw new Error('no zone'); return new Intl.DateTimeFormat(locale, { ...settings, timeZone: settings?.timeZone || zone }); } } },
    performance: { now: () => monotonic },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay, due: monotonic + delay }); return id; }, clearTimeout: id => timers.delete(id) });
  vm.runInContext(source, context);
  const fetchJson = (url, config) => new Promise((resolve, reject) => reads.push({ url, signal: config?.signal, resolve, reject }));
  if (options.legacy) window.WorkspaceNews.startLegacy({ fetchJson });
  else window.WorkspaceNews.startWorkspace({ fetchJson, postJson: (url, body, config) => new Promise((resolve, reject) => writes.push({ url, body: JSON.parse(JSON.stringify(body)), signal: config?.signal, resolve, reject })) });
  const all = root => [root, ...root.children.flatMap(all)];
  async function advance(milliseconds) {
    const end = monotonic + milliseconds;
    while (true) {
      const next = [...timers.entries()].filter(([_id, timer]) => timer.due <= end).sort((a, b) => a[1].due - b[1].due)[0];
      if (!next) break;
      timers.delete(next[0]); wall += next[1].due - monotonic; monotonic = next[1].due; next[1].callback(); await tick();
    }
    wall += end - monotonic; monotonic = end; await tick();
  }
  return { node, reads, writes, timers, window, document, events, documentEvents, advance,
    zone(value) { zone = value; }, wall(value) { wall = Date.parse(value); }, now: () => wall,
    cards: id => all(node(id)).filter(row => row.dataset.newsId), all: id => all(node(id)),
    render() { window.WorkspaceNews.onView('news'); } };
}
const item = (fields = {}) => ({ id: 'synthetic-story', source_id: 'openai', source_name: 'Synthetic source', title: 'Synthetic AI', url: 'https://example.com/story',
  published_at: '2026-10-17T00:00:00+00:00', fetched_at: '2026-10-17T00:20:00+00:00', published_raw: 'Sat, 17 Oct 2026 00:00:00 GMT', date_reason: null,
  updated_at: null, submitted_at: null, content_origin: 'publisher_feed', summary: 'Source text', summary_kind: 'rss_description', summary_truncated: false, summary_reason: null, title_truncated: false, ...fields });
function envelope(items = [item()], fields = {}) {
  const stream = fields.stream || 'ai';
  const sourceIds = stream === 'ai' ? ['google_ai', 'openai', 'huggingface'] : ['hn'];
  return { schema: 1, stream, status: 'available', reason: null, sampled_at: '2026-10-17T00:20:00+00:00', items,
    sources: sourceIds.map(id => { const length = items.filter(row => row.source_id === id).length; return { id, name: id, url: 'https://example.com/feed', status: 'available', reason: null,
      sampled_at: '2026-10-17T00:20:00+00:00', last_success_at: '2026-10-17T00:20:00+00:00', item_count: length, cache_age_ms: 0, fresh_for_ms: 900000,
      scanned_count: length, valid_count: length, filtered_count: 0, duplicate_count: 0, returned_count: length, truncated: false, format: stream === 'ai' ? 'rss2' : 'hn' }; }),
    refresh: { state: 'idle', reason: null, retry_after_ms: 0 }, ...fields };
}
async function deliver(f, value, index = f.reads.length - 1) { f.reads[index].resolve(value); await tick(); }
test('public scripts keep four original relative positions and both news consumers', () => {
  const scripts = [...html.matchAll(/<script src="([^"]+)" defer/g)].map(row => row[1]);
  assert.deepEqual(scripts, ['/execution.js', '/observation.js', '/task-center.js', '/news.js', '/quota.js', '/workspace.js']);
  assert.match(legacyHtml, /src="\/news.js" defer[^]*src="\/progress.js" defer/);
  const workspace = fs.readFileSync(path.join(repo, 'web/workspace/workspace.js'), 'utf8');
  assert.match(workspace, /WorkspaceNews\?\.startWorkspace\(\{ fetchJson: api, postJson: post \}\)/);
  assert.ok(!/function (?:renderNews|loadNews)\(/.test(workspace)); assert.ok(!workspace.includes('loadUsage(), loadNews()'));
  const progress = fs.readFileSync(path.join(repo, 'web/progress/progress.js'), 'utf8');
  assert.match(progress, /WorkspaceNews\?\.startLegacy/); assert.ok(!progress.includes('newsZone.replaceChildren'));
});
test('today preview shows only two actual local today cards; full four groups retain all returned facts', async () => {
  const f = fixture(); const rows = [item(), item({ id: 'two' }), item({ id: 'three' }), item({ id: 'earlier', published_at: '2026-10-15T00:00:00Z' }), item({ id: 'unknown', published_at: null, date_reason: 'missing' }), item({ id: 'future', published_at: '2026-10-17T10:00:00Z' })];
  await deliver(f, envelope(rows)); assert.equal(f.cards('news-preview').length, 2); assert.equal(f.cards('news-list').length, 6);
  assert.deepEqual(f.all('news-list').filter(node => node.dataset.newsGroup).map(node => node.dataset.newsGroup), ['today', 'earlier', 'unknown', 'future']);
  assert.match(f.node('news-preview').textContent, /Asia\/Shanghai/); assert.ok(!f.node('news-preview').textContent.includes('来源标为未来'));
});
test('year boundary and local zone changes immediately reclassify without a new network response', async () => {
  const f = fixture({ now: '2026-01-01T00:30:00Z', zone: 'America/Los_Angeles' });
  await deliver(f, envelope([item({ published_at: '2025-12-31T10:00:00Z' })])); assert.equal(f.cards('news-preview').length, 1);
  f.zone('Asia/Shanghai'); f.render(); assert.equal(f.cards('news-preview').length, 0); assert.match(f.node('news-preview').textContent, /2026-01-01/); assert.equal(f.reads.length, 1);
});
test('midnight regrouping and cache expiry happen offline; backward wall clock cannot renew freshness', async () => {
  const f = fixture({ now: '2026-10-17T15:59:30Z' }); await deliver(f, envelope([item({ published_at: '2026-10-17T01:00:00Z' })]));
  assert.equal(f.cards('news-preview').length, 1); await f.advance(60000); assert.equal(f.cards('news-preview').length, 0);
  f.wall('2026-10-16T00:00:00Z'); await f.advance(900000); assert.ok(f.cards('news-list').every(node => node.dataset.freshness === 'stale')); assert.match(f.node('news-status').textContent, /陈旧/); assert.equal(f.reads.length, 1);
});
test('no today is limited to returned window; empty, unknown, updated and truncated stay explicit', async () => {
  const f = fixture(); const value = envelope([item({ published_at: null, date_reason: 'timezone_unknown', updated_at: '2026-10-17T00:10:00Z', summary: null, summary_kind: null })]); value.sources[1].truncated = true; value.sources[1].valid_count = 21;
  await deliver(f, value); assert.match(f.node('news-preview').textContent, /本次已返回的文章中未见今日内容/); assert.match(f.node('news-preview').textContent, /截断.*不代表全网/);
  assert.match(f.node('news-list').textContent, /来源更新.*不作出版时间/); assert.ok(!f.node('news-list').textContent.includes('来源摘要：'));
  const empty = fixture(); await deliver(empty, envelope([])); assert.match(empty.node('news-list').textContent, /本次源响应为空/);
});
test('unavailable local time zone never falls back to UTC today', async () => {
  const f = fixture({ zone: 'unavailable' }); await deliver(f, envelope()); assert.equal(f.cards('news-preview').length, 0); assert.match(f.node('news-list').textContent, /无法判定本地日期/);
});
test('source title and summary are literal text; unsafe external links are non-clickable', async () => {
  const f = fixture(); await deliver(f, envelope([item({ title: '<img onerror=attack()>', summary: '<script>literal source text</script>', summary_truncated: true, title_truncated: true, url: 'https://127.0.0.1/private' })]));
  assert.match(f.node('news-list').textContent, /<img onerror=attack\(\)>/); assert.match(f.node('news-list').textContent, /来源摘要节选：<script>/); assert.match(f.node('news-list').textContent, /标题节选/);
  assert.equal(f.all('news-list').filter(node => node.tagName === 'A').length, 0);
  const safe = fixture(); await deliver(safe, envelope()); const link = safe.all('news-list').find(node => node.tagName === 'A'); assert.equal(link.target, '_blank'); assert.equal(link.rel, 'noopener noreferrer');
});
test('first hanging request is bounded by 20 seconds, aborts and discards late completion', async () => {
  const f = fixture(); assert.equal(f.reads.length, 1); await f.advance(20000); assert.equal(f.reads[0].signal.aborted, true); assert.equal(f.node('refresh-news').disabled, false);
  assert.match(f.node('news-status').textContent, /仍未取得完成结果/); await deliver(f, envelope()); assert.equal(f.cards('news-list').length, 0); await f.advance(120000); assert.equal(f.reads.length, 1);
});
test('pending polls are single in-flight, max ten reads, then visibly stop; explicit refresh recovers once', async () => {
  const f = fixture();
  for (let i = 0; i < 10; i++) { assert.equal(f.reads.length, i + 1); await deliver(f, envelope([], { status: 'pending', refresh: { state: 'pending', reason: 'refreshing', retry_after_ms: 0 } })); await f.advance(2000); }
  assert.equal(f.reads.length, 10); assert.match(f.node('news-status').textContent, /仍未取得完成结果/);
  f.node('refresh-news').listeners.click(); assert.equal(f.writes.length, 1); assert.deepEqual(f.writes[0].body, { stream: 'ai' });
  f.writes[0].resolve(envelope()); await tick(); assert.equal(f.cards('news-preview').length, 1); await f.advance(10000); assert.equal(f.writes.length, 1);
});
test('read failure keeps old cards visibly untrusted and every bad response fails closed', async () => {
  const mutations = [value => { delete value.items[0].summary; }, value => { value.sources[0].cache_age_ms = NaN; }, value => { value.refresh.retry_after_ms = -1; }, value => { value.sources[1].item_count = 2; }, value => { value.items[0].published_at = '2026-02-30T00:00:00Z'; }, value => { value.stream = 'hn'; }];
  for (const mutate of mutations) {
    const f = fixture(); await deliver(f, envelope()); f.node('refresh-news').listeners.click(); const bad = envelope(); mutate(bad); f.writes[0].resolve(bad); await tick();
    assert.match(f.node('news-status').textContent, /读取失败/); assert.ok(f.cards('news-list').every(node => node.dataset.freshness === 'stale')); assert.equal(f.writes.length, 1);
  }
  const f = fixture(); f.reads[0].reject(new Error('private transport detail')); await tick(); assert.match(f.node('news-status').textContent, /来源不可用/); assert.ok(!f.node('news-status').textContent.includes('private'));
});
test('visibility/pagehide cancel reads and late fields are not consumed; pageshow can recover', async () => {
  const f = fixture(); f.document.hidden = true; f.documentEvents.visibilitychange(); assert.equal(f.reads[0].signal.aborted, true);
  f.reads[0].resolve({ get schema() { throw new Error('late object read'); } }); await tick();
  f.document.hidden = false; f.documentEvents.visibilitychange(); assert.equal(f.reads.length, 2); await deliver(f, envelope());
  f.events.pagehide(); assert.equal(f.timers.size, 0); f.events.pageshow(); assert.equal(f.reads.length, 3); await deliver(f, envelope()); assert.equal(f.cards('news-preview').length, 1);
});
test('HN selection stays separate from AI preview and late old stream cannot replace selection', async () => {
  const f = fixture(); await deliver(f, envelope()); const before = f.node('news-preview').textContent;
  f.node('news-stream').value = 'hn'; f.node('news-stream').listeners.change(); assert.equal(f.reads[1].url, '/api/workspace/news?stream=hn');
  await deliver(f, envelope([item({ source_id: 'hn', source_name: 'Hacker News', content_origin: 'hn_submission', published_at: null, submitted_at: '2026-10-17T00:00:00Z', summary: null, summary_kind: null })], { stream: 'hn' }));
  assert.match(f.node('news-list').textContent, /文章发布时间未知.*HN收录/); assert.equal(f.node('news-preview').textContent, before);
  f.node('news-stream').value = 'ai'; f.node('news-stream').listeners.change(); const late = f.reads[2]; f.node('news-stream').value = 'hn'; f.node('news-stream').listeners.change();
  late.resolve(envelope([item({ title: 'LATE AI' })])); await tick(); assert.ok(!f.node('news-list').textContent.includes('LATE AI')); assert.equal(late.signal.aborted, true);
});
test('HN failure cannot mark an independently valid AI home preview untrusted', async () => {
  const f = fixture(); await deliver(f, envelope()); const before = f.node('news-preview').textContent;
  f.node('news-stream').value = 'hn'; f.node('news-stream').listeners.change(); await deliver(f, envelope([], { stream: 'hn' }));
  f.node('refresh-news').listeners.click(); f.writes[0].reject(new Error('synthetic HN failure')); await tick();
  assert.match(f.node('news-status').textContent, /读取失败/); assert.equal(f.node('news-preview').textContent, before);
  assert.ok(f.cards('news-preview').every(node => node.dataset.freshness === 'available'));
});
test('refresh pending and cooldown/busy limits remain visible without automatic POST replay', async () => {
  const f = fixture(); await deliver(f, envelope()); f.node('refresh-news').listeners.click();
  f.writes[0].resolve(envelope([], { status: 'stale', refresh: { state: 'idle', reason: 'cooldown', retry_after_ms: 60000 } })); await tick();
  assert.match(f.node('news-status').textContent, /刷新间隔.*60秒/); await f.advance(120000); assert.equal(f.writes.length, 1); assert.equal(f.reads.length, 1);
});
test('legacy uses only the independent HN GET and never owns todo or launcher DOM', async () => {
  const f = fixture({ legacy: true }); assert.equal(f.reads[0].url, '/api/desk/news');
  const draft = f.node('todo-zone'); draft.value = 'kept draft';
  await deliver(f, envelope([], { stream: 'hn' })); assert.equal(draft.value, 'kept draft'); assert.match(f.node('legacy-news-sources').textContent, /非AI筛选/); assert.equal(f.writes.length, 0);
});
test('all 17 item and 17 source facts are checked from unknown, including nullable fields', async () => {
  const valid = envelope();
  assert.equal(Object.keys(valid.items[0]).length, 17); assert.equal(Object.keys(valid.sources[0]).length, 17);
  for (const [array, sample] of [['items', valid.items[0]], ['sources', valid.sources[0]]]) for (const key of Object.keys(sample)) {
    const f = fixture(), bad = envelope(); delete bad[array][0][key]; await deliver(f, bad);
    assert.match(f.node('news-status').textContent, /来源不可用/, `${array}.${key}`); assert.equal(f.cards('news-list').length, 0);
  }
});
test('hanging explicit refresh is aborted at watchdog and never replayed or late-applied', async () => {
  const f = fixture(); await deliver(f, envelope()); f.node('refresh-news').listeners.click();
  assert.equal(f.writes.length, 1); await f.advance(20000); assert.equal(f.writes[0].signal.aborted, true);
  f.writes[0].resolve(envelope([item({ title: 'late refresh' })])); await tick();
  assert.ok(!f.node('news-list').textContent.includes('late refresh')); assert.match(f.node('news-status').textContent, /仍未取得/); await f.advance(60000); assert.equal(f.writes.length, 1);
});
test('actual workspace transport forwards news cancellation while keeping 40 second and CSRF defaults', async () => {
  const source = fs.readFileSync(path.join(repo, 'web/workspace/workspace.js'), 'utf8'), calls = [];
  const sandbox = { document: {}, AbortController, setTimeout, clearTimeout, Error, SyntaxError, TypeError,
    fetch: (url, options) => new Promise((_resolve, reject) => { calls.push({ url, options }); options.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), { once: true }); }) };
  vm.createContext(sandbox); vm.runInContext(source.slice(0, source.indexOf('\nfunction showView')) + '\nthis.api=api;this.post=post;this.app=app;', sandbox);
  assert.match(source, /controller\.abort\(\), 40000/);
  const read = new AbortController(), pending = sandbox.api('/api/workspace/news', { signal: read.signal }); read.abort();
  await assert.rejects(pending, error => error.reason === 'timeout'); assert.equal(calls[0].options.signal.aborted, true);
  sandbox.app.csrf = 'synthetic'; const write = new AbortController(), saving = sandbox.post('/api/workspace/news/refresh', { stream: 'ai' }, { signal: write.signal });
  assert.equal(calls[1].options.headers['X-Session-CSRF'], 'synthetic'); assert.equal(calls[1].options.method, 'POST'); write.abort(); await assert.rejects(saving, error => error.reason === 'timeout');
});
test('all fixed DOM ids check actual HTML tags and reject missing/wrong elements', () => {
  for (const id of ['news-list', 'news-sources', 'news-status', 'news-preview', 'refresh-news', 'news-stream']) {
    assert.throws(() => fixture({ missing: id }), /新闻页面元素/); assert.throws(() => fixture({ wrongTag: id }), /新闻页面元素/);
  }
  for (const id of ['news-zone', 'legacy-news-sources', 'legacy-news-status']) assert.throws(() => fixture({ legacy: true, missing: id }), /新闻页面元素/);
});
