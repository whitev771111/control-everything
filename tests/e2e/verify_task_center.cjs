// Root-only Edge harness. HTTP mode requires an explicitly owned synthetic profile.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const options = {};
for (let index = 2; index < process.argv.length; index++) {
  const key = process.argv[index];
  assert.ok(['--fixture', '--owned-fixture', '--base-url', '--output'].includes(key) && !options[key], `Unknown/duplicate option ${key}`);
  options[key] = ['--fixture', '--owned-fixture'].includes(key) ? true : process.argv[++index];
}
assert.ok(options['--output'] && path.isAbsolute(options['--output']), 'Pass an absolute --output directory.');
assert.ok(Boolean(options['--fixture']) !== Boolean(options['--base-url']), 'Choose --fixture OR --base-url <loopback origin> --owned-fixture.');
assert.ok(options['--fixture'] || options['--owned-fixture'], 'HTTP writes require --owned-fixture; never point at a personal profile.');
const root = path.resolve(__dirname, '../..'), output = options['--output'];
const origin = new URL(options['--base-url'] || 'http://task-center.fixture');
assert.ok(origin.protocol === 'http:' && !origin.username && !origin.password && origin.pathname === '/' && !origin.search && !origin.hash);
if (!options['--fixture']) assert.ok(['127.0.0.1', 'localhost', '[::1]'].includes(origin.hostname), 'HTTP mode only accepts loopback.');
const report = { mode: options['--fixture'] ? 'synthetic-routes' : 'owned-profile-http', checks: [], failures: [], requests: [], screenshots: [], real_provider_verified: false };
const check = (label, value) => { report.checks.push({ label, passed: Boolean(value) }); assert.ok(value, label); };
const states = ['todo', 'doing', 'blocked', 'review', 'done'];
const initialItems = Array.from({ length: 532 }, (_, index) => ({ id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`,
  origin: index < 31 ? 'todo' : 'task', origin_id: index === 0 ? '' : index === 1 || index === 31 ? 'same' : `raw-${index}`,
  title: index === 0 ? '  合成原值\r\n' + '中😀'.repeat(1000) + '  ' : `合成事项 ${index}`,
  project_id: index % 3 ? 'alpha' : null, provider_id: null, state: states[index % 5], session_key: null, source: null,
  created_at: null, updated_at: null, todo_position: index < 31 ? index : null, sort_index: index, resume_state: 'todo', revision: 1, session_link_state: 'none' }));
let browser;
(async () => {
  fs.mkdirSync(output, { recursive: true });
  const { chromium } = require('playwright');
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 }, serviceWorkers: 'block' });
    const page = await context.newPage(), consoleErrors = [];
    let items = structuredClone(initialItems), listRevision = 1, writes = 0, failWrite = false;
    const projects = [{ id: 'alpha', name: '合成项目', archived: false, directories: [{ id: 'main', path: 'C:/example/project' }], source: { kind: 'none', directory_id: null, relative_path: null }, state: '在做', percent: 23, manual_recorded: true, records: [], records_available: true }];
    const counts = () => ({ total: items.filter(row => row.project_id === 'alpha').length, ...Object.fromEntries(states.map(state => [state, items.filter(row => row.project_id === 'alpha' && row.state === state).length])) });
    const getPage = query => {
      const scope = query.get('scope') || 'all', project = query.get('project_id'), provider = query.get('provider_id'), state = query.get('state');
      const rows = items.filter(row => (!project || row.project_id === project) && (!provider || row.provider_id === provider) && (!state || row.state === state)
        && (scope === 'all' || scope === 'global' && !row.project_id || scope === 'active' && projects.some(project => project.id === row.project_id && !project.archived) || scope === 'archived' && projects.some(project => project.id === row.project_id && project.archived) || scope === 'orphan' && row.project_id && !projects.some(project => project.id === row.project_id)));
      const offset = Number(query.get('offset') || 0), limit = Number(query.get('limit') || 50);
      return { items: rows.slice(offset, offset + limit), total: rows.length, offset, limit, list_revision: listRevision };
    };
    const segment = { items: [], total: 0, offset: 0, limit: 50 };
    page.on('pageerror', error => consoleErrors.push(error.message));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin.origin) { report.failures.push('external request blocked'); return route.abort(); }
      const method = request.method(); report.requests.push({ width, method, path: url.pathname });
      if (/^\/api\/workspace\/execution|^\/api\/(launch|workbench|sessions)(?:\/|$)/.test(url.pathname)) { report.failures.push('unexpected runtime request'); return route.abort(); }
      if (url.pathname === '/api/workspace/items' || url.pathname === '/api/workspace/items/update') {
        if (method === 'POST') { writes++; if (failWrite) { failWrite = false; return route.abort('failed'); } }
      }
      if (!options['--fixture']) return route.continue();
      const send = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
      const fail = reason => send({ status: 'unknown', reason, source: 'synthetic', observed_at: null }, 409);
      if (url.pathname === '/workspace') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/workspace/workspace.html')) });
      if (['/workspace.css', '/workspace.js', '/execution.js', '/observation.js', '/task-center.js'].includes(url.pathname)) return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, 'web/workspace', url.pathname.slice(1))) });
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 });
      assert.equal(request.headers()['x-session-client'], 'panel');
      if (method === 'POST') assert.equal(request.headers()['x-session-csrf'], 'fixture');
      if (url.pathname === '/api/bootstrap') return send({ csrf_token: 'fixture', capabilities: { profile: true, execution: false } });
      if (url.pathname === '/api/profile') return send({ data_dir: 'C:/example/profile', config: { codex: { enabled: false, db_path: null } }, can_import: false, integrations: {} });
      if (url.pathname === '/api/workspace') { const first = getPage(new URLSearchParams()); return send({ status: 'ok', sampled_at: new Date().toISOString(), today: '2026-10-17', projects: { status: 'available', items: projects.map(project => ({ ...project, task_counts: counts() })) }, tasks: { ...segment, status: 'available' }, todos: { ...segment, status: 'available' }, items: first.items, items_page: first, task_providers: ['codex', 'claude', 'grok', 'dsh', 'opencode', 'hermes', 'pi'], providers: [], sessions: { available: false, items: [] } }); }
      if (url.pathname === '/api/workspace/items' && method === 'GET') {
        if (Number(url.searchParams.get('offset') || 0) > 0 && Number(url.searchParams.get('list_revision')) !== listRevision) return fail('list_changed');
        return send(getPage(url.searchParams));
      }
      if (url.pathname === '/api/workspace/items' && method === 'POST') {
        const body = request.postDataJSON(), id = `10000000-0000-4000-8000-${String(listRevision).padStart(12, '0')}`;
        const created = { ...initialItems[0], id, origin: 'task', origin_id: id, title: body.title, project_id: null, provider_id: null, session_key: null, state: 'todo', revision: 1, ...body };
        items.unshift(created); listRevision++; return send({ item: created });
      }
      if (url.pathname === '/api/workspace/items/update') {
        const body = request.postDataJSON(), current = items.find(row => row.id === body.id);
        if (current.revision !== body.expected_revision) return fail('item_changed');
        Object.assign(current, body.changes); current.revision++; listRevision++; return send({ item: current });
      }
      if (url.pathname === '/api/workspace/projects/save') { Object.assign(projects.find(project => project.id === request.postDataJSON().id), request.postDataJSON()); listRevision++; return send({ status: 'ok', project: projects[0] }); }
      if (url.pathname === '/api/profile/observations') return send({ revision: 0, hash: '', registrations: [], validation: [], application: { status: 'pending', reason: 'not_configured' } });
      if (url.pathname === '/api/workspace/observations') return send({ status: 'unavailable', reason: 'not_configured', coverage: { status: 'unknown' }, installations: [], instances: [], resources: [], project_refs: [], unassigned: [], registrations: [] });
      if (url.pathname === '/api/workspace/usage') return send({ status: 'ok', codex: { status: 'not_checked', buckets: [] }, manual_records: [], manual_status: 'available', manual_total: 101, manual_limit: 50 });
      if (url.pathname === '/api/workspace/news') return send({ status: 'unavailable', items: [], sources: [] });
      if (['/api/workspace/compatibility', '/api/workspace/records', '/api/workspace/progress/history'].includes(url.pathname)) return send(segment);
      throw new Error(`Unexpected synthetic route ${url.pathname}`);
    });
    await page.goto(origin.origin + '/workspace#tasks');
    await page.locator('#items-list [data-item-id]').first().waitFor();
    const call = (method, route, body) => page.evaluate(async ({ method, route, body }) => {
      const headers = { 'X-Session-Client': 'panel' };
      if (method === 'POST') { const bootstrap = await (await fetch('/api/bootstrap', { headers })).json(); headers['X-Session-CSRF'] = bootstrap.csrf_token; headers['Content-Type'] = 'application/json'; }
      const response = await fetch(route, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
      return { status: response.status, body: await response.json() };
    }, { method, route, body });
    const baseline = (await call('GET', '/api/workspace')).body;
    check(`${width}: full fixture contains 532 original items`, baseline.items_page.total >= 532);
    check(`${width}: no old profile task/todo reads`, !report.requests.some(row => row.width === width && ['/api/workspace/tasks', '/api/workspace/todos'].includes(row.path)));
    await page.locator('#item-quick-title').fill('快速全局 ' + '😀'.repeat(2000 - 5));
    await page.getByRole('button', { name: '快速记录', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('item-quick-title').value === '');
    const created = (await call('GET', '/api/workspace/items?offset=0&limit=50')).body.items[0];
    check(`${width}: quick item is global`, created.project_id === null);
    await page.locator('#items-list [data-item-id]').first().getByRole('button', { name: '编辑详情' }).click();
    await page.locator('#item-provider_id').selectOption('hermes'); await page.locator('#item-state').selectOption('review');
    await page.getByRole('button', { name: '保存事项', exact: true }).click(); await page.locator('#item-editor').waitFor({ state: 'hidden' });
    await page.locator('#items-list [data-item-id]').first().getByRole('button', { name: '编辑详情' }).click();
    await page.locator('#item-title').fill('保留的冲突草稿');
    const current = (await call('GET', '/api/workspace/items?offset=0&limit=50')).body.items[0];
    const peer = await context.newPage(); await peer.goto(origin.origin + '/workspace#tasks');
    await peer.locator(`#items-list [data-item-id="${current.id}"]`).getByRole('button', { name: '编辑详情' }).click();
    await peer.locator('#item-state').selectOption('blocked');
    await peer.getByRole('button', { name: '保存事项', exact: true }).click(); await peer.locator('#item-editor').waitFor({ state: 'hidden' });
    await page.getByRole('button', { name: '保存事项', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('item-feedback').textContent.includes('另一处修改'));
    check(`${width}: CAS keeps draft`, await page.locator('#item-title').inputValue() === '保留的冲突草稿');
    await page.getByRole('button', { name: '取消', exact: true }).click();
    const project = baseline.projects.items.find(row => !row.archived);
    assert.ok(project, 'Owned fixture must contain an active project.');
    await page.locator('#items-list [data-item-id]').first().getByRole('button', { name: '编辑详情' }).click();
    await page.locator('#item-title').fill('项目变化期间保留的草稿');
    for (const archived of [true, false]) {
      await peer.locator('[data-view="projects"]').click();
      await peer.locator(`[data-project-id="${project.id}"]`).getByRole('button', { name: archived ? '归档项目' : '恢复项目', exact: true }).click();
      await peer.locator(`[data-project-id="${project.id}"]`).getByRole('button', { name: archived ? '恢复项目' : '归档项目', exact: true }).waitFor();
      await page.locator('#items-pager').getByRole('button', { name: '下一页', exact: true }).click();
      await page.waitForFunction(() => document.getElementById('items-status').textContent.includes('第一页'));
      check(`${width}: project ${archived ? 'archive' : 'unarchive'} page conflict keeps draft`, await page.locator('#item-title').inputValue() === '项目变化期间保留的草稿');
    }
    await peer.close();
    await page.getByRole('button', { name: '取消', exact: true }).click();
    const beforeFailure = writes; failWrite = true;
    await page.locator('#item-quick-title').fill('断网草稿'); await page.getByRole('button', { name: '快速记录', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('item-quick-feedback').textContent.includes('未自动重试'));
    check(`${width}: failed create keeps draft and sends once`, await page.locator('#item-quick-title').inputValue() === '断网草稿' && writes === beforeFailure + 1);
    await page.locator('#item-quick-title').fill('');
    const overflow = async selector => page.locator(selector).evaluate(element => element.scrollWidth > element.clientWidth + 1);
    check(`${width}: task panel fits`, !await overflow('#task-center-root'));
    await page.screenshot({ path: path.join(output, `tasks-${width}.png`), fullPage: true }); report.screenshots.push(`tasks-${width}.png`);
    await page.locator('#items-list [data-item-id]').first().getByRole('button', { name: '编辑详情' }).click();
    check(`${width}: editor fits`, !await overflow('#item-editor'));
    await page.screenshot({ path: path.join(output, `editor-${width}.png`), fullPage: true }); report.screenshots.push(`editor-${width}.png`);
    await page.getByRole('button', { name: '取消', exact: true }).click();
    await page.locator('[data-view="projects"]').click();
    check(`${width}: task counts do not overwrite manual percent`, (await call('GET', '/api/workspace')).body.projects.items.find(row => row.id === project.id).percent === project.percent);
    check(`${width}: one manual editor`, await page.locator('#manual-form').count() === 1);
    check(`${width}: no page errors`, consoleErrors.length === 0);
    await context.close();
  }
})().catch(error => { report.failures.push(error.stack); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ mode: report.mode, checks: report.checks.length, failures: report.failures, output }));
  if (report.failures.length) process.exitCode = 1;
});
