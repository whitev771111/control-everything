// Controlled HTTP and browser fixtures only. Never connects to production 8766.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const readline = require('node:readline');
const { spawn, spawnSync } = require('node:child_process');
const { chromium } = require('C:/example/user/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const stamp = new Date().toISOString().replace(/[:.]/g, '-');
const output = path.join(root, 'work', `robot-cockpit-fixture-r1-${stamp}.json`);
const ids = Array.from({ length: 48 }, (_, index) => `10000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`);
const fixtureSource = { host: 'synthetic.invalid', user: 'fixture', config_dir: '/synthetic/config' };
const categories = ['confirmed', 'pending', 'retained', 'unknown'];
const report = { scope: 'controlled loopback HTTP / Edge fixtures; no real messages or focus', owner: 'root/coordinator executing harness',
  started_at: new Date().toISOString(), command: `${process.execPath} tests/verify_robot_cockpit.cjs ${process.argv.slice(2).join(' ')}`,
  fixture_only: true, private_content_used: false, production_port_used: false, cases: [], failures: [], limitations: [],
  syntax: [], requests: [], static_responses: [], blocked_requests: [], cleanup: {}, hashes: {} };
const python = String.raw`
import copy, json, sys, threading
from datetime import datetime, timezone
from session_panel.gateway import make_server
import session_panel.gateway as fixture_gateway

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
  return { session: 'workbench-v4', status: 'observed', server_state: 'running', gui_state: 'observed', gui_count: 1,
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

function installObservation(options) {
  const observation = window.__fixture = { views: [], canvasFrames: [], intervalCallbacks: [],
    clockOffset: 0, hiddenOverride: null, glCanvases: [], contextsLost: 0, contextsRestored: 0, drawCount: 0, renderedFrames: [] };
  const nativeNow = performance.now.bind(performance);
  let manualTime = 1000;
  Object.defineProperty(performance, 'now', { value: () => (options.manualFrames ? manualTime : nativeNow()) + observation.clockOffset });
  if (options.manualFrames) {
    const callbacks = new Map(); let nextId = 0;
    window.requestAnimationFrame = callback => { callbacks.set(++nextId, callback); return nextId; };
    window.cancelAnimationFrame = id => callbacks.delete(id);
    observation.advanceFrame = ms => {
      manualTime += ms; const pending = [...callbacks.values()]; callbacks.clear();
      pending.forEach(callback => callback(manualTime));
    };
  }
  const nativeHidden = Object.getOwnPropertyDescriptor(Document.prototype, 'hidden').get;
  Object.defineProperty(document, 'hidden', { get: () => observation.hiddenOverride ?? nativeHidden.call(document) });
  const nativeInterval = window.setInterval;
  window.setInterval = (callback, delay, ...args) => {
    observation.intervalCallbacks.push({ callback, delay }); return nativeInterval(callback, delay, ...args);
  };
  let cockpit;
  Object.defineProperty(window, 'SessionCockpit', { configurable: true, get: () => cockpit, set: value => {
    cockpit = value;
    const nativeCreate = value.create;
    value.create = args => {
      if (options.cockpit === 'create') throw new Error('fixture cockpit create failure');
      const instance = nativeCreate(args); const update = instance.update.bind(instance);
      instance.update = view => {
        observation.views.push(JSON.parse(JSON.stringify(view)));
        if (options.cockpit === 'update') throw new Error('fixture cockpit update failure');
        return update(view);
      };
      observation.cockpit = instance; return instance;
    };
  } });
  if (options.captureThree && location.protocol === 'http:') {
    observation.threeReady = import(location.origin + '/vendor/three-0.186.0/three.module.js').then(THREE => {
      observation.THREE = THREE;
      observation.batchDisposed = 0;
      const nativeBatchDispose = THREE.InstancedMesh.prototype.dispose;
      THREE.InstancedMesh.prototype.dispose = function () { observation.batchDisposed++; return nativeBatchDispose.call(this); };
      // Renderer.render is an instance method in this fixed package. Observe its real matrix updates.
      const nativeSceneUpdate = THREE.Scene.prototype.updateMatrixWorld;
      THREE.Scene.prototype.updateMatrixWorld = function (...args) {
        observation.scene = this;
        const result = nativeSceneUpdate.apply(this, args);
        let robot;
        this.traverse(item => { if (!robot && item.userData.sessionId) robot = item; });
        observation.renderedFrames.push({ time: performance.now(), camera: observation.camera?.position.toArray(), modelY: robot?.position.y,
          modelPosition: robot?.position.toArray(), limbPitch: robot?.userData.limbs?.map(limb => limb.rotation.x) });
        return result;
      };
  const nativeProjection = THREE.OrthographicCamera.prototype.updateProjectionMatrix;
      THREE.OrthographicCamera.prototype.updateProjectionMatrix = function (...args) {
        observation.camera = this; return nativeProjection.apply(this, args);
      };
      const nativePerspective = THREE.PerspectiveCamera.prototype.updateProjectionMatrix;
      THREE.PerspectiveCamera.prototype.updateProjectionMatrix = function (...args) {
        observation.camera = this; return nativePerspective.apply(this, args);
      };
    });
  }
  const nativeContext = HTMLCanvasElement.prototype.getContext;
  HTMLCanvasElement.prototype.getContext = function (type, ...args) {
    if (type === 'webgl2' && options.webgl === 'null') return null;
    if (type === 'webgl2' && options.webgl === 'throw') throw new Error('fixture WebGL2 getContext failure');
    if (type === '2d' && options.pixel === 'null') return null;
    if (type === '2d' && options.pixel === 'throw') throw new Error('fixture Canvas2D getContext failure');
    const context = nativeContext.call(this, type, ...args);
    if (!context) return context;
    if (type === 'webgl2' && !observation.glCanvases.includes(this)) {
      observation.glCanvases.push(this);
      this.addEventListener('webglcontextlost', () => observation.contextsLost++);
      this.addEventListener('webglcontextrestored', () => observation.contextsRestored++);
      if (options.webgl === 'init') context.getParameter = () => { throw new Error('fixture renderer initialization failure'); };
      for (const name of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
        const nativeDraw = context[name].bind(context);
        context[name] = (...values) => {
          observation.drawCount++;
          if (options.webgl === 'draw' || observation.failDraw) throw new Error('fixture WebGL draw failure');
          return nativeDraw(...values);
        };
      }
    }
    if (type === '2d' && !this.__fixtureRects) {
      this.__fixtureRects = [];
      const nativeClear = context.clearRect.bind(context), nativeFill = context.fillRect.bind(context);
      context.clearRect = (...values) => { this.__fixtureRects = []; return nativeClear(...values); };
      context.fillRect = (...values) => {
        if (values[0] === 0 && values[1] === 0 && values[2] === this.width && values[3] === this.height) this.__fixtureRects = [];
        this.__fixtureRects.push({ values, color: String(context.fillStyle) });
        if (options.pixel === 'draw') throw new Error('fixture pixel draw failure');
        return nativeFill(...values);
      };
    }
    return context;
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
const latestView = page => page.evaluate(() => window.__fixture.views.at(-1));
const settle = page => page.waitForTimeout(120);
async function mode(page, name) {
  await page.locator(`#robot-mode-${name}`).click();
  if (name === '3d') await page.waitForFunction(() => document.querySelector('#robot-stage').dataset.rendererState !== 'loading', null, { timeout: 10000, polling: 50 });
  await settle(page);
}
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
    if (url.pathname === '/robot3d.js' && options.captureThree) await request.frame().evaluate(() => window.__fixture.threeReady);
    if (url.pathname === '/cockpit.js' && options.pixelScale) {
      const response = await route.fetch(); const body = await response.text();
      const scaleLine = 'const scale = Math.max(2, Math.min(5, Math.floor(width / 100)));';
      assert.equal(body.split(scaleLine).length, 2, 'fixture-only scale substitution targets the single actual scale expression');
      return route.fulfill({ response, body: body.replace(scaleLine, `const scale = ${options.pixelScale};`) });
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
      const view = await latestView(page);
      check(`N=${count} exact renderer population and category`, view?.records.length === count && view.records.every((row, index) => row.sessionId === ids[index] && row.category === categories[index % 4]), view);
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
    check('add/remove/return leaves current UUID population only', (await latestView(page)).records.length === 13 && await page.locator('#sessions button[data-session-id]').count() === 13);
    const old = snapshot(); old.sessions.forEach(row => delete row.registration_state);
    await refresh(page, old);
    check('old API uses current provenance conservatively', (await latestView(page)).records[2].category === 'retained');
    old.sessions.forEach(row => delete row.provenance); await refresh(page, old);
    check('old API with no current provenance is unknown', (await latestView(page)).records.every(row => row.category === 'unknown'));
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
      check(`${item.name} preserves old UUIDs with unknown classification`, await page.locator('#sessions button[data-session-id]').count() === 4 && (await latestView(page)).records.every(row => row.category === 'unknown'));
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
      if (age > 30000) check('30-second stale overrides every renderer category', (await latestView(page)).records.every(row => row.category === 'unknown'));
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
    check(`${registration} current registration category remains unknown`, (await latestView(page)).records[0].category === 'unknown');
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
    check('source observation and disconnected workbench are independent', (await latestView(page)).records[0].category === 'confirmed'
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
      check(`${label} does not reclassify UUID or reread private page`, (await latestView(page)).records[0].category === 'confirmed' && reads().length === beforeReads && await page.locator('#messages article').count() === 20);
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
    check('workbench404 preserves independently observed UUID and page', (await latestView(page)).records[0].category === 'confirmed' && reads().length === beforeReads);
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

async function graphicsFailure(options, label, expectedMode = 'pixel') {
  const session = await openPage({ ...options, expectedErrors: true }); const { page } = session;
  try {
    await mode(page, '3d');
    const status = await page.locator('#robot-status').textContent();
    check(`${label} explicitly reports unavailable graphics`, /失败|不可用|丢失|不足|不支持/.test(status), status);
    check(`${label} keeps native UUID buttons usable`, await page.locator('#sessions button[data-session-id]').count() === 4);
    await selectCard(page, ids[1], true);
    check(`${label} uses ${expectedMode} or text fallback`, expectedMode === 'text' ? await page.locator('#robot-stage canvas:visible').count() === 0 : await page.locator('#robot-stage canvas:visible').count() <= 1);
    const before = reads().length; await mode(page, 'text'); await mode(page, 'pixel');
    check(`${label} modes retain selection without extra reads`, await selected(page) === ids[1] && reads().length === before);
  } finally { await session.close(); }
}

async function canvasGeometry(page, kind) {
  return page.evaluate(type => {
    const stage = document.querySelector('#robot-stage');
    const canvas = [...stage.querySelectorAll('canvas')].find(item => !item.hidden && item.getBoundingClientRect().width > 0 && (type === '3d' ? window.__fixture.glCanvases.includes(item) : !window.__fixture.glCanvases.includes(item)));
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    return { width: canvas.width, height: canvas.height, cssWidth: rect.width, cssHeight: rect.height,
      left: rect.left, top: rect.top, rects: canvas.__fixtureRects || [], dataset: { ...canvas.dataset } };
  }, kind);
}

async function pickEveryRobot(page, kind) {
  const view = await latestView(page);
  if (kind === '3d') {
    await page.locator('#robot-rotate-reset').click(); await page.waitForTimeout(900);
    if (await page.locator('#robot-motion-toggle').getAttribute('aria-pressed') !== 'true') await page.locator('#robot-motion-toggle').click();
    await settle(page);
  }
  const points = kind === '3d' ? await page.evaluate(() => {
    const points = [];
    window.__fixture.scene.traverse(robot => { if (robot.userData.sessionId && robot.userData.colored?.[0]) points.push({ sessionId: robot.userData.sessionId }); });
    return points;
  }) : await page.evaluate(() => {
    const canvas = document.querySelector('#robot-stage canvas[data-renderer-mode="pixel"]');
    const scale = Number(canvas.dataset.pixelScale), rect = canvas.getBoundingClientRect();
    const heads = canvas.__fixtureRects.filter(item => item.values[2] === 18 * scale && item.values[3] === 12 * scale);
    return heads.map((item, index) => ({ sessionId: window.__fixture.views.at(-1).records[index].sessionId,
      x: (item.values[0] + item.values[2] / 2) * rect.width / canvas.width,
      y: (item.values[1] + item.values[3] / 2) * rect.height / canvas.height }));
  });
  check(`${kind} actual rendering exposes each current UUID position`, points.length === view.records.length);
  const canvas = page.locator(`#robot-stage canvas[data-renderer-mode="${kind}"]`);
  for (const storedPoint of points) {
    const point = kind === '3d' ? await page.evaluate(sessionId => {
      const observation = window.__fixture, rect = document.querySelector('#robot-stage canvas[data-renderer-mode="3d"]').getBoundingClientRect();
      let mesh;
      observation.scene.traverse(robot => { if (robot.userData.sessionId === sessionId) mesh = robot.userData.colored[0]; });
      if (!mesh) return null;
      const vector = mesh.getWorldPosition(new observation.THREE.Vector3()).project(observation.camera);
      return { sessionId, x: (vector.x + 1) * rect.width / 2, y: (1 - vector.y) * rect.height / 2 };
    }, storedPoint.sessionId) : storedPoint;
    check(`${kind} current real mesh projection is available`, Boolean(point));
    if (!point) continue;
    await canvas.scrollIntoViewIfNeeded();
    await page.locator('#robot-stage').evaluate((stage, y) => { stage.scrollTop = Math.max(0, y - stage.clientHeight / 2); }, point.y);
    const bounds = await page.evaluate(position => {
      const stage = document.querySelector('#robot-stage'), canvas = stage.querySelector('canvas');
      const rect = canvas.getBoundingClientRect(), clip = stage.getBoundingClientRect();
      return { x: rect.left + position.x, y: rect.top + position.y, clip: { left: clip.left, right: clip.right, top: clip.top, bottom: clip.bottom }, viewport: { width: innerWidth, height: innerHeight } };
    }, point);
    const visible = bounds.x >= Math.max(0, bounds.clip.left) && bounds.x < Math.min(bounds.viewport.width, bounds.clip.right)
      && bounds.y >= Math.max(0, bounds.clip.top) && bounds.y < Math.min(bounds.viewport.height, bounds.clip.bottom);
    check(`${kind} click ${point.sessionId} is inside live canvas clip and viewport`, visible, bounds);
    if (!visible) continue;
    const before = reads().length, alreadySelected = await selected(page) === point.sessionId;
    // The observed positions feed a real pointer click; onSelect and raycast are never called by the harness.
    await page.mouse.click(bounds.x, bounds.y); await waitMessages(page);
    check(`${kind} actual ${kind === '3d' ? 'raycast' : 'Canvas hitbox'} chooses ${point.sessionId}`, await selected(page) === point.sessionId);
    check(`${kind} selection reads exactly its full UUID once`, reads().length - before === (alreadySelected ? 0 : 1)
      && (alreadySelected || reads().at(-1).session_id === point.sessionId));
    activeCase.pointer_actions = [...(activeCase.pointer_actions || []), { mode: kind, session_id: point.sessionId,
      source: kind === '3d' ? 'real scene mesh projection / native pointer / product Raycaster' : 'actual Canvas2D head rectangle / native pointer / product hitbox', position: point }];
  }
}

async function graphicsNormal() {
  const session = await openPage({ snapshot: snapshot(4, { allConfirmed: true }), captureThree: true }); const { page } = session;
  try {
    await mode(page, '3d');
    const status = await page.locator('#robot-status').textContent();
    check('normal actual WebGL 3D mode is available', /3D|三维/.test(status) && !/失败|不可用/.test(status), status);
    const canvas = page.locator('#robot-stage canvas:visible').first();
    check('actual renderer drew through WebGL context', await page.evaluate(() => window.__fixture.glCanvases.length > 0 && window.__fixture.drawCount > 0));
    const beforeApi = apiRequests().length;
    const first = await canvas.screenshot(); const box = await canvas.boundingBox();
    await canvas.hover({ position: { x: box.width * .5, y: box.height * .5 } }); await page.mouse.down();
    await page.mouse.move(box.x + box.width * .5 + 80, box.y + box.height * .5, { steps: 8 }); await page.mouse.up(); await settle(page);
    const second = await canvas.screenshot();
    check('real pointer drag changes rendered image', !first.equals(second), { before_sha256: crypto.createHash('sha256').update(first).digest('hex'), after_sha256: crypto.createHash('sha256').update(second).digest('hex') });
    await page.locator('#robot-rotate-reset').click(); await page.locator('#robot-rotate-left').focus(); await page.keyboard.press('Enter'); await page.locator('#robot-rotate-right').click(); await settle(page);
    check('rotation and frames do not query any API', apiRequests().length === beforeApi);
    await page.locator('#robot-rotate-reset').click(); await settle(page);
    await pickEveryRobot(page, '3d');
    const afterPicks = apiRequests().length;
    await mode(page, 'pixel'); await pickEveryRobot(page, 'pixel');
    const afterPixelPicks = apiRequests().length;
    await mode(page, 'text'); await mode(page, '3d');
    check('mode transitions never create multiple visible canvases', await page.locator('#robot-stage canvas:visible').count() === 1);
    const modeRequests = apiRequests().slice(afterPixelPicks);
    check('mode transitions send no action or body request while normal polling continues', modeRequests.every(item => item.method === 'GET'
      && ['/api/sessions', '/api/workbench', '/api/codex/threads'].includes(item.path)), modeRequests);
    check('each 3D actual raycast selection issues only one message request', afterPicks - beforeApi === 4);
    await mode(page, 'text');
    for (const sessionId of ids.slice(0, 4)) await selectCard(page, sessionId, true);
    const beforeReturn = apiRequests().length;
    await mode(page, '3d');
    const returnRequests = apiRequests().slice(beforeReturn);
    check('returning from native text keyboard selection preserves UUID with no action or body query', await selected(page) === ids[3] && returnRequests.every(item => item.method === 'GET'
      && ['/api/sessions', '/api/workbench', '/api/codex/threads'].includes(item.path)), returnRequests);
    await selectCard(page, ids[0]);
    const contextEvidence = await page.evaluate(() => {
      const canvas = [...document.querySelectorAll('#robot-stage canvas')].find(item => window.__fixture.glCanvases.includes(item));
      const gl = canvas?.getContext('webgl2'), extension = gl?.getExtension('WEBGL_lose_context');
      if (!extension) return { supported: false };
      window.__fixture.contextExtension = extension; window.__fixture.lostCanvas = canvas; extension.loseContext();
      return { supported: true };
    });
    if (!contextEvidence.supported) {
      report.limitations.push('WEBGL_lose_context unavailable: real context lost/restored unverified; synthetic events cannot replace it.');
      activeCase.real_context_loss = { status: 'unverified', extension_supported: false };
    } else {
      await page.waitForFunction(() => window.__fixture.contextsLost > 0); await settle(page);
      check('actual extension context loss triggers fallback', await page.locator('#robot-stage').getAttribute('data-renderer-state') === 'context_lost' && await selected(page) === ids[0]);
      await page.evaluate(() => window.__fixture.contextExtension.restoreContext());
      try { await page.waitForFunction(() => window.__fixture.contextsRestored > 0, null, { timeout: 5000 }); }
      catch { report.limitations.push('Real context loss observed but restore event unavailable after disposal; restored branch unverified.'); }
      activeCase.real_context_loss = await page.evaluate(() => ({ extension_supported: true, lost_events: window.__fixture.contextsLost, restored_events: window.__fixture.contextsRestored }));
      check('context restoration does not automatically reclaim 3D', await page.locator('#robot-mode-3d').getAttribute('aria-pressed') !== 'true');
      await mode(page, '3d');
      check('manual retry retains selected UUID', await selected(page) === ids[0]);
    }
    const beforeFail = reads().length;
    await page.evaluate(() => { window.__fixture.failDraw = true; }); await page.locator('#robot-rotate-left').click(); await settle(page);
    check('runtime draw failure retains UUID and message page', await selected(page) === ids[0] && await page.locator('#messages article').count() === 20 && reads().length === beforeFail);
    check('runtime draw failure explicitly falls back to a visible pixel or text mode', await page.locator('#robot-stage').getAttribute('data-renderer-state') === 'draw_failed'
      && ['pixel', 'text'].includes(await page.locator('#robot-stage').getAttribute('data-renderer-mode')));
    await selectCard(page, ids[1], true);
  } finally { await session.close(); }
}

async function pixelAndPicking() {
  for (const width of [320, 360, 1024, 1440]) for (const dpr of [1, 2]) {
    const session = await openPage({ width, dpr, snapshot: snapshot(13) }); const { page } = session;
    try {
      await mode(page, 'pixel'); const geometry = await canvasGeometry(page, 'pixel');
      check(`${width}px DPR${dpr} has actual pixel canvas`, Boolean(geometry) && geometry.width > 0 && geometry.height > 0);
      const bounds = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth,
        stage: document.querySelector('#robot-stage').clientWidth, canvas: [...document.querySelectorAll('#robot-stage canvas')].find(item => !item.hidden)?.width }));
      check(`${width}px DPR${dpr} has no horizontal overflow`, bounds.scroll <= bounds.width + 1, bounds);
      check(`${width}px DPR${dpr} retains all thirteen keyboard UUIDs`, await page.locator('#sessions button[data-session-id]').count() === 13);
      await selectCard(page, ids[12], true);
      check(`${width}px DPR${dpr} last row UUID remains accessible`, await selected(page) === ids[12]);
      const frames = geometry?.rects || [];
      check(`${width}px DPR${dpr} pixel rectangles use integer coordinates`, frames.length > 0 && frames.every(item => item.values.every(Number.isInteger)));
      check(`${width}px DPR${dpr} pixel rectangles fit drawing bounds`, frames.every(item => {
        const [x, y, w, h] = item.values; return x >= 0 && y >= 0 && x + w <= geometry.width && y + h <= geometry.height;
      }));
      await pickEveryRobot(page, 'pixel');
      activeCase.pixel_layouts = [...(activeCase.pixel_layouts || []), { width, dpr, geometry }];
    } finally { await session.close(); }
  }
  for (const scale of [2, 3, 4, 5]) {
    const session = await openPage({ width: 320, pixelScale: scale, snapshot: snapshot(13) }); const { page } = session;
    try {
      await mode(page, 'pixel');
      const geometry = await canvasGeometry(page, 'pixel');
      check(`fixture integer scale=${scale} is actually drawn`, geometry?.dataset.pixelScale === String(scale));
      check(`fixture scale=${scale} all actual rectangles fit canvas`, geometry && geometry.rects.every(item => {
        const [x, y, w, h] = item.values; return [x, y, w, h].every(Number.isInteger) && x >= 0 && y >= 0 && x + w <= geometry.width && y + h <= geometry.height;
      }));
      await pickEveryRobot(page, 'pixel');
    } finally { await session.close(); }
  }
}

async function pendingModes() {
  let release; const hold = new Promise(resolve => { release = resolve; });
  const session = await openPage({ fault: { path: '/robot3d.js', hold } }); const { page } = session;
  try {
    await mode(page, 'text'); await selectCard(page, ids[1]); await refresh(page, snapshot(7)); release(); await settle(page);
    check('late 3D import cannot override explicit text mode', await page.locator('#robot-mode-text').getAttribute('aria-pressed') === 'true' && await page.locator('#robot-stage canvas:visible').count() === 0);
    check('late import preserves latest selected UUID and population', await selected(page) === ids[1] && (await latestView(page)).records.length === 7);
    const before = reads().length;
    for (const name of ['pixel', 'text', 'pixel', '3d', 'text']) await mode(page, name);
    check('rapid mode changes preserve selection without reads', reads().length === before && await selected(page) === ids[1]);
    check('rapid modes leave no visible orphan canvas', await page.locator('#robot-stage canvas:visible').count() === 0);
  } finally { release(); await session.close(); }
}

async function motionLifecycle() {
  const frameEvidence = page => page.evaluate(() => ({ count: window.__fixture.renderedFrames.length,
    frames: window.__fixture.renderedFrames.slice(-120), perspective: window.__fixture.camera?.isPerspectiveCamera === true }));
  const session = await openPage({ captureThree: true }); const { page } = session;
  try {
    await mode(page, '3d'); const initial = await frameEvidence(page); await page.waitForTimeout(500); const moving = await frameEvidence(page);
    const samples = moving.frames.filter(frame => frame.time >= initial.frames.at(-1)?.time), intervals = samples.slice(1).map((frame, i) => frame.time - samples[i].time).sort((a, b) => a - b);
    activeCase.motion_evidence = { purpose: 'new user goal: continuous 3D motion and smooth orbit', local_frame_samples: samples,
      median_interval_ms: intervals[Math.floor(intervals.length / 2)] ?? null, p95_interval_ms: intervals[Math.floor(intervals.length * .95)] ?? null,
      observed_frames_per_second: samples.length > 1 ? (samples.length - 1) * 1000 / (samples.at(-1).time - samples[0].time) : null,
      performance_scope: 'this local browser sample only; no guaranteed FPS' };
    check('motion default renders successive actual Perspective frames', moving.perspective && moving.count - initial.count >= 3);
    check('motion default changes actual camera or robot pose', JSON.stringify(moving.frames.at(-1)?.camera) !== JSON.stringify(initial.frames.at(-1)?.camera) || moving.frames.at(-1)?.modelY !== initial.frames.at(-1)?.modelY);
    await page.locator('#robot-motion-toggle').click(); await page.waitForTimeout(900); const paused = await frameEvidence(page);
    await page.waitForTimeout(300); check('actual pause toggle stops decorative render loop', (await frameEvidence(page)).count === paused.count && await page.locator('#robot-motion-toggle').getAttribute('aria-pressed') === 'true');
    const canvas = page.locator('#robot-stage canvas[data-renderer-mode="3d"]'), box = await canvas.boundingBox(), beforeApi = apiRequests().length, beforeReads = reads().length, beforeFocus = focusRequests().length, beforeConnect = connectRequests().length;
    const beforeDrag = await frameEvidence(page);
    await canvas.hover({ position: { x: box.width * .5, y: box.height * .5 } }); await page.mouse.down();
    await page.mouse.move(box.x + box.width * .5 + 70, box.y + box.height * .5 + 50, { steps: 5 }); await page.mouse.up();
    await page.waitForTimeout(900); const afterDrag = await frameEvidence(page);
    check('horizontal and vertical drag animate across multiple real frames', afterDrag.count - beforeDrag.count >= 3 && JSON.stringify(beforeDrag.frames.at(-1)?.camera) !== JSON.stringify(afterDrag.frames.at(-1)?.camera)
      && beforeDrag.frames.at(-1)?.camera?.[1] !== afterDrag.frames.at(-1)?.camera?.[1]);
    await page.mouse.wheel(0, 90); await page.waitForTimeout(900); const afterWheel = await frameEvidence(page);
    const distance = frame => Math.hypot(...frame.camera);
    check('real wheel smoothly changes camera distance', afterWheel.count - afterDrag.count >= 2 && Math.abs(distance(afterWheel.frames.at(-1)) - distance(afterDrag.frames.at(-1))) > .01);
    check('drag and wheel do not select or query messages or terminal actions', apiRequests().length === beforeApi && reads().length === beforeReads && focusRequests().length === beforeFocus && connectRequests().length === beforeConnect && await page.locator('#selected').isHidden());
    await page.locator('#robot-motion-toggle').click(); await settle(page);
    await page.evaluate(() => { window.__fixture.hiddenOverride = true; document.dispatchEvent(new Event('visibilitychange')); });
    await settle(page); const hidden = await frameEvidence(page); await page.waitForTimeout(300);
    check('actual visibility lifecycle stops hidden 3D frames', (await frameEvidence(page)).count === hidden.count);
    await page.evaluate(() => { window.__fixture.hiddenOverride = false; document.dispatchEvent(new Event('visibilitychange')); });
    await page.waitForTimeout(300); const resumed = await frameEvidence(page);
    const resumeFrame = resumed.frames.find(frame => frame.time > hidden.frames.at(-1)?.time), previous = hidden.frames.at(-1);
    const jump = resumeFrame && previous ? Math.hypot(...resumeFrame.camera.map((value, i) => value - previous.camera[i])) : Infinity;
    activeCase.motion_evidence.resume_camera_jump = jump;
    check('visibility resume restarts frames without a large first camera jump', resumed.count > hidden.count && jump < .2);
    await mode(page, 'text'); const disposed = await frameEvidence(page); await page.waitForTimeout(300);
    check('text mode disposes and stops 3D frames', (await frameEvidence(page)).count === disposed.count && await page.locator('#robot-stage canvas:visible').count() === 0);
  } finally { await session.close(); }
  const reduced = await openPage({ captureThree: true, reducedMotion: 'reduce' });
  try {
    await mode(reduced.page, '3d'); await reduced.page.waitForTimeout(900); const paused = await frameEvidence(reduced.page); await reduced.page.waitForTimeout(300);
    check('reduced motion defaults to real paused stable view', await reduced.page.locator('#robot-motion-toggle').getAttribute('aria-pressed') === 'true' && (await frameEvidence(reduced.page)).count === paused.count);
    await reduced.page.locator('#robot-motion-toggle').click(); await reduced.page.waitForTimeout(300);
    check('reduced-motion user can explicitly play decoration', await reduced.page.locator('#robot-motion-toggle').getAttribute('aria-pressed') === 'false' && (await frameEvidence(reduced.page)).count > paused.count);
  } finally { await reduced.close(); }
  const orbit = await openPage({ captureThree: true });
  try {
    const page = orbit.page;
    await mode(page, '3d'); await page.locator('#robot-motion-toggle').click(); await settle(page);
    const start = await page.evaluate(() => window.__fixture.renderedFrames.length);
    for (let turn = 0; turn < 23; turn++) await page.locator('#robot-rotate-right').click();
    await page.waitForTimeout(900);
    const evidence = await page.evaluate(start => ({ frames: window.__fixture.renderedFrames.slice(start),
      beforeReset: window.__fixture.renderedFrames.length }), start);
    const heading = frame => Math.atan2(frame.camera[0], frame.camera[2]);
    const shortDelta = (left, right) => Math.atan2(Math.sin(right - left), Math.cos(right - left));
    const travel = frames => frames.slice(1).reduce((sum, frame, i) => sum + Math.abs(shortDelta(heading(frames[i]), heading(frame))), 0);
    check('actual right-arrow controls orbit more than one full turn', travel(evidence.frames) > Math.PI * 2);
    await page.locator('#robot-rotate-reset').click(); await page.waitForTimeout(900);
    const resetFrames = await page.evaluate(start => window.__fixture.renderedFrames.slice(start - 1), evidence.beforeReset);
    activeCase.motion_evidence.long_orbit_reset = { source: 'real DOM arrow/reset buttons and actual rendered camera; no synthetic clock',
      orbit_radians: travel(evidence.frames), reset_travel_radians: travel(resetFrames), frame_count: resetFrames.length };
    check('reset after a full orbit uses the short camera path', resetFrames.length >= 3 && travel(resetFrames) < Math.PI
      && Math.abs(shortDelta(heading(resetFrames.at(-1)), -.28)) < .01, activeCase.motion_evidence.long_orbit_reset);
    check('long orbit and reset do not read messages or send terminal actions', report.requests.slice(activeCase.request_start)
      .every(item => item.method !== 'POST' && item.endpoint !== 'messages') && await page.locator('#selected').isHidden());
  } finally { await orbit.close(); }
}

async function gameSpace() {
  const session = await openPage({ snapshot: snapshot(12), captureThree: true, dpr: 2 }); const { page } = session;
  const sceneEvidence = () => page.evaluate(() => {
    const robots = [], points = [], geometries = new Set(), materials = new Set();
    window.__fixture.scene.traverse(item => {
      if (item.geometry) geometries.add(item.geometry.uuid);
      if (item.material) (Array.isArray(item.material) ? item.material : [item.material]).forEach(value => materials.add(value.uuid));
      if (item.userData.sessionId) robots.push({ id: item.userData.sessionId, home: item.userData.home,
        slot: item.userData.slot, position: item.position.toArray(), limbs: item.userData.limbs.map(limb => limb.rotation.x) });
      if (item.isPoints) {
        window.__fixture.particleBuffer ||= item.geometry.attributes.position.array;
        points.push({ uuid: item.uuid, buffer_stable: window.__fixture.particleBuffer === item.geometry.attributes.position.array,
          count: item.geometry.attributes.position.count, first: Array.from(item.geometry.attributes.position.array.slice(0, 3)) });
      }
    });
    return { robots, points, geometries: [...geometries].sort(), materials: [...materials].sort(), frames: window.__fixture.renderedFrames.length };
  });
  try {
    await mode(page, '3d'); await page.mouse.move(0, 0);
    const initial = await sceneEvidence(); await page.waitForTimeout(1200); const walking = await sceneEvidence();
    check('game space uses only 12 real fixture UUIDs', walking.robots.length === 12 && walking.robots.every(robot => ids.includes(robot.id)));
    check('robots walk horizontally with articulated limbs', walking.robots.some(robot => {
      const before = initial.robots.find(item => item.id === robot.id);
      return Math.hypot(robot.position[0] - before.position[0], robot.position[2] - before.position[2]) > .1 && robot.limbs.some(value => Math.abs(value) > .02);
    }));
    check('one batch of 192 particles advances', initial.points.length === 1 && walking.points.length === 1 && walking.points[0].count === 192 && JSON.stringify(initial.points[0].first) !== JSON.stringify(walking.points[0].first));
    check('particle updates reuse the same actual typed buffer', initial.points[0].buffer_stable && walking.points[0].buffer_stable && initial.points[0].uuid === walking.points[0].uuid);
    const reversed = snapshot(12); reversed.sessions.reverse(); await refresh(page, reversed);
    const reordered = await sceneEvidence();
    check('metadata reorder preserves each UUID home and slot', reordered.robots.every(robot => {
      const before = initial.robots.find(item => item.id === robot.id); return before.slot === robot.slot && JSON.stringify(before.home) === JSON.stringify(robot.home);
    }));
    check('metadata reorder keeps geometry and material resources stable', JSON.stringify(initial.geometries) === JSON.stringify(reordered.geometries) && JSON.stringify(initial.materials) === JSON.stringify(reordered.materials));
    await page.locator('#robot-roam-home').click(); await page.mouse.move(0, 0); const beforeActions = { reads: reads().length, focus: focusRequests().length, connect: connectRequests().length };
    await page.waitForTimeout(2700); const home = await sceneEvidence();
    check('return-to-workstations places every UUID at its own home', home.robots.every(robot => Math.hypot(robot.position[0] - robot.home.x, robot.position[2] - robot.home.z) < .01));
    check('workstation controls use accessible mutually exclusive state', await page.locator('#robot-roam-home').getAttribute('aria-pressed') === 'true' && await page.locator('#robot-roam-free').getAttribute('aria-pressed') === 'false');
    await page.locator('#robot-roam-free').click(); await page.mouse.move(0, 0); await page.waitForTimeout(600);
    const free = await sceneEvidence(); check('free roam restarts bounded movement', free.robots.some(robot => Math.hypot(robot.position[0] - robot.home.x, robot.position[2] - robot.home.z) > .1));
    const canvas = page.locator('#robot-stage canvas[data-renderer-mode="3d"]');
    await canvas.hover({ position: { x: 10, y: 10 } }); const hoverStart = await sceneEvidence(); await page.waitForTimeout(300); const hoverEnd = await sceneEvidence();
    check('ordinary hover keeps actual patrol and particles moving', JSON.stringify(hoverStart.points[0].first) !== JSON.stringify(hoverEnd.points[0].first)
      && hoverEnd.robots.some(robot => JSON.stringify(robot.position) !== JSON.stringify(hoverStart.robots.find(before => before.id === robot.id).position)));
    await page.mouse.down(); await page.waitForTimeout(100); const held = await sceneEvidence(); await page.waitForTimeout(300); const heldEnd = await sceneEvidence();
    check('held pointer stabilizes actual selectable robot positions', JSON.stringify(held.robots) === JSON.stringify(heldEnd.robots) && JSON.stringify(held.points) === JSON.stringify(heldEnd.points));
    await page.mouse.up(); await page.waitForTimeout(550); const released = await sceneEvidence();
    check('release resumes motion after the short pickup cooldown', JSON.stringify(released.points[0].first) !== JSON.stringify(heldEnd.points[0].first));
    check('each patrol remains within its workstation corridor', free.robots.every(robot => robot.position[0] >= robot.home.x - .01 && robot.position[0] <= robot.home.x + .81 && robot.position[2] >= robot.home.z - .01 && robot.position[2] <= robot.home.z + 1.11));
    check('layout and walking do not read messages or send terminal actions', reads().length === beforeActions.reads && focusRequests().length === beforeActions.focus && connectRequests().length === beforeActions.connect);
    check('3D drawing caps DPR at 1.5 with 420px viewport', (await canvasGeometry(page, '3d')).height === 630);
    await page.locator('#robot-motion-toggle').click(); await page.waitForTimeout(300); const paused = await sceneEvidence(); await page.waitForTimeout(300);
    check('pause freezes particles and patrol', JSON.stringify(paused) === JSON.stringify(await sceneEvidence()));
    check('pause freezes evidence flow CSS animation', await page.locator('#flow-source').evaluate(node => getComputedStyle(node, '::after').animationPlayState) === 'paused');
    check('unselected flow never fabricates task or identity status', (await page.locator('#flow-identity .flow-value').textContent()).includes('等待选中') && await page.locator('#flow-identity').getAttribute('data-available') === 'false');
    await selectCard(page, ids[0]);
    check('flow uses selected real evidence and actual history page', await page.locator('#flow-identity').getAttribute('data-available') === 'true' && await page.locator('#flow-message').getAttribute('data-available') === 'true');
    await page.locator('#robot-motion-toggle').click(); await page.mouse.move(0, 0); const selectionStart = await sceneEvidence(); await page.waitForTimeout(600); const selectionEnd = await sceneEvidence();
    const selectedStart = selectionStart.robots.find(robot => robot.id === ids[0]), selectedEnd = selectionEnd.robots.find(robot => robot.id === ids[0]);
    check('selected robot keeps X/Z and settles to standing while its peers keep walking', selectedStart.position[0] === selectedEnd.position[0] && selectedStart.position[2] === selectedEnd.position[2]
      && selectedEnd.position[1] < .001 && selectedEnd.limbs.every(value => Math.abs(value) < .001)
      && selectionEnd.robots.some(robot => robot.id !== ids[0] && JSON.stringify(robot.position) !== JSON.stringify(selectionStart.robots.find(before => before.id === robot.id).position)));
    await mode(page, 'pixel'); check('workstation controls disable on graphic fallback', await page.locator('#robot-roam-free').isDisabled() && await page.locator('#robot-roam-home').isDisabled());
    activeCase.game_evidence = { initial, walking, reordered, home, free, paused };
  } finally { await session.close(); }
}

async function personalSpaceMotion() {
  const samples = [];
  for (const hz of [15, 30, 60]) {
    const population = hz === 15 ? 48 : 12;
    const session = await openPage({ snapshot: snapshot(population), captureThree: true, manualFrames: true }); const { page } = session;
    try {
      await mode(page, '3d');
      const result = await page.evaluate(({ hz, population }) => {
        const o = window.__fixture, T = o.THREE, robots = [], territories = new Map();
        o.scene.traverse(item => {
          if (item.userData.sessionId) robots.push(item);
          if (item.userData.ownerKey) territories.set(item.userData.ownerKey, new T.Box3().setFromObject(item.userData.boundary, true));
        });
        const violations = [], trace = []; let overlap = false, checkingTurns = false, maxReturnTurn = 0;
        const lastYaws = robots.map(robot => robot.rotation.y);
        const regions = [...territories.values()];
        for (let i = 0; i < regions.length; i++) for (let j = i + 1; j < regions.length; j++) {
          const a = regions[i], b = regions[j];
          if (a.min.x < b.max.x && a.max.x > b.min.x && a.min.z < b.max.z && a.max.z > b.min.z) overlap = true;
        }
        function sample() {
          for (const [index, robot] of robots.entries()) {
            if (checkingTurns) maxReturnTurn = Math.max(maxReturnTurn, Math.abs(robot.rotation.y - lastYaws[index]));
            lastYaws[index] = robot.rotation.y;
            const box = new T.Box3().setFromObject(robot, true), own = territories.get(robot.userData.sessionId);
            if (!own || box.min.x < own.min.x - 1e-5 || box.max.x > own.max.x + 1e-5 || box.min.z < own.min.z - 1e-5 || box.max.z > own.max.z + 1e-5)
              violations.push({ id: robot.userData.sessionId, min: box.min.toArray(), max: box.max.toArray() });
          }
          const first = robots[0]; return { position: first.position.toArray(), yaw: first.rotation.y, limbs: first.userData.limbs.map(limb => limb.rotation.x) };
        }
        o.advanceFrame(1000 / hz); trace.push(sample());
        for (let i = 0; i < hz * 12; i++) { o.advanceFrame(1000 / hz); trace.push(sample()); }
        let travel = 0, minSpeed = Infinity, maxSpeed = 0, maxDirectionChange = 0, turning = 0, previousHeading;
        for (let i = 1; i < trace.length; i++) {
          const a = trace[i - 1].position, b = trace[i].position, dx = b[0] - a[0], dz = b[2] - a[2], distance = Math.hypot(dx, dz);
          travel += distance;
          if (i / hz > .8) {
            minSpeed = Math.min(minSpeed, distance * hz); maxSpeed = Math.max(maxSpeed, distance * hz);
            const heading = Math.atan2(dx, dz);
            if (previousHeading !== undefined) { const turn = Math.abs(Math.atan2(Math.sin(heading - previousHeading), Math.cos(heading - previousHeading))); maxDirectionChange = Math.max(maxDirectionChange, turn); turning += turn; }
            previousHeading = heading;
          }
        }
        const patrolOffsets = robots.map(robot => [robot.position.x - robot.userData.home.x, robot.position.z - robot.userData.home.z]);
        checkingTurns = true;
        document.querySelector('#robot-roam-home').click();
        for (let i = 0; i < 8; i++) { o.advanceFrame(50); sample(); }
        document.querySelector('#robot-roam-free').click(); let maxRestartStep = 0, before = sample().position;
        for (let i = 0; i < 40; i++) { o.advanceFrame(50); const after = sample().position; maxRestartStep = Math.max(maxRestartStep, Math.hypot(after[0] - before[0], after[2] - before[2])); before = after; }
        document.querySelector('#robot-roam-home').click();
        for (let i = 0; i < 80; i++) { o.advanceFrame(50); sample(); }
        const standing = robots.every(robot => Math.hypot(robot.position.x - robot.userData.home.x, robot.position.z - robot.userData.home.z) < .01
          && robot.position.y < .001 && robot.userData.limbs.every(limb => Math.abs(limb.rotation.x) < .001));
        return { hz, population, robots: robots.length, regions: territories.size, regionOverlap: overlap, violations: violations.slice(0, 5), violationCount: violations.length,
          travel, minSpeed, maxSpeed, maxDirectionChange, turning, maxRestartStep, maxReturnTurn, standing, patrolOffsets, firstFrames: trace.slice(0, 6), finalFrames: trace.slice(-6) };
      }, { hz, population });
      samples.push(result);
      check(`${hz}Hz actual meshes have ${population} complete UUID-owned regions`, result.robots === population && result.regions === population);
      check(`${hz}Hz complete robot meshes including selection ring stay inside non-overlapping territories`, !result.regionOverlap && result.violationCount === 0, result);
      check(`${hz}Hz patrol covers more than two turns with no hard corners or waypoint stalls`, result.turning > Math.PI * 4 && result.maxDirectionChange < .3 && result.minSpeed > .70 && result.maxSpeed < .76, result);
      check(`${hz}Hz return-then-roam has no teleport, no multi-turn spin and settles at home`, result.maxRestartStep <= .040001 && result.maxReturnTurn <= .300001 && result.standing, result);
      check(`${hz}Hz individual patrol rhythms differ without inventing work status`, result.patrolOffsets.some(offset => Math.hypot(offset[0] - result.patrolOffsets[0][0], offset[1] - result.patrolOffsets[0][1]) > .1));
      check(`${hz}Hz motion has no message or terminal side effects`, report.requests.slice(activeCase.request_start).every(item => item.method !== 'POST' && item.endpoint !== 'messages'));
      if (hz === 60) {
        await page.locator('#robot-roam-free').click();
        await page.evaluate(() => { for (let i = 0; i < 40; i++) window.__fixture.advanceFrame(1000 / 60); });
        await selectCard(page, ids[0]);
        const selectedPose = await page.evaluate(id => {
          const o = window.__fixture; let robot, station;
          o.scene.traverse(item => { if (item.userData.sessionId === id) robot = item; if (item.userData.ownerKey === id) station = item; });
          const start = { position: robot.position.toArray(), limbs: robot.userData.limbs.map(limb => limb.rotation.x) }, poses = [];
          for (let i = 0; i < 36; i++) { o.advanceFrame(1000 / 60); poses.push({ position: robot.position.toArray(), limbs: robot.userData.limbs.map(limb => limb.rotation.x) }); }
          return { start, poses, boundary: station.userData.boundary.material.color.getHex() };
        }, ids[0]);
        check('selected X/Z stay fixed, half-step gently settles within 0.6s, private boundary highlights', selectedPose.poses.every(pose => pose.position[0] === selectedPose.start.position[0] && pose.position[2] === selectedPose.start.position[2])
          && selectedPose.start.limbs.some(value => Math.abs(value) > .02) && selectedPose.poses.at(-1).limbs.every(value => Math.abs(value) < .001) && selectedPose.poses.at(-1).position[1] < .001
          && selectedPose.poses.every((pose, i) => Math.abs(pose.position[1] - (i ? selectedPose.poses[i - 1] : selectedPose.start).position[1]) < .006) && selectedPose.boundary === 0xe5edf5, selectedPose);
        activeCase.selected_pose = selectedPose;
      }
    } finally { await session.close(); }
  }
  check('15/30/60Hz equal-time actual distance differs by under 2%', Math.max(...samples.map(sample => sample.travel)) / Math.min(...samples.map(sample => sample.travel)) - 1 <= .02, samples);
  activeCase.motion_space_samples = samples;
}

async function gamePerformance() {
  const sustained = process.argv.includes('--perf48');
  const duration = sustained ? 60000 : 10000;
  for (const count of sustained ? [48] : [0, 12, 48]) {
    const session = await openPage({ snapshot: snapshot(count), captureThree: true }); const { page } = session;
    const pose = () => page.evaluate(() => {
      const o = window.__fixture, robots = [], stations = [], particles = [];
      o.scene.traverse(item => {
        if (item.userData.sessionId) robots.push({ key: item.userData.sessionId, travel: item.userData.travel, cruise: item.userData.cruise,
          position: item.position.toArray(), limbs: item.userData.limbs.map(limb => limb.rotation.x), parts: item.userData.parts?.length });
        if (item.userData.ownerKey) stations.push({ key: item.userData.ownerKey, parts: item.userData.parts?.length });
        if (item.isPoints) particles.push({ count: item.geometry.attributes.position.count, first: Array.from(item.geometry.attributes.position.array.slice(0, 3)), size: item.material.size, opacity: item.material.opacity });
      });
      const canvas = o.glCanvases.at(-1);
      return { time: performance.now(), robots, stations, particles, playing: document.querySelector('#robot-motion-toggle').getAttribute('aria-pressed') === 'false',
        selected: o.views.at(-1).selected, antialias: canvas.getContext('webgl2').getContextAttributes().antialias, backing: [canvas.width, canvas.height] };
    });
    try {
      await mode(page, '3d'); await page.mouse.move(0, 0); await page.waitForTimeout(1000);
      const startPose = sustained ? await pose() : null;
      const start = await page.evaluate(() => ({ index: window.__fixture.renderedFrames.length, draw: window.__fixture.drawCount }));
      await page.waitForTimeout(duration);
      const result = await page.evaluate(start => {
        const frames = window.__fixture.renderedFrames.slice(start.index), raw = frames.slice(1).map((frame, index) => frame.time - frames[index].time), sorted = [...raw].sort((a, b) => a - b);
        return { count: frames.length, duration_ms: frames.at(-1)?.time - frames[0]?.time, median_ms: sorted[Math.floor(sorted.length / 2)],
          p95_ms: sorted[Math.ceil(sorted.length * .95) - 1], slow_ratio: raw.filter(value => value > 33.3).length / raw.length,
          fps: raw.length * 1000 / raw.reduce((sum, value) => sum + value, 0), draw_calls_per_frame: (window.__fixture.drawCount - start.draw) / frames.length,
          viewport: { width: innerWidth, height: innerHeight, dpr: devicePixelRatio }, visible: !document.hidden,
          metrics: document.querySelector('#robot-frame-metrics').textContent, raw_intervals_ms: raw };
      }, start);
      result.population = count; result.scope = 'local headless Edge fixture / real rAF and WebGL calls / not Codex IAB or arbitrary hardware';
      if (sustained) result.motion_quality = { start: startPose, end: await pose() };
      result.typical_target_met = result.p95_ms <= 20 && result.slow_ratio < .01;
      activeCase.performance_samples = [...(activeCase.performance_samples || []), result];
      check(`N=${count} has a continuous visible ${duration / 1000}-second actual sample`, result.count > 20 && result.duration_ms >= duration - 200 && result.visible && Number.isFinite(result.fps), { ...result, raw_intervals_ms: 'saved in performance_samples' });
      check(`N=${count} visible metrics are measured`, /\d+ FPS · p95 [\d.]+ ms/.test(result.metrics));
      if (count === 12 && !result.typical_target_met) report.limitations.push({ name: 'typical 12-robot performance target not met', result });
      if (sustained) {
        const endPose = result.motion_quality.end, seconds = (endPose.time - startPose.time) / 1000;
        check('60-second window starts and ends with all 48 complete moving identities at original quality', [startPose, endPose].every(pose => pose.playing && !pose.selected && pose.antialias
          && pose.robots.length === 48 && pose.stations.length === 48 && pose.robots.every(robot => robot.parts === 15) && pose.stations.every(station => station.parts === 5)
          && pose.robots.map(robot => robot.key).sort().join() === ids.slice(0, 48).sort().join()
          && pose.particles.length === 1 && pose.particles[0].count === 192 && pose.particles[0].size === .075 && pose.particles[0].opacity === .6)
          && startPose.robots.every(robot => { const end = endPose.robots.find(item => item.key === robot.key); const speed = (end.travel - robot.travel) / seconds;
            return speed >= robot.cruise * .9 && speed <= robot.cruise * 1.1 && end.limbs.every(Number.isFinite); })
          && JSON.stringify(startPose.particles[0].first) !== JSON.stringify(endPose.particles[0].first), { seconds, motion_quality: result.motion_quality });
        check('48-robot sustained performance target', result.fps >= 60 && result.typical_target_met, { fps: result.fps, p95_ms: result.p95_ms, slow_ratio: result.slow_ratio });
        check('48-robot drawing budget without reducing population', result.draw_calls_per_frame <= 128, result.draw_calls_per_frame);
        check('60-second sample keeps normal metadata polling and zero body/action requests', report.requests.slice(activeCase.request_start).filter(item => item.path === '/api/sessions').length >= 6
          && report.requests.slice(activeCase.request_start).every(item => item.method !== 'POST' && item.endpoint !== 'messages'));
      }
    } finally { await session.close(); }
  }
}

async function gameBatching() {
  const session = await openPage({ snapshot: snapshot(48), captureThree: true, manualFrames: true }); const { page } = session;
  const evidence = () => page.evaluate(() => {
    const o = window.__fixture, T = o.THREE, proxies = [], batches = [], matrix = new T.Matrix4();
    o.scene.traverse(item => {
      if (item.isInstancedMesh) batches.push(item);
      else if (item.isMesh && item.layers.mask === 2) proxies.push(item);
    });
    let mismatches = 0, instances = 0;
    const owners = {}, poses = [];
    o.scene.traverse(item => { if (item.userData.sessionId) poses.push({ key: item.userData.sessionId, travel: item.userData.travel }); });
    for (const node of proxies) {
      let owner = node; while (owner && !owner.userData.sessionId && !owner.userData.ownerKey) owner = owner.parent;
      const key = owner?.userData.sessionId || owner?.userData.ownerKey, counts = owners[key] ||= { robot: 0, station: 0 };
      counts[owner?.userData.sessionId ? 'robot' : 'station']++;
    }
    const remaining = new Set(proxies);
    for (const batch of batches) for (let i = 0; i < batch.count; i++) {
      instances++; batch.getMatrixAt(i, matrix); matrix.premultiply(batch.matrixWorld);
      const match = [...remaining].find(node => node.geometry === batch.geometry && node.material === batch.material
        && node.matrixWorld.elements.every((value, index) => Math.abs(value - matrix.elements[index]) < 1e-5));
      if (match) remaining.delete(match); else mismatches++;
    }
    return { proxies: proxies.length, instances, owners, poses, batches: batches.map(batch => batch.uuid).sort(), mismatches: mismatches + remaining.size,
      disposed: o.batchDisposed, keys: [...new Set(proxies.map(node => { let root = node; while (root && !root.userData.sessionId && !root.userData.ownerKey) root = root.parent; return root?.userData.sessionId || root?.userData.ownerKey; }))].sort() };
  });
  try {
    await mode(page, '3d');
    await page.evaluate(() => { for (let i = 0; i < 60; i++) window.__fixture.advanceFrame(1000 / 60); });
    const first = await evidence();
    check('48 complete robots and stations draw identical proxy transforms through instances', first.proxies === 48 * 20 && first.instances === first.proxies && first.mismatches === 0 && first.batches.length > 0, first);
    await page.evaluate(() => { for (let i = 0; i < 60; i++) window.__fixture.advanceFrame(1000 / 60); });
    const moved = await evidence();
    check('moving limbs and feet keep identical actual instance matrices', moved.mismatches === 0 && moved.instances === 960, moved);
    check('each full identity owns 15 robot and 5 station parts and actually moves', Object.keys(moved.owners).sort().join() === ids.slice(0, 48).sort().join()
      && Object.values(moved.owners).every(counts => counts.robot === 15 && counts.station === 5)
      && moved.poses.every(pose => pose.travel > first.poses.find(item => item.key === pose.key).travel), moved);
    const primitives = await page.evaluate(() => {
      const o = window.__fixture, gl = o.glCanvases.at(-1).getContext('webgl2'), original = {}, result = { triangles: 0, points: 0 };
      for (const name of ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced']) {
        original[name] = gl[name]; gl[name] = (...values) => {
          const vertices = name.startsWith('drawElements') ? values[1] : values[2], instances = name.endsWith('Instanced') ? values.at(-1) : 1;
          if (values[0] === gl.TRIANGLES) result.triangles += vertices / 3 * instances;
          if (values[0] === gl.POINTS) result.points += vertices * instances;
          return original[name](...values);
        };
      }
      o.advanceFrame(1000 / 60);
      for (const name of Object.keys(original)) gl[name] = original[name];
      return result;
    });
    check('actual instanced draws preserve 31092 original triangles and 192 points', primitives.triangles === 31092 && primitives.points === 192, primitives);
    const reversed = snapshot(48); reversed.sessions.reverse(); await refresh(page, reversed);
    await page.evaluate(() => window.__fixture.advanceFrame(1000 / 60));
    const reordered = await evidence();
    check('metadata reorder reuses exact batches and does not dispose buffers', JSON.stringify(first.batches) === JSON.stringify(reordered.batches) && reordered.disposed === first.disposed, reordered);
    await refresh(page, snapshot(24, { allConfirmed: true }));
    await page.evaluate(() => window.__fixture.advanceFrame(1000 / 60));
    const removed = await evidence();
    check('removed identities and old categories leave no ghost instances and dispose old buffers', removed.instances === 480 && removed.mismatches === 0 && removed.keys.length === 24 && removed.disposed > reordered.disposed, removed);
    await refresh(page, snapshot(48)); await page.evaluate(() => window.__fixture.advanceFrame(1000 / 60));
    const restored = await evidence();
    check('restored identities and categories have exact instance transforms', restored.instances === 960 && restored.mismatches === 0 && restored.keys.length === 48, restored);
    await mode(page, 'pixel');
    const disposed = await page.evaluate(() => window.__fixture.batchDisposed);
    check('renderer disposal releases all remaining instance buffers', disposed === restored.disposed + restored.batches.length, { disposed, expected: restored.disposed + restored.batches.length });
    check('batching itself reads no history and sends no terminal action', report.requests.slice(activeCase.request_start).every(item => item.method !== 'POST' && item.endpoint !== 'messages'));
    activeCase.batch_samples = { first, moved, reordered, removed, restored, disposed };
  } finally { await session.close(); }
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

async function gameFrustum() {
  for (const width of [360, 1024]) {
    const session = await openPage({ width, snapshot: snapshot(12), captureThree: true }); const { page } = session;
    try {
      await mode(page, '3d');
      const result = await page.evaluate(() => {
        const observation = window.__fixture, corners = []; let floor;
        observation.scene.traverse(item => { if (item.isMesh && item.geometry.parameters?.height === .12) floor = item; });
        for (const sx of [-1, 1]) for (const sz of [-1, 1]) for (const y of [-.2, 2.6]) {
          const projected = new observation.THREE.Vector3(sx * floor.scale.x, y, sz * floor.scale.z).project(observation.camera);
          corners.push(projected.toArray());
        }
        return { corners, aspect: observation.camera.aspect, canvasWidth: document.querySelector('#robot-stage canvas').getBoundingClientRect().width,
          pageWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth };
      });
      check(`${width}px actual Perspective frustum contains the complete room and robot height`, result.corners.every(point => point.every(value => Number.isFinite(value)) && Math.abs(point[0]) <= 1 && Math.abs(point[1]) <= 1 && Math.abs(point[2]) <= 1), result);
      check(`${width}px game layout has no horizontal overflow`, result.pageWidth <= result.viewportWidth);
    } finally { await session.close(); }
  }
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

async function pickCodexRobot(page, key, kind) {
  if (kind === '3d') {
    await page.locator('#robot-rotate-reset').click(); await page.waitForTimeout(900);
    if (await page.locator('#robot-motion-toggle').getAttribute('aria-pressed') !== 'true') await page.locator('#robot-motion-toggle').click();
    await settle(page);
  }
  const point = await page.evaluate(({ key, kind }) => {
    const observation = window.__fixture, canvas = document.querySelector(`#robot-stage canvas[data-renderer-mode="${kind}"]`), rect = canvas.getBoundingClientRect();
    if (kind === '3d') {
      let mesh; observation.scene.traverse(item => { if (item.userData.sessionId === key) mesh = item.userData.colored[0]; });
      const vector = mesh.getWorldPosition(new observation.THREE.Vector3()).project(observation.camera);
      return { x: rect.left + (vector.x + 1) * rect.width / 2, y: rect.top + (1 - vector.y) * rect.height / 2 };
    }
    const scale = Number(canvas.dataset.pixelScale), index = observation.views.at(-1).records.findIndex(record => record.sessionId === key);
    const heads = canvas.__fixtureRects.filter(item => item.values[2] === 18 * scale && item.values[3] === 12 * scale), head = heads[index];
    return { x: rect.left + (head.values[0] + head.values[2] / 2) * rect.width / canvas.width,
      y: rect.top + (head.values[1] + head.values[3] / 2) * rect.height / canvas.height };
  }, { key, kind });
  await page.mouse.click(point.x, point.y); await settle(page);
  check(`${kind} actual pointer chooses the complete Codex key`, (await latestView(page)).selected === key && (await page.locator('#codex-detail').textContent()).includes(key.slice(6)), point);
}

async function codexIdentity() {
  const session = await openPage({ codexItems: codexThreads(25), captureThree: true }); const { page } = session;
  const initialActions = { reads: reads().length, focus: focusRequests().length, connect: connectRequests().length };
  try {
    await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 12);
    check('Codex has 12 current metadata cards and 4 remote identities', (await latestView(page)).records.length === 16 && await page.locator('#sessions button[data-session-id]').count() === 4);
    const key = `codex:${ids[0]}`, first = page.locator('#codex-list button').filter({ hasText: ids[0] });
    await first.click(); await settle(page);
    check('same UUID from Codex and remote remains two distinct identities', (await latestView(page)).records.some(item => item.sessionId === key) && (await latestView(page)).records.some(item => item.sessionId === ids[0]));
    check('Codex card selection shows only metadata and disables remote actions', (await page.locator('#codex-detail').textContent()).includes(ids[0]) && await page.locator('#selected').isHidden() && await page.locator('#focus-session').isDisabled() && reads().length === initialActions.reads && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
    await selectCard(page, ids[0]); const beforeReads = reads().length;
    await first.focus(); await page.keyboard.press('Enter'); await settle(page);
    check('Codex keyboard selection clears displayed remote history without rereading', await page.locator('#messages article').count() === 0 && await page.locator('#selected').isHidden() && reads().length === beforeReads);
    const beforePicks = reads().length;
    await mode(page, '3d'); await pickCodexRobot(page, `codex:${ids[1]}`, '3d');
    await mode(page, 'pixel'); await page.locator('#robot-stage').scrollIntoViewIfNeeded(); await pickCodexRobot(page, key, 'pixel');
    check('Codex graphical selection never calls Claude reader or terminal actions', reads().length === beforePicks && focusRequests().length === initialActions.focus && connectRequests().length === initialActions.connect);
    await page.locator('#codex-next').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex second page exposes the next 12 exact IDs and clears selection', await page.locator('#codex-list button').count() === 12 && (await page.locator('#codex-list').textContent()).includes(ids[12]) && !(await page.locator('#codex-list').textContent()).includes(ids[0]) && !(await latestView(page)).selected);
    await page.locator('#codex-next').click(); await page.waitForFunction(() => !document.querySelector('#refresh-codex').disabled); await settle(page);
    check('Codex final page exposes the remaining exact ID with no extra roles', await page.locator('#codex-list button').count() === 1 && (await page.locator('#codex-list').textContent()).includes(ids[24]) && await page.locator('#codex-next').isDisabled() && (await latestView(page)).records.length === 5);
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
    check('expired Codex sampling clears roles and selected metadata', !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')) && !(await latestView(page)).selected && await page.locator('#codex-list button').count() === 0);
    await refreshCodex(); check('fresh Codex sampling recovers independently of stale remote evidence', await page.locator('#codex-list button').count() === 12);
    await fixture.update({ codex_available: false }); session.expectHttpFailure('/api/codex/threads?offset=0', 503, 'codex_index_unavailable'); await refreshCodex();
    check('Codex503 clears current evidence while retaining remote UUID cards', await page.locator('#codex-list button').count() === 0 && !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')) && await page.locator('#sessions button[data-session-id]').count() === 4);
    const bad = codexThreads(1); bad[0].session_key = `codex:${ids[1]}`; await fixture.update({ codex_available: true, codex_items: bad }); await refreshCodex();
    check('invalid Codex namespace response cannot become a role', await page.locator('#codex-list button').count() === 0 && !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')));
    const badType = codexThreads(1); badType[0].thread_id = [ids[0]]; await fixture.update({ codex_items: badType }); await refreshCodex();
    check('array UUID is rejected instead of coerced into a Codex identity', await page.locator('#codex-list button').count() === 0 && !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')));
    await fixture.update({ codex_items: [] }); await refreshCodex(); check('valid empty Codex page is explicit and creates no fake robot', await page.locator('#codex-list button').count() === 0 && (await latestView(page)).records.length === 4);
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
    check('a valid Codex page arriving after 30seconds cannot restore robots or selection', await page.locator('#codex-list button').count() === 0 && !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')) && !(await latestView(page)).selected);
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
    check('accepted Codex evidence age includes the preceding request wait', await page.locator('#codex-list button').count() === 0 && !(await latestView(page)).records.some(item => item.sessionId.startsWith('codex:')));
  } finally { releaseShort(); await delayed.close(); }
  let releaseMessage; const messageHold = new Promise(resolve => { releaseMessage = resolve; }); const messageFault = { path: `/api/sessions/${ids[0]}/messages`, hold: messageHold };
  const remote = await openPage({ codexItems: codexThreads(1), fault: messageFault });
  try {
    const page = remote.page; await page.waitForFunction(() => document.querySelectorAll('#codex-list button').length === 1);
    await card(page, ids[0]).click(); await page.waitForTimeout(100); await page.locator('#codex-list button').click(); releaseMessage(); await settle(page);
    check('late remote message response cannot overwrite Codex identity selection', (await latestView(page)).selected === `codex:${ids[0]}` && await page.locator('#selected').isHidden() && await page.locator('#messages article').count() === 0);
  } finally { releaseMessage(); await remote.close(); }
}

async function main() {
  if (process.argv.slice(2).some(argument => !['--fixture', '--game', '--space', '--perf48', '--r11', '--core'].includes(argument))) throw new Error('Only fixture modes are accepted; live mode is intentionally absent.');
  for (const filename of ['web/app.js', 'web/cockpit.js', 'tests/verify_robot_cockpit.cjs']) {
    const result = spawnSync(process.execPath, ['--check', path.join(root, filename)], { encoding: 'utf8' });
    report.syntax.push({ filename, exit_code: result.status, stdout: result.stdout, stderr: result.stderr });
    if (result.status !== 0) throw new Error(`syntax check failed: ${filename}`);
  }
  const moduleText = fs.readFileSync(path.join(root, 'web/robot3d.js'), 'utf8');
  const moduleResult = spawnSync(process.execPath, ['--input-type=module', '--check'], { input: moduleText, encoding: 'utf8' });
  report.syntax.push({ filename: 'web/robot3d.js', exit_code: moduleResult.status, stdout: moduleResult.stdout, stderr: moduleResult.stderr });
  if (moduleResult.status !== 0) throw new Error('robot3d module syntax check failed');
  for (const filename of ['web/index.html', 'web/style.css', 'web/app.js', 'web/cockpit.js', 'web/robot3d.js',
    'web/vendor/three-0.186.0/three.module.js', 'web/vendor/three-0.186.0/three.core.js', 'web/vendor/three-0.186.0/LICENSE']) {
    const bytes = fs.readFileSync(path.join(root, filename));
    report.hashes[filename] = { bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
  }
  fixture = await startFixture();
  browser = await chromium.launch({ channel: 'msedge', headless: true });
  if (process.argv.includes('--core')) {
    await scenario('old harness behavior / reader / 45 boundary / focus fixtures', basicBehavior);
    await scenario('real WebGL / drag / context loss / manual restoration / runtime draw', graphicsNormal);
    return;
  }
  if (process.argv.includes('--r11')) {
    await scenario('R11 exact instance matrices / category / membership / disposal', gameBatching);
    await scenario('R11 selected source / full key / capability / evidence age mapping', mappingEvidence);
    await scenario('R11 missing action credential capability consistency', mappingWithoutCsrf);
    await scenario('R11 selected mapping responsive computed layout', mappingResponsive);
    return;
  }
  if (process.argv.includes('--perf48')) {
    await scenario('48 robots continuous 60-second rAF / normal polling', gamePerformance);
    return;
  }
  if (process.argv.includes('--space')) {
    await scenario('personal territories / actual full mesh / 15-30-60Hz continuous patrol', personalSpaceMotion);
    await scenario('game workstations / patrol / particles / truthful evidence flow', gameSpace);
    return;
  }
  if (process.argv.includes('--game')) {
    await scenario('game workstations / patrol / particles / truthful evidence flow', gameSpace);
    await scenario('game actual 10-second Edge performance 0/12/48', gamePerformance);
    await scenario('game actual narrow and wide Perspective room fit', gameFrustum);
    return;
  }
  await scenario('old harness behavior / reader / 45 boundary / focus fixtures', basicBehavior);
  await scenario('dynamic 0/1/2/4/7/13 categories / old API / missing selection', classification);
  await scenario('late selection epoch response', epoch);
  await scenario('API failure / unknown / ages', apiFailures);
  await scenario('workbench explicit connect / repeated connect / UUID page retention', workbenchBehavior);
  await scenario('workbench unknown fields / clients / stale / endpoint404', workbenchFailures);
  await scenario('workbench pending action lock / late age / cleanup latch', workbenchPending);
  for (const registration of ['unknown', 'future_enum', 'legacy without provenance']) {
    await scenario(`unknown registration revokes focus: ${registration}`, () => unknownRegistrationFocus(registration));
  }
  await scenario('classic cockpit404 keeps independent app DOM', () => graphicsFailure({ fault: { path: '/cockpit.js', status: 404, body: { reason: 'route_not_found' } } }, 'classic cockpit404', 'text'));
  await scenario('classic cockpit syntax failure keeps independent app DOM', () => graphicsFailure({ fault: { path: '/cockpit.js', status: 200, mime: 'text/javascript', body: '= fixture syntax failure' }, expectedPageErrors: ['Unexpected token'] }, 'classic cockpit syntax', 'text'));
  for (const cockpit of ['create', 'update']) await scenario(`app catches cockpit ${cockpit}`, () => graphicsFailure({ cockpit }, `cockpit ${cockpit}`, 'text'));
  for (const resource of ['/robot3d.js', '/vendor/three-0.186.0/three.module.js', '/vendor/three-0.186.0/three.core.js']) {
    await scenario(`${resource} 404 fallback`, () => graphicsFailure({ fault: { path: resource, status: 404, body: { reason: 'route_not_found' } } }, 'module404'));
    await scenario(`${resource} wrong MIME fallback`, () => graphicsFailure({ fault: { path: resource, status: 200, mime: 'text/plain', body: 'fixture wrong MIME' } }, 'module MIME'));
  }
  await scenario('CSP module rejection fallback', () => graphicsFailure({ fault: { path: '/', csp: `default-src 'self'; script-src ${fixture.origin}/app.js ${fixture.origin}/cockpit.js; connect-src 'self'; img-src 'none'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'` } }, 'CSP'));
  for (const webgl of ['null', 'throw', 'init', 'draw']) await scenario(`WebGL ${webgl} fallback`, () => graphicsFailure({ webgl }, `WebGL ${webgl}`));
  for (const pixel of ['null', 'throw', 'draw']) await scenario(`Canvas2D ${pixel} text fallback`, () => graphicsFailure({ webgl: 'null', pixel }, `Canvas2D ${pixel}`, 'text'));
  await scenario('real WebGL / drag / context loss / manual restoration / runtime draw', graphicsNormal);
  await scenario('actual pixel narrow bounds / DPR / last UUID', pixelAndPicking);
  await scenario('pending import / rapid mode generations', pendingModes);
  await scenario('new motion actual Perspective / damping / lifecycle / reduced motion', motionLifecycle);
  await scenario('game workstations / patrol / particles / truthful evidence flow', gameSpace);
  await scenario('R11 exact instance matrices / category / membership / disposal', gameBatching);
  await scenario('R11 selected source / full key / capability / evidence age mapping', mappingEvidence);
  await scenario('R11 missing action credential capability consistency', mappingWithoutCsrf);
  await scenario('R11 selected mapping responsive computed layout', mappingResponsive);
  await scenario('personal territories / actual full mesh / 15-30-60Hz continuous patrol', personalSpaceMotion);
  await scenario('game actual 10-second Edge performance 0/12/48', gamePerformance);
  await scenario('game actual narrow and wide Perspective room fit', gameFrustum);
  await scenario('Codex metadata namespace / keyboard / pixel / 3D / all pages', codexIdentity);
  await scenario('Codex metadata expired / unavailable / malformed / empty', codexFailures);
  await scenario('Codex delayed page and old remote message response isolation', codexLateResponses);
  await scenario('same-origin static / network guard', async () => {
    for (const resource of ['/robot3d.js', '/vendor/three-0.186.0/three.module.js', '/vendor/three-0.186.0/three.core.js']) {
      const response = report.static_responses.find(item => item.path === resource && item.status === 200 && item.case.startsWith('old harness'));
      check(`${resource} actual HTTP200 JS MIME CSP nosniff`, response && /javascript/.test(response.mime) && response.nosniff === 'nosniff' && response.csp.includes("default-src 'self'") && response.csp.includes("img-src 'none'"), response);
    }
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
