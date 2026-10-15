// Controlled browser fixture for the garden view. It never contacts port 8766.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('C:/example/user/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const root = path.resolve(__dirname, '..');
const report = { fixture_only: true, production_port_used: false, cases: [], failures: [], started_at: new Date().toISOString() };
const output = path.join(root, 'work', `garden-browser-${report.started_at.replace(/[:.]/g, '-')}.json`);
const ids = {
  root: 'a1000000-0000-4000-8000-000000000001',
  worker: 'a1000000-0000-4000-8000-000000000002',
  reviewer: 'a1000000-0000-4000-8000-000000000003',
  foreign: 'a1000000-0000-4000-8000-000000000004',
  task: 'a1000000-0000-4000-8000-000000000005',
  accept: 'a1000000-0000-4000-8000-000000000006',
  claim: 'a1000000-0000-4000-8000-000000000007',
  extraOne: 'a1000000-0000-4000-8000-000000000008',
  extraTwo: 'a1000000-0000-4000-8000-000000000009',
  extraThree: 'a1000000-0000-4000-8000-000000000010',
};
const actors = {
  [ids.root]: { actor_id: ids.root, root_thread_id: ids.root, parent_thread_id: null, nickname: 'Root', agent_path: '/root', role: 'coordinator', capabilities: ['garden_accept_artifact'] },
  [ids.worker]: { actor_id: ids.worker, root_thread_id: ids.root, parent_thread_id: ids.root, nickname: 'Worker', agent_path: '/root/capability_research', role: 'worker', capabilities: ['garden_submit_artifact'] },
  [ids.reviewer]: { actor_id: ids.reviewer, root_thread_id: ids.root, parent_thread_id: ids.root, nickname: 'Reviewer', agent_path: '/root/reviewer', role: 'reviewer', capabilities: ['garden_claim_review', 'garden_record_review'] },
};
function snapshot(phase = 'accepted', revision = 1) {
  const digest = 'a'.repeat(64);
  const receipt = { id: ids.accept, root_thread_id: ids.root, actor_id: ids.root, operation: 'garden_accept_artifact', task_id: ids.task,
    version: 1, artifact_sha256: digest, summary: '修复登录问题，测试通过 <script>bad()</script>' };
  const task = { task_id: ids.task, assignee_id: ids.worker, reviewer_id: ids.reviewer, title: '能力调研', state: phase, version: 1,
    current_valid: true, artifact_path: 'work/report.md', artifact_sha256: digest, submit_receipt: { summary: '产物已交付' },
    claim_receipt: { id: ids.claim }, review_request: { id: ids.claim }, review: { outcome: 'pass', summary: '独立复核通过' },
    accept_receipt: phase === 'accepted' ? receipt : null };
  return { available: true, protocol_version: 1, root_thread_id: ids.root, revision, actors, tasks: { [ids.task]: task },
    receipts: phase === 'accepted' ? [receipt] : [] };
}
const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="/style.css"><title>原观察页面</title><body><header><h1>原观察页面</h1><p class="muted">观察</p></header>
<nav id="garden-tabs" hidden><button id="garden-view">协作小院</button><button id="observation-view">会话与终端观察</button></nav>
<main><section class="panel robot-panel"><h2 id="robot-heading">机器人工作空间</h2><div id="garden-peek" class="garden-peek"><span id="garden-peek-domain"></span><strong id="garden-peek-title"></strong><span id="garden-peek-summary"></span></div><div id="garden-scene-roster" class="garden-scene-roster"></div><div class="robot-stage" style="height:420px">真实页面的 3D 庭院由原场景负责</div><button id="robot-motion-toggle" aria-pressed="false">暂停</button></section>
<section id="garden-panel" class="panel garden-panel" hidden><h2>伙伴工位</h2><p id="garden-counts"></p><code id="garden-root-key"></code>
<button id="refresh-garden">刷新小院</button><p id="garden-notice"></p><p id="garden-announcement" aria-live="polite"></p><div class="garden-layout"><div id="garden-actors"></div><div id="garden-detail"></div></div></section></main>
<script type="module">import { mount } from '/garden.js'; window.fixture = { state: null, pending: null, pendingPublic: null, publicReads: [], publicResult: {available:true,root_thread_id:'${ids.root}',actor_id:'${ids.worker}',coverage:'最近公开发言',messages:[{phase:'commentary',text:'正在整理能力边界 <img src=x onerror=private()>',created_at:null}]}, pendingPost: null, posts: [], viewCalls: 0 };
const query = async (route, options) => { if (options) { fixture.posts.push({route, body:JSON.parse(options.body)}); if (fixture.pendingPost) return fixture.pendingPost.promise; return {root_thread_id:'${ids.root}',revision:fixture.state.revision+1,receipt:{root_thread_id:'${ids.root}'}}; }
  if (route.includes('/public-messages')) { fixture.publicReads.push(route); if (fixture.pendingPublic) return fixture.pendingPublic.promise; return {...fixture.publicResult,actor_id:route.split('/')[4]}; }
  if (fixture.pending) return fixture.pending.promise; return fixture.state; };
window.start = (value) => { fixture.state=value; fixture.garden=mount({root:'${ids.root}',query,csrf:()=> 'fixture-csrf',onView:()=>fixture.viewCalls++}); };
window.pauseGet = () => { let resolve; const promise = new Promise(done=>resolve=done); fixture.pending={promise,resolve}; };
window.finishGet = (value) => { const pending=fixture.pending; fixture.pending=null; pending.resolve(value); };
window.pausePublic = () => { let resolve; const promise = new Promise(done=>resolve=done); fixture.pendingPublic={promise,resolve}; };
window.finishPublic = (value) => { const pending=fixture.pendingPublic; fixture.pendingPublic=null; pending.resolve(value); };
window.pausePost = () => { let resolve; const promise = new Promise(done=>resolve=done); fixture.pendingPost={promise,resolve}; };
window.finishPost = (value) => { const pending=fixture.pendingPost; fixture.pendingPost=null; pending.resolve(value); };</script></body></html>`;
const server = http.createServer((request, response) => {
  const files = { '/': [html, 'text/html; charset=utf-8'], '/garden.js': [fs.readFileSync(path.join(root, 'web', 'garden.js')), 'text/javascript; charset=utf-8'],
    '/style.css': [fs.readFileSync(path.join(root, 'web', 'style.css')), 'text/css; charset=utf-8'] };
  const item = files[request.url]; response.writeHead(item ? 200 : 404, { 'Content-Type': item ? item[1] : 'text/plain' }); response.end(item ? item[0] : 'missing');
});
async function check(name, fn) { try { await fn(); report.cases.push(name); } catch (error) { report.failures.push(`${name}: ${error.stack || error}`); } }
async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 574, height: 866 } });
  try {
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    await page.waitForFunction(() => typeof window.start === 'function');
    await page.evaluate(value => window.start(value), snapshot());
    await page.locator('.garden-cloud').first().waitFor();
    await check('verified accepted summary and escaped content', async () => {
      assert.match(await page.locator('.garden-cloud').first().innerText(), /修复登录问题，测试通过/);
      assert.match(await page.locator('.garden-cloud').nth(1).innerText(), /独立复核通过/);
      assert.equal(await page.locator('script').count(), 1);
      assert.equal(await page.locator('#garden-counts').innerText(), '2 个已登记伙伴 · 1 项已验收 · 0 项待复核或验收');
      assert.equal(await page.evaluate(() => window.fixture.publicReads.length), 0);
      assert.equal(await page.locator('.garden-roster-card').count(), 2);
      assert.match(await page.locator('.garden-roster-card').first().innerText(), /提交者[\s\S]*能力调研[\s\S]*完成交付/);
    });
    await check('explicit role selection loads only bounded public assistant text', async () => {
      await page.locator('.garden-roster-card').first().click();
      await page.waitForFunction(() => window.fixture.publicReads.length === 1);
      assert.match(await page.locator('.garden-conversation').innerText(), /正在整理能力边界/);
      assert.equal(await page.locator('script').count(), 1);
      assert.equal(await page.locator('img').count(), 0);
      assert.match(await page.locator('.garden-timeline').innerText(), /验收交付/);
      assert.match(await page.locator('.garden-timeline').innerText(), new RegExp(`收据 #${ids.accept.slice(-8)}`));
      await page.locator('#garden-detail details').nth(1).locator('summary').click();
      assert.match(await page.locator('#garden-detail details').nth(1).innerText(), new RegExp(ids.accept));
      assert.match(await page.locator('.garden-responsibility').innerText(), /交付已分配/);
      assert.equal(await page.locator('.garden-roster-card').first().getAttribute('aria-pressed'), 'true');
    });
    await check('late public response cannot cross root', async () => {
      await page.evaluate(() => window.pausePublic());
      await page.locator('.garden-conversation button').click();
      await page.waitForFunction(() => window.fixture.publicReads.length === 2);
      await page.evaluate(key => window.fixture.garden.observeDomain(key), `codex:${ids.foreign}`);
      await page.evaluate(value => window.finishPublic(value), {available:true,root_thread_id:ids.root,actor_id:ids.worker,coverage:'wrong',messages:[{phase:'commentary',text:'LATE SECRET',created_at:null}]});
      assert.equal(await page.locator('.garden-utterance').count(), 0);
      assert.equal(await page.locator('.garden-roster-card').count(), 0);
      await page.evaluate(() => window.fixture.garden.observeDomain(`codex:${window.fixture.state.root_thread_id}`));
      await page.evaluate(() => window.fixture.garden.refresh());
    });
    await check('hidden page cancels an in-flight public read', async () => {
      await page.evaluate(() => window.pausePublic());
      await page.locator('.garden-roster-card').first().click();
      await page.waitForFunction(() => window.fixture.publicReads.length === 3);
      await page.evaluate(() => { Object.defineProperty(document, 'hidden', {configurable:true,value:true}); document.dispatchEvent(new Event('visibilitychange')); });
      await page.evaluate(value => window.finishPublic(value), {available:true,root_thread_id:ids.root,actor_id:ids.worker,coverage:'wrong',messages:[{phase:'commentary',text:'HIDDEN SECRET',created_at:null}]});
      assert.equal(await page.locator('.garden-utterance').count(), 0);
      assert.equal(await page.locator('.garden-conversation button').isEnabled(), true);
      await page.evaluate(() => { delete document.hidden; document.dispatchEvent(new Event('visibilitychange')); });
    });
    await check('identity details and keyboard cloud toggle', async () => {
      await page.locator('#garden-detail details').first().locator('summary').click();
      assert.match(await page.locator('#garden-detail').innerText(), new RegExp(ids.worker));
      await page.locator('.garden-cloud').first().focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('.garden-cloud').first().getAttribute('aria-expanded'), 'false');
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('.garden-cloud').first().getAttribute('aria-expanded'), 'true');
    });
    await check('poll does not replay accepted announcement', async () => {
      await page.evaluate(() => { document.querySelector('#garden-announcement').textContent=''; });
      await page.evaluate(() => window.fixture.garden.refresh());
      assert.equal(await page.locator('#garden-announcement').innerText(), '');
      assert.equal(await page.locator('.garden-cloud.new-result').count(), 0);
    });
    await check('foreign root clears cards and late response', async () => {
      await page.evaluate(() => window.pauseGet());
      const pending = page.evaluate(() => window.fixture.garden.refresh());
      await page.waitForTimeout(50);
      await page.evaluate(key => window.fixture.garden.observeDomain(key), `codex:${ids.foreign}`);
      await page.evaluate(value => window.finishGet(value), snapshot('accepted', 2)); await pending;
      assert.equal(await page.locator('.garden-booth').count(), 0);
      assert.equal(await page.locator('#garden-announcement').innerText(), '');
      assert.equal(await page.evaluate(() => window.fixture.garden.owns('codex:' + window.fixture.state.root_thread_id)), false);
    });
    await check('explicit return restores this root only', async () => {
      await page.evaluate(() => window.fixture.garden.observeDomain(`codex:${window.fixture.state.root_thread_id}`));
      await page.evaluate(() => window.fixture.garden.refresh());
      assert.equal(await page.locator('.garden-booth').count(), 2);
    });
    await check('invalid digest rejects accepted snapshot', async () => {
      const bad=snapshot('accepted',3); bad.tasks[ids.task].artifact_sha256='b'.repeat(64);
      await page.evaluate(value => { window.fixture.state=value; return window.fixture.garden.refresh(); },bad);
      assert.equal(await page.locator('.garden-booth').count(), 0);
    });
    await check('stale review and accepted summary withdraw together', async () => {
      const stale=snapshot('artifact_changed',4); stale.tasks[ids.task].current_valid=false;
      await page.evaluate(value => { window.fixture.state=value; return window.fixture.garden.refresh(); },stale);
      assert.equal(await page.locator('.garden-booth').count(),2);
      assert.equal(await page.locator('#garden-counts').innerText(),'2 个已登记伙伴 · 0 项已验收 · 0 项待复核或验收 · 1 项需关注');
      assert.equal(await page.locator('#garden-peek-title').innerText(),'等待真实交付');
      assert.match(await page.locator('.garden-cloud').nth(1).innerText(),/文件已变化/);
      assert.doesNotMatch(await page.locator('.garden-booth').nth(1).innerText(),/独立复核通过/);
      await page.locator('.garden-actor').nth(1).click();
      assert.doesNotMatch(await page.locator('#garden-detail').innerText(),/独立复核通过/);
    });
    await check('stage-specific real route and guarded accept', async () => {
      const delivered=snapshot('delivered',5);
      await page.evaluate(value => { window.fixture.state=value; return window.fixture.garden.refresh(); },delivered);
      await page.locator('#garden-detail select').selectOption(ids.reviewer);
      await page.getByRole('button',{name:'请求复核'}).click();
      await page.waitForFunction(() => window.fixture.posts.length === 1);
      const posts=await page.evaluate(() => window.fixture.posts);
      assert.equal(posts[0].route,'/api/garden/request-review');
      assert.equal(posts[0].body.reviewer_id,ids.reviewer);
      assert.equal(await page.getByRole('button',{name:'验收交付'}).count(),0);
    });
    await check('late root POST cannot repopulate foreign domain', async () => {
      const delivered=snapshot('delivered',7);
      await page.evaluate(value => { window.fixture.state=value; return window.fixture.garden.refresh(); },delivered);
      await page.evaluate(() => window.pausePost());
      await page.getByRole('button',{name:'请求复核'}).click();
      await page.waitForFunction(() => window.fixture.posts.length === 2);
      await page.evaluate(key => window.fixture.garden.observeDomain(key),`codex:${ids.foreign}`);
      await page.evaluate(rootId => window.finishPost({root_thread_id:rootId,revision:6,receipt:{root_thread_id:rootId}}),ids.root);
      assert.equal(await page.locator('.garden-booth').count(),0);
      assert.match(await page.locator('#garden-notice').innerText(),/其它会话/);
    });
    const four = snapshot('accepted',8);
    four.actors[ids.extraOne] = { actor_id: ids.extraOne, root_thread_id: ids.root, parent_thread_id: ids.root, nickname: '长中文名称能力边界研究伙伴', agent_path: '/root/extra_one', role: 'worker', capabilities: ['garden_submit_artifact'] };
    four.actors[ids.extraTwo] = { actor_id: ids.extraTwo, root_thread_id: ids.root, parent_thread_id: ids.root, nickname: '复核伙伴', agent_path: '/root/extra_two', role: 'reviewer', capabilities: ['garden_claim_review'] };
    await page.evaluate(value => { window.fixture.state=value; window.fixture.garden.observeDomain(`codex:${value.root_thread_id}`); return window.fixture.garden.refresh(); }, four);
    for (const width of [360,574,1024]) await check(`responsive width ${width}`, async () => {
      await page.setViewportSize({width,height:866});
      await page.evaluate(() => window.scrollTo({top:0,behavior:'instant'}));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true);
      assert.equal(await page.locator('#garden-panel').isVisible(),true);
      const geometry = await page.evaluate(() => {
        const peek = document.querySelector('#garden-peek').getBoundingClientRect();
        const roster = document.querySelector('#garden-scene-roster').getBoundingClientRect();
        const stage = document.querySelector('.robot-stage').getBoundingClientRect();
        return {peekBottom:peek.bottom,rosterBottom:roster.bottom,stageTop:stage.top,stageVisible:Math.min(866,stage.bottom)-stage.top};
      });
      assert.ok(geometry.rosterBottom < 866 && geometry.stageTop < 866 && geometry.stageVisible >= 120, JSON.stringify(geometry));
      assert.equal(await page.locator('.garden-roster-card').count(), 4);
      assert.match(await page.locator('.garden-roster-card').first().innerText(), /提交者[\s\S]*能力调研/);
    });
    await check('more than four actors keep the compact first fold', async () => {
      const five = structuredClone(four);
      five.revision = 9;
      five.actors[ids.extraThree] = {actor_id:ids.extraThree,root_thread_id:ids.root,parent_thread_id:ids.root,nickname:'第五位伙伴',agent_path:'/root/extra_three',role:'worker',capabilities:['garden_submit_artifact']};
      await page.evaluate(value => { window.fixture.state=value; return window.fixture.garden.refresh(); }, five);
      await page.setViewportSize({width:360,height:866});
      await page.evaluate(() => window.scrollTo({top:0,behavior:'instant'}));
      assert.equal(await page.locator('.garden-roster-card').count(),4);
      assert.match(await page.locator('.garden-roster-more').innerText(),/还有 1 位伙伴/);
      assert.equal(await page.locator('.garden-booth').count(),5);
      const stageTop = await page.locator('.robot-stage').evaluate(element => element.getBoundingClientRect().top);
      assert.ok(stageTop <= 746, `stage top ${stageTop}`);
    });
    await check('keyboard selection retains roster focus after render', async () => {
      await page.locator('.garden-roster-card').first().focus();
      await page.keyboard.press('Enter');
      assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('garden-roster-card')), true);
      await page.keyboard.press('Space');
      assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('garden-roster-card')), true);
      await page.evaluate(() => window.fixture.garden.refresh());
      assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('garden-roster-card')), true);
      await page.locator('.garden-roster-more').focus();
      await page.evaluate(() => window.fixture.garden.refresh());
      assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('garden-roster-more')), true);
    });
  } finally { await browser.close(); }
}
main().catch(error => report.failures.push(`fatal: ${error.stack || error}`)).finally(async () => {
  await new Promise(resolve => server.close(resolve)); report.finished_at=new Date().toISOString(); report.exit_code=report.failures.length ? 1:0;
  fs.writeFileSync(output, JSON.stringify(report,null,2)); console.log(JSON.stringify({output,checks:report.cases.length,failures:report.failures,exit_code:report.exit_code}));
  process.exitCode=report.exit_code;
});
