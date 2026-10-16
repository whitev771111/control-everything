// Browser fixture harness. Run only by the coordinator; every request is intercepted.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { chromium } = require('playwright');

if (process.argv.length !== 3 || process.argv[2] !== '--fixture') throw new Error('Use --fixture only. This harness never connects to a real workspace API.');
const root = path.resolve(__dirname, '../..');
const outputDir = path.join(root, 'work', 'agent-execution-20261013');
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const output = path.join(outputDir, `ui-fixture-${stamp}.json`);
const origin = 'http://workspace.fixture';
const now = '2026-10-13T09:00:00+00:00';
const currentKey = 'codex:10000000-0000-4000-8000-000000000001';
const expiredKey = 'codex:10000000-0000-4000-8000-000000000099';
const report = { scope: 'intercepted synthetic browser fixtures only', owner: 'root/coordinator executing harness', command: `${process.execPath} ${process.argv.slice(1).join(' ')}`, started_at: new Date().toISOString(), fixture_only: true, real_api_used: false, cases: [], failures: [], requests: [], blocked: [], screenshots: [] };
const providerNames = { codex: 'Codex', claude: 'Claude Code', grok: 'Grok', dsh: 'DSH', opencode: 'OpenCode' };
const clone = value => JSON.parse(JSON.stringify(value));
function fixtures() {
  const task = { id: '20000000-0000-4000-8000-000000000001', title: '过期关联仍可更新状态', project_id: 'alpha', provider_id: 'codex', state: 'doing', session_key: expiredKey, session_link_state: 'not_in_current_sample', source: 'manual', created_at: now, updated_at: now };
  return {
    snapshot: { status: 'ok', sampled_at: now, today: '2026-10-13',
      projects: { status: 'available', reason: null, items: [
        { id: 'alpha', name: 'Alpha 开发项目', state: '在做', note: '专注下一步', percent: 39, manual_recorded: true, manual_reason: null, records: [{ feature: '备注优先回归', label: '当前阶段', text: 'Impl' }], records_available: true, sessions: [] },
        { id: 'beta', name: 'Beta 未记录项目', state: '未开始', note: '', percent: null, manual_recorded: false, manual_reason: 'not_recorded', records: [{ feature: '阶段摘要回归', label: '当前阶段', text: 'Impl' }], records_available: true, sessions: [] },
      ] }, todos: { status: 'available', reason: null, items: [{ id: 'todo-fixture', text: '合成待办，不写真实文件', project_id: 'alpha', done: false }] },
      tasks: { status: 'available', reason: null, total: 1, limit: 500, items: [task] },
      providers: Object.entries(providerNames).map(([id, name]) => ({ id, name, discovery: { status: id === 'dsh' ? 'not_found' : 'found', commands: id === 'dsh' ? [] : [id], reason: id === 'dsh' ? 'cli_not_found' : null }, capabilities: { observe_sessions: ['codex', 'claude'].includes(id), open_existing_ui: id !== 'dsh', dispatch: false, cancel: false, approve: false, read_quota: id === 'codex' }, observation_note: id === 'claude' ? '原会话页远端观察，当前连接需在那里核验' : '合成能力事实；活动与完成未知', entrypoints: [{ label: '已有入口', href: '/' }] })),
      sessions: { available: true, reason: null, source: 'codex_local', sampled_at: now, items: [{ thread_id: currentKey.slice(6), session_key: currentKey, cwd: 'C:/example/fixture\\alpha', source_kind: 'cli', updated_at: now, activity_state: 'unknown', task_completion: 'unknown' }], offset: 0, limit: 12, total_visible: 17, next_offset: 1, coverage: { total_count: 20, archived_count: 1, excluded_subagent_count: 2, excluded_other_source_count: 0 } }, entrypoints: [] },
    usage: { status: 'ok', codex: { status: 'not_checked', source: 'codex_app_server', sampled_at: null, last_success_at: null, reason: null, error_code: null, buckets: [] }, manual_records: [], manual_status: 'available', manual_reason: null, manual_total: 0, manual_limit: 100 },
    news: { status: 'stale', sampled_at: now, items: [{ id: 'story1', source_id: 'openai', source_name: 'OpenAI News', title: '<script>literal synthetic title</script>', url: 'https://openai.com/news/', published_at: now, fetched_at: now }], sources: [{ id: 'openai', name: 'OpenAI News', status: 'available', reason: null, sampled_at: now, last_success_at: now, item_count: 1 }, { id: 'google_ai', name: 'Google AI', status: 'unavailable', reason: 'timeout', sampled_at: now, last_success_at: null, item_count: 0 }] },
    failTask: false, failRecord: false, quotaMode: 'error', holdTask: null, holdQuota: null,
  };
}
let browser;
async function scenario(name, callback, width = 1440) {
  const result = { name, width, checks: [], errors: [], console_errors: [], expected_http_failures: [], expected_console_errors: [] };
  report.cases.push(result);
  const fixture = fixtures();
  const context = await browser.newContext({ viewport: { width, height: width < 700 ? 844 : 1000 }, serviceWorkers: 'block' });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { report.blocked.push(request.url()); await route.abort(); return; }
    const requestRecord = { case: name, path: url.pathname, method: request.method(), client: request.headers()['x-session-client'], csrf: request.headers()['x-session-csrf'] };
    if (request.method() === 'POST') requestRecord.body = request.postDataJSON();
    report.requests.push(requestRecord);
    const json = async (body, status = 200) => {
      if (status >= 400) result.expected_http_failures.push({ url: request.url(), status, reason: body.reason });
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    };
    const error = (reason, status = 503) => json({ status: 'unknown', reason, source: 'fixture', observed_at: now }, status);
    try {
      if (url.pathname === '/workspace') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/workspace/workspace.html')), headers: { 'content-security-policy': "default-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'" } });
      if (['/workspace.css', '/workspace.js', '/execution.js'].includes(url.pathname)) return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, 'web/workspace', url.pathname.slice(1))) });
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 });
      assert.equal(request.headers()['x-session-client'], 'panel');
      if (request.method() === 'POST') assert.equal(request.headers()['x-session-csrf'], 'fixture-csrf');
      if (url.pathname === '/api/bootstrap') return json({ csrf_token: 'fixture-csrf' });
      if (url.pathname === '/api/workspace') return json(clone(fixture.snapshot));
      if (url.pathname === '/api/workspace/execution' && !url.search) return json({ capability: { implemented: true, available: false, reason: 'execution_unavailable', is_owner: false, max_active: 2 }, runs: [], active: [], total: 0, limit: 50 });
      if (url.pathname === '/api/workspace/news') return json(clone(fixture.news));
      if (url.pathname === '/api/workspace/usage') return json(clone(fixture.usage));
      if (url.pathname === '/api/workspace/usage/refresh') {
        if (fixture.holdQuota) await fixture.holdQuota;
        fixture.usage.codex = fixture.quotaMode === 'success'
          ? { status: 'available', source: 'codex_app_server', sampled_at: now, last_success_at: now, reason: null, error_code: null, buckets: [{ id: 'codex', primary: { used_percent: 36, window_minutes: 10080, resets_at: now }, secondary: { used_percent: null, window_minutes: null, resets_at: null } }] }
          : { status: 'unavailable', source: 'codex_app_server', sampled_at: now, last_success_at: null, reason: 'rpc_error', error_code: -32603, buckets: [] };
        return json(clone(fixture.usage));
      }
      if (['/api/workspace/tasks', '/api/workspace/tasks/update'].includes(url.pathname)) {
        if (fixture.holdTask) await fixture.holdTask;
        if (fixture.failTask) return error('storage_unavailable');
        const body = request.postDataJSON(), update = url.pathname.endsWith('/update');
        const index = fixture.snapshot.tasks.items.findIndex(task => task.id === body.id);
        if (update && index < 0) return error('task_not_found', 404);
        const existing = fixture.snapshot.tasks.items[index];
        const unchanged = existing && ['project_id', 'provider_id', 'session_key'].every(key => body[key] === existing[key]);
        if (body.session_key && !unchanged && body.session_key !== currentKey) return error('session_not_in_sample', 409);
        if (body.session_key && !unchanged && body.project_id !== 'alpha') return error('session_project_mismatch', 409);
        const task = { ...body, id: body.id || randomUUID(), source: 'manual', created_at: existing?.created_at || now, updated_at: now, session_link_state: body.session_key === expiredKey ? 'not_in_current_sample' : body.session_key ? 'verified' : 'none' };
        if (index >= 0) fixture.snapshot.tasks.items.splice(index, 1); else fixture.snapshot.tasks.total++;
        fixture.snapshot.tasks.items.unshift(task); return json({ status: 'ok', task });
      }
      if (url.pathname === '/api/workspace/records') {
        if (fixture.failRecord) return error('storage_unavailable');
        const body = request.postDataJSON(), index = fixture.usage.manual_records.findIndex(record => record.id === body.id);
        if (body.id && index < 0) return error('record_not_found', 404);
        const record = { ...body, id: body.id || randomUUID(), source: 'manual', created_at: now, updated_at: now };
        if (index >= 0) fixture.usage.manual_records.splice(index, 1); else fixture.usage.manual_total++;
        fixture.usage.manual_records.unshift(record); return json({ status: 'ok', record });
      }
      report.blocked.push(request.url()); return route.abort();
    } catch (error) { result.errors.push(error.stack); return route.abort(); }
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') result.console_errors.push({ text: message.text(), url: message.location().url }); });
  const check = (label, condition) => { result.checks.push({ label, passed: Boolean(condition) }); assert.ok(condition, label); };
  try {
    await page.goto(`${origin}/workspace`, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !document.getElementById('new-task').disabled && !document.getElementById('new-record').disabled);
    await callback(page, fixture, check);
  } catch (error) { report.failures.push({ case: name, error: error.stack }); }
  finally {
    for (const error of result.console_errors) {
      const expected = result.expected_http_failures.some(item => item.url === error.url && error.text.includes(`status of ${item.status}`));
      if (expected) result.expected_console_errors.push(error);
      else report.failures.push({ case: name, error: `Unexpected console error: ${error.text}` });
    }
    for (const error of result.errors) report.failures.push({ case: name, error });
    await context.close();
  }
}
const navigate = (page, view) => page.locator(`[data-view="${view}"]`).click();
const saveTask = async page => { await page.locator('#task-form button[type="submit"]').click(); await page.locator('#task-form').waitFor({ state: 'hidden' }); };
const saveRecord = async page => { await page.locator('#record-form button[type="submit"]').click(); await page.locator('#record-form').waitFor({ state: 'hidden' }); };
const reload = async page => { await page.locator('#refresh-workspace').click(); await page.waitForFunction(() => !document.getElementById('refresh-workspace').disabled); };

async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  await scenario('truthful sources, task creation, provider/state/session editing and filtering', async (page, fixture, check) => {
    check('missing project progress has no fake zero meter', await page.locator('.project-row').filter({ hasText: 'Beta 未记录项目' }).locator('progress').count() === 0);
    check('missing project progress is explicit', (await page.locator('#projects-list').textContent()).includes('未记录'));
    check('no automatic quota refresh', report.requests.filter(item => item.path === '/api/workspace/usage/refresh').length === 0);
    await navigate(page, 'tasks');
    await page.locator('#new-task').click();
    await page.locator('#task-title').fill('<img src=x> 合成任务');
    await page.locator('#task-provider').selectOption('codex');
    await page.locator('#task-session').selectOption(currentKey);
    await saveTask(page);
    const task = fixture.snapshot.tasks.items[0];
    check('created record reflects server reply', task.title === '<img src=x> 合成任务' && task.session_key === currentKey);
    check('task text is not interpreted as HTML', await page.locator('#tasks-list img').count() === 0);
    await page.locator(`[data-task-id="${task.id}"] button`).filter({ hasText: /^编辑$/ }).click();
    await page.locator('#task-state').selectOption('review');
    await page.locator('#task-provider').selectOption('grok');
    check('non-Codex provider clears association', await page.locator('#task-session').inputValue() === '');
    await saveTask(page);
    check('provider and state edited', fixture.snapshot.tasks.items[0].provider_id === 'grok' && fixture.snapshot.tasks.items[0].state === 'review');
    await page.locator('#project-filter').selectOption('beta');
    check('project filter hides unrelated tasks', await page.locator('.task-row').count() === 0);
    await page.locator('#project-filter').selectOption('');
    await page.locator('[data-task-id="20000000-0000-4000-8000-000000000001"] button').filter({ hasText: /^编辑$/ }).click();
    check('expired association retained in form', await page.locator('#task-session').inputValue() === expiredKey);
    await page.locator('#task-state').selectOption('done'); await saveTask(page);
    const expired = fixture.snapshot.tasks.items.find(item => item.id === '20000000-0000-4000-8000-000000000001');
    check('expired key preserved after manual state change', expired.session_key === expiredKey && expired.state === 'done');
    await navigate(page, 'agents');
    check('five provider capability rows', await page.locator('.provider-row').count() === 5);
    check('sample coverage explicit', (await page.locator('#sessions-status').textContent()).includes('1 / 17'));
    check('execution remains unavailable', (await page.locator('#providers-list').textContent()).includes('派发 / 审批 / 取消 · 未接入'));
  });
  await scenario('failed task save retains draft, mismatch feedback and pending lock', async (page, fixture, check) => {
    await page.locator('#new-task').click(); await page.locator('#task-title').fill('失败草稿');
    await page.locator('#task-provider').selectOption('codex'); await page.locator('#task-session').selectOption(currentKey);
    fixture.failTask = true;
    await page.locator('#task-form button[type="submit"]').click();
    await page.waitForFunction(() => document.getElementById('task-feedback').classList.contains('error'));
    check('failed save retains title and association', await page.locator('#task-title').inputValue() === '失败草稿' && await page.locator('#task-session').inputValue() === currentKey);
    await reload(page);
    check('refresh leaves draft untouched', await page.locator('#task-title').inputValue() === '失败草稿');
    fixture.failTask = false; await page.locator('#task-project').selectOption('beta');
    await page.locator('#task-form button[type="submit"]').click();
    await page.waitForFunction(() => document.getElementById('task-feedback').textContent.includes('不属于'));
    check('mismatched association reports actionable error', await page.locator('#task-project').inputValue() === 'beta');
    await page.locator('#task-project').selectOption('alpha');
    let release; fixture.holdTask = new Promise(resolve => { release = resolve; });
    await page.locator('#task-form button[type="submit"]').click();
    await page.locator('#new-task').click();
    check('pending submit disables fields and preserves form', await page.locator('#task-title').isDisabled() && await page.locator('#task-title').inputValue() === '失败草稿');
    release(); await page.locator('#task-form').waitFor({ state: 'hidden' });
    check('one successful task only', fixture.snapshot.tasks.total === 2);
  });
  await scenario('manual record create/edit/failure and explicit quota success/error/null', async (page, fixture, check) => {
    await navigate(page, 'usage');
    check('quota starts unchecked without zero', (await page.locator('#quota-detail').textContent()).includes('未查询') && await page.locator('#quota-detail progress').count() === 0);
    await page.locator('#refresh-quota').click();
    await page.waitForFunction(() => document.getElementById('quota-detail').textContent.includes('-32603'));
    check('RPC error explicitly displayed', (await page.locator('#quota-detail').textContent()).includes('不可用'));
    fixture.quotaMode = 'success'; await page.locator('#refresh-quota').click();
    await page.waitForFunction(() => document.getElementById('quota-detail').textContent.includes('剩余 64%'));
    check('missing percentage remains unknown', (await page.locator('#quota-detail').textContent()).includes('剩余未知') && await page.locator('#quota-detail progress').count() === 1);
    await page.locator('#new-record').click(); await page.locator('#record-provider').selectOption('grok'); await page.locator('#record-product').selectOption('api');
    await page.locator('#record-metric').selectOption('daily_cost'); await page.locator('#record-value').fill('12.5'); await page.locator('#record-currency').selectOption('CNY'); await page.locator('#record-note').fill('合成观察');
    await saveRecord(page);
    check('currency/product retained without aggregation', fixture.usage.manual_records[0].currency === 'CNY' && fixture.usage.manual_records[0].product === 'api');
    await page.locator('.record-row button').click(); await page.locator('#record-value').fill('18'); fixture.failRecord = true;
    await page.locator('#record-form button[type="submit"]').click(); await page.waitForFunction(() => document.getElementById('record-feedback').classList.contains('error'));
    check('failed record keeps complete draft', await page.locator('#record-value').inputValue() === '18' && await page.locator('#record-product').inputValue() === 'api' && await page.locator('#record-note').inputValue() === '合成观察');
    await page.locator('#refresh-quota').click(); await page.waitForFunction(() => !document.getElementById('refresh-quota').disabled);
    check('source refresh keeps record draft', await page.locator('#record-value').inputValue() === '18');
    fixture.failRecord = false; await saveRecord(page);
    check('record edit updates one existing row', fixture.usage.manual_records.length === 1 && fixture.usage.manual_records[0].value === 18);
    check('manual label explicit', (await page.locator('#records-list').textContent()).includes('手工记录'));
    await page.locator('#new-record').click(); await page.locator('#record-metric').selectOption('daily_tokens');
    await page.locator('#record-value').fill('9007199254740993');
    const recordRequests = report.requests.filter(item => item.path === '/api/workspace/records').length;
    await page.locator('#record-form button[type="submit"]').click();
    await page.waitForFunction(() => document.getElementById('record-feedback').textContent.includes('可精确表示'));
    check('unsafe token integer is rejected before network', report.requests.filter(item => item.path === '/api/workspace/records').length === recordRequests);
    check('unsafe token input is retained exactly', await page.locator('#record-value').inputValue() === '9007199254740993');
    await page.locator('#record-value').fill('123456'); await saveRecord(page);
    check('safe integer tokens sent exactly', fixture.usage.manual_records[0].value === 123456 && fixture.usage.manual_records[0].metric === 'daily_tokens');
  });
  for (const width of [1440, 390, 360]) await scenario(`responsive ${width}, all views/forms, partial and empty sources`, async (page, fixture, check) => {
    const bounds = async label => check(label, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.body.scrollWidth <= innerWidth));
    const projectSummary = name => page.locator('.project-row').filter({ hasText: name }).locator('.project-copy p');
    check('document summary visibly preserves feature, label and text', await projectSummary('Beta 未记录项目').isVisible() && await projectSummary('Beta 未记录项目').innerText() === '阶段摘要回归 · 当前阶段 · Impl');
    check('manual note remains preferred over document summary', await projectSummary('Alpha 开发项目').innerText() === '专注下一步');
    fixture.snapshot.projects.items[1].records[0].text = 'Impl · 较长阶段文字仍按来源原文显示，并换行保留其具体内容，不推断任务已经完成。';
    await reload(page); await bounds('long document stage wraps without horizontal overflow');
    fixture.snapshot.projects.items[1].records[0].text = '';
    await reload(page);
    check('missing document text has explicit fallback', (await projectSummary('Beta 未记录项目').innerText()).includes('具体内容未提供'));
    fixture.snapshot.projects.items[1].records = []; await reload(page);
    check('empty document records remain distinct', await projectSummary('Beta 未记录项目').innerText() === '暂无未收口的文档线索');
    fixture.snapshot.projects.items[1].records_available = false; await reload(page);
    check('unavailable document source remains distinct', await projectSummary('Beta 未记录项目').innerText() === '文档线索暂不可用');
    for (const view of ['overview', 'tasks', 'agents', 'usage', 'news']) {
      await navigate(page, view); await bounds(`${view} no horizontal overflow`);
      if (view === 'overview') check('overview keeps project summary first', await page.locator('.primary-column > section').first().getAttribute('id') === 'projects-section');
      if (view === 'tasks') {
        check('task view puts task section first in DOM and keyboard order', await page.locator('.primary-column > section').first().getAttribute('id') === 'tasks-section');
        check('new task entry visible in first viewport', await page.locator('#new-task').evaluate(node => { const rect = node.getBoundingClientRect(); return rect.top >= 0 && rect.bottom <= innerHeight; }));
        await page.locator('#new-task').click(); await page.locator('#task-title').fill('窄屏可编辑任务');
        await navigate(page, 'overview'); await navigate(page, 'tasks');
        check('moving existing sections preserves task draft', await page.locator('#task-title').inputValue() === '窄屏可编辑任务');
        await bounds('task form fits viewport'); await saveTask(page);
      }
      if (view === 'usage') { await page.locator('#new-record').click(); await page.locator('#record-value').fill('25'); await page.locator('#record-window').fill('本周'); await bounds('record form fits viewport'); await saveRecord(page); }
      if (view === 'news') {
        check('untrusted news title stays literal text', (await page.locator('#news-list').textContent()).includes('<script>') && await page.locator('#news-list script').count() === 0);
        check('partial failure shown', (await page.locator('#news-status').textContent()).includes('部分来源失败'));
        check('external links isolated', await page.locator('#news-list a').evaluateAll(nodes => nodes.every(node => node.protocol === 'https:' && node.rel.includes('noopener') && node.target === '_blank')));
      }
    }
    fixture.snapshot.tasks.items = []; fixture.snapshot.tasks.total = 0; fixture.snapshot.todos.items = [];
    await navigate(page, 'overview'); await reload(page); await bounds('empty state fits viewport');
    check('empty state offers meaningful next action', (await page.locator('#tasks-list').textContent()).includes('把下一步') && await page.locator('#new-task').isEnabled());
    const shot = path.join(outputDir, `workspace-fixture-${width}-${stamp}.png`);
    await page.screenshot({ path: shot, fullPage: true }); report.screenshots.push(shot);
    fixture.snapshot.projects.status = 'unavailable'; fixture.snapshot.projects.reason = 'projects_unavailable'; fixture.snapshot.projects.items = [];
    fixture.snapshot.tasks.status = 'unavailable'; fixture.snapshot.tasks.reason = 'storage_unavailable';
    fixture.snapshot.sessions.available = false; fixture.snapshot.sessions.items = []; fixture.snapshot.sessions.reason = 'codex_index_unavailable';
    await reload(page);
    check('source failure not represented as empty task list', (await page.locator('#tasks-list').textContent()).includes('暂时不可用'));
    check('creation unavailable without project/storage', await page.locator('#new-task').isDisabled());
  }, width);
  assert.equal(report.blocked.length, 0, 'No request escaped fixture routes');
  assert.ok(report.requests.filter(item => item.path.startsWith('/api/')).every(item => item.client === 'panel'), 'All API requests retain client header');
}
(async () => {
  try { await main(); } catch (error) { report.failures.push({ case: 'startup/global', error: error.stack }); }
  finally {
    if (browser) await browser.close();
    report.finished_at = new Date().toISOString(); report.exit_code = report.failures.length ? 1 : 0;
    fs.mkdirSync(outputDir, { recursive: true }); fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
    console.log(JSON.stringify({ output, checks: report.cases.reduce((sum, item) => sum + item.checks.length, 0), failures: report.failures, exit_code: report.exit_code }, null, 2));
    process.exitCode = report.exit_code;
  }
})();
