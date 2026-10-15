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
  note.placeholder = "用一句话写下现在在做的事";
  const slider = document.createElement("input");
  slider.className = "slider";
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  slider.addEventListener("input", () => {
    const percent = Number(slider.value);
    slider.style.setProperty("--fill", percent + "%");
    setRing(percent);
  });
  slider.addEventListener("change", () => {
    const project = currentProject();
    const percent = Number(slider.value);
    const state = percent === 0 ? "未开始" : percent === 100 ? "完成" : project.state === "等我" ? "等我" : "在做";
    save(project.id, state, note.value, percent);
  });
  note.addEventListener("change", () => {
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
      const percent = name === "完成" ? 100 : name === "未开始" ? 0 : Number(slider.value);
      save(project.id, name, note.value, percent);
    });
    choices.append(choice);
  }
  const records = document.createElement("section");
  records.className = "records";
  stage.append(hero, note, slider, choices, records);
  built = {kicker, heading, value, label, note, slider, choices, records};
}

function setRing(percent) {
  built.value.style.strokeDashoffset = String(ringLength * (1 - percent / 100));
  built.label.textContent = String(percent);
}

function fillRecords(project) {
  built.records.replaceChildren();
  const title = document.createElement("h3");
  title.textContent = "未收口的开发";
  built.records.append(title);
  if (!project.records.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这个项目还没有未收口的开发记录。";
    built.records.append(empty);
    return;
  }
  project.records.forEach((record, index) => {
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
  if (!project) return;
  selected = project.id;
  const apply = () => {
    const accent = accentOf(project);
    stage.style.setProperty("--accent", accent);
    built.kicker.textContent = project.state;
    built.heading.textContent = project.name;
    if (document.activeElement !== built.note) built.note.value = project.note || "";
    built.note.setAttribute("aria-label", project.name);
    built.slider.value = String(project.percent);
    built.slider.style.setProperty("--fill", project.percent + "%");
    built.slider.setAttribute("aria-label", project.name + "的进度");
    setRing(project.percent);
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
  const waiting = project.records.some((record) => record.text.includes("USER_CONFIRMATION") || record.text.includes("BLOCKED"));
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
    state.textContent = project.state + " · " + project.percent + "%";
    row.append(name, state);
    const feature = document.createElement("b");
    feature.textContent = project.records[0]?.feature || "没有未收口记录";
    const text = document.createElement("span");
    text.textContent = project.records[0] ? project.records[0].text : "进度由你在下面设定";
    button.append(row, feature, text);
    if (project.sessions.length) {
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
    meta.textContent = project.state + " · " + (project.records[0]?.feature || "无记录");
    words.append(name, meta);
    const meter = document.createElement("b");
    meter.className = "meter";
    const fill = document.createElement("i");
    fill.style.setProperty("--fill", project.percent + "%");
    meter.append(fill);
    button.append(swatch, words, meter);
    button.addEventListener("click", () => {
      if (selected === project.id) return;
      selected = project.id;
      paintRail();
      paintBoard();
      showProject(true);
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
  input.placeholder = "写一条待办";
  input.setAttribute("aria-label", "待办");
  const add = document.createElement("button");
  add.type = "submit";
  add.textContent = "添加";
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
    row.addEventListener("click", () => toggleTodo(item.id));
    list.append(row);
  }
  if (!(desk.todos || []).length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有待办。";
    list.append(empty);
  }
  todoZone.append(form, list);
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
  try {
    const response = await fetch("/api/progress/set", {
      method: "POST",
      headers: {...headers(), "Content-Type": "application/json", "Origin": location.origin},
      body: JSON.stringify({project_id: projectId, state, note, percent}),
    });
    if (!response.ok) throw new Error("save");
    projects = (await response.json()).projects || [];
    selected = projectId;
    statusLine.textContent = "";
    paintRail();
    paintBoard();
    showProject(false);
  } catch {
    statusLine.textContent = "没有记下。";
  }
}

async function refreshDesk() {
  const desk = await fetch("/api/desk", {headers: headers()});
  if (desk.ok) paintSide(await desk.json());
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
  csrf = (await bootstrap.json()).csrf_token;
  const progress = await fetch("/api/progress", {headers: headers()});
  if (!progress.ok) throw new Error("progress");
  projects = (await progress.json()).projects || [];
  selected = (projects.find((item) => item.state === "在做") || projects[0] || {}).id || "";
  ensureStage();
  paintRail();
  paintBoard();
  showProject(false);
  const desk = await fetch("/api/desk", {headers: headers()});
  if (desk.ok) paintSide(await desk.json());
}

load().catch(() => { statusLine.textContent = "工作台没有读出来。"; });
