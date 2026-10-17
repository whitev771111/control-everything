// Root-only Edge harness; an owned loopback service must already be running.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const options = {};
for (let i = 2; i < process.argv.length; i++) {
  const key = process.argv[i];
  assert.ok(['--base-url', '--legacy-base-url', '--output', '--owned-fixture', '--synthetic-news'].includes(key) && !options[key], `Unknown/duplicate option ${key}`);
  options[key] = ['--owned-fixture', '--synthetic-news'].includes(key) ? true : process.argv[++i];
}
assert.ok(options['--owned-fixture'] && options['--base-url'] && path.isAbsolute(options['--output'] || ''), 'Pass --owned-fixture --base-url <loopback origin> --output <absolute ignored directory>.');
function origin(value) { const url = new URL(value); assert.ok(url.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname) && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash); return url.origin; }
const base = origin(options['--base-url']), legacy = options['--legacy-base-url'] ? origin(options['--legacy-base-url']) : null;
const output = options['--output'];
assert.ok(!fs.existsSync(path.join(output, 'result.json')), 'Use a fresh evidence directory.');
const report = { mode: options['--synthetic-news'] ? 'real static HTTP with synthetic news routes' : 'owned HTTP news reads', checks: [], failures: [], screenshots: [], requests: [], private_assets: false, model_verified: false };
function check(label, value) { report.checks.push({ label, passed: Boolean(value) }); assert.ok(value, label); }
function fixture(stream) {
  const now = Date.now(), time = delta => new Date(now + delta).toISOString();
  const row = (id, published, extra = {}) => ({ id, source_id: stream === 'hn' ? 'hn' : 'openai', source_name: stream === 'hn' ? 'Hacker News' : 'Synthetic source',
    title: id === 'today-one' ? '<img onerror=attack()> 合成标题' : `合成 ${id}`, url: 'https://example.com/story', published_at: published, fetched_at: time(0), published_raw: null,
    date_reason: published ? null : 'missing', updated_at: null, submitted_at: null, content_origin: 'publisher_feed', summary: '<b>来源原样文字</b>', summary_kind: 'rss_description', summary_truncated: false, summary_reason: null, title_truncated: false, ...extra });
  const items = stream === 'hn' ? [row('hn-one', null, { content_origin: 'hn_submission', summary: null, summary_kind: null, submitted_at: time(-60000) })] :
    [row('today-one', time(-1000)), row('today-two', time(-2000)), row('today-three', time(-3000)), row('old', time(-172800000)), row('unknown', null, { updated_at: time(-10000) }), row('future', time(86400000))];
  return { schema: 1, stream, status: 'available', reason: null, sampled_at: time(0), items, sources: (stream === 'hn' ? ['hn'] : ['google_ai', 'openai', 'huggingface']).map(id => {
    const length = items.filter(item => item.source_id === id).length;
    return { id, name: id, url: 'https://example.com/feed', status: 'available', reason: null, sampled_at: time(0), last_success_at: time(0), item_count: length, cache_age_ms: 0, fresh_for_ms: 900000,
      scanned_count: length, valid_count: length, filtered_count: 0, duplicate_count: 0, returned_count: length, truncated: false, format: stream === 'hn' ? 'hn' : 'rss2' };
  }), refresh: { state: 'idle', reason: null, retry_after_ms: 0 } };
}
let browser;
(async () => {
  fs.mkdirSync(output, { recursive: true });
  browser = await require('playwright').chromium.launch({ channel: 'msedge', headless: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 }, timezoneId: 'Asia/Shanghai', serviceWorkers: 'block' });
    await context.addInitScript(() => { globalThis.__newsShift = 0; const original = Date.now; Date.now = () => original() + globalThis.__newsShift; });
    let releaseNews; const held = new Promise(resolve => { releaseNews = resolve; }); let first = true;
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (![base, legacy].includes(url.origin)) { report.failures.push('External browser request blocked'); return route.abort(); }
      report.requests.push({ width, method: request.method(), path: url.pathname });
      if (!options['--synthetic-news'] || !['/api/workspace/news', '/api/workspace/news/refresh', '/api/desk/news'].includes(url.pathname)) return route.continue();
      if (first && url.pathname === '/api/workspace/news') { first = false; await held; }
      const stream = url.pathname === '/api/desk/news' || url.searchParams.get('stream') === 'hn' || request.method() === 'POST' && request.postDataJSON().stream === 'hn' ? 'hn' : 'ai';
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(fixture(stream)) });
    });
    const page = await context.newPage(), errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/workspace', { waitUntil: 'domcontentloaded' });
    await page.locator('#item-quick-title').waitFor();
    if (options['--synthetic-news']) {
      await page.locator('#item-quick-title').fill('新闻等待期间的未提交草稿');
      check(`${width}: task input usable while news pending`, await page.locator('#item-quick-title').inputValue() === '新闻等待期间的未提交草稿');
      releaseNews(); await page.locator('#news-preview [data-news-id]').first().waitFor();
      check(`${width}: later news retains task draft`, await page.locator('#item-quick-title').inputValue() === '新闻等待期间的未提交草稿');
      check(`${width}: today preview exactly two`, await page.locator('#news-preview [data-news-id]').count() === 2);
    }
    await page.locator('[data-view="news"]').click();
    await page.waitForFunction(() => !document.getElementById('news-status').textContent.includes('正在读取公开来源'));
    if (options['--synthetic-news']) {
      check(`${width}: complete returned list and four date groups`, await page.locator('#news-list [data-news-id]').count() === 6 && await page.locator('#news-list [data-news-group]').count() === 4);
      check(`${width}: source summary literal, no content HTML`, (await page.locator('#news-list').textContent()).includes('<b>来源原样文字</b>') && await page.locator('#news-list img,#news-list script,#news-list b').count() === 0);
      await page.evaluate(() => { globalThis.__newsShift = 901000; window.WorkspaceNews.onView('news'); });
      check(`${width}: expired offline cards explicitly stale`, await page.locator('#news-list [data-news-id][data-freshness="stale"]').count() === 6);
      await page.evaluate(() => { globalThis.__newsShift = 0; });
      await page.locator('#news-stream').selectOption('hn'); await page.locator('#news-list [data-news-id="hn-one"]').waitFor();
      check(`${width}: HN submission distinct`, (await page.locator('#news-list').textContent()).includes('文章发布时间未知') && (await page.locator('#news-list').textContent()).includes('HN收录'));
      await page.locator('#news-stream').selectOption('ai'); await page.locator('#news-list [data-news-id="today-one"]').waitFor();
    }
    const overflow = await page.locator('#news-view').evaluate(node => node.scrollWidth > node.clientWidth + 1);
    check(`${width}: news viewport fits`, !overflow);
    await page.screenshot({ path: path.join(output, `news-${width}.png`), fullPage: false }); report.screenshots.push(`news-${width}.png`);
    if (await page.locator('#news-list .daily-news-card').count()) { await page.locator('#news-list .daily-news-card').first().screenshot({ path: path.join(output, `card-${width}.png`) }); report.screenshots.push(`card-${width}.png`); }
    if (legacy) {
      await page.goto(legacy + '/progress', { waitUntil: 'domcontentloaded' }); await page.locator('#progress-todo-input').waitFor();
      await page.locator('#progress-todo-input').fill('旧待办未提交草稿');
      await page.waitForFunction(() => !document.getElementById('legacy-news-status').textContent.includes('正在读取公开来源'));
      check(`${width}: legacy draft survives news rendering`, await page.locator('#progress-todo-input').inputValue() === '旧待办未提交草稿');
      check(`${width}: legacy HN provenance`, (await page.locator('.zone-news').textContent()).includes('HN收录'));
      await page.screenshot({ path: path.join(output, `legacy-${width}.png`), fullPage: false }); report.screenshots.push(`legacy-${width}.png`);
    }
    check(`${width}: no page exceptions`, errors.length === 0); await context.close();
  }
})().catch(error => { report.failures.push(error.stack); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ mode: report.mode, checks: report.checks.length, failures: report.failures, output }));
  if (report.failures.length) process.exitCode = 1;
});
