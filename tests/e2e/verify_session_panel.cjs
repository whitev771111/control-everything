// Controlled HTTP and browser fixtures only. Never connects to production 8766.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('playwright');

const root = path.resolve(__dirname, '../..');
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const output = path.join(root, 'work', `session-panel-fixture-${stamp}.json`);
const ids = Array.from({ length: 48 }, (_, index) => `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`);
const fixtureSource = { host: 'synthetic.invalid', user: 'fixture', config_dir: '/synthetic/config' };
const categories = ['confirmed', 'pending', 'retained', 'unknown'];
const report = { scope: 'controlled loopback HTTP / Edge fixtures; no real messages or focus', owner: 'root/coordinator executing harness',
  started_at: new Date().toISOString(), command: `${process.execPath} tests/e2e/verify_session_panel.cjs ${process.argv.slice(2).join(' ')}`,
  fixture_only: true, private_content_used: false, production_port_used: false, cases: [], failures: [], limitations: [],
  syntax: [], requests: [], static_responses: [], blocked_requests: [], cleanup: {}, hashes: {} };
const python = String.raw`
import copy, json, sys, threading
from datetime import datetime, timezone
from session_panel.http.gateway import make_server
import session_panel.http.gateway as fixture_gateway

def now(): return datetime.now(timezone.utc).isoformat()
state = {"snapshot": {}, "message_total": 45, "message_reason": None, "revision": "fixture-v1", "focus_reason": None,
         "workbench": {}, "connect_reason": None, "connect_launched": True, "codex_items": [], "codex_available": True}
lock = threading.Lock()
class FakeWorkbench:
    def public_status(self):
        with lock: result = copy.deepcopy(state["workbench"])
        if isinstance(result, dict): result["observed_at"] = now()
        return result
class FakeTerminal:
    workbench = FakeWorkbench()
    def connect_workbench(self):
        with lock:
            reason = state["connect_reason"]; launched = state["connect_launched"]
            if reason is None:
                state["workbench"].update({"status": "observed", "server_state": "running", "gui_state": "observed", "gui_count": 1,
                                          "interactive_client_count": 1, "association_state": "unique_observed", "can_connect": False,
                                          "can_focus": True, "age_ms": 0, "reason": None})
        return {"connected": reason is None, "launched": launched if reason is None else False,
                "status": "unknown" if reason else "unique_observed", "reason": reason,
                "workbench": self.workbench.public_status(), "cleanup": "unverified" if reason == "cleanup_unverified" else "not_needed"}
    def focus_session(self, service, session_id):
        with lock: reason = state["focus_reason"]
        return {"focused": reason is None, "status": "unknown" if reason else "confirmed", "reason": reason,
                "session_id": session_id, "pane_id": "terminal_fixture", "observed_at": now()}
class FakeService:
    terminal = FakeTerminal()
    def list_sessions(self):
        with lock: result = copy.deepcopy(state["snapshot"])
        result["observed_at"] = now()
        for row in result.get("sessions", []):
            row["observed_at"] = now(); row["last_success_at"] = now()
        return result
    def get_session_status(self, session_id):
        return {"status": "observed", "session_id": session_id}
    def get_session_messages(self, session_id, limit=20, offset=None, revision=None):
        with lock: value = copy.deepcopy(state)
        reason = value["message_reason"]
        if revision is not None and revision != value["revision"]: reason = "revision_mismatch"
        if reason:
            return {"available": False, "reason": reason, "items": [], "total_visible": None,
                    "next_offset": None, "read_at": now()}
        total = value["message_total"]; start = max(0, total-limit) if offset is None else min(offset, total)
        end = min(start+limit, total)
        items = [{"type": "assistant", "uuid": "synthetic-message-"+str(index), "session_id": session_id,
                  "message": {"content": [{"type": "text", "text": "<script>synthetic text only</script> message "+str(index)},
                                           {"type": "tool_use", "name": "synthetic-tool", "input": {"text": "fixture"}}]},
                  "parent_tool_use_id": None, "parent_agent_id": None} for index in range(start,end)]
        return {"available": True, "items": items, "offset": start, "limit": limit, "total_visible": total,
                "next_offset": end if end < total else None, "revision": value["revision"],
                "reader_version": "0.2.163", "read_at": now()}
def fake_codex_threads(offset=0):
    with lock: value = copy.deepcopy(state)
    items = value["codex_items"]
    available = value["codex_available"]
    page = items[offset:offset+12] if available else []
    return {"available": available, "reason": None if available else "codex_index_unavailable", "source": "codex_local",
            "sampled_at": now() if available else None, "items": page, "offset": offset, "limit": 12,
            "total_visible": len(items) if available else None,
            "next_offset": offset+len(page) if available and page and offset+len(page)<len(items) else None,
            "coverage": {"total_count": len(items) if available else None, "archived_count": 0 if available else None,
                         "excluded_subagent_count": 0 if available else None, "excluded_other_source_count": 0 if available else None}}
fixture_gateway.list_codex_threads = fake_codex_threads
server = make_server(FakeService(), ("127.0.0.1",0))
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
print(json.dumps({"ready": True, "origin": server.origin}), flush=True)
try:
    for line in sys.stdin:
        command = json.loads(line)
        if command.get("stop"):
            print(json.dumps({"seq": command["seq"], "stopping": True}), flush=True); break
        with lock: state.update(command.get("state", {}))
        print(json.dumps({"seq": command["seq"], "updated": True}), flush=True)
finally:
    server.shutdown(); server.server_close(); thread.join(timeout=3)
`;

function snapshot(count = 4, options = {}) {
  const records = ids.slice(0, count).map((sessionId, index) => {
    const category = options.allConfirmed ? 'confirmed' : categories[index % 4];
    return { session_id: sessionId, source: fixtureSource, cwd: '/same/synthetic-project', slot: 'same-slot',
      registration_state: category === 'retained' ? 'missing' : category === 'unknown' ? 'unknown' : 'present',
      identity_state: category === 'pending' ? 'conflict' : 'verified', process_state: 'observed_running',
      terminal_binding: category === 'confirmed' ? 'confirmed' : 'unknown', pane_id: `terminal_fixture_${index}`,
      activity_state: 'unknown', task_completion: 'unknown', age_ms: options.age ?? 0,
      reason: category === 'retained' ? 'registration_missing' : category === 'pending' ? 'identity_conflict' : null,
      reason_codes: category === 'retained' ? ['registration_missing'] : category === 'pending' ? ['identity_conflict'] : [],
      provenance: { registrations: category === 'retained' ? [] : [{ session_id: sessionId }] },
      last_evidence: { slot: 'old-slot', reason: 'source_unavailable', reason_codes: ['source_unavailable'],
        observed_at: '2026-01-01T00:00:00Z', registration_state: 'unknown' } };
  });
  return { status: options.status || 'observed', reason: options.reason || null, source: fixtureSource, age_ms: options.age ?? 0,
    observation_revision: 'synthetic-observation', sessions: records,
    coverage: { registration_count: records.filter(row => row.registration_state === 'present').length,
      process_count: count + 2, confirmed_binding_count: records.filter(row => row.terminal_binding === 'confirmed').length, warnings: [] } };
}
function workbench(overrides = {}) {
  return { session: 'fixture-session', status: 'observed', server_state: 'running', gui_state: 'observed', gui_count: 1,
    interactive_client_count: 1, unmanaged_mux_count: 0, client_scope: 'interactive_only', watcher_count: null, gui_visibility: 'unverified',
    association_state: 'unique_observed', observed_at: new Date().toISOString(), age_ms: 0, can_connect: false, can_focus: true,
    reason: null, actions_blocked: false, ...overrides };
}
function codexThreads(count) {
  return ids.slice(0, count).map(threadId => ({ thread_id: threadId, session_key: `codex:${threadId}`,
    cwd: '/synthetic/codex-project', created_at: '2026-01-01T00:00:00+00:00', updated_at: '2026-01-02T00:00:00+00:00',
    archived: false, source_kind: 'vscode', activity_state: 'unknown', task_completion: 'unknown' }));
}
const disconnectedWorkbench = overrides => workbench({ gui_state: 'absent', gui_count: 0, interactive_client_count: 0,
  association_state: 'disconnected', can_connect: true, can_focus: false, ...overrides });

async function startFixture() {
  const child = spawn(path.join(root, '.venv', 'Scripts', 'python.exe'), ['-B', '-u', '-c', python],
    { cwd: root, stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONIOENCODING: 'utf-8' } });
  const pending = new Map(); let sequence = 0; let stderr = '';
  child.stderr.on('data', chunk => { stderr += chunk.toString(); });
  const lines = readline.createInterface({ input: child.stdout });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('fixture startup timed out')), 10000);
    lines.on('line', line => {
      let message; try { message = JSON.parse(line); } catch { return reject(new Error('fixture stdout was not JSON')); }
      if (message.ready) { clearTimeout(timer); resolve(message.origin); }
      else if (pending.has(message.seq)) { pending.get(message.seq)(message); pending.delete(message.seq); }
    });
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', code => { clearTimeout(timer); if (code) reject(new Error(`fixture startup exited ${code}`)); });
  });
  const send = value => new Promise((resolve, reject) => {
    const seq = ++sequence;
    const timer = setTimeout(() => { pending.delete(seq); reject(new Error('fixture command timed out')); }, 5000);
    pending.set(seq, message => { clearTimeout(timer); resolve(message); });
    child.stdin.write(JSON.stringify({ ...value, seq }) + '\n');
  });
  let origin;
  try { origin = await ready; } catch (error) { child.stdin.end(); child.kill(); lines.close(); throw error; }
  try {
    assert.match(origin, /^http:\/\/127\.0\.0\.1:\d+$/);
    assert.notEqual(new URL(origin).port, '8766');
  } catch (error) { await send({ stop: true }); child.stdin.end(); lines.close(); throw error; }
  return { origin, update: state => send({ state }), async close() {
    await send({ stop: true }); child.stdin.end();
    const result = await new Promise(resolve => {
      if (child.exitCode !== null) return resolve({ code: child.exitCode });
      const timer = setTimeout(() => { child.kill(); resolve({ code: null, forced: true }); }, 5000);
      child.once('exit', (code, signal) => { clearTimeout(timer); resolve({ code, signal }); });
    });
    lines.close(); report.cleanup.fixture = { ...result, stderr }; assert.equal(result.code, 0, 'owned fixture exits normally');
  } };
}

function installObservation() {
  const observation = window.__fixture = { intervalCallbacks: [], clockOffset: 0, hiddenOverride: null };
  const nativeNow = performance.now.bind(performance);
  Object.defineProperty(performance, 'now', { value: () => nativeNow() + observation.clockOffset });
  const nativeHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden').get;
  Object.defineProperty(document, 'hidden', { get: () => observation.hiddenOverride ?? nativeHidden.call(document) });
  const nativeInterval = window.setInterval;
  window.setInterval = (callback, delay, ...args) => {
    observation.intervalCallbacks.push({ callback, delay }); return nativeInterval(callback, delay, ...args);
  };
}

let fixture, browser, activeCase;
function check(name, value, details = undefined) {
  const item = { name, passed: Boolean(value) }; if (details !== undefined) item.details = details;
  activeCase.checks.push(item);
  if (!item.passed) report.failures.push({ case: activeCase.name, ...item });
}
async function scenario(name, callback) {
  activeCase = { name, checks: [], page_errors: [], expected_page_errors: [], console_errors: [], expected_console_errors: [], raw_console_errors: [], request_start: report.requests.length };
  report.cases.push(activeCase);
  try { await callback(); } catch (error) { check('scenario execution', false, error.stack); }
  activeCase.request_end = report.requests.length;
}
const reads = () => report.requests.filter(item => item.endpoint === 'messages');
const focusRequests = () => report.requests.filter(item => item.endpoint === 'focus');
const connectRequests = () => report.requests.filter(item => item.endpoint === '/api/workbench/connect');
const apiRequests = () => report.requests.filter(item => item.api);
const card = (page, sessionId) => page.locator(`#sessions button[data-session-id="${sessionId}"]`);
const selected = page => page.locator('#session-id').textContent();
const identityView = page => page.evaluate(() => ({
  records: [
    ...Array.from(document.querySelectorAll('#sessions button[data-session-id]'), button => ({ sessionId: button.dataset.sessionId, category: button.closest('[data-category]').dataset.category })),
    ...Array.from(document.querySelectorAll('#codex-list button[data-session-key]'), button => ({ sessionId: button.dataset.sessionKey, category: 'unknown' }))
  ],
  selected: document.querySelector('#mapping-key').textContent || null,
  pressed: Array.from(document.querySelectorAll('#sessions button[aria-pressed=true],#codex-list button[aria-pressed=true]'), button => button.dataset.sessionKey || button.dataset.sessionId)
}));
const settle = page => page.waitForTimeout(120);
async function refresh(page, value) {
  if (value) await fixture.update({ snapshot: value });
  await page.locator('#refresh-sessions').click();
  await page.waitForFunction(() => !document.querySelector('#refresh-sessions').disabled && !document.querySelector('#refresh-workbench').disabled, null, { polling: 50 });
  await settle(page);
}
async function waitMessages(page) {
  await page.waitForFunction(() => !document.querySelector('#refresh-messages').disabled, null, { polling: 50 });
  await settle(page);
}
async function selectCard(page, sessionId, keyboard = false) {
  if (keyboard) { await card(page, sessionId).focus(); await page.keyboard.press('Enter'); }
  else await card(page, sessionId).click();
  await waitMessages(page); check(`DOM ${keyboard ? 'Enter' : 'click'} selects full synthetic UUID`, await selected(page) === sessionId);
}

async function openPage(options = {}) {
  await fixture.update({ snapshot: options.snapshot || snapshot(), message_total: options.total ?? 45,
    message_reason: null, revision: 'fixture-v1', focus_reason: null, workbench: options.workbench ?? workbench(), connect_reason: null, connect_launched: true,
    codex_items: options.codexItems || [], codex_available: true });
  const context = await browser.newContext({ viewport: { width: options.width || 1024, height: 900 },
    deviceScaleFactor: options.dpr || 1, serviceWorkers: 'block', reducedMotion: options.reducedMotion || 'no-preference' });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: fixture.origin });
  await context.addInitScript(installObservation, options);
  const fault = options.fault || {};
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== fixture.origin) {
      report.blocked_requests.push({ case: activeCase.name, origin: url.origin });
      return route.abort('blockedbyclient');
    }
    const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(messages|focus)$/);
    report.requests.push({ case: activeCase.name, path: url.pathname, method: request.method(), api: url.pathname.startsWith('/api/'),
      endpoint: match ? match[2] : url.pathname, session_id: match ? match[1] : null,
      limit: url.searchParams.get('limit'), offset: url.searchParams.get('offset'), revision: url.searchParams.get('revision'),
      client_header_valid: !url.pathname.startsWith('/api/') || request.headers()['x-session-client'] === 'panel',
      focus_body_empty: match?.[2] === 'focus' ? request.postData() === '{}' : null,
      action_body_empty: match?.[2] === 'focus' || url.pathname === '/api/workbench/connect' ? request.postData() === '{}' : null,
      csrf_present: match?.[2] === 'focus' || url.pathname === '/api/workbench/connect' ? Boolean(request.headers()['x-session-csrf']) : null });
    if (fault.path && url.pathname === fault.path) {
      if (fault.hold) await fault.hold;
      if (fault.abort) return route.abort('failed');
      if (fault.status || fault.body !== undefined) return route.fulfill({ status: fault.status || 200,
        contentType: fault.mime || 'application/json', body: typeof fault.body === 'string' ? fault.body : JSON.stringify(fault.body) });
      if (fault.csp) {
        const response = await route.fetch();
        return route.fulfill({ response, headers: { ...response.headers(), 'content-security-policy': fault.csp } });
      }
    }
    await route.continue();
  });
  const page = await context.newPage(), caseRecord = activeCase;
  const expectedHttpErrors = [], httpFailures = [], responseTasks = [];
  const consoleStart = caseRecord.raw_console_errors.length;
  page.on('pageerror', error => {
    const expected = (options.expectedPageErrors || []).some(pattern => error.message.includes(pattern));
    (expected ? caseRecord.expected_page_errors : caseRecord.page_errors).push(error.message);
  });
  page.on('console', message => {
    if (message.type() === 'error') caseRecord.raw_console_errors.push({ text: message.text(), location: message.location() });
  });
  page.on('response', response => {
    const url = new URL(response.url());
    if (url.pathname.startsWith('/api/') && response.status() >= 400) {
      responseTasks.push(response.json().then(result => { httpFailures.push({ url: response.url(), status: response.status(), reason: result.reason }); })
        .catch(() => { httpFailures.push({ url: response.url(), status: response.status(), reason: null }); }));
    }
    if (!url.pathname.startsWith('/api/')) {
      const headers = response.headers(); report.static_responses.push({ case: caseRecord.name, path: url.pathname,
        status: response.status(), mime: headers['content-type'], csp: headers['content-security-policy'], nosniff: headers['x-content-type-options'] });
    }
  });
  page.setDefaultTimeout(10000);
  try {
    await page.goto(fixture.origin, { waitUntil: 'domcontentloaded' });
    await page.waitForFunction(() => !document.querySelector('#refresh-sessions').disabled && !document.querySelector('#refresh-workbench').disabled, null, { polling: 50 });
    await settle(page);
  } catch (error) { await context.close(); throw error; }
  return { page, fault, expectHttpFailure(pathname, status, reason) {
    expectedHttpErrors.push({ url: new URL(pathname, fixture.origin).href, status, reason, matched: false });
  }, async close() {
    await Promise.allSettled(responseTasks);
    for (const message of caseRecord.raw_console_errors.slice(consoleStart)) {
      const status = message.text.match(/^Failed to load resource: the server responded with a status of (\d+) \(/);
      const expected = status && expectedHttpErrors.find(item => !item.matched && item.url === message.location.url
        && item.status === Number(status[1]) && httpFailures.some(response => response.url === item.url && response.status === item.status && response.reason === item.reason));
      if (expected) {
        expected.matched = true;
        caseRecord.expected_console_errors.push({ ...message, rejection: { url: expected.url, status: expected.status, reason: expected.reason } });
      } else (options.expectedErrors ? caseRecord.expected_console_errors : caseRecord.console_errors).push(message);
    }
    caseRecord.expected_http_errors = [...(caseRecord.expected_http_errors || []), ...expectedHttpErrors];
    caseRecord.http_failures = [...(caseRecord.http_failures || []), ...httpFailures];
    check('retired collaboration UI stays absent', await page.locator('[id^=garden-],[class*=garden-],#observation-view').count() === 0);
    check('no retired canvas, characters or controls', await page.locator('canvas,.garden-bot,#robot-stage,#robot-motion-toggle,[id^=robot-mode-]').count() === 0);
    check('no unexpected page errors', caseRecord.page_errors.length === 0, caseRecord.page_errors);
    check('no unexpected console errors', caseRecord.console_errors.length === 0, caseRecord.console_errors);
    await context.close();
  } };
}

async function basicBehavior() {
  const session = await openPage({ total: 1 }); const { page } = session;
  try {
    check('default selection reads zero messages', reads().length === 0);
    check('default selected detail stays hidden', await page.locator('#selected').isHidden());
    check('same cwd and slot retain independent full UUID buttons', await page.locator('#sessions button[data-session-id]').count() === 4);
    await selectCard(page, ids[0]);
    check('selected UUID issues exactly one read', reads().length === 1 && reads()[0].session_id === ids[0]);
    check('HTML and tool blocks remain literal text', (await page.locator('#messages').textContent()).includes('<script>synthetic text only</script>') && (await page.locator('#messages').textContent()).includes('synthetic-tool'));
    check('no message attachment/script/iframe elements', await page.locator('#messages script,#messages img,#messages iframe').count() === 0);
    check('read time explicitly differs from unprovided message time', (await page.locator('#read-time').textContent()).includes('单条消息时间未提供'));
    await page.locator('#copy-id').click();
    check('copy preserves entire selected UUID', await page.evaluate(() => navigator.clipboard.readText()) === ids[0]);
    const before = reads().length; await card(page, ids[0]).click(); await refresh(page);
    check('same UUID and metadata refresh do not reread messages', reads().length === before);
    check('metadata refresh retains selected UUID', await selected(page) === ids[0]);
    await page.evaluate(() => { window.__fixture.hiddenOverride = true; });
    const hiddenBefore = apiRequests().length;
    await page.evaluate(() => window.__fixture.intervalCallbacks.filter(timer => timer.delay === 10000).forEach(timer => timer.callback()));
    await settle(page); check('hidden page pauses real poll callback', apiRequests().length === hiddenBefore);
    await page.evaluate(() => { window.__fixture.hiddenOverride = false; });
    await selectCard(page, ids[1], true);
    await card(page, ids[0]).focus(); await page.keyboard.press('Space'); await waitMessages(page);
    check('native Space selection preserves full UUID', await selected(page) === ids[0]);
    for (const reason of ['reader_empty_unverified', 'reader_scope_unverified', 'reader_failed']) {
      await fixture.update({ message_reason: reason }); await page.locator('#refresh-messages').click(); await waitMessages(page);
      const notice = await page.locator('#message-notice').textContent();
      check(`${reason} retains old page with explicit warning`, notice.includes(reason) && notice.includes('上次结果') && await page.locator('#messages article').count() === 1);
      check(`${reason} disables paging`, await page.locator('#older').isDisabled() && await page.locator('#newer').isDisabled());
    }
    await fixture.update({ message_reason: null, message_total: 45 });
    const pageStart = reads().length; await page.locator('#refresh-messages').click(); await waitMessages(page);
    for (const button of ['older', 'older', 'newer']) { await page.locator(`#${button}`).click(); await waitMessages(page); }
    const pages = reads().slice(pageStart).map(item => [item.offset === null ? 25 : Number(item.offset), Number(item.limit)]);
    check('45 messages use tail/older/oldest/newer exact boundaries', JSON.stringify(pages) === JSON.stringify([[25, 20], [5, 20], [0, 5], [5, 20]]), pages);
    check('pagination keeps revision after first page', reads().slice(pageStart + 1).every(item => item.revision === 'fixture-v1'));
    await fixture.update({ revision: 'fixture-v2' });
    session.expectHttpFailure(`/api/sessions/${ids[0]}/messages?limit=5&offset=0&revision=fixture-v1`, 409, 'revision_mismatch');
    await page.locator('#older').click(); await waitMessages(page);
    check('revision409 marks old page and disables navigation', (await page.locator('#message-notice').textContent()).includes('revision_mismatch') && await page.locator('#older').isDisabled() && await page.locator('#newer').isDisabled());
    await page.locator('#refresh-messages').click(); await waitMessages(page);
    await fixture.update({ focus_reason: null }); await refresh(page);
    await page.locator('#focus-session').click(); await settle(page);
    check('explicit focus success is confirmed by fake terminal', (await page.locator('#action-notice').textContent()).includes('已确认 Zellij'));
    await fixture.update({ focus_reason: 'binding_changed' }); await refresh(page);
    session.expectHttpFailure(`/api/sessions/${ids[0]}/focus`, 409, 'binding_changed');
    await page.locator('#focus-session').click(); await page.waitForFunction(() => !document.querySelector('#refresh-sessions').disabled); await settle(page);
    check('focus rejection is explicit', (await page.locator('#action-notice').textContent()).includes('binding_changed'));
    check('focus uses UUID POST empty body and process token', focusRequests().every(item => item.method === 'POST' && item.session_id === ids[0] && item.focus_body_empty && item.csrf_present));
    await fixture.update({ focus_reason: 'focus_sent_unverified' }); await refresh(page);
    const unverifiedResponse = page.waitForResponse(response => new URL(response.url()).pathname === `/api/sessions/${ids[0]}/focus` && response.request().method() === 'POST');
    await page.locator('#focus-session').click(); await settle(page);
    const earlyNotice = await page.locator('#action-notice').textContent(), response = await unverifiedResponse;
    const responseBody = await response.json();
    await page.waitForFunction(() => document.querySelector('#action-notice').textContent.includes('focus_sent_unverified'), null, { polling: 50 });
    const finalNotice = await page.locator('#action-notice').textContent();
    check('unverified focus is never declared success', responseBody.focused === false && responseBody.reason === 'focus_sent_unverified'
      && finalNotice.includes('focus_sent_unverified') && !finalNotice.includes('已确认 Zellij'), { early_notice: earlyNotice, response: responseBody, final_notice: finalNotice });
  } finally { await session.close(); }
}

async function classification() {
  for (const count of [0, 1, 2, 4, 7, 13]) {
    const beforeReads = reads().length;
    const session = await openPage({ snapshot: snapshot(count) }); const { page } = session;
    try {
      check(`N=${count} exact DOM UUID population`, await page.locator('#sessions button[data-session-id]').count() === count);
      const view = await identityView(page);
      check(`N=${count} exact DOM identity population and category`, view?.records.length === count && ids.slice(0, count).every((id, index) => view.records.find(row => row.sessionId === id)?.category === categories[index % 4]), view);
      check(`N=${count} never silently reads messages`, reads().length === beforeReads);
      if (count >= 4) {
        check('current retained reason visible without reading', (await card(page, ids[2]).textContent()).includes('registration_missing'));
        check('retained slot explicitly historical', (await card(page, ids[2]).textContent()).includes('上次'));
        check('unknown row does not declare exited', !(await card(page, ids[3]).textContent()).includes('已退出'));
        await selectCard(page, ids[2]);
        check('current and prior reason sections retain sampling distinction', (await page.locator('#evidence').textContent()).includes('last_evidence') && (await page.locator('#selected').textContent()).includes('历史原因发生时间未核验'));
        check('retained row never enables focus', await page.locator('#focus-session').isDisabled());
      }
    } finally { await session.close(); }
  }
  const session = await openPage(); const { page } = session;
  try {
    await selectCard(page, ids[0]); const before = reads().length;
    const missing = snapshot(4); missing.sessions = missing.sessions.filter(row => row.session_id !== ids[0]);
    await refresh(page, missing);
    check('missing selected row keeps UUID and not_found evidence', await selected(page) === ids[0] && (await page.locator('#evidence').textContent()).includes('not_found'));
    check('missing selected row does not select/read another UUID', reads().length === before && await page.locator('#focus-session').isDisabled());
    await refresh(page, snapshot(7)); await refresh(page, snapshot(1)); await refresh(page, snapshot(13));
    check('add/remove/return leaves current UUID population only', (await identityView(page)).records.length === 13 && await page.locator('#sessions button[data-session-id]').count() === 13);
    const old = snapshot(); old.sessions.forEach(row => delete row.registration_state);
    await refresh(page, old);
    check('old API uses current provenance conservatively', (await identityView(page)).records.find(row => row.sessionId === ids[2]).category === 'retained');
    old.sessions.forEach(row => delete row.provenance); await refresh(page, old);
    check('old API with no current provenance is unknown', (await identityView(page)).records.every(row => row.category === 'unknown'));
  } finally { await session.close(); }
}

async function epoch() {
  let release;
  const hold = new Promise(resolve => { release = resolve; });
  const fault = { path: `/api/sessions/${ids[0]}/messages`, hold };
  const session = await openPage({ fault }); const { page } = session;
  try {
    await card(page, ids[0]).click(); await page.waitForTimeout(100);
    await selectCard(page, ids[1]); release(); await settle(page);
    check('late first UUID response never overwrites second UUID', await selected(page) === ids[1] && (await page.locator('#messages').textContent()).includes(ids[1]) && !(await page.locator('#messages').textContent()).includes(ids[0]));
  } finally { release(); await session.close(); }
}

async function apiFailures() {
  for (const item of [
    { name: 'reject', abort: true }, { name: '503', status: 503, body: { status: 'unknown', reason: 'gateway_query_failed' } },
    { name: 'nonJSON', status: 200, body: 'fixture non JSON', mime: 'text/plain' },
    { name: 'invalid sessions shape', status: 200, body: { status: 'observed', sessions: {} } },
    { name: 'unknown status enum', status: 200, body: { status: 'impossible', sessions: [] } },
    { name: 'HTTP200 unknown source', snapshot: snapshot(4, { status: 'unknown', reason: 'source_unavailable' }) },
    { name: 'HTTP200 unknown terminal', snapshot: snapshot(4, { status: 'unknown', reason: 'terminal_unavailable' }) }
  ]) {
    const fault = {}; const session = await openPage({ fault, expectedErrors: true }); const { page } = session;
    try {
      await selectCard(page, ids[0]); const before = reads().length;
      if (item.snapshot) await refresh(page, item.snapshot);
      else { Object.assign(fault, item, { path: '/api/sessions' }); await refresh(page); }
      check(`${item.name} preserves old UUIDs with unknown classification`, await page.locator('#sessions button[data-session-id]').count() === 4 && (await identityView(page)).records.every(row => row.category === 'unknown'));
      check(`${item.name} revokes focus`, await page.locator('#focus-session').isDisabled());
      check(`${item.name} retains page with explicit metadata warning`, await page.locator('#messages article').count() === 20 && (await page.locator('#message-metadata-notice').textContent()).includes('上次读取'));
      check(`${item.name} does not automatically reread messages`, reads().length === before);
      check(`${item.name} marks counts as prior sampling`, (await page.locator('#coverage').textContent()).includes('上次'));
    } finally { await session.close(); }
  }
  for (const age of [5001, 30001]) {
    const session = await openPage({ snapshot: snapshot(4, { age }) }); const { page } = session;
    try {
      await selectCard(page, ids[0]); check(`age=${age} disables focus`, await page.locator('#focus-session').isDisabled());
      if (age > 30000) check('30-second stale overrides every displayed category', (await identityView(page)).records.every(row => row.category === 'unknown'));
    } finally { await session.close(); }
  }
  let release; const hold = new Promise(resolve => { release = resolve; });
  const fault = {}; const session = await openPage({ snapshot: snapshot(0), fault }); const { page } = session;
  try {
    check('empty observed snapshot starts with fresh coverage', !(await page.locator('#coverage').textContent()).includes('上次采样'));
    const beforeReads = reads().length, beforeFocus = focusRequests().length;
    Object.assign(fault, { path: '/api/sessions', hold });
    const request = page.waitForRequest(item => new URL(item.url()).pathname === '/api/sessions');
    await page.locator('#refresh-sessions').click(); await request;
    check('empty snapshot metadata refresh is genuinely pending', await page.locator('#refresh-sessions').isDisabled());
    await page.evaluate(() => {
      window.__fixture.clockOffset += 30001;
      window.__fixture.intervalCallbacks.filter(timer => timer.delay === 1000).forEach(timer => timer.callback());
    });
    check('empty snapshot pending over thirty seconds marks counts as prior sampling', (await page.locator('#coverage').textContent()).includes('上次采样'));
    check('empty snapshot pending over thirty seconds removes fresh connection indicator', !(await page.locator('#connection-dot').getAttribute('class')).split(/\s+/).includes('ok'));
    check('empty snapshot ageing does not read messages or send focus', reads().length === beforeReads && focusRequests().length === beforeFocus);
  } finally { release(); await session.close(); }
}

async function unknownRegistrationFocus(registration) {
  const value = snapshot(1, { allConfirmed: true, age: 10 });
  const row = value.sessions[0];
  if (registration === 'legacy without provenance') { delete row.registration_state; delete row.provenance; }
  else row.registration_state = registration;
  const session = await openPage({ snapshot: value }); const { page } = session;
  try {
    await selectCard(page, ids[0]);
    check(`${registration} retains fresh verified identity and old confirmed binding`, (await page.locator('#facts').textContent()).includes('已核验')
      && (await page.locator('#facts').textContent()).includes('terminal_fixture_0'));
    check(`${registration} current registration category remains unknown`, (await identityView(page)).records.find(row => row.sessionId === ids[0]).category === 'unknown');
    check(`${registration} disables actual focus DOM button`, await page.locator('#focus-session').isDisabled());
    const beforeFocus = focusRequests().length;
    // Force only skips Playwright's enabled wait; the native disabled button still suppresses its click.
    await page.locator('#focus-session').click({ force: true }); await settle(page);
    check(`${registration} native pointer cannot issue focus POST`, focusRequests().length === beforeFocus);
    activeCase.pointer_actions = [{ source: 'native pointer on actual disabled DOM focus button; no synthetic event or focus function call', registration }];
  } finally { await session.close(); }
}

async function refreshWorkbenchFixture(page, value) {
  if (value) await fixture.update({ workbench: value });
  await page.locator('#refresh-workbench').click();
  await page.waitForFunction(() => !document.querySelector('#refresh-workbench').disabled); await settle(page);
}
async function workbenchBehavior() {
  const session = await openPage({ workbench: disconnectedWorkbench({ unmanaged_mux_count: 1 }), width: 360 }); const { page } = session;
  try {
    await selectCard(page, ids[0]);
    check('source observation and disconnected workbench are independent', (await identityView(page)).records.find(row => row.sessionId === ids[0]).category === 'confirmed'
      && (await page.locator('#workbench-state').textContent()).includes('未关联') && await page.locator('#focus-session').isDisabled());
    check('fresh disconnected workbench enables explicit connect', await page.locator('#connect-workbench').isEnabled());
    check('external mux count is distinct and coverage limits are visible', (await page.locator('#workbench-facts').textContent()).includes('外部独立 mux1')
      && (await page.locator('.workbench-panel').textContent()).includes('watcher 覆盖未验证'));
    const beforeReads = reads().length, beforeConnect = connectRequests().length;
    await page.locator('#connect-workbench').click();
    await page.waitForFunction(() => document.querySelector('#workbench-notice').textContent.includes('唯一关联观察')); await settle(page);
    check('explicit connect confirms association and enables eligible UUID focus', await page.locator('#focus-session').isEnabled());
    check('connect retains selected UUID and page without rereading', await selected(page) === ids[0] && reads().length === beforeReads && await page.locator('#messages article').count() === 20);
    check('connect uses one fixed empty-body CSRF POST', connectRequests().length === beforeConnect + 1
      && connectRequests().at(-1).method === 'POST' && connectRequests().at(-1).action_body_empty && connectRequests().at(-1).csrf_present);
    await page.locator('#connect-workbench').click({ force: true }); await settle(page);
    check('already associated native button does not create a second request', connectRequests().length === beforeConnect + 1);
    await refreshWorkbenchFixture(page); check('terminal-only refresh preserves page and selection', reads().length === beforeReads && await selected(page) === ids[0]);
    check('360px workbench has no horizontal overflow', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
  } finally { await session.close(); }

  const race = await openPage({ workbench: disconnectedWorkbench() });
  try {
    await fixture.update({ connect_launched: false }); await race.page.locator('#connect-workbench').click();
    await race.page.waitForFunction(() => document.querySelector('#workbench-notice').textContent.includes('未重复创建窗口'));
    check('backend already-connected race explicitly reports no new GUI', (await race.page.locator('#workbench-notice').textContent()).includes('未重复创建窗口'));
  } finally { await race.close(); }
}
async function workbenchFailures() {
  for (const [label, value] of [
    ['legacy missing fields', {}], ['unknown status', workbench({ status: 'unknown', reason: 'workbench_observation_unknown' })],
    ['future server enum', workbench({ server_state: 'future' })], ['future GUI enum', workbench({ gui_state: 'future' })],
    ['future association enum', workbench({ association_state: 'future' })], ['null age', workbench({ age_ms: null })],
    ['bad client count', workbench({ interactive_client_count: '1' })], ['unknown mux count', disconnectedWorkbench({ unmanaged_mux_count: null })],
    ['missing mux count', workbench({ unmanaged_mux_count: undefined })], ['multiple clients', workbench({ gui_state: 'multiple', gui_count: 2, interactive_client_count: 2, association_state: 'ambiguous', can_focus: false })],
    ['foreign or watcher evidence', workbench({ status: 'unknown', reason: 'workbench_association_unknown', can_focus: false })],
    ['exited backend', disconnectedWorkbench({ status: 'unknown', server_state: 'exited', association_state: 'unknown', can_connect: false })],
    ['missing backend', disconnectedWorkbench({ status: 'unknown', server_state: 'missing', association_state: 'unknown', can_connect: false })],
    ['five-second stale', workbench({ age_ms: 5001 })], ['thirty-second stale', workbench({ age_ms: 30001 })]
  ]) {
    const session = await openPage({ workbench: value }); const { page } = session;
    try {
      await selectCard(page, ids[0]); const beforeReads = reads().length, beforeFocus = focusRequests().length, beforeConnect = connectRequests().length;
      check(`${label} revokes connect and focus`, await page.locator('#connect-workbench').isDisabled() && await page.locator('#focus-session').isDisabled());
      await page.locator('#connect-workbench').click({ force: true }); await page.locator('#focus-session').click({ force: true }); await settle(page);
      check(`${label} actual disabled buttons send no action POST`, focusRequests().length === beforeFocus && connectRequests().length === beforeConnect);
      check(`${label} does not reclassify UUID or reread private page`, (await identityView(page)).records.find(row => row.sessionId === ids[0]).category === 'confirmed' && reads().length === beforeReads && await page.locator('#messages article').count() === 20);
      if (label === 'unknown mux count') check('null external mux count displays unknown instead of zero', (await page.locator('#workbench-facts').textContent()).includes('外部独立 mux未知'));
      if (['unknown status', 'exited backend', 'missing backend'].includes(label)) check(`${label} valid fresh result displays current facts, not prior evidence`, !(await page.locator('#workbench-facts').textContent()).includes('上次证据'));
      if (label === 'thirty-second stale') check('stale terminal values explicitly mark prior evidence', (await page.locator('#workbench-state').textContent()).includes('上次证据'));
    } finally { await session.close(); }
  }
  const fault = {}, session = await openPage({ fault }); const { page } = session;
  try {
    await selectCard(page, ids[0]); const beforeReads = reads().length;
    Object.assign(fault, { path: '/api/workbench', status: 404, body: { reason: 'route_not_found' } });
    session.expectHttpFailure('/api/workbench', 404, 'route_not_found'); await refreshWorkbenchFixture(page);
    check('missing workbench endpoint revokes focus and keeps prior evidence', await page.locator('#focus-session').isDisabled() && (await page.locator('#workbench-state').textContent()).includes('上次证据'));
    check('workbench404 preserves independently observed UUID and page', (await identityView(page)).records.find(row => row.sessionId === ids[0]).category === 'confirmed' && reads().length === beforeReads);
  } finally { await session.close(); }
}
async function workbenchPending() {
  for (const action of ['connect', 'focus']) {
    let release; const hold = new Promise(resolve => { release = resolve; }), fault = {};
    const session = await openPage({ fault, workbench: action === 'connect' ? disconnectedWorkbench() : workbench() }); const { page } = session;
    try {
      await selectCard(page, ids[0]); const beforeReads = reads().length, beforeFocus = focusRequests().length, beforeConnect = connectRequests().length;
      Object.assign(fault, { path: action === 'connect' ? '/api/workbench/connect' : `/api/sessions/${ids[0]}/focus`, hold });
      const request = page.waitForRequest(item => new URL(item.url()).pathname === fault.path);
      await page.locator(action === 'connect' ? '#connect-workbench' : '#focus-session').click(); await request;
      check(`${action} pending disables both terminal actions`, await page.locator('#connect-workbench').isDisabled() && await page.locator('#focus-session').isDisabled());
      await page.locator('#connect-workbench').click({ force: true }); await page.locator('#focus-session').click({ force: true }); await settle(page);
      check(`${action} pending native repeated clicks cannot issue another POST`, focusRequests().length === beforeFocus + (action === 'focus' ? 1 : 0) && connectRequests().length === beforeConnect + (action === 'connect' ? 1 : 0));
      check(`${action} pending preserves selected UUID and message page`, await selected(page) === ids[0] && reads().length === beforeReads && await page.locator('#messages article').count() === 20);
      delete fault.hold; release(); await settle(page);
      await page.waitForFunction(() => !document.querySelector('#refresh-sessions').disabled && !document.querySelector('#refresh-workbench').disabled);
    } finally { release(); await session.close(); }
  }
  let release; const hold = new Promise(resolve => { release = resolve; }), fault = {};
  const session = await openPage({ fault }); const { page } = session;
  try {
    await selectCard(page, ids[0]); Object.assign(fault, { path: '/api/sessions', hold });
    await page.locator('#refresh-sessions').click();
    await page.waitForFunction(() => !document.querySelector('#refresh-workbench').disabled && document.querySelector('#refresh-sessions').disabled);
    const workbenchGets = report.requests.filter(item => item.path === '/api/workbench').length;
    await page.evaluate(() => {
      window.__fixture.clockOffset += 6000;
      window.__fixture.intervalCallbacks.filter(timer => timer.delay === 1000).forEach(timer => timer.callback());
    });
    const beforeReleaseAge = Number((await page.locator('#workbench-state').textContent()).match(/证据年龄 ([\d.]+) 秒/)?.[1]);
    check('late-response clock advance renders actual one-second age callback while source stays pending', beforeReleaseAge >= 6 && await page.locator('#refresh-sessions').isDisabled());
    delete fault.hold; release(); await page.waitForFunction(() => !document.querySelector('#refresh-sessions').disabled); await settle(page);
    const evidenceAge = Number((await page.locator('#workbench-state').textContent()).match(/证据年龄 ([\d.]+) 秒/)?.[1]);
    const details = { before_release_age_seconds: beforeReleaseAge, after_source_age_seconds: evidenceAge, focus_disabled: await page.locator('#focus-session').isDisabled(),
      intervening_workbench_gets: report.requests.filter(item => item.path === '/api/workbench').length - workbenchGets };
    activeCase.late_response_timing = details;
    check('late remote response does not reset earlier workbench evidence age', details.focus_disabled && evidenceAge >= 6 && evidenceAge >= beforeReleaseAge, details);
    check('late-response age proof is not replaced by another workbench request', details.intervening_workbench_gets === 0, details);
  } finally { release(); await session.close(); }
  const cleanup = await openPage({ workbench: disconnectedWorkbench() });
  try {
    await fixture.update({ connect_reason: 'cleanup_unverified' }); cleanup.expectHttpFailure('/api/workbench/connect', 409, 'cleanup_unverified');
    await cleanup.page.locator('#connect-workbench').click(); await cleanup.page.waitForFunction(() => document.querySelector('#workbench-notice').textContent.includes('回收未确认'));
    await refreshWorkbenchFixture(cleanup.page, workbench());
    check('cleanup_unverified remains latched despite later positive payload', await cleanup.page.locator('#connect-workbench').isDisabled() && await cleanup.page.locator('#focus-session').isDisabled());
    check('cleanup UI provides clear fault without automatic restart or unlock', (await cleanup.page.locator('#workbench-notice').textContent()).includes('请核查') && !(await cleanup.page.locator('.workbench-panel').textContent()).includes('解除'));
  } finally { await cleanup.close(); }
}












async function mappingEvidence() {
  const session = await openPage({ snapshot: snapshot(4), codexItems: codexThreads(2) }); const { page } = session;
  const text = id => page.evaluate(id => document.getElementById(id)?.textContent || '', id);
  try {
    await page.locator(`#codex-list button[data-session-key="codex:${ids[0]}"]`).click();
    check('Codex mapping preserves the complete original source key', await text('mapping-key') === `codex:${ids[0]}`);
    check('Codex evidence flow follows selected local source', (await text('flow-heading')).includes('Codex') && !(await text('flow-source')).includes('固定来源'));
    check('Codex missing body and terminal mapping explain capabilities without guessing', (await text('mapping-history')).includes('未接入') && (await text('mapping-terminal')).includes('未接入'));
    check('Codex mapping selection has zero body or action reads', report.requests.slice(activeCase.request_start).every(item => item.method !== 'POST' && item.endpoint !== 'messages'));
    let before = apiRequests().length;
    await page.locator('#copy-mapping-key').focus(); await page.keyboard.press('Enter');
    check('mapping copies full local key through native keyboard without HTTP', await page.evaluate(() => navigator.clipboard.readText()) === `codex:${ids[0]}` && apiRequests().length === before);
    await page.locator('#mapping-detail-link').click();
    check('mapping detail anchor targets existing source detail without HTTP', await page.evaluate(() => location.hash) === '#codex-detail' && apiRequests().length === before);
    await selectCard(page, ids[0]);
    check('same UUID remote selection uses its original separate key and source', await text('mapping-key') === ids[0] && (await text('mapping-source')).includes('synthetic.invalid'));
    check('remote mapping shows independently read history and real pane', (await text('mapping-history')).includes('已读取') && (await text('mapping-terminal')).includes('terminal_fixture_0'));
    before = apiRequests().length; await page.locator('#copy-mapping-key').click();
    check('mapping copies bare remote UUID without carrying local namespace', await page.evaluate(() => navigator.clipboard.readText()) === ids[0] && apiRequests().length === before);
    await refresh(page, snapshot(4, { age: 6000 }));
    check('5-30 second position evidence explains expired action and forbids focus', (await text('mapping-terminal')).includes('5 秒') && await page.locator('#focus-session').isDisabled());
    await refresh(page, snapshot(4, { age: 31000 }));
    check('older metadata remains explicitly old while history stays independent', (await text('mapping-source')).includes('上次') && (await text('mapping-history')).includes('已读取') && await page.locator('#focus-session').isDisabled());
    await refresh(page, snapshot(4));
    await selectCard(page, ids[2]);
    check('retained registration explains why mapping is unavailable', (await text('mapping-terminal')).includes('registration_missing'));
    await selectCard(page, ids[1]);
    check('identity conflict is explained and cannot enable focus', (await text('mapping-terminal')).includes('identity_conflict') && await page.locator('#focus-session').isDisabled());
    await refreshWorkbenchFixture(page, workbench({ status: 'unknown', reason: 'workbench_observation_unknown', can_focus: false }));
    check('workbench unknown is explained independently from history', (await text('mapping-workbench')).includes('workbench_observation_unknown') && (await text('mapping-history')).includes('已读取') && await page.locator('#focus-session').isDisabled());
    await page.locator(`#codex-list button[data-session-key="codex:${ids[0]}"]`).click();
    check('returning to Codex clears remote body and restores local mapping', await text('mapping-key') === `codex:${ids[0]}` && await page.locator('#messages article').count() === 0);
  } finally { await session.close(); }
}


async function mappingWithoutCsrf() {
  const session = await openPage({ snapshot: snapshot(4), fault: { path: '/api/bootstrap', body: { csrf_token: '' } } });
  try {
    await selectCard(session.page, ids[0]);
    check('missing action credential disables button and both capability descriptions', await session.page.locator('#focus-session').isDisabled()
      && await session.page.locator('#flow-terminal').getAttribute('data-available') === 'false'
      && !(await session.page.locator('#flow-terminal .flow-value').textContent()).includes('可定位')
      && (await session.page.locator('#mapping-workbench').textContent()).includes('动作凭据未确认'));
    check('missing credential cannot submit a terminal action', report.requests.slice(activeCase.request_start).every(item => item.method !== 'POST'));
  } finally { await session.close(); }
}

async function mappingResponsive() {
  for (const width of [360, 1024]) {
    const session = await openPage({ width, codexItems: codexThreads(1) });
    try {
      await session.page.locator('#codex-list button').click();
      const layout = await session.page.evaluate(() => ({ columns: getComputedStyle(document.querySelector('.mapping-facts')).gridTemplateColumns.split(' ').length,
        page_width: document.documentElement.scrollWidth, viewport: innerWidth, key: document.querySelector('#mapping-key').textContent }));
      check(`${width}px selected mapping uses planned columns and fits the viewport`, layout.columns === (width === 360 ? 1 : 3)
        && layout.page_width <= layout.viewport && layout.key === `codex:${ids[0]}`, layout);
    } finally { await session.close(); }
  }
}


async function codexIdentity() {
  const session = await openPage({ codexItems: codexThreads(25),  }); const { page } = session;
  const initialActions = { reads: reads().length, focus: focusRequests().length, connect: connectRequests().length };
  try {
    await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 12);
    check('Codex has 12 current metadata cards and 4 remote identities', (await identityView(page)).records.length === 16 && await page.locator('#sessions button[data-session-id]').count() === 4);
    const key = `codex:${ids[0]}`, first = page.locator('#codex-list button').filter({ hasText: ids[0] });
    await first.click(); await settle(page);
    check('same UUID from Codex and remote remains two distinct identities', (await identityView(page)).records.some(item => item.sessionId === key) && (await identityView(page)).records.some(item => item.sessionId === ids[0]));
    check('Codex card selection shows only metadata and disables remote actions', (await page.locator('#codex-detail').textContent()).includes(ids[0]) && await page.locator('#selected').isHidden() && await page.locator('#focus-session').isDisabled() && reads().length === initialActions.reads && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
    await selectCard(page, ids[0]); const beforeReads = reads().length;
    await first.focus(); await page.keyboard.press('Enter'); await settle(page);
    check('Codex keyboard selection clears displayed remote history without rereading', await page.locator('#messages article').count() === 0 && await page.locator('#selected').isHidden() && reads().length === beforeReads);
    const beforePicks = reads().length;
    await page.locator(`#codex-list button[data-session-key="codex:${ids[1]}"]`).click();
    await first.click();
    check('Codex native selection never calls Claude reader or terminal actions', reads().length === beforePicks && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
    await page.locator('#codex-next').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex second page exposes the next 12 exact IDs and clears selection', await page.locator('#codex-list button').count() === 12 && (await page.locator('#codex-list').textContent()).includes(ids[12]) && !(await page.locator('#codex-list').textContent()).includes(ids[0]) && (!(await identityView(page)).selected && (await identityView(page)).pressed.length === 0));
    await page.locator('#codex-next').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex final page exposes the remaining exact ID with no extra roles', await page.locator('#codex-list button').count() === 1 && (await page.locator('#codex-list').textContent()).includes(ids[24]) && await page.locator('#codex-next').isDisabled() && (await identityView(page)).records.length === 5);
    await page.locator('#codex-previous').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex previous-page control preserves complete index access', (await page.locator('#codex-list').textContent()).includes(ids[12]));
    await page.setViewportSize({ width: 360, height: 900 }); await settle(page);
    check('Codex cards and complete IDs remain accessible at 360px', await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  } finally { await session.close(); }
}

async function codexFailures() {
  const session = await openPage({ codexItems: codexThreads(13) }); const { page } = session;
  const initialActions = { reads: reads().length, focus: focusRequests().length, connect: connectRequests().length };
  const refreshCodex = async () => { await page.locator('#refresh-codex').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page); };
  try {
    await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 12);
    await page.locator('#codex-list button').filter({ hasText: ids[0] }).click();
    await page.evaluate(() => { window.__fixture.clockOffset += 30001; window.__fixture.intervalCallbacks.filter(timer => timer.delay === 1000).forEach(timer => timer.callback()); });
    check('expired Codex sampling clears roles and selected metadata', !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')) && (!(await identityView(page)).selected && (await identityView(page)).pressed.length === 0) && await page.locator('#codex-list button').count() === 0);
    await refreshCodex(); check('fresh Codex sampling recovers independently of stale remote evidence', await page.locator('#codex-list button').count() === 12);
    await fixture.update({ codex_available: false }); session.expectHttpFailure('/api/codex/threads?offset=0', 503, 'codex_index_unavailable'); await refreshCodex();
    check('Codex503 clears current evidence while retaining remote UUID cards', await page.locator('#codex-list button').count() === 0 && !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')) && await page.locator('#sessions button[data-session-id]').count() === 4);
    const bad = codexThreads(1); bad[0].session_key = `codex:${ids[1]}`; await fixture.update({ codex_available: true, codex_items: bad }); await refreshCodex();
    check('invalid Codex namespace response cannot become a role', await page.locator('#codex-list button').count() === 0 && !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')));
    const badType = codexThreads(1); badType[0].thread_id = [ids[0]]; await fixture.update({ codex_items: badType }); await refreshCodex();
    check('array UUID is rejected instead of coerced into a Codex identity', await page.locator('#codex-list button').count() === 0 && !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')));
    await fixture.update({ codex_items: [] }); await refreshCodex(); check('valid empty Codex page is explicit and creates no fabricated identity', await page.locator('#codex-list button').count() === 0 && (await identityView(page)).records.length === 4);
    check('Codex expiry and failures never query private messages or send actions', reads().length === initialActions.reads && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
  } finally { await session.close(); }
}

async function codexLateResponses() {
  let release; const hold = new Promise(resolve => { release = resolve; }); const fault = {};
  const session = await openPage({ codexItems: codexThreads(13), fault }); const { page } = session;
  const initialActions = { reads: reads().length, focus: focusRequests().length, connect: connectRequests().length };
  try {
    await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 12);
    Object.assign(fault, { path: '/api/codex/threads', hold }); await page.locator('#codex-next').click(); await page.waitForTimeout(120);
    await page.evaluate(() => { window.__fixture.clockOffset += 30001; window.__fixture.intervalCallbacks.filter(timer => timer.delay === 1000).forEach(timer => timer.callback()); });
    release(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('a valid Codex page arriving after 30seconds cannot restore identities or selection', await page.locator('#codex-list button').count() === 0 && !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')) && (!(await identityView(page)).selected && (await identityView(page)).pressed.length === 0));
    check('late Codex page rejection does not read private messages or send actions', reads().length === initialActions.reads && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
  } finally { release(); await session.close(); }
  let releaseShort; const shortHold = new Promise(resolve => { releaseShort = resolve; }); const shortFault = {};
  const delayed = await openPage({ codexItems: codexThreads(13), fault: shortFault });
  try {
    const page = delayed.page; await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 12);
    Object.assign(shortFault, { path: '/api/codex/threads', hold: shortHold }); await page.locator('#codex-next').click(); await page.waitForTimeout(120);
    await page.evaluate(() => { window.__fixture.clockOffset += 20000; }); releaseShort();
    await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex sampling within the 30second budget can be accepted', await page.locator('#codex-list button').count() === 1);
    await page.evaluate(() => { window.__fixture.clockOffset += 11000; window.__fixture.intervalCallbacks.filter(timer => timer.delay === 1000).forEach(timer => timer.callback()); });
    check('accepted Codex evidence age includes the preceding request wait', await page.locator('#codex-list button').count() === 0 && !(await identityView(page)).records.some(item => item.sessionId.startsWith('codex:')));
  } finally { releaseShort(); await delayed.close(); }
  let releaseMessage; const messageHold = new Promise(resolve => { releaseMessage = resolve; }); const messageFault = { path: `/api/sessions/${ids[0]}/messages`, hold: messageHold };
  const remote = await openPage({ codexItems: codexThreads(1), fault: messageFault });
  try {
    const page = remote.page; await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 1);
    await card(page, ids[0]).click(); await page.waitForTimeout(100); await page.locator('#codex-list button').click(); releaseMessage(); await settle(page);
    check('late remote message response cannot overwrite Codex identity selection', (await identityView(page)).selected === `codex:${ids[0]}` && await page.locator('#selected').isHidden() && await page.locator('#messages article').count() === 0);
  } finally { releaseMessage(); await remote.close(); }
}

async function main() {
  if (process.argv.slice(2).some(argument => argument !== '--fixture')) throw new Error('Only fixture mode is accepted; live mode is intentionally absent.');
  for (const filename of ['web/sessions/app.js', 'tests/e2e/verify_session_panel.cjs']) {
    const result = spawnSync(process.execPath, ['--check', path.join(root, filename)], { encoding: 'utf8' });
    report.syntax.push({ filename, exit_code: result.status, stdout: result.stdout, stderr: result.stderr });
    if (result.status !== 0) throw new Error(`${filename} syntax check failed`);
  }
  for (const filename of ['web/sessions/index.html', 'web/sessions/style.css', 'web/sessions/app.js']) {
    report.hashes[filename] = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, filename))).digest('hex');
  }
  fixture = await startFixture();
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  await scenario('retired collaboration bootstrap field is ignored', async () => {
    const session = await openPage({ fault: { path: '/api/bootstrap', body: { csrf_token: 'fixture', garden_root: ids[0] } } });
    try {
      check('session observation still renders', await session.page.locator('#sessions button[data-session-id]').count() > 0);
    } finally { await session.close(); }
  });
  await scenario('reader / 45 boundary / focus fixtures', basicBehavior);
  await scenario('dynamic 0/1/2/4/7/13 categories / old API / missing selection', classification);
  await scenario('late selection epoch response', epoch);
  await scenario('API failure / unknown / ages', apiFailures);
  await scenario('workbench explicit connect / repeated connect / UUID page retention', workbenchBehavior);
  await scenario('workbench unknown fields / clients / stale / endpoint404', workbenchFailures);
  await scenario('workbench pending action lock / late age / cleanup latch', workbenchPending);
  for (const registration of ['unknown', 'future_enum', 'legacy without provenance']) {
    await scenario(`unknown registration revokes focus: ${registration}`, () => unknownRegistrationFocus(registration));
  }
  await scenario('selected source / full key / capability / evidence age mapping', mappingEvidence);
  await scenario('missing action credential capability consistency', mappingWithoutCsrf);
  await scenario('selected mapping responsive computed layout', mappingResponsive);
  await scenario('Codex metadata namespace / keyboard / all pages', codexIdentity);
  await scenario('Codex metadata expired / unavailable / malformed / empty', codexFailures);
  await scenario('Codex delayed page and old remote message response isolation', codexLateResponses);
  await scenario('same-origin static / retired assets / network guard', async () => {
    for (const resource of ['/app.js', '/style.css']) {
      const response = report.static_responses.find(item => item.path === resource && item.status === 200);
      check(`${resource} HTTP200 correct MIME CSP nosniff`, response && (resource.endsWith('.js') ? /javascript/ : /css/).test(response.mime) && response.nosniff === 'nosniff' && response.csp.includes("default-src 'self'") && response.csp.includes("img-src 'none'"), response);
    }
    check('retired rendering resources are never requested', report.requests.every(item => !/^\/(?:cockpit\.js|robot3d\.js|vendor\/three-)/.test(item.path)));
    check('retired collaboration resources are never requested', report.requests.every(item => !/^\/(?:garden\.js|api\/garden(?:\/|$))/.test(item.path)));
    check('all API requests carry existing client header', report.requests.every(item => item.client_header_valid));
    check('no external or production requests escaped network guard', report.blocked_requests.length === 0, report.blocked_requests);
  });
}

(async () => {
  try { await main(); }
  catch (error) { report.failures.push({ case: activeCase?.name || 'startup', name: 'fatal', passed: false, details: error.stack }); }
  finally {
    if (browser) { try { await browser.close(); report.cleanup.browser_closed = true; } catch (error) { report.failures.push({ name: 'browser cleanup', details: error.message }); } }
    if (fixture) { try { await fixture.close(); } catch (error) { report.failures.push({ name: 'fixture cleanup', details: error.message }); } }
    report.finished_at = new Date().toISOString(); report.exit_code = report.failures.length ? 1 : 0;
    report.checks_count = report.cases.reduce((count, item) => count + item.checks.length, 0);
    fs.writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
    console.log(JSON.stringify({ output, checks: report.checks_count, failures: report.failures, limitations: report.limitations, exit_code: report.exit_code }, null, 2));
    process.exitCode = report.exit_code;
  }
})();
