// Coordinator-owned browser verification. No default endpoint, service, or real data discovery.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const options = {};
for (let index = 2; index < process.argv.length; index += 2) {
  const key = process.argv[index];
  assert.ok(['--base-url', '--output', '--fixture', '--mode'].includes(key), `Unknown option ${key}`);
  assert.ok(process.argv[index + 1] && !options[key], `Missing or duplicate option ${key}`);
  options[key] = process.argv[index + 1];
}
assert.ok(options['--fixture'] && options['--output'] && options['--mode'], 'Pass --mode protocol|http --fixture <absolute JSON> --output <absolute directory>; http also requires --base-url.');
assert.ok(path.isAbsolute(options['--fixture']) && path.isAbsolute(options['--output']), 'Fixture and output paths must be absolute.');
assert.ok(['protocol', 'http'].includes(options['--mode']), 'Mode must be protocol or http.');
const fixture = JSON.parse(fs.readFileSync(options['--fixture'], 'utf8'));
const output = options['--output'];
fs.mkdirSync(output, { recursive: true });
const report = { owner: 'coordinator executing harness', mode: options['--mode'], started_at: new Date().toISOString(), command: process.argv.join(' '), fixture_path: options['--fixture'], operations: [], checks: [], requests: [], console: [], expected_failures: [], blocked: [], screenshots: [], failures: [] };
const check = (name, condition) => { report.checks.push({ name, passed: Boolean(condition) }); assert.ok(condition, name); };
function protocol() {
  const source = fs.readFileSync(path.join(__dirname, '../../web/workspace/workspace.js'), 'utf8');
  const functions = ['projectBody', 'taskSummary', 'observationSummary'].map(name => {
    const value = source.match(new RegExp(`function ${name}\\([^]*?\\n\\}`)); assert.ok(value, name); return value[0];
  }).join('\n');
  const fields = { 'project-source-kind': { value: 'none' }, 'project-name': { value: ' Sample ' }, 'project-archived': { checked: false }, 'project-source-directory': { value: 'main' }, 'project-source-path': { value: 'progress.json' } };
  const sandbox = { app: { projectId: 'stable' }, $: id => fields[id], directoryRows: () => [{ dataset: { bindingId: 'main' }, querySelector: () => ({ value: 'C:/example/synthetic/project' }) }], stateNames: { todo: '待办', doing: '进行中', blocked: '阻塞', review: '待验收', done: '完成' } };
  vm.createContext(sandbox); vm.runInContext(functions, sandbox);
  const body = JSON.parse(vm.runInContext('JSON.stringify(projectBody())', sandbox));
  check('complete save body keeps stable ID and main binding', body.id === 'stable' && body.directories[0].id === 'main');
  check('none source clears binding and path', body.source.directory_id === null && body.source.relative_path === null);
  fields['project-source-kind'].value = 'wfc';
  check('preset uses fixed relative path', vm.runInContext('projectBody().source.relative_path', sandbox) === 'docs/current');
  fields['project-source-kind'].value = 'json';
  check('JSON retains explicitly selected file', vm.runInContext('projectBody().source.relative_path', sandbox) === 'progress.json');
  check('no tasks is not an overall progress percentage', sandbox.taskSummary({ total: 0 }) === '任务统计：没有任务');
  check('source zero remains a source report', sandbox.observationSummary({ status: 'available', reported: { percent: 0, state: 'completed' } }).includes('来源报告 completed · 0%'));
  check('unknown source is explicit', sandbox.observationSummary({ status: 'missing' }).includes('文件或目录缺失'));
  report.operations.push({ name: 'pure implementation protocol checks', network: false, browser: false });
}
async function http() {
  assert.ok(options['--base-url'], 'HTTP mode requires an explicit --base-url.');
  const base = new URL(options['--base-url']);
  assert.ok(base.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname) && base.pathname === '/' && !base.search && !base.hash && !base.username && !base.password, 'Use an explicit loopback HTTP origin only.');
  assert.ok(fixture.project_root && fixture.rebound_root && fixture.extra_root, 'HTTP fixture needs project_root/rebound_root/extra_root absolute temporary paths.');
  const { chromium } = require('playwright');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, serviceWorkers: 'block' });
  const expectedPaths = new Set();
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== base.origin) { report.blocked.push({ url: request.url(), reason: 'outside explicit origin' }); await route.abort(); return; }
    const item = { method: request.method(), url: request.url(), path: url.pathname, client: request.headers()['x-session-client'] };
    if (request.method() === 'POST') item.body = request.postDataJSON(); report.requests.push(item);
    if (/^\/api\/(sessions|workbench|launch|garden)|^\/api\/workspace\/execution/.test(url.pathname)) { report.failures.push(`Forbidden profile request: ${url.pathname}`); await route.abort(); return; }
    // Same-origin API and static resources are handled by the coordinator's own server.
    await route.continue();
  });
  const page = await context.newPage(); page.setDefaultTimeout(15000);
  page.on('pageerror', error => report.failures.push(error.stack));
  page.on('console', message => report.console.push({ type: message.type(), text: message.text(), url: message.location().url }));
  page.on('response', response => { if (response.status() >= 400) { const item = { status: response.status(), url: response.url(), expected: expectedPaths.has(new URL(response.url()).pathname) }; if (item.expected) report.expected_failures.push(item); else report.failures.push(item); } });
  const step = async (name, callback) => { const item = { name, started_at: new Date().toISOString() }; report.operations.push(item); await callback(); item.finished_at = new Date().toISOString(); };
  const refresh = async () => { await page.locator('#refresh-workspace').click(); await page.waitForFunction(() => !document.getElementById('refresh-workspace').disabled); };
  const settle = async () => page.waitForFunction(() => document.getElementById('workspace-status').textContent.includes('采样于'));
  const submitProject = async () => { await page.locator('#project-form button[type=submit]').click(); await page.locator('#project-form').waitFor({ state: 'hidden' }); };
  const saveManual = async () => { await page.locator('#manual-form button[type=submit]').click(); await page.waitForFunction(() => document.getElementById('manual-feedback').textContent.includes('已保存')); };
  const apiGet = async url => page.evaluate(async value => { const response = await fetch(value, { headers: { 'X-Session-Client': 'panel' } }); if (!response.ok) throw new Error(`GET ${value}: ${response.status}`); return response.json(); }, url);
  const screenshot = async (name, widths = [1440, 768, 375, 320]) => { for (const width of widths) { await page.setViewportSize({ width, height: 1000 }); const file = path.join(output, `${name}-${width}.png`); await page.screenshot({ path: file, fullPage: true }); report.screenshots.push(file); check(`${name} no horizontal overflow at ${width}`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)); } await page.setViewportSize({ width: 1440, height: 1000 }); };
  try {
    await step('open explicit profile server', async () => { await page.goto(`${base.origin}/workspace`, { waitUntil: 'domcontentloaded' }); await settle(); check('profile path is explicit', (await page.locator('#profile-path').textContent()).includes('data-dir')); check('execution navigation hidden', await page.locator('[data-view=runs]').isHidden()); });
    const initial = await apiGet('/api/profile');
    if (initial.can_import) {
      check('empty profile has all three entry points', await page.locator('#profile-empty').isVisible()); await screenshot('empty-workspace');
      await page.goto(`${base.origin}/progress`); await page.locator('.profile-info').waitFor(); check('empty progress stage hidden', await page.locator('#progress-stage').isHidden()); check('no personal launch buttons', await page.locator('#launch-zone button').count() === 0); await screenshot('empty-progress');
      await page.goto(`${base.origin}/workspace`); await settle();
    }
    if (fixture.import_source_dir && initial.can_import) await step('import complete synthetic legacy management data', async () => {
      await page.locator('[data-view=settings]').click(); await page.locator('#import-source-dir').fill(fixture.import_source_dir); await page.locator('#import-submit').click(); await page.waitForFunction(() => document.getElementById('import-feedback').textContent.includes('已提交'));
      check('backup and recovery command visible', (await page.locator('#import-result').textContent()).includes('session_panel.profile restore'));
      for (const [key, value] of Object.entries(fixture.expected_import_counts || {})) check(`complete imported count ${key}`, (await page.locator('#import-result').textContent()).includes(`${key}：${value}`));
    });
    if (fixture.codex_db_path) await step('settings failure retains draft, successful config persists after reload', async () => {
      await page.locator('[data-view=settings]').click(); await page.locator('#codex-enabled').check(); await page.locator('#codex-db-path').fill('relative-invalid.sqlite'); expectedPaths.add('/api/profile/settings'); await page.locator('#settings-form button[type=submit]').click(); await page.waitForFunction(() => document.getElementById('settings-feedback').classList.contains('error')); check('settings draft retained', await page.locator('#codex-db-path').inputValue() === 'relative-invalid.sqlite');
      expectedPaths.delete('/api/profile/settings'); await page.locator('#codex-db-path').fill(fixture.codex_db_path); await page.locator('#settings-form button[type=submit]').click(); await page.waitForFunction(() => document.getElementById('settings-feedback').textContent.includes('已保存')); await page.reload(); await settle(); await page.locator('[data-view=settings]').click(); check('configured path reloaded', await page.locator('#codex-db-path').inputValue() === fixture.codex_db_path);
    });
    const name = fixture.project_name || '兼容验收 <literal> 项目'; let id;
    await step('create project with stable bindings and independent unknown progress', async () => {
      await page.locator('[data-view=tasks]').click(); await page.locator('#new-project').click(); await page.locator('#project-name').fill(name); await page.locator('[data-binding-id=main] input').fill(fixture.project_root); await page.locator('#add-directory').click(); await page.locator('.directory-row input').nth(1).fill(fixture.extra_root); await page.locator('#project-name').press('Enter'); await page.locator('#project-form').waitFor({ state: 'hidden' });
      const snapshot = await apiGet('/api/workspace'), project = snapshot.projects.items.find(item => item.name === name); assert.ok(project); id = project.id; report.project_id = id; report.binding_ids = project.directories.map(item => item.id); check('unknown manual is still null', project.percent === null && project.state === null); check('literal project text', await page.locator('#projects-list literal').count() === 0);
    });
    await step('manual note-only NULL, genuine zero, clearing, and history', async () => {
      await page.locator('#manual-state').selectOption('在做'); await page.locator('#manual-note').fill('人工说明，不从 PASS 推导完成'); await saveManual();
      let value = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check('note-only save keeps NULL', value.percent === null && value.state === '在做');
      await page.locator('#manual-percent').fill('0'); await saveManual(); value = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check('zero is a real manual value', value.percent === 0);
      await page.locator('#clear-percent').click(); await saveManual(); value = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check('clear preserves explicit NULL', value.percent === null); const history = await apiGet(`/api/workspace/progress/history?project_id=${id}&offset=0&limit=20`); check('actual changes create independent history', history.total >= 3);
      for (let index = 0; index < 19; index++) { await page.locator('#manual-note').fill(`分页历史合成记录 ${index}`); await saveManual(); }
      await Promise.all([page.waitForResponse(response => response.url().includes('/api/workspace/progress/history?') && new URL(response.url()).searchParams.get('offset') === '20'), page.locator('#history-pager').getByRole('button', { name: '下一页' }).click()]); await page.waitForFunction(() => document.getElementById('history-pager').textContent.includes('21–')); check('manual history next page is operable', (await page.locator('#history-pager').textContent()).includes('21'));
      await screenshot('manual-workspace');
    });
    await step('rebind keeps project and binding IDs; exercise sources', async () => {
      const row = page.locator(`[data-project-id="${id}"]`); await row.getByRole('button', { name: '编辑项目 / 来源' }).click(); await page.locator('[data-binding-id=main] input').fill(fixture.rebound_root); await submitProject();
      let project = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check('rebind preserves directory binding IDs', JSON.stringify(project.directories.map(item => item.id)) === JSON.stringify(report.binding_ids));
      for (const [kind, relative] of [['wfc', 'docs/current'], ['text', fixture.text_path || 'progress.txt'], ['json', fixture.json_path || 'progress.json']]) {
        await row.getByRole('button', { name: '编辑项目 / 来源' }).click(); await page.locator('#project-source-kind').selectOption(kind); if (kind !== 'wfc') await page.locator('#project-source-path').fill(relative); await submitProject(); project = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check(`${kind} observation remains independent`, project.percent === null && project.state === '在做' && project.observation.kind === kind); report.operations.push({ name: `${kind} source result`, observation: project.observation });
      }
      await row.getByRole('button', { name: '编辑项目 / 来源' }).click(); await page.locator('#project-source-kind').selectOption('text'); await page.locator('#project-source-path').fill('../outside.txt'); expectedPaths.add('/api/workspace/projects/save'); await page.locator('#project-form button[type=submit]').click(); await page.waitForFunction(() => document.getElementById('project-feedback').classList.contains('error')); check('bad source draft retained', await page.locator('#project-source-path').inputValue() === '../outside.txt'); expectedPaths.delete('/api/workspace/projects/save'); await page.locator('#cancel-project').click();
    });
    await step('manual task creation, archive editing and restore', async () => {
      await page.locator('#new-task').click(); await page.locator('#task-title').fill('稳定原项目的人工任务'); await page.locator('#task-project').selectOption(id); await page.locator('#task-form button[type=submit]').click(); await page.locator('#task-form').waitFor({ state: 'hidden' });
      await page.locator(`[data-project-id="${id}"]`).getByRole('button', { name: '归档项目', exact: true }).click(); await page.locator(`[data-project-id="${id}"]`).getByRole('button', { name: '恢复项目', exact: true }).waitFor(); await page.locator('#task-scope').selectOption('archived');
      const task = page.locator('.task-row').filter({ hasText: '稳定原项目的人工任务' }); await task.getByRole('button', { name: /^编辑任务 / }).click(); check('archived original ID option retained', await page.locator('#task-project').inputValue() === id); await page.locator('#task-state').selectOption('done'); await page.locator('#task-form button[type=submit]').click(); await page.locator('#task-form').waitFor({ state: 'hidden' });
      await page.locator(`[data-project-id="${id}"]`).getByRole('button', { name: '恢复项目', exact: true }).click(); await page.locator(`[data-project-id="${id}"]`).getByRole('button', { name: '归档项目', exact: true }).waitFor(); await page.locator('#task-scope').selectOption('all');
    });
    if (fixture.orphan_id) await step('orphan task and manual retain original ID, then restore registration', async () => {
      await page.locator('#task-scope').selectOption('orphan'); const orphan = page.locator('.task-row').filter({ hasText: fixture.orphan_id }).first(); if (await orphan.count()) { await orphan.getByRole('button', { name: /^编辑任务 / }).click(); check('orphan original task ID retained', await page.locator('#task-project').inputValue() === fixture.orphan_id); await page.locator('#task-state').selectOption('review'); await page.locator('#task-form button[type=submit]').click(); await page.locator('#task-form').waitFor({ state: 'hidden' }); }
      await page.locator('#compatibility summary').click(); const row = page.locator(`[data-orphan-id="${fixture.orphan_id}"]`); if (await row.getByRole('button', { name: '编辑原人工记录' }).count()) { await row.getByRole('button', { name: '编辑原人工记录' }).click(); await page.locator('#manual-note').fill('孤立记录保留原 ID'); await saveManual(); }
      await row.getByRole('button', { name: '恢复原 ID 登记' }).click(); await page.locator('#project-name').fill('恢复登记'); await page.locator('[data-binding-id=main] input').fill(fixture.orphan_root || `${fixture.extra_root}-orphan`); await submitProject(); check('orphan recovered with original ID', (await apiGet('/api/workspace')).projects.items.some(item => item.id === fixture.orphan_id)); await page.locator('#task-scope').selectOption('all');
    });
    await step('paginated collections remain accessible', async () => {
      for (const [name, pager, view] of [['tasks', '#tasks-pager', 'tasks'], ['records', '#records-pager', 'usage'], ['todos', '#todos-pager', 'overview']]) {
        await page.locator(`[data-view=${view}]`).click(); const next = page.locator(pager).getByRole('button', { name: '下一页' }); let pages = 0;
        while (await next.isEnabled()) { const [response] = await Promise.all([page.waitForResponse(response => response.url().includes(`/api/workspace/${name}?`) && response.status() === 200), next.click()]); const offset = Number(new URL(response.url()).searchParams.get('offset')); await page.waitForFunction(({ selector, start }) => document.querySelector(selector).textContent.includes(`${start}–`), { selector: pager, start: offset + 1 }); pages++; assert.ok(pages <= 2000, 'pagination bounded by import budget'); }
        if (pages) check(`${name} next page has positive offset`, report.requests.some(item => item.path === `/api/workspace/${name}` && Number(new URL(item.url).searchParams.get('offset')) > 0));
        if (name === 'tasks' && fixture.expected_import_counts?.tasks > 500) { check('task beyond first 500 is reachable', report.requests.some(item => item.path === '/api/workspace/tasks' && Number(new URL(item.url).searchParams.get('offset')) >= 500)); await page.locator('.task-row').first().getByRole('button', { name: /^编辑任务 / }).click(); await page.locator('#task-state').selectOption('review'); await page.locator('#task-form button[type=submit]').click(); await page.locator('#task-form').waitFor({ state: 'hidden' }); }
        if (name === 'records' && fixture.expected_import_counts?.manual_records > 100) { check('record beyond first 100 is reachable', report.requests.some(item => item.path === '/api/workspace/records' && Number(new URL(item.url).searchParams.get('offset')) >= 100)); await page.locator('.record-row').first().getByRole('button', { name: /编辑/ }).click(); await page.locator('#record-note').fill('超过首100条的记录可编辑'); await page.locator('#record-form button[type=submit]').click(); await page.locator('#record-form').waitFor({ state: 'hidden' }); }
      }
    });
    await page.locator('[data-view=tasks]').click(); await page.locator(`[data-project-id="${id}"]`).getByRole('button', { name: '人工记录与历史' }).click(); await screenshot('projects-final');
    await step('second page preserves NULL on note-only save and disables personal entrypoints', async () => {
      await page.goto(`${base.origin}/progress`); await page.locator('.profile-info').waitFor(); await page.getByRole('button').filter({ hasText: name }).first().click(); await page.waitForFunction(value => document.querySelector('.hero h2').textContent === value, name); check('progress NULL is explicit', await page.locator('#progress-percent-enabled').isChecked() === false); await page.locator('#progress-note').fill('旧页只改说明'); await page.locator('#progress-manual-state').selectOption('在做'); await page.locator('#progress-manual-form button[type=submit]').click(); await page.waitForFunction(() => document.getElementById('progress-manual-feedback').textContent.includes('已保存')); check('note-only old-page edit keeps NULL', (await apiGet('/api/workspace')).projects.items.find(item => item.id === id).percent === null); check('no null% text', !(await page.locator('body').textContent()).includes('null%')); check('no personal launcher buttons', await page.locator('#launch-zone button').count() === 0); await screenshot('progress-final');
    });
    await step('new document load reads actual stored values', async () => { await page.goto(`${base.origin}/workspace`); await settle(); const project = (await apiGet('/api/workspace')).projects.items.find(item => item.id === id); check('stored identity, rebind and NULL survive page reload', project.directories[0].path === fixture.rebound_root && project.note === '旧页只改说明' && project.percent === null); report.persisted_project = project; });
    check('quota cache and public news requests retained', report.requests.some(item => item.path === '/api/workspace/usage') && report.requests.some(item => item.path === '/api/workspace/news'));
    for (const error of report.console.filter(item => item.type === 'error')) { const expected = report.expected_failures.some(item => item.url === error.url && error.text.includes(`status of ${item.status}`)); if (!expected) report.failures.push(error); }
  } finally { await context.close(); await browser.close(); }
}
(async () => { try { if (options['--mode'] === 'protocol') protocol(); else await http(); } catch (error) { report.failures.push(error.stack); } finally { report.finished_at = new Date().toISOString(); fs.writeFileSync(path.join(output, 'profile-report.json'), JSON.stringify(report, null, 2)); console.log(JSON.stringify({ mode: report.mode, checks: report.checks.length, failures: report.failures, report: path.join(output, 'profile-report.json') }, null, 2)); process.exitCode = report.failures.length ? 1 : 0; } })();
