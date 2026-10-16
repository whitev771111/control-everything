const statusLine = document.querySelector("#progress-status");
const rail = document.querySelector("#progress-rail");
const newsZone = document.querySelector("#news-zone");
const todoZone = document.querySelector("#todo-zone");
const launchZone = document.querySelector("#launch-zone");
const glance = document.querySelector("#progress-board");
const stage = document.querySelector("#progress-stage");
const side = document.querySelector("#progress-side");
const states = ["未开始", "在做", "等我", "完成"];
const accents = {
  biji: "#3ecfb8",
  "mianban-develop": "#7aa2ff",
  "n8n-for-me": "#f0b429",
  "operations-agent": "#ff7b89",
  "terminal-workbench": "#b79bff",
  "workflow-for-coding": "#63d391",
};
const motion = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
let csrf = "";
let selected = "";
let projects = [];
let ringLength = 0;
let built = null;
let profileMode = false;
let profile = null;
let saving = false;
let todoOffset = 0;
let historyOffset = 0;
let historyRequest = 0;
let deskSnapshot = null;

function headers() {
  const value = {"X-Session-Client": "panel"};
  if (csrf) value["X-Session-CSRF"] = csrf;
  return value;
}

function currentProject() {
  return projects.find((item) => item.id === selected) || projects[0];
}

function accentOf(project) {
  return accents[project.id] || "#7aa2ff";
}

function ensureStage() {
  if (built) return;
  const hero = document.createElement("div");
  hero.className = "hero";
  const title = document.createElement("div");
  const kicker = document.createElement("p");
  kicker.className = "kicker";
  const heading = document.createElement("h2");
  title.append(kicker, heading);
  const dial = document.createElement("div");
  dial.className = "dial";
  const radius = 68;
  ringLength = 2 * Math.PI * radius;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 180 180");
  svg.classList.add("ring");
  const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  track.setAttribute("class", "track");
  track.setAttribute("cx", "90");
  track.setAttribute("cy", "90");
  track.setAttribute("r", String(radius));
  const value = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  value.setAttribute("class", "value");
  value.setAttribute("cx", "90");
  value.setAttribute("cy", "90");
  value.setAttribute("r", String(radius));
  value.style.strokeDasharray = String(ringLength);
  const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
  label.setAttribute("x", "90");
  label.setAttribute("y", "100");
  svg.append(track, value, label);
  dial.append(svg);
  hero.append(title, dial);
  const note = document.createElement("input");
  note.className = "note";
  note.maxLength = 24;
  if (profileMode) note.maxLength = 1000;
  note.id = 'progress-note';
  note.placeholder = "用一句话写下现在在做的事";
  const slider = document.createElement("input");
  slider.className = "slider";
  slider.type = "range";
  slider.id = 'progress-slider';
  slider.min = "0";
  slider.max = "100";
  slider.addEventListener("input", () => {
    const percent = Number(slider.value);
    slider.style.setProperty("--fill", percent + "%");
    setRing(percent);
  });
  slider.addEventListener("change", () => {
    if (profileMode) return;
    const project = currentProject();
    const percent = Number(slider.value);
    const state = percent === 0 ? "未开始" : percent === 100 ? "完成" : project.state === "等我" ? "等我" : "在做";
    save(project.id, state, note.value, percent);
  });
  note.addEventListener("change", () => {
    if (profileMode) return;
    const project = currentProject();
    save(project.id, project.state, note.value, Number(slider.value));
  });
  const choices = document.createElement("div");
  choices.className = "states";
  for (const name of states) {
    const choice = document.createElement("button");
    choice.type = "button";
    choice.textContent = name;
    choice.addEventListener("click", () => {
      const project = currentProject();
      if (profileMode) { built.state.value = name; for (const button of choices.children) button.setAttribute('aria-pressed', String(button === choice)); return; }
      const percent = name === "完成" ? 100 : name === "未开始" ? 0 : Number(slider.value);
      save(project.id, name, note.value, percent);
    });
    choices.append(choice);
  }
  const records = document.createElement("section");
  records.className = "records";
  stage.append(hero, note, slider, choices, records);
  built = {kicker, heading, value, label, note, slider, choices, records};
  if (profileMode) {
    const form = document.createElement('form'); form.id = 'progress-manual-form';
    const fields = document.createElement('fieldset'); fields.id = 'progress-manual-fields';
    const stateLabel = document.createElement('label'); stateLabel.textContent = '人工状态';
    const state = document.createElement('select'); state.id = 'progress-manual-state'; state.required = true;
    for (const name of ['', ...states]) { const option = document.createElement('option'); option.value = name; option.textContent = name || '请选择状态'; state.append(option); } stateLabel.append(state);
    const enabledLabel = document.createElement('label'); const enabled = document.createElement('input'); enabled.type = 'checkbox'; enabled.id = 'progress-percent-enabled'; enabledLabel.append(enabled, document.createTextNode('记录百分比（关闭则保存为空）'));
    const percentLabel = document.createElement('label'); percentLabel.textContent = '人工百分比';
    const percent = document.createElement('input'); percent.id = 'progress-percent'; percent.type = 'number'; percent.min = '0'; percent.max = '100'; percent.step = 'any'; percentLabel.append(percent);
    const noteLabel = document.createElement('label'); noteLabel.textContent = '人工说明'; noteLabel.append(note);
    const submit = document.createElement('button'); submit.type = 'submit'; submit.textContent = '保存人工记录';
    const feedback = document.createElement('p'); feedback.id = 'progress-manual-feedback'; feedback.setAttribute('role', 'status');
    const history = document.createElement('div'); history.id = 'progress-history';
    enabled.addEventListener('change', () => { percent.disabled = slider.disabled = !enabled.checked; if (enabled.checked && percent.value === '') percent.value = slider.value; if (!enabled.checked) { percent.value = ''; setRing(null); } });
    percent.addEventListener('input', () => { slider.value = percent.value; setRing(percent.value === '' ? null : Number(percent.value)); });
    slider.addEventListener('input', () => { percent.value = slider.value; });
    fields.append(stateLabel, enabledLabel, percentLabel, slider, noteLabel, choices, submit); form.append(fields, feedback);
    stage.insertBefore(form, records); stage.append(history);
    Object.assign(built, { state, enabled, percent, fields, feedback, history });
    form.addEventListener('submit', event => { event.preventDefault(); save(currentProject().id, state.value, note.value, enabled.checked && percent.value !== '' ? Number(percent.value) : null); });
  }
}

function setRing(percent) {
  built.value.style.strokeDashoffset = String(ringLength * (1 - (Number.isFinite(percent) ? percent : 0) / 100));
  built.value.style.opacity = Number.isFinite(percent) ? '1' : '0';
  built.label.textContent = Number.isFinite(percent) ? String(percent) : '未记录';
}

function fillRecords(project) {
  built.records.replaceChildren();
  const title = document.createElement("h3");
  title.textContent = "来源观察";
  built.records.append(title);
  if (profileMode) {
    const counts = project.task_counts || {}; const tasks = document.createElement('p'); tasks.textContent = counts.total ? `任务统计：${counts.total} 条 · 待办 ${counts.todo} / 进行中 ${counts.doing} / 阻塞 ${counts.blocked} / 待验收 ${counts.review} / 完成 ${counts.done}` : '任务统计：没有任务';
    const observation = project.observation || {}, labels = { not_configured: '未配置', missing: '文件或目录缺失', unrecognized: '没有可识别内容', error: '读取错误', available: '可用' };
    const source = document.createElement('p'); source.textContent = `来源观察：${labels[observation.status] || '未配置'}${observation.reason ? ` · ${observation.reason}` : ''}`;
    built.records.append(tasks, source);
    if (observation.reported) { const reported = document.createElement('p'), value = observation.reported; reported.textContent = `来源报告：${[value.state, value.stage, Number.isFinite(value.percent) ? `${value.percent}%` : '', value.note].filter(Boolean).join(' · ')}`; built.records.append(reported); }
  }
  if (!(project.records || []).length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这个项目暂无来源记录。人工进度与任务仍可独立管理。";
    built.records.append(empty);
    return;
  }
  (project.records || []).forEach((record, index) => {
    const row = document.createElement("article");
    row.className = "record";
    row.style.animationDelay = (index * 45) + "ms";
    const name = document.createElement("strong");
    name.textContent = record.feature;
    const text = document.createElement("span");
    text.textContent = record.label + " · " + record.text;
    row.append(name, text);
    built.records.append(row);
  });
}

function showProject(animate) {
  const project = currentProject();
  if (profileMode) { historyRequest++; built.history.textContent = '正在读取人工历史…'; }
  if (!project) { stage.hidden = true; return; }
  stage.hidden = false;
  selected = project.id;
  const apply = () => {
    if (selected !== project.id) return;
    const accent = accentOf(project);
    stage.style.setProperty("--accent", accent);
    built.kicker.textContent = `人工记录 · ${project.state || '未记录'}${project.archived ? ' · 已归档' : ''}`;
    built.heading.textContent = project.name;
    if (document.activeElement !== built.note || built.projectId !== project.id) built.note.value = project.note || "";
    built.note.setAttribute("aria-label", project.name);
    built.slider.value = Number.isFinite(project.percent) ? String(project.percent) : '0';
    built.slider.style.setProperty("--fill", Number.isFinite(project.percent) ? project.percent + '%' : '0%');
    built.slider.setAttribute("aria-label", project.name + "的进度");
    setRing(project.percent);
    if (profileMode) { built.state.value = project.state || ''; built.enabled.checked = Number.isFinite(project.percent); built.percent.value = Number.isFinite(project.percent) ? project.percent : ''; built.percent.disabled = built.slider.disabled = !built.enabled.checked; if (built.projectId !== project.id) historyOffset = 0; loadHistory().catch(error => { built.feedback.textContent = error.message; }); }
    built.projectId = project.id;
    for (const choice of built.choices.children) {
      choice.setAttribute("aria-pressed", String(choice.textContent === project.state));
    }
    fillRecords(project);
    stage.classList.remove("is-out");
    stage.classList.add("is-in");
  };
  if (animate && motion) {
    stage.classList.remove("is-in");
    stage.classList.add("is-out");
    window.setTimeout(apply, 140);
  } else {
    apply();
  }
}

function priority(project) {
  const waiting = (project.records || []).some((record) => (record.text || '').includes("USER_CONFIRMATION") || (record.text || '').includes("BLOCKED"));
  if (project.state === "等我" || waiting) return 0;
  if (project.state === "在做") return 1;
  if (project.state === "未开始") return 2;
  return 3;
}

function orderedProjects() {
  return projects.map((project, index) => ({project, index})).sort((a, b) => priority(a.project) - priority(b.project) || a.index - b.index).map((item) => item.project);
}

function paintBoard() {
  glance.replaceChildren();
  for (const project of orderedProjects()) {
    const button = document.createElement("button");
    button.type = "button";
    button.style.setProperty("--accent", accentOf(project));
    button.setAttribute("aria-current", String(project.id === selected));
    const waiting = priority(project) === 0;
    if (waiting) button.classList.add("wait");
    const row = document.createElement("div");
    row.className = "row";
    const name = document.createElement("strong");
    name.textContent = project.name;
    const state = document.createElement("em");
    state.textContent = (project.state || '未记录') + ' · ' + (Number.isFinite(project.percent) ? project.percent + '%' : '未填写百分比');
    row.append(name, state);
    const feature = document.createElement("b");
    feature.textContent = project.records[0]?.feature || "没有未收口记录";
    const text = document.createElement("span");
    text.textContent = project.records[0] ? project.records[0].text : "进度由你在下面设定";
    button.append(row, feature, text);
    if ((project.sessions || []).length && !profileMode) {
      const chips = document.createElement("div");
      chips.className = "chips";
      for (const hit of project.sessions) {
        const chip = document.createElement(hit.href ? "a" : "i");
        chip.textContent = hit.tool + " · " + hit.place;
        if (hit.href) chip.href = hit.href;
        chips.append(chip);
      }
      button.append(chips);
    }
    button.addEventListener("click", () => {
      if (selected === project.id) return;
      selected = project.id;
      paintRail();
      paintBoard();
      showProject(true);
      if (profileMode && deskSnapshot) paintSide(deskSnapshot);
    });
    glance.append(button);
  }
}

function paintRail() {
  if (!rail) return;
  rail.replaceChildren();
  rail.className = "rail";
  for (const project of projects) {
    const button = document.createElement("button");
    button.type = "button";
    button.style.setProperty("--accent", accentOf(project));
    button.setAttribute("aria-current", String(project.id === selected));
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    const words = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = project.name;
    const meta = document.createElement("em");
    meta.textContent = (project.state || '未记录') + " · " + (project.records[0]?.feature || "无记录");
    words.append(name, meta);
    const meter = document.createElement("b");
    meter.className = "meter";
    const fill = document.createElement("i");
    fill.hidden = !Number.isFinite(project.percent);
    if (Number.isFinite(project.percent)) fill.style.setProperty("--fill", project.percent + "%");
    meter.append(fill);
    button.append(swatch, words, meter);
    button.addEventListener("click", () => {
      if (selected === project.id) return;
      selected = project.id;
      paintRail();
      paintBoard();
      showProject(true);
      if (profileMode && deskSnapshot) paintSide(deskSnapshot);
    });
    rail.append(button);
  }
}

const launchGroups = [
  ["desk", "工作台", "你平时写代码的那套终端"],
  ["code", "用 Grok 写", "网关开着之后，再选短或长上下文"],
  ["lab", "原油实验", "看进度不会启动实验"],
  ["net", "网络", "偶尔才用"],
];

function paintSide(desk) {
  deskSnapshot = desk;
  newsZone.replaceChildren();
  const stories = desk.news || [];
  if (!stories.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这会儿没有拉到标题。";
    newsZone.append(empty);
  }
  for (const item of stories) {
    const link = document.createElement("a");
    link.className = "story";
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.textContent = item.title;
    newsZone.append(link);
  }
  todoZone.replaceChildren();
  const form = document.createElement("form");
  form.className = "todo-form";
  const input = document.createElement("input");
  input.maxLength = 40;
  input.id = 'progress-todo-input';
  input.placeholder = "写一条待办";
  input.setAttribute("aria-label", "待办");
  const add = document.createElement("button");
  add.type = "submit";
  add.textContent = "添加";
  const activeProject = currentProject();
  if (profileMode) { input.disabled = add.disabled = !activeProject || activeProject.archived || !csrf; }
  form.append(input, add);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    createTodo(input.value, selected);
  });
  const list = document.createElement("div");
  list.className = "todos";
  for (const item of desk.todos || []) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = item.done ? "done" : "";
    const mark = document.createElement("i");
    mark.textContent = item.done ? "✓" : "○";
    const text = document.createElement("span");
    const owner = (projects.find((project) => project.id === item.project_id) || {}).name;
    text.textContent = owner ? owner + " · " + item.text : item.text;
    row.append(mark, text);
    row.dataset.todoId = item.id; row.addEventListener("click", () => toggleTodo(item.id));
    list.append(row);
  }
  if (!(desk.todos || []).length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有待办。";
    list.append(empty);
  }
  todoZone.append(form, list);
  if (profileMode) {
    todoZone.append(pageControls(desk.todos_total || 0, todoOffset, 20, async value => { todoOffset = value; await refreshDesk(); }));
    launchZone.replaceChildren(); const note = document.createElement('p'); note.textContent = '当前 profile 不提供个人启动器、SSH、小院或执行入口。'; launchZone.append(note);
    document.querySelector('#launch-heading h2').textContent = '当前能力'; document.querySelector('#launch-heading p').textContent = '项目、人工记录与任务可直接使用。'; return;
  }
  launchZone.replaceChildren();
  for (const [id, title, about] of launchGroups) {
    const group = document.createElement("article");
    group.className = "group";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const copy = document.createElement("p");
    copy.textContent = about;
    group.append(heading, copy);
    for (const item of (desk.launchers || []).filter((entry) => entry.group === id)) {
      const button = document.createElement("button");
      button.type = "button";
      const name = document.createElement("strong");
      name.textContent = item.name;
      const blurb = document.createElement("span");
      const mark = item.status === "up" ? "在跑" : item.status === "missing" ? "文件缺失" : "可打开";
      blurb.textContent = item.blurb + " · " + mark;
      button.append(name, blurb);
      button.disabled = item.status === "missing";
      button.addEventListener("click", () => launch(item.id, button));
      group.append(button);
    }
    launchZone.append(group);
  }
}

function headers() {
  const value = {"X-Session-Client": "panel"};
  if (csrf) value["X-Session-CSRF"] = csrf;
  return value;
}

async function save(projectId, state, note, percent) {
  if (saving) return;
  saving = true; if (profileMode) { built.fields.disabled = true; built.feedback.textContent = '正在保存…'; }
  try {
    const response = await fetch("/api/progress/set", {
      method: "POST",
      headers: {...headers(), "Content-Type": "application/json", "Origin": location.origin},
      body: JSON.stringify({project_id: projectId, state, note, percent}),
    });
    if (!response.ok) { const error = await response.json(); throw new Error(`人工记录没有保存：${error.reason || response.status}${error.field ? ` · ${error.field}` : ''}。草稿已保留。`); }
    projects = (await response.json()).projects || [];
    selected = projectId;
    statusLine.textContent = "";
    paintRail();
    paintBoard();
    showProject(false);
    if (profileMode) built.feedback.textContent = '人工记录已保存。';
  } catch (error) { if (profileMode) built.feedback.textContent = error.message; else statusLine.textContent = '没有记下。'; }
  finally { saving = false; if (profileMode) built.fields.disabled = false; }
}

async function refreshDesk() {
  const desk = await fetch("/api/desk", {headers: headers()});
  if (desk.ok) { const value = await desk.json(); if (profileMode) { const response = await fetch(`/api/workspace/todos?offset=${todoOffset}&limit=20`, { headers: headers() }); if (!response.ok) throw new Error('待办分页读取失败'); const page = await response.json(); value.todos = page.items; value.todos_total = page.total; } paintSide(value); }
}

async function createTodo(text, projectId) {
  const response = await fetch("/api/todos", {
    method: "POST",
    headers: {...headers(), "Content-Type": "application/json", "Origin": location.origin},
    body: JSON.stringify({text, project_id: projectId || ""}),
  });
  if (!response.ok) statusLine.textContent = "待办没有记下。";
  else await refreshDesk();
}

async function toggleTodo(id) {
  const response = await fetch("/api/todos/toggle", {
    method: "POST",
    headers: {...headers(), "Content-Type": "application/json", "Origin": location.origin},
    body: JSON.stringify({id}),
  });
  if (!response.ok) statusLine.textContent = "待办没有改成。";
  else await refreshDesk();
}

async function launch(id, button) {
  button.classList.add("is-busy");
  const response = await fetch("/api/launch", {
    method: "POST",
    headers: {...headers(), "Content-Type": "application/json", "Origin": location.origin},
    body: JSON.stringify({id}),
  });
  button.classList.remove("is-busy");
  if (!response.ok) statusLine.textContent = "这个入口没有打开。";
  else paintSide(await response.json());
}

document.querySelector("#progress-date").textContent = new Intl.DateTimeFormat("zh-CN", {
  month: "long", day: "numeric", weekday: "long",
}).format(new Date());

async function load() {
  const bootstrap = await fetch("/api/bootstrap", {headers: headers()});
  if (!bootstrap.ok) throw new Error("bootstrap");
  const boot = await bootstrap.json(); csrf = boot.csrf_token; profileMode = boot.capabilities?.profile === true;
  if (profileMode) { const response = await fetch('/api/profile', { headers: headers() }); if (!response.ok) throw new Error('profile'); profile = await response.json(); document.querySelector('.topbar nav').replaceChildren(profileLink('项目管理', '/workspace#tasks'), profileLink('来源与导入', '/workspace#settings')); }
  const progress = await fetch("/api/progress", {headers: headers()});
  if (!progress.ok) throw new Error("progress");
  projects = ((await progress.json()).projects || []).map(item => ({ ...item, records: item.records || [], sessions: item.sessions || [] }));
  selected = (projects.find((item) => item.state === "在做") || projects[0] || {}).id || "";
  ensureStage();
  paintRail();
  paintBoard();
  showProject(false);
  if (profileMode) { const info = document.createElement('p'); info.className = 'profile-info'; info.textContent = `当前 data-dir：${profile.data_dir}`; glance.before(info); if (!projects.length) { const empty = document.createElement('div'); empty.className = 'profile-empty'; const title = document.createElement('p'); title.textContent = '还没有登记项目。添加项目后可记录人工进度与任务。'; empty.append(title, profileLink('添加项目', '/workspace#tasks'), profileLink('导入旧目录', '/workspace#settings'), profileLink('来源设置', '/workspace#settings')); glance.append(empty); } }
  await refreshDesk();
}

function profileLink(text, href) { const link = document.createElement('a'); link.textContent = text; link.href = href; link.className = 'back'; return link; }
function pageControls(total, offset, limit, callback) {
  const root = document.createElement('div'); root.className = 'pager';
  const previous = document.createElement('button'), next = document.createElement('button'), text = document.createElement('span');
  previous.type = next.type = 'button'; previous.textContent = '上一页'; next.textContent = '下一页'; previous.disabled = offset <= 0; next.disabled = offset + limit >= total;
  const change = async value => { previous.disabled = next.disabled = true; try { await callback(value); } catch (error) { statusLine.textContent = error.message; } };
  previous.addEventListener('click', () => change(Math.max(0, offset - limit))); next.addEventListener('click', () => change(offset + limit));
  text.textContent = `${total ? offset + 1 : 0}–${Math.min(offset + limit, total)} / ${total} 条`; root.append(previous, text, next); return root;
}
async function loadHistory() {
  const projectId = selected, offset = historyOffset, request = ++historyRequest;
  const current = () => request === historyRequest && selected === projectId && historyOffset === offset;
  built.history.textContent = '正在读取人工历史…';
  let value;
  try {
    const response = await fetch(`/api/workspace/progress/history?project_id=${encodeURIComponent(projectId)}&offset=${offset}&limit=20`, { headers: headers() });
    if (!current()) return;
    if (!response.ok) throw new Error('人工历史读取失败');
    value = await response.json();
  } catch (error) { if (current()) throw error; return; }
  if (!current()) return;
  const title = document.createElement('h3'); title.textContent = '人工历史'; built.history.replaceChildren(title);
  for (const row of value.items) { const node = document.createElement('p'); node.textContent = `${row.recorded_at || '来源未提供时间'} · ${row.origin === 'import' ? '导入快照' : '人工'} · ${row.state} · ${Number.isFinite(row.percent) ? `${row.percent}%` : '未填写百分比'} · ${row.note}`; built.history.append(node); }
  built.history.append(pageControls(value.total, offset, 20, async nextOffset => { historyOffset = nextOffset; await loadHistory(); }));
}

load().catch(() => { statusLine.textContent = "工作台没有读出来。"; });
