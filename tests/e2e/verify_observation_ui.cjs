// Coordinator-only browser harness. All resources and API responses are synthetic.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');
if (process.argv.length !== 3 || process.argv[2] !== '--fixture') throw new Error('Use --fixture; this harness intercepts every request.');
const root = path.resolve(__dirname, '../..');
const output = path.join(root, 'work/native-agent-runtime-20261016/root/registration/ui-fixture', new Date().toISOString().replace(/[:.]/g, '-'));
const origin = 'http://observation.fixture';
const report = { fixture_only: true, real_daemon_verified: false, cases: [], failures: [], requests: [] };
const segment = { items: [], total: 0, offset: 0, limit: 20 };
let browser;
(async () => {
  fs.mkdirSync(output, { recursive: true });
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  for (const width of [1440, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 960 }, serviceWorkers: 'block' });
    const page = await context.newPage(), errors = [];
    let lists = 0, returns = 0, revision = 1;
    const registration = { id: 'a'.repeat(32), category: 'custom_exe', provider_id: null, environment_id: 'windows:local', label: 'Example host', locator_kind: 'executable', path: 'C:/example/host.exe', enabled: true, binding_state: 'confirmed' };
    let registrations = [registration];
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== origin) throw new Error('Unexpected external request');
      const send = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
      if (url.pathname === '/workspace') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(path.join(root, 'web/workspace/workspace.html')) });
      if (['/workspace.css', '/workspace.js', '/execution.js', '/observation.js'].includes(url.pathname)) return route.fulfill({ contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript', body: fs.readFileSync(path.join(root, 'web/workspace', url.pathname.slice(1))) });
      if (url.pathname === '/favicon.ico') return route.fulfill({ status: 204 });
      assert.equal(request.headers()['x-session-client'], 'panel');
      report.requests.push({ width, method: request.method(), path: url.pathname });
      if (request.method() === 'POST') assert.equal(request.headers()['x-session-csrf'], 'fixture');
      if (url.pathname === '/api/bootstrap') return send({ csrf_token: 'fixture', capabilities: { profile: true, execution: false } });
      if (url.pathname === '/api/profile') return send({ data_dir: 'C:/example/profile', config: { codex: { enabled: false, db_path: null } }, can_import: true, integrations: {} });
      if (url.pathname === '/api/workspace') return send({ status: 'ok', sampled_at: new Date().toISOString(), today: '2026-10-16',
        projects: { status: 'available', items: [{ id: 'alpha', name: '合成项目', archived: false, records: [], records_available: true, task_counts: { total: 0, done: 0 }, observation: { status: 'not_configured' } }] },
        tasks: { ...segment, status: 'available' }, todos: { ...segment, status: 'available' }, providers: [],
        sessions: { available: false, items: [], reason: 'not_configured' } });
      if (['/api/workspace/tasks', '/api/workspace/todos', '/api/workspace/records', '/api/workspace/compatibility'].includes(url.pathname)) return send(segment);
      if (url.pathname === '/api/workspace/usage') return send({ status: 'ok', codex: { status: 'not_checked', buckets: [] }, manual_records: [], manual_status: 'available', manual_total: 0, manual_limit: 50 });
      if (url.pathname === '/api/workspace/news') return send({ status: 'unavailable', items: [], sources: [] });
      if (url.pathname === '/api/profile/observations') {
        if (request.method() === 'POST') { const body = request.postDataJSON(); assert.equal(body.expected_revision, revision); assert.equal(body.expected_hash, 'hash-' + revision); registrations = body.registrations; revision++; }
        return send({ revision, hash: 'hash-' + revision, registrations, validation: [], application: { status: 'pending', reason: 'config_pending' } });
      }
      if (url.pathname === '/api/profile/observations/candidates') return send({ status: 'available', reason: null,
        config_revision: revision, config_hash: 'hash-' + revision, coverage: { status: 'complete', omitted: 0 },
        candidates: [{ provider_id: 'codex', locator_kind: 'npm_command', version: null,
          path: 'C:/example/' + 'long-installation-directory-'.repeat(8) + '/codex.cmd' }] });
      if (url.pathname === '/api/workspace/observations') return send({ config_revision: revision, config_hash: 'hash-' + revision, registrations: registrations.map(row => ({ registration_id: row.id, category: row.category, label: row.label, status: 'available', reason: null, enabled: row.enabled, binding_state: row.binding_state, can_pick: row.enabled })), status: 'available', reason: null, generation: 'g', seq: 3, sampled_at_ms: Date.now(), coverage: { status: 'complete' },
        installations: [{ provider_id: 'hermes', status: 'available', version: '1.0' }], resources: [{ resource_id: 'r', cpu_percent_one_core: null, rss_bytes: 1048576, threads: 2, handles: 4, metrics_interval_ms: 1000 }],
        instances: [{ instance_id: 'i', provider_id: 'hermes', kind: 'cli', pid: 123, resource_id: 'r', status: 'available', project_id: 'alpha', return_action: null }], project_refs: [{ project_id: 'alpha', instance_ids: ['i'] }], unassigned: [] });
      if (url.pathname === '/api/workspace/observations/window-choices') {
        lists++;
        assert.deepEqual(request.postDataJSON(), { registration_id: registration.id });
        return send({ config_revision: revision, config_hash: 'hash-' + revision, status: 'available', reason: null, generation: 'g', selection_id: 'selection', expires_at_ms: Date.now() + 9000,
          coverage: { status: 'complete', omitted: 0 }, choices: [{ action_id: 'manual', issued_seq: 1, title: '<script>synthetic literal title</script>', expires_at_ms: Date.now() + 9000 }] });
      }
      if (url.pathname === '/api/workspace/observations/window-choices/close') return send({ status: 'available' });
      if (url.pathname === '/api/workspace/observations/return') {
        returns++;
        assert.deepEqual(request.postDataJSON(), { action_id: 'manual', generation: 'g', seq: 1 });
        return send({ status: 'unknown', reason: 'return_unknown' }, 409);
      }
      throw new Error(`Unexpected route ${url.pathname}`);
    });
    await page.goto(origin + '/workspace');
    await page.locator('[data-project-id="alpha"] .native-project-facts').waitFor();
    assert.equal(lists, 0);
    await page.getByRole('button', { name: 'Agents', exact: true }).click();
    await page.getByRole('heading', { name: '本机工具与 Agent 观察' }).waitFor();
    assert.match(await page.locator('#native-instances').textContent(), /Hermes.*会话未知.*待处理未知/s);
    await page.getByRole('button', { name: '选择工具窗口', exact: true }).click();
    await page.locator('.window-choice-title').waitFor();
    assert.equal(await page.locator('.window-choice-title').textContent(), '<script>synthetic literal title</script>');
    assert.equal(await page.locator('#window-choices-list script').count(), 0);
    const pickerOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(pickerOverflow, false);
    await page.screenshot({ path: path.join(output, `picker-${width}.png`), fullPage: true });
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    assert.equal(await page.locator('#window-choices-list').textContent(), '');
    await page.getByRole('button', { name: '选择工具窗口', exact: true }).click();
    await page.getByRole('button', { name: '选择并返回此窗口', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('observation-feedback').textContent.includes('不会自动重试'));
    assert.equal(returns, 1);
    assert.equal(await page.locator('#window-choices-list').textContent(), '');
    await page.locator('#open-registration-settings').click();
    await page.locator('#registration-settings[open]').waitFor();
    await page.locator('#registration-candidates .button').waitFor();
    await page.screenshot({ path: path.join(output, `registration-candidates-${width}.png`), fullPage: true });
    const settingsOverflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(settingsOverflow, false);
    await page.getByRole('button', { name: '修改', exact: true }).click();
    await page.locator('#registration-label').fill('Updated host');
    await page.locator('#registration-confirm').check();
    await page.getByRole('button', { name: '保存登记', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('registrations-feedback').textContent.includes('登记已保存'));
    assert.equal(registrations[0].label, 'Updated host');
    await page.getByRole('button', { name: '禁用', exact: true }).click();
    await page.getByRole('button', { name: '启用', exact: true }).waitFor();
    assert.equal(registrations[0].enabled, false);
    await page.getByRole('button', { name: '删除登记', exact: true }).click();
    await page.waitForFunction(() => document.getElementById('registrations-list').children.length === 0);
    assert.equal(registrations.length, 0);
    await page.screenshot({ path: path.join(output, `registration-${width}.png`), fullPage: true });
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(overflow, false);
    assert.deepEqual(errors, []);
    report.cases.push({ width, literal_title: true, default_no_list: true, cleared_after_close_and_action: true, no_action_retry: true,
      page_errors: errors, overflow, picker_overflow: pickerOverflow, settings_overflow: settingsOverflow });
    await context.close();
  }
})().catch(error => { report.failures.push(error.stack); process.exitCode = 1; }).finally(async () => {
  if (browser) await browser.close();
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report, null, 2));
});
