'use strict';
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.text = ''; this.value = ''; this.className = ''; this.classList = { toggle() {} }; }
  append(...rows) { for (const row of rows) { if (row.parentElement) row.parentElement.children = row.parentElement.children.filter(value => value !== row); row.parentElement = this; this.children.push(row); } }
  replaceChildren(...rows) { for (const row of this.children) row.parentElement = null; this.children = []; this.text = ''; this.append(...rows); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(row => row.textContent).join(''); }
  get lastElementChild() { return this.children.at(-1); }
  get all() { return [this, ...this.children.flatMap(row => row.all)]; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { (this.listeners[name] ??= []).push(fn); }
  querySelectorAll(selector) { return this.all.filter(row => row !== this && row.tag === selector); }
  contains(node) { return this.all.includes(node); }
  show() { this.open = true; }
  close() { this.open = false; }
  focus() {}
  async fire(name) { for (const fn of this.listeners[name] || []) await fn({ preventDefault() {} }); }
}
const tick = () => new Promise(resolve => setImmediate(resolve));
const session = (n, extra = {}) => ({ thread_id: `thread-${n}`, session_key: `codex:${n}`, title: `真实标题 ${n}`, cwd: 'C:/example/shared', project_id: 'cx-a', assignment_state: 'assigned', binding_available: true, updated_at: '2026-10-18T01:00:00Z', archived: false, ...extra });
const result = (items, extra = {}) => ({ status: 'available', source_id: 'source', sampled_at: 'first', revision: 'same', complete: true, items, total: 35, offset: 0, limit: 20, ...extra });
function fixture() {
  const document = { createElement: tag => new Element(tag), createElementNS: (_ns, tag) => new Element(tag) }, context = vm.createContext({ document, window: {}, URL, URLSearchParams, TextEncoder });
  for (const file of ['task-center.js', 'project-space.js']) vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../web/workspace', file), 'utf8'), context);
  const root = new Element('section'), navigation = new Element('nav'), home = new Element('div'), taskRoot = new Element('div'), reads = [], itemReads = [], writes = [], routes = [];
  home.append(taskRoot);
  const tasks = context.window.WorkspaceTaskCenter.create({ root: taskRoot, canWrite: () => true,
    fetchJson: url => new Promise(resolve => itemReads.push({ url, resolve })), postJson: async (_url, body) => { writes.push(body); return { item: { id: '00000000-0000-4000-8000-000000000001', revision: 1 } }; } });
  const project = { id: 'cx-a', name: '真实项目', read_only: true, binding_available: true, source: { kind: 'codex' } };
  const snapshot = { projects: { status: 'available', items: [project, { id: 'cx-b', name: '同目录另一项目', read_only: true, binding_available: true }] }, codex_desktop: { status: 'available', source_id: 'source', sampled_at: 'first' }, sessions: { available: true, items: Array.from({ length: 12 }, (_, n) => session(n)) }, items_page: { items: [], total: 0, offset: 0, limit: 50 } };
  tasks.update(snapshot);
  const api = context.window.WorkspaceProjectSpace.create({ root, navigation, taskRoot, tasks, navigate: route => routes.push(route), manage: () => { throw Error('read-only settings opened'); }, readSessions: params => new Promise((resolve, reject) => reads.push({ params, resolve, reject })), onSession: (value, id) => tasks.openSession(value, id) });
  api.update(snapshot); api.route('cx-a', 'session-import'); api.show('project');
  const find = (text, tag = 'button') => { const row = root.all.find(row => row.tag === tag && row.textContent === text); assert.ok(row, text); return row; };
  const get = id => { const row = taskRoot.all.find(row => row.id === id); assert.ok(row, id); return row; };
  return { api, root, taskRoot, tasks, reads, itemReads, writes, routes, snapshot, project, find, get };
}
test('real project sessions paginate beyond legacy 12 and fill an explicit unsaved editor', async () => {
  const f = fixture(); assert.deepEqual(JSON.parse(JSON.stringify(f.reads[0].params)), { project_id: 'cx-a', scope: 'active', offset: 0, limit: 20 });
  f.reads[0].resolve(result(Array.from({ length: 20 }, (_, n) => session(n)))); await tick();
  assert.match(f.root.textContent, /真实标题 19/); assert.doesNotMatch(f.root.textContent, /当前样本/);
  void f.find('下一页').fire('click'); assert.equal(f.reads[1].params.offset, 20);
  f.reads[1].resolve(result([session(25)], { offset: 20, sampled_at: 'later' })); await tick();
  assert.equal(f.reads.length, 2); assert.match(f.root.textContent, /真实标题 25/);
  void f.find('关联为新事项').fire('click');
  assert.equal(f.get('item-editor').open, true); assert.equal(f.get('item-title').value, '真实标题 25');
  assert.equal(f.get('item-project_id').value, 'cx-a'); assert.equal(f.get('item-provider_id').value, 'codex'); assert.equal(f.get('item-session_key').value, 'codex:25'); assert.equal(f.writes.length, 0);
  f.tasks.update({ ...f.snapshot, sessions: { available: true, items: [] } }); assert.equal(f.get('item-session_key').value, 'codex:25');
  const saving = f.get('item-form').fire('submit'); await tick(); assert.equal(f.writes[0].session_key, 'codex:25');
  f.itemReads.at(-1).resolve({ items: [], total: 0, offset: 0, limit: 50 }); await saving;
});
test('session editor cancellation makes no write and existing detailed or quick draft cannot be replaced', async () => {
  const f = fixture(); assert.equal(f.tasks.openSession(session(30), 'cx-a'), true);
  await f.get('item-editor').querySelectorAll('button').find(row => row.textContent === '取消').fire('click'); assert.equal(f.get('item-editor').open, false); assert.equal(f.writes.length, 0);
  f.get('item-quick-title').value = 'quick draft'; assert.equal(f.tasks.openSession(session(31), 'cx-a'), false); assert.equal(f.get('item-quick-title').value, 'quick draft');
  assert.match(f.get('item-quick-feedback').textContent, /先保存或取消/);
  f.get('item-quick-title').value = ''; f.tasks.open(); f.get('item-title').value = 'detail draft';
  assert.equal(f.tasks.openSession(session(31), 'cx-a'), false); assert.equal(f.get('item-title').value, 'detail draft'); assert.equal(f.writes.length, 0);
});
test('switching project and archive filter ignores late requests', async () => {
  const f = fixture(); f.api.route('cx-b', 'session-import'); f.api.show('project');
  f.reads[1].resolve(result([session(40, { project_id: 'cx-b' })])); await tick(); f.reads[0].resolve(result([session(0)])); await tick();
  assert.match(f.root.textContent, /真实标题 40/); assert.doesNotMatch(f.root.textContent, /真实标题 0/);
  void f.find('已归档').fire('click'); assert.equal(f.reads[2].params.scope, 'archived');
  void f.find('未归档').fire('click'); f.reads[3].resolve(result([session(50)])); await tick(); f.reads[2].resolve(result([session(60, { archived: true })])); await tick();
  assert.match(f.root.textContent, /真实标题 50/); assert.doesNotMatch(f.root.textContent, /真实标题 60/);
});
test('read failures retain content, prevent associations and allow retry; stale and disabled remain honest', async () => {
  const f = fixture(); f.reads[0].resolve(result([session(25)])); await tick();
  void f.find('重新读取会话').fire('click'); f.reads[1].reject(Error('network lost')); await tick();
  assert.match(f.root.textContent, /真实标题 25/); assert.match(f.root.textContent, /保留已读内容/); assert.equal(f.find('关联为新事项').disabled, true);
  void f.find('重新读取会话').fire('click'); f.reads[2].resolve(result([session(25)], { status: 'stale' })); await tick(); assert.equal(f.find('关联为新事项').disabled, true);
  f.tasks.update({ ...f.snapshot, codex_desktop: { status: 'not_configured' } }); assert.equal(f.tasks.openSession(session(25), 'cx-a'), false);
  f.api.update({ ...f.snapshot, codex_desktop: { status: 'not_configured' } }); assert.match(f.root.textContent, /真实标题 25/); assert.equal(f.find('关联为新事项').disabled, true);
});
test('source membership revision changes reset pagination while sampling alone does not', async () => {
  const f = fixture(); f.reads[0].resolve(result([session(1)])); await tick(); void f.find('下一页').fire('click');
  f.reads[1].resolve(result([session(2)], { offset: 20, revision: 'changed' })); await tick(); assert.equal(f.reads[2].params.offset, 0);
  f.reads[2].resolve(result([session(3)], { revision: 'changed' })); await tick(); assert.match(f.root.textContent, /真实标题 3/);
});
test('readonly project hides editing and global projectless and unknown routes are separate', async () => {
  const f = fixture(); assert.match(f.root.textContent, /Codex · 只读/); assert.ok(!f.root.all.some(row => row.tag === 'button' && row.textContent === '项目设置'));
  void f.find('来源设置').fire('click'); assert.equal(f.routes.at(-1), 'settings');
  f.api.routeSessions('projectless'); f.api.show('codex-sessions'); assert.equal(f.reads.at(-1).params.scope, 'projectless'); assert.equal(f.reads.at(-1).params.project_id, undefined);
  f.reads.at(-1).resolve(result([session(70, { project_id: null, assignment_state: 'projectless' })])); await tick(); assert.match(f.root.textContent, /明确未分配项目/);
  f.api.routeSessions('unknown'); f.api.show('codex-sessions'); f.reads.at(-1).resolve(result([session(80, { project_id: null, assignment_state: 'conflict', binding_available: false })])); await tick(); assert.match(f.root.textContent, /归属未知或有冲突/); assert.equal(f.find('关联为新事项').disabled, true);
});
test('moving an existing associated item preserves mismatched selection and asks for explicit reselection', async () => {
  const f = fixture(); f.tasks.openSession(session(30), 'cx-a'); f.get('item-project_id').value = 'cx-b'; await f.get('item-project_id').fire('change');
  assert.equal(f.get('item-session_key').value, 'codex:30'); assert.match(f.get('item-feedback').textContent, /重新选择/); await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0);
});
test('manual project session page can save a projectless association for service directory validation', async () => {
  const f = fixture(), manual = { id: 'manual', name: '手工项目', directories: [{ path: 'C:/example/shared' }] };
  const snapshot = { ...f.snapshot, projects: { status: 'available', items: [...f.snapshot.projects.items, manual] } };
  f.tasks.update(snapshot); f.api.update(snapshot); f.api.route('manual', 'session-import'); f.api.show('project');
  f.reads.at(-1).resolve(result([session(90, { project_id: null, assignment_state: 'projectless', binding_available: true })])); await tick();
  assert.equal(f.find('关联为新事项').disabled, false); void f.find('关联为新事项').fire('click');
  assert.equal(f.get('item-project_id').value, 'manual'); assert.equal(f.get('item-session_key').value, 'codex:90');
  const saving = f.get('item-form').fire('submit'); await tick();
  assert.equal(f.writes.length, 1); assert.equal(f.writes[0].project_id, 'manual'); assert.equal(f.writes[0].session_key, 'codex:90');
  f.itemReads.at(-1).resolve({ items: [], total: 0, offset: 0, limit: 50 }); await saving;
});
test('projectless manual draft moved to an external project keeps selection and blocks the mismatch', async () => {
  const f = fixture(), manual = { id: 'manual', name: '手工项目' };
  f.tasks.update({ ...f.snapshot, projects: { items: [...f.snapshot.projects.items, manual] } });
  assert.equal(f.tasks.openSession(session(91, { project_id: null, assignment_state: 'projectless' }), 'manual'), true);
  f.get('item-project_id').value = 'cx-a'; await f.get('item-project_id').fire('change');
  assert.equal(f.get('item-session_key').value, 'codex:91'); assert.match(f.get('item-feedback').textContent, /重新选择/);
  await f.get('item-form').fire('submit'); assert.equal(f.writes.length, 0);
});


function linkedFixture(options = {}) {
  const context = vm.createContext({window:{confirm:()=>true},document:{createElement:tag => new Element(tag)},URL});
  vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../web/workspace/project-space.js'),'utf8'),context);
  const root = new Element('section'), reads = [];
  let current = true;
  const api = context.window.WorkspaceSessionList.create({root,taskId:'task-a',agents:[{id:'agent-a',name:'已登记 Agent'}],read:() => new Promise((resolve,reject) => reads.push({resolve,reject})),isCurrent:()=>current,...options});
  return {root,reads,api,close:()=>{current=false;}};
}
const linked = (extra={}) => ({id:'link-a',task_id:'task-a',project_id:'p-a',agent_id:'agent-a',source:'dsh',external_id:'external-a',title:'关联会话',last_seen_at:null,...extra});
test('linked conversations show provider-neutral metadata and safe entry points without archived status', async () => {
  const f=linkedFixture();
  f.reads[0].resolve({item:{id:'task-a'},items:[linked({url:'https://example.com/chat',source_instance:'local-a'}),linked({source:'codex',legacy:true,url:'javascript:alert(1)',title:'<img src=x>'}),linked({url:'https://user:secret@example.com'})]});await tick();
  assert.match(f.root.textContent,/已登记 Agent/);assert.match(f.root.textContent,/实例：local-a/);assert.match(f.root.textContent,/旧关联/);assert.match(f.root.textContent,/<img src=x>/);
  assert.match(f.root.textContent,/未同步聊天正文/);assert.doesNotMatch(f.root.textContent,/已归档|未归档/);
  const links=f.root.querySelectorAll('a');assert.equal(links.length,1);assert.equal(links[0].attributes.rel,'noopener noreferrer');assert.equal(links[0].attributes.href,'https://example.com/chat');
});
test('linked conversation lists exclude unlinked and foreign project records', async () => {
  const f=linkedFixture({taskId:null,projectId:'p-a'});
  f.reads[0].resolve({items:[linked(),linked({task_id:null,title:'未关联'}),linked({project_id:'p-b',title:'别的项目'})]});await tick();
  assert.match(f.root.textContent,/1 条关联会话/);assert.doesNotMatch(f.root.textContent,/别的项目|未关联/);
});
test('late or mismatched task sessions cannot populate a new detail', async () => {
  const f=linkedFixture();f.api.refresh();
  f.reads[1].resolve({item:{id:'wrong'},items:[linked()]});await tick();assert.match(f.root.textContent,/会话暂不可用/);
  f.reads[0].resolve({item:{id:'task-a'},items:[linked({title:'旧响应'})]});await tick();assert.doesNotMatch(f.root.textContent,/旧响应/);
  f.api.refresh();f.close();f.reads[2].resolve({item:{id:'task-a'},items:[linked({title:'关闭后响应'})]});await tick();assert.doesNotMatch(f.root.textContent,/关闭后响应/);
});
test('failed linked sessions allow explicit retry and reject foreign task rows', async () => {
  const f=linkedFixture();f.reads[0].reject(Error('offline'));await tick();assert.match(f.root.textContent,/会话暂不可用/);
  const retry=f.root.all.find(row=>row.tag==='button');void retry.fire('click');f.reads[1].resolve({item:{id:'task-a'},items:[linked({task_id:'another',title:'外部事项'}),linked()]});await tick();
  assert.match(f.root.textContent,/1 条关联会话/);assert.doesNotMatch(f.root.textContent,/外部事项/);
});

function editorFixture(options={}) {
  const context=vm.createContext({window:{confirm:()=>true},document:{createElement:tag=>new Element(tag)},URL});
  vm.runInContext(fs.readFileSync(path.resolve(__dirname,'../../web/workspace/project-space.js'),'utf8'),context);
  const root=new Element('section'),writes=[],dirty=[],busy=[];let current=true;
  context.window.WorkspaceSessionList.createEditor({root,item:{id:'task-a',project_id:'p-a'},agents:[{id:'agent-a',name:'启用对象',enabled:true,project_ids:['p-a']},{id:'disabled',name:'已禁用',enabled:false}],postJson:async(url,body)=>{writes.push({url,body});return {task_id:'task-a',...body};},refresh:async()=>true,isCurrent:()=>current,onDirty:value=>dirty.push(value),onBusy:value=>busy.push(value),...options});
  const get=id=>root.all.find(row=>row.id===id),button=text=>root.all.find(row=>row.tag==='button'&&row.textContent===text),form=root.all.find(row=>row.tag==='form');
  const fill=async(id,value)=>{const input=get(id);input.value=value;await input.fire('input');};
  const confirm=async()=>{get('sl-confirm').checked=true;await get('sl-confirm').fire('change');};
  return {root,writes,dirty,busy,get,button,form,fill,confirm,close:()=>{current=false;}};
}
test('manual conversation association requires confirmation, preserves instance identity and clears only after success',async()=>{
  const f=editorFixture();await f.fill('sl-source','dsh');await f.fill('sl-source_instance','local');await f.fill('sl-external_id','chat-1');
  await f.form.fire('submit');assert.equal(f.writes.length,0);await f.confirm();await f.form.fire('submit');
  assert.equal(f.writes.length,1);assert.equal(f.writes[0].body.source_instance,'local');assert.match(f.writes[0].url,/tasks\/task-a\/sessions$/);assert.equal(f.get('sl-external_id').value,'');assert.equal(f.dirty.at(-1),false);assert.deepEqual(f.busy,[true,false]);
});
test('editing association invalidates confirmation and rejects unsafe conversation URLs',async()=>{
  const f=editorFixture();await f.fill('sl-source','codex');await f.fill('sl-external_id','chat');await f.confirm();await f.fill('sl-url','javascript:alert(1)');
  assert.equal(f.get('sl-confirm').checked,false);await f.confirm();await f.form.fire('submit');assert.equal(f.writes.length,0);assert.match(f.get('sl-status').textContent,/http/);assert.doesNotMatch(f.root.textContent,/已禁用/);
});
test('unknown association writes block replay until explicit successful reread, retaining draft',async()=>{
  let posts=0,reads=0;const f=editorFixture({postJson:async()=>{posts++;throw Error('connection lost');},refresh:async()=>{reads++;return true;}});
  await f.fill('sl-source','codex');await f.fill('sl-external_id','chat');await f.confirm();await f.form.fire('submit');await f.form.fire('submit');
  assert.equal(posts,1);assert.equal(f.get('sl-external_id').value,'chat');assert.match(f.get('sl-status').textContent,/结果未知/);assert.equal(f.button('确认关联会话').disabled,true);
  await f.button('核对关联结果').fire('click');assert.equal(reads,1);assert.equal(f.get('sl-confirm').checked,false);await f.confirm();await f.form.fire('submit');assert.equal(posts,2);
});
test('association conflicts preserve fields, and concurrent submits create one request',async()=>{
  let resolve,posts=0;const f=editorFixture({postJson:()=>{posts++;return new Promise(value=>{resolve=value;});}});
  await f.fill('sl-source','codex');await f.fill('sl-external_id','chat');await f.confirm();const saving=f.form.fire('submit');await tick();await f.form.fire('submit');assert.equal(posts,1);resolve({task_id:'task-a',source:'codex',source_instance:'',external_id:'chat'});await saving;
  const conflict=editorFixture({postJson:async()=>{throw Object.assign(Error('conflict'),{status:409,reason:'session_conflict'});}});
  await conflict.fill('sl-source','codex');await conflict.fill('sl-external_id','chat');await conflict.confirm();await conflict.form.fire('submit');assert.equal(conflict.get('sl-external_id').value,'chat');assert.match(conflict.get('sl-status').textContent,/其他事项/);
});
test('late association response cannot erase another detail or report success after close',async()=>{
  let resolve;const f=editorFixture({postJson:()=>new Promise(value=>{resolve=value;})});
  await f.fill('sl-source','dsh');await f.fill('sl-external_id','chat');await f.confirm();const saving=f.form.fire('submit');await tick();f.close();resolve({task_id:'task-a',source:'dsh',source_instance:'',external_id:'chat'});await saving;assert.equal(f.get('sl-external_id').value,'chat');assert.doesNotMatch(f.get('sl-status').textContent,/已保存/);
});

test('unlink carries both snapshot guards, confirms human intent and refreshes after success',async()=>{
  const writes=[];let reads=0;
  const f=linkedFixture({postJson:async(url,body)=>{writes.push({url,body});return {id:'link-a',task_id:null};},read:async()=>{reads++;return {item:{id:'task-a'},items:reads===1?[linked({updated_at:'2026-10-20T00:00:00Z'})]:[]};}});
  await tick();f.root.all.find(row=>row.tag==='button'&&row.textContent==='解除此事项关联');
  const unlink=f.root.all.find(row=>row.tag==='button'&&row.textContent==='解除此事项关联');await unlink.fire('click');
  assert.equal(writes.length,1);assert.equal(writes[0].body.expected_task_id,'task-a');assert.equal(writes[0].body.expected_updated_at,'2026-10-20T00:00:00Z');assert.match(f.root.textContent,/尚未关联/);assert.equal(f.root.all.find(row=>row.textContent==='重新读取会话').disabled,false);
});
test('unlink conflict freezes stale buttons until a successful reread and hides legacy unlink',async()=>{
  const f=linkedFixture({postJson:async()=>{throw Object.assign(Error('changed'),{reason:'session_changed',status:409});}});
  f.reads[0].resolve({item:{id:'task-a'},items:[linked({updated_at:'2026-10-20T00:00:00Z'}),linked({legacy:true,id:null})]});await tick();
  assert.equal(f.root.all.filter(row=>row.textContent==='解除此事项关联').length,1);
  await f.root.all.find(row=>row.textContent==='解除此事项关联').fire('click');assert.match(f.root.textContent,/其他窗口更新/);assert.equal(f.root.all.find(row=>row.textContent==='解除此事项关联').disabled,true);
  void f.root.all.find(row=>row.textContent==='重新读取会话').fire('click');f.reads[1].resolve({item:{id:'task-a'},items:[linked({updated_at:'2026-10-20T00:00:01Z'})]});await tick();assert.equal(f.root.all.find(row=>row.textContent==='解除此事项关联').disabled,false);
});
