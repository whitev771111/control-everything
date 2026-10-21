// Coordinator-only browser fixtures. Every resource and API request is intercepted.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { randomUUID } = require('node:crypto');
const { chromium } = require('playwright');
if (process.argv.length !== 3 || process.argv[2] !== '--fixture') throw new Error('Explicit --fixture is required; no real API is supported.');
const root = path.resolve(__dirname, '../..'), outputDir = path.join(root, 'work/agent-execution-20261013');
assert.ok(outputDir.startsWith(path.join(root, 'work', 'agent-execution-20261013')));
const stamp = new Date().toISOString().replace(/[:.]/g, '-'), origin = 'http://127.0.0.1:18769', base = '/api/workspace/execution';
const now = '2026-10-13T09:00:00Z', taskId = '20000000-0000-4000-8000-000000000001';
const clone = value => JSON.parse(JSON.stringify(value));
const report = { command: `${process.execPath} ${process.argv.slice(1).join(' ')}`, owner: 'root/coordinator executing harness', started_at: new Date().toISOString(), fixture_only: true, real_api_used: false, scope: 'Synthetic browser behavior, not actual Agent execution', cases: [], failures: [], requests: [], blocked: [], screenshots: [] };
let browser;
function makeRun(overrides = {}) {
  return { id: randomUUID(), request_id: randomUUID(), task_id: taskId, task_title: 'Fixture · Codex 任务', project_id: 'alpha', project_name: 'Fixture · Alpha', cwd: 'C:/example/fixture\\alpha', mode: 'workspace-write', state: 'running', reason: null, thread_id: 'native-thread-fixture', turn_id: 'native-turn-fixture', model: 'fixture-model', cancel_requested: false, cleanup_state: 'pending', token_usage: null, token_observed_at: null, created_at: now, updated_at: now, ended_at: null, prompt: 'Fixture prompt', effective_policy: { mode: 'workspace-write', network_access: false }, ...overrides };
}
function fixtures() {
  const tasks = [{ id: taskId, title: 'Fixture · 检查工作目录', project_id: 'alpha', provider_id: 'codex', state: 'todo', session_key: null }, { id: randomUUID(), title: 'Fixture · 其他工具任务', project_id: 'beta', provider_id: 'claude', state: 'todo', session_key: null }];
  return {
    snapshot: { status: 'ok', today: '2026-10-13', sampled_at: now, projects: { status: 'available', items: [{ id: 'alpha', name: 'Fixture · Alpha', percent: null, manual_recorded: false, records: [], records_available: true }, { id: 'beta', name: 'Fixture · Beta', percent: null, manual_recorded: false, records: [], records_available: true }] }, tasks: { status: 'available', items: tasks, total: 2, limit: 500 }, todos: { status: 'available', items: [] }, providers: ['codex', 'claude'].map(id => ({ id, name: id === 'codex' ? 'Codex' : 'Claude Code', discovery: { status: 'found' }, capabilities: { observe_sessions: true, read_quota: id === 'codex' }, observation_note: 'Fixture capability', entrypoints: [] })), sessions: { available: true, items: [], total_visible: 0, limit: 12, sampled_at: now } },
    usage: { status: 'ok', codex: { status: 'not_checked', buckets: [], reason: null }, manual_records: [], manual_status: 'available', manual_total: 0, manual_limit: 100 }, news: { status: 'available', items: [], sources: [], sampled_at: now },
    capability: { implemented: true, available: true, reason: null, is_owner: true, max_active: 2 },
    context: { task: { id: taskId, title: 'Fixture · 检查工作目录' }, project: { id: 'alpha', name: 'Fixture · Alpha', cwd: 'C:/example/fixture\\alpha\\真实规范目录' }, context_hash: 'fixture-hash-1', policy: { mode: 'workspace-write', approval_policy: 'on-request', approvals_reviewer: 'user', network_access: false, writable_roots: [], inheritance_note: '本机独立 MCP 与 managed 设置可能继承；外部工具有独立权限边界。' } },
    runs: [], approvals: {}, events: {}, postMode: 'success', lookupMode: 'normal', contextMode: 'normal', gap: false, holdPost: null, holdDetail: null, heldId: null, contextCalls: 0, listFail: false, actionFail: false,
  };
}
async function scenario(name, callback, width = 1440, setup) {
  const result = { name, width, checks: [], errors: [], console_errors: [], expected_errors: [] }; report.cases.push(result);
  const fixture = fixtures(); if (setup) setup(fixture);
  const context = await browser.newContext({ viewport: { width, height: width < 700 ? 844 : 1000 }, serviceWorkers: 'block' });
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    const record = { case: name, path: url.pathname, query: url.search, method: request.method() };
    if (request.method() === 'POST') record.body = request.postDataJSON(); report.requests.push(record);
    const json = (body, status = 200) => {
      if (status >= 400) result.expected_errors.push({ url: request.url(), status });
      return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    };
    const fail = (reason, status = 503) => json({ status: 'unknown', reason }, status);
    const abort = () => { result.expected_errors.push({ url: request.url(), aborted: true }); return route.abort('failed'); };
    try {
      if (url.origin !== origin) { report.blocked.push(request.url()); return route.abort(); }
      if (url.pathname === '/workspace') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/workspace/workspace.html')) });
      if (['/execution.js', '/workspace.js', '/workspace.css', '/observation.js', '/task-center.js', '/news.js', '/quota.js', '/project-space.js', '/onboarding.js', '/agents.js'].includes(url.pathname)) return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, 'web/workspace', url.pathname.slice(1))) });
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 });
      assert.equal(request.headers()['x-session-client'], 'panel');
      if (request.method() === 'POST') assert.equal(request.headers()['x-session-csrf'], 'fixture-csrf');
      if (url.pathname === '/api/bootstrap') return json({ csrf_token: 'fixture-csrf' });
      if (url.pathname === '/api/workspace') return json(fixture.snapshot);
      if (url.pathname === '/api/workspace/usage') return json(fixture.usage);
      if (url.pathname === '/api/workspace/news') return json(fixture.news);
      if (url.pathname === base && url.searchParams.has('request_id')) {
        if (fixture.lookupMode === '503') return fail('storage_unavailable');
        if (fixture.lookupMode === 'network') return abort();
        const run = fixture.runs.find(item => item.request_id === url.searchParams.get('request_id'));
        return run ? json({ run }) : fail('request_not_found', 404);
      }
      if (url.pathname === base) return fixture.listFail ? fail('storage_unavailable') : json({ capability: fixture.capability, runs: fixture.runs, active: fixture.runs.filter(item => ['starting', 'running', 'waiting_approval', 'cancel_requested'].includes(item.state)), total: fixture.runs.length, limit: 50 });
      if (url.pathname === `${base}/context`) {
        fixture.contextCalls++;
        return fixture.contextMode === 'fail' ? fail('storage_unavailable') : json(fixture.context);
      }
      if (url.pathname === `${base}/runs` && request.method() === 'POST') {
        if (fixture.holdPost) await fixture.holdPost;
        if (fixture.postMode === 'context') return fail('context_changed', 409);
        if (fixture.postMode.startsWith('reject:')) { const [, status, reason] = fixture.postMode.split(':'); return fail(reason, Number(status)); }
        if (fixture.postMode === 'lost404') return abort();
        if (fixture.postMode === '503') return fail('storage_unavailable');
        let run = fixture.runs.find(item => item.request_id === record.body.request_id);
        if (!run) { run = makeRun({ ...record.body, task_title: fixture.context.task.title, cwd: fixture.context.project.cwd }); fixture.runs.unshift(run); }
        if (fixture.postMode === 'lost200') return abort();
        if (fixture.postMode === 'conflict') return fail('idempotency_conflict', 409);
        return json({ run, replayed: false }, 202);
      }
      const match = url.pathname.match(/\/runs\/([^/]+)(?:\/(cancel|approvals)(?:\/([^/]+))?)?$/);
      if (match) {
        const [, id, action, approvalId] = match, run = fixture.runs.find(item => item.id === id);
        if (!run) return fail('run_not_found', 404);
        if (action === 'cancel') { run.cancel_requested = true; run.state = 'cancel_requested'; return json({ run }, 202); }
        if (action === 'approvals') {
          if (fixture.actionFail) return fail('approval_expired', 409);
          const approval = (fixture.approvals[id] || []).find(item => item.id === approvalId);
          if (!approval) return fail('approval_not_found', 404);
          approval.status = 'queued'; approval.decision = record.body.decision; return json({ approval }, 202);
        }
        const captured = clone(run);
        if (fixture.holdDetail && fixture.heldId === id) await fixture.holdDetail;
        const events = (fixture.events[id] || []).filter(item => item.seq > Number(url.searchParams.get('after') || 0));
        return json({ run: captured, approvals: fixture.approvals[id] || [], events, first_cursor: fixture.gap ? 20 : 0, gap: fixture.gap, next_cursor: events.at(-1)?.seq || Number(url.searchParams.get('after') || 0), has_more: false });
      }
      report.blocked.push(request.url()); return route.abort();
    } catch (error) { result.errors.push(error.stack); return route.abort(); }
  });
  const page = await context.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') result.console_errors.push({ text: message.text(), url: message.location().url }); });
  const check = (label, passed) => { result.checks.push({ label, passed: Boolean(passed) }); assert.ok(passed, label); };
  try {
    await page.goto(`${origin}/workspace`);
    await page.waitForFunction(() => !document.getElementById('new-task').disabled);
    await callback(page, fixture, check, result);
  } catch (error) { report.failures.push({ case: name, error: error.stack }); }
  finally {
    for (const error of result.errors) report.failures.push({ case: name, error });
    for (const error of result.console_errors) if (!result.expected_errors.some(item => item.url === error.url && (item.aborted ? error.text.includes('ERR_FAILED') : error.text.includes(`status of ${item.status}`)))) report.failures.push({ case: name, error: error.text });
    await context.close();
  }
}
const go = (page, view) => page.locator(`[data-view="${view}"]`).click();
async function prepare(page) { await go(page, 'tasks'); await page.locator(`[data-run-task="${taskId}"]`).click(); await page.waitForFunction(() => !document.getElementById('execution-submit').disabled); }
async function submit(page) { await page.locator('#execution-confirm').check(); await page.locator('#execution-submit').click(); }
const posts = result => report.requests.filter(item => item.case === result.name && item.method === 'POST' && item.path === `${base}/runs`);
async function refresh(page) { await page.waitForFunction(() => !document.getElementById('execution-refresh').disabled); await page.locator('#execution-refresh').click(); await page.waitForFunction(() => !document.getElementById('execution-refresh').disabled); }
async function shot(page, label, width) {
  const file = path.join(outputDir, `ui-execution-fixture-${label}-${width}-${stamp}.png`);
  await page.screenshot({ path: file, fullPage: true }); report.screenshots.push({ path: file, source: 'synthetic fixture' });
}
async function main() {
  fs.mkdirSync(outputDir, { recursive: true });
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  await scenario('prepare and single admission', async (page, fixture, check, result) => {
    check('no automatic submission on bootstrap', posts(result).length === 0);
    check('only Codex task has explicit execution entry', await page.locator('[data-run-task]').count() === 1);
    await prepare(page);
    check('real registered cwd and inherited MCP are disclosed', (await page.locator('#execution-context').innerText()).includes('真实规范目录') && (await page.locator('#execution-inheritance').innerText()).includes('MCP'));
    check('prepare title receives focus, no dispatch from preparation', await page.locator('#execution-prepare-title').evaluate(el => el === document.activeElement) && posts(result).length === 0);
    await page.locator('#execution-prompt').fill('固定 body 提示词');
    check('confirmation starts unchecked', !await page.locator('#execution-confirm').isChecked());
    await shot(page, 'confirmation', 1440);
    await page.setViewportSize({ width: 360, height: 900 });
    check('confirmation fits narrow viewport', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await shot(page, 'confirmation', 360);
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#execution-submit').click();
    check('unconfirmed click sends nothing', posts(result).length === 0);
    await page.locator('#execution-confirm').check();
    let release; fixture.holdPost = new Promise(resolve => { release = resolve; });
    await page.locator('#execution-form').evaluate(form => { form.requestSubmit(); form.requestSubmit(); });
    await page.waitForFunction(() => document.getElementById('execution-prompt').disabled);
    check('in-flight content frozen and double submit sent once', posts(result).length === 1);
    release(); await page.locator('#execution-another').waitFor({ state: 'visible' });
    await page.waitForFunction(() => document.getElementById('execution-run-meta').textContent.includes('native-thread'));
    check('actual model/native identities visible', (await page.locator('#execution-run-meta').innerText()).includes('fixture-model'));
    check('manual task state unchanged by run', fixture.snapshot.tasks.items[0].state === 'todo');
    await shot(page, 'admitted', 1440);
  });
  for (const mode of ['lost200', 'lost404', '503', 'conflict']) await scenario(`receipt recovery ${mode}`, async (page, fixture, check, result) => {
    fixture.postMode = mode; if (mode === '503') fixture.lookupMode = '503';
    await prepare(page); await page.locator('#execution-prompt').fill('回执恢复原内容'); await submit(page);
    await page.waitForFunction(() => document.getElementById('execution-recovery-text').textContent !== '' && !document.getElementById('execution-recovery-text').textContent.includes('正在提交'));
    check('uncertain admission always looks up original request', report.requests.some(item => item.case === result.name && item.query.includes('request_id=')));
    if (mode === 'lost200' || mode === 'conflict') check('original run recovered without new POST', await page.locator('#execution-another').isVisible() && posts(result).length === 1);
    if (mode === 'lost404') {
      check('404 retains locked draft and explicit retry', await page.locator('#execution-prompt').isDisabled() && await page.locator('#execution-retry').isVisible());
      fixture.postMode = 'success'; await page.locator('#execution-retry').click(); await page.locator('#execution-another').waitFor({ state: 'visible' });
      check('explicit retry keeps identical request body/key', JSON.stringify(posts(result)[0].body) === JSON.stringify(posts(result)[1].body));
    }
    if (mode === '503') {
      check('503 keeps original key and offers query only', await page.locator('#execution-retry').isHidden() && await page.locator('#execution-prompt').isDisabled());
      fixture.lookupMode = 'network'; await page.locator('#execution-lookup').click();
      await page.waitForFunction(() => !document.getElementById('execution-lookup').disabled);
      check('network query failure also prevents a new submission', posts(result).length === 1 && await page.locator('#execution-another').isHidden());
    }
  });
  for (const combo of ['400:invalid_request', '404:task_not_found', '409:project_busy', '409:capacity_busy', '409:other_instance']) await scenario(`definitive rejection ${combo}`, async (page, fixture, check, result) => {
    fixture.postMode = `reject:${combo}`; await prepare(page); await page.locator('#execution-prompt').fill('拒绝后仍保留'); await submit(page);
    await page.waitForFunction(() => document.getElementById('execution-status').textContent.includes('未登记'));
    check('admission rejection retains editable draft and sends no lookup', await page.locator('#execution-prompt').inputValue() === '拒绝后仍保留' && await page.locator('#execution-prompt').isEnabled() && !report.requests.some(item => item.case === result.name && item.query.includes('request_id=')));
  });
  await scenario('context refresh failure then explicit success', async (page, fixture, check, result) => {
    await prepare(page); await page.locator('#execution-prompt').fill('上下文草稿'); fixture.postMode = 'context'; fixture.contextMode = 'fail';
    await submit(page); await page.waitForFunction(() => document.getElementById('execution-status').textContent.includes('重新读取摘要失败'));
    check('failed context refresh retains draft but blocks submit', await page.locator('#execution-submit').isDisabled() && await page.locator('#execution-prompt').inputValue() === '上下文草稿');
    fixture.contextMode = 'normal'; fixture.context.context_hash = 'fixture-hash-2'; fixture.postMode = 'success';
    await page.locator('#execution-context-refresh').click(); await page.waitForFunction(() => !document.getElementById('execution-submit').disabled);
    check('context refresh never automatically resubmits', posts(result).length === 1);
    await submit(page); await page.locator('#execution-another').waitFor({ state: 'visible' });
    check('new explicit submission has fresh hash/key', posts(result)[0].body.request_id !== posts(result)[1].body.request_id && posts(result)[1].body.context_hash === 'fixture-hash-2');
  });
  await scenario('reload unknown has no replay body', async (page, fixture, check, result) => {
    fixture.postMode = 'lost404'; await prepare(page); await submit(page); await page.locator('#execution-retry').waitFor({ state: 'visible' });
    await page.reload(); await page.waitForFunction(() => document.getElementById('execution-recovery-text').textContent.includes('重载后'));
    check('reload only stores request/task metadata', await page.evaluate(() => Object.keys(JSON.parse(sessionStorage.getItem('workspace.execution.receipt'))).sort().join(',')) === 'request_id,task_id');
    check('reload has no automatic POST or original-body retry', posts(result).length === 1 && await page.locator('#execution-retry').isHidden());
  });
  for (const width of [1440, 390, 360]) await scenario(`two runs, approvals and text safety ${width}`, async (page, fixture, check, result) => {
    const a = makeRun({ state: 'waiting_approval', task_title: 'Fixture · Alpha 等待审批', cwd: 'C:/example/fixture\\' + '长目录'.repeat(60), token_usage: { total: { input_tokens: 120, output_tokens: 30, total_tokens: 150 }, last: null, source: 'codex_app_server' }, token_observed_at: now });
    const b = makeRun({ task_title: 'Fixture · Beta 独立运行', project_id: 'beta', project_name: 'Beta', cwd: 'C:/example/fixture\\beta' });
    fixture.runs = [a, b];
    const complete = { id: randomUUID(), kind: 'command', detail: { command: 'echo <img src=x onerror=alert(1)>', cwd: a.cwd, reason: 'Fixture', scope_verified: true }, detail_complete: true, status: 'pending', decision: null, can_accept: true };
    const incomplete = { id: randomUUID(), kind: 'file_change', detail: { changes: [{ path: 'very-long.txt', diff: '+ ' + 'x'.repeat(2000) }] }, detail_complete: false, status: 'pending', decision: null, can_accept: false };
    fixture.approvals[a.id] = [complete, incomplete]; fixture.events[a.id] = [{ seq: 21, kind: 'agent_message', payload: { text: '<script>unsafe()</script> [link](https://example.com)', final: true }, observed_at: now }]; fixture.gap = true;
    await go(page, 'runs'); await refresh(page);
    check('two independent active cards', await page.locator('#execution-active [data-run-id]').count() === 2);
    await page.locator(`[data-run-id="${a.id}"]`).click(); await page.locator(`[data-approval-id="${complete.id}"]`).waitFor();
    check('full details allow once; incomplete details block approval', await page.locator(`[data-approval-id="${complete.id}"] [data-decision="accept"]`).isEnabled() && await page.locator(`[data-approval-id="${incomplete.id}"] [data-decision="accept"]`).isDisabled());
    check('Agent HTML and links remain literal text', await page.locator('#execution-events script,#execution-events a,#execution-approvals img').count() === 0 && (await page.locator('#execution-events').innerText()).includes('<script>'));
    check('gap and run-scoped token meaning visible', (await page.locator('#execution-gap').innerText()).includes('超出保留范围') && (await page.locator('#execution-token').innerText()).includes('不是每日用量'));
    const approve = page.locator(`[data-approval-id="${complete.id}"] [data-decision="accept"]`);
    await page.waitForFunction(() => !document.getElementById('execution-refresh').disabled);
    await approve.focus(); await page.evaluate(() => window.WorkspaceExecution.refresh());
    check('poll does not take focus from stable approval action', await approve.evaluate(el => document.activeElement === el));
    incomplete.detail.reason = 'Fixture · 另一张审批更新，不应重建当前审批按钮';
    await page.evaluate(() => window.WorkspaceExecution.refresh());
    check('other approval updates preserve the focused card', await approve.evaluate(el => document.activeElement === el));
    check('approval details use Chinese labels and preserve literal commands', (await page.locator(`[data-approval-id="${complete.id}"] pre`).innerText()).includes('命令：echo <img src=x onerror=alert(1)>'));
    check('no page-level horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.body.scrollWidth <= innerWidth));
    check('long diff scrolls within its own code area', await page.locator(`[data-approval-id="${incomplete.id}"] pre`).evaluate(el => el.scrollWidth > el.clientWidth && el.getBoundingClientRect().right <= innerWidth));
    await shot(page, 'approvals', width);
    await approve.click(); await page.waitForFunction(id => document.querySelector(`[data-approval-id="${id}"]`).textContent.includes('答复已登记'), complete.id);
    check('accept sent once and not presented as command success', report.requests.filter(item => item.case === result.name && item.path.endsWith(`/approvals/${complete.id}`)).length === 1 && !(await page.locator('#execution-action-status').innerText()).includes('命令已成功'));
    await page.locator(`[data-approval-id="${incomplete.id}"] [data-decision="decline"]`).click();
    await page.waitForFunction(id => document.querySelector(`[data-approval-id="${id}"]`).textContent.includes('答复已登记'), incomplete.id);
    check('incomplete operation can be declined', fixture.approvals[a.id][1].decision === 'decline');
    await page.locator('#execution-cancel').click(); await page.waitForFunction(() => document.getElementById('execution-action-status').textContent.includes('取消请求已登记'));
    a.state = 'completed'; a.cleanup_state = 'unverified'; await refresh(page);
    check('completed/cancel race and cleanup uncertainty remain explicit', (await page.locator('#execution-run-state').innerText()).includes('结束先于取消生效') && (await page.locator('#execution-run-state').innerText()).includes('目录仍被占用') && !b.cancel_requested);
    a.state = 'unknown'; a.cleanup_state = 'verified'; await refresh(page);
    check('native unknown has independent new-run entry and no fake resume', await page.locator('#execution-unknown').isVisible() && !(await page.locator('#execution-unknown').innerText()).includes('继续执行'));
    await shot(page, 'unknown', width);
    await go(page, 'agents'); check('provider separates feature and readiness', (await page.locator('#providers-list').innerText()).includes('执行功能已接入') && (await page.locator('#providers-list').innerText()).includes('本次启动结果') && (await page.locator('#providers-list').innerText()).includes('派发 / 审批 / 取消 · 未接入'));
    await go(page, 'usage'); check('execution never initiates a quota query', (await page.locator('#usage-view').innerText()).includes('尚未读取额度') && !report.requests.some(item => item.case === result.name && item.method === 'POST' && item.path.includes('quota')));
  }, width);
  await scenario('owner read-only and source failure', async (page, fixture, check) => {
    const run = makeRun(); fixture.runs = [run]; fixture.capability.available = false; fixture.capability.is_owner = false; fixture.capability.reason = 'other_instance';
    await go(page, 'runs'); await refresh(page); await page.locator(`[data-run-id="${run.id}"]`).click(); await page.waitForFunction(() => document.getElementById('execution-run-meta').textContent.includes('native-thread'));
    check('other owner retains readable history but disables cancel', await page.locator('#execution-cancel').isDisabled() && (await page.locator('#execution-capability').innerText()).includes('只读'));
    fixture.listFail = true; await refresh(page);
    check('source failure preserves last facts with error', (await page.locator('#execution-status').innerText()).includes('暂未更新') && (await page.locator('#execution-run-meta').innerText()).includes('native-thread'));
  });
  await scenario('selection stale replies and inactive view polling', async (page, fixture, check, result) => {
    const a = makeRun({ task_title: 'Fixture · old selection' }), b = makeRun({ task_title: 'Fixture · current selection' }); fixture.runs = [a, b];
    await go(page, 'runs'); await refresh(page);
    let release; fixture.holdDetail = new Promise(resolve => { release = resolve; }); fixture.heldId = a.id;
    await page.locator(`[data-run-id="${a.id}"]`).click();
    await page.waitForFunction(() => document.getElementById('execution-refresh').disabled);
    await page.locator(`[data-run-id="${b.id}"]`).click(); release();
    await page.waitForFunction(() => document.getElementById('execution-run-title').textContent.includes('current selection'));
    check('late selected-run reply never overwrites current selection', (await page.locator('#execution-run-title').innerText()).includes('current selection'));
    await go(page, 'news'); await page.waitForTimeout(1200);
    const count = report.requests.filter(item => item.case === result.name && item.path === base).length;
    await page.waitForTimeout(1200);
    check('polling stops outside run view', report.requests.filter(item => item.case === result.name && item.path === base).length === count);
  });
  assert.equal(report.blocked.length, 0, 'No request may escape the fixture route set');
}
(async () => {
  try { await main(); } catch (error) { report.failures.push({ case: 'startup/global', error: error.stack }); }
  finally {
    if (browser) await browser.close();
    report.finished_at = new Date().toISOString(); report.exit_code = report.failures.length ? 1 : 0;
    fs.mkdirSync(outputDir, { recursive: true });
    const output = path.join(outputDir, `ui-execution-fixture-${stamp}.json`); fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
    console.log(JSON.stringify({ output, checks: report.cases.reduce((sum, item) => sum + item.checks.length, 0), failures: report.failures, exit_code: report.exit_code }, null, 2)); process.exitCode = report.exit_code;
  }
})();
