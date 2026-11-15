/* =====================================================================
   TaleAI 管理面板（面板设计 06 · P0）
   ---------------------------------------------------------------------
   页面：人格（在线编辑器）· 模型 · 平台 · 设置。
   纪律（与 index / logs / settings 三页同）：
     · 不引 CDN、不引框架；编辑器内核 Monaco 放在 /static/vendor/monaco/，按需加载，
       取不到就退回普通文本框，保存、对比照常；
     · 不把字符串当 HTML 塞进 DOM：一律 createElement + textContent（h() 助手）；
     · 字段由 /api/settings/fields 驱动——fields.py 加一项，面板自动出现。
   ===================================================================== */
"use strict";

/* ---------- DOM 助手 ---------- */
const $ = (s, r = document) => r.querySelector(s);
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (typeof v === "boolean") el[k] = v;
      else el.setAttribute(k, v);
    }
  }
  for (const c of kids.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}
/* 替换子节点；null / undefined / false 跳过（原生 replaceChildren 会把 null 写成 "null"） */
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}
/* 搜索命中的字高亮（mark 元素，不拼 HTML） */
function hl(text, q) {
  const s = String(text ?? "");
  const i = q ? s.toLowerCase().indexOf(q.toLowerCase()) : -1;
  if (i < 0) return [s];
  return [s.slice(0, i), h("mark", { text: s.slice(i, i + q.length) }), s.slice(i + q.length)];
}

/* ---------- 请求 ---------- */
async function getJSON(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  return r.json();
}
async function postJSON(url, body) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  let json = {};
  try { json = await r.json(); } catch (_) { json = { ok: false, error: `HTTP ${r.status}` }; }
  return { status: r.status, json };
}

/* ---------- 提示（可撤销） ---------- */
let toastTimer;
function toast(msg, { undo, bad } = {}) {
  const t = $("#toast");
  fill(t, h("span", { text: msg }), undo ? h("button", { type: "button", text: "撤销", onclick: () => { t.classList.remove("on"); undo(); } }) : null);
  t.classList.toggle("bad", !!bad);
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), bad ? 6000 : 4000);
}

/* ---------- 抽屉 ---------- */
function openDrawer(title, lead, ...body) {
  const d = $("#drawer");
  fill(d, 
    h("div", { class: "dh" }, h("h2", { text: title }), h("span", { class: "spacer" }), h("button", { class: "link", type: "button", text: "关闭", onclick: closeDrawer })),
    lead ? h("p", { class: "lead", text: lead }) : null,
    ...body,
  );
  d.classList.add("on"); d.setAttribute("aria-hidden", "false"); $("#scrim").classList.add("on");
}
function closeDrawer() {
  $("#drawer").classList.remove("on"); $("#drawer").setAttribute("aria-hidden", "true"); $("#scrim").classList.remove("on");
}

/* ---------- 逐行对比（抽屉里用；编辑器有 Monaco 时用它自己的对比） ---------- */
function lineDiff(a, b) {
  const A = a.split("\n"), B = b.split("\n");
  const L = Array.from({ length: A.length + 1 }, () => new Array(B.length + 1).fill(0));
  for (let i = A.length - 1; i >= 0; i--) for (let j = B.length - 1; j >= 0; j--)
    L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = []; let i = 0, j = 0;
  while (i < A.length && j < B.length) {
    if (A[i] === B[j]) { out.push(["ctx", A[i]]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) out.push(["del", A[i++]]);
    else out.push(["add", B[j++]]);
  }
  while (i < A.length) out.push(["del", A[i++]]);
  while (j < B.length) out.push(["add", B[j++]]);
  return h("div", { class: "diff" }, out.map(([t, l]) => h("div", { class: t, text: l || " " })));
}

/* ---------- 主题 ---------- */
const THEMES = [["light", "浅色"], ["dark", "深色"], ["auto", "跟随系统"]];
let themePref = (() => { try { return localStorage.getItem("taleai.theme") || "auto"; } catch (_) { return "auto"; } })();
function applyTheme(pref) {
  themePref = pref;
  const dark = pref === "dark" || (pref === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  try { localStorage.setItem("taleai.theme", pref); } catch (_) {}
  if (ED.monaco) ED.monaco.editor.setTheme(dark ? "tale-dark" : "tale");
}
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => themePref === "auto" && applyTheme("auto"));

/* ---------- 侧栏抽屉 ---------- */
const narrow = () => innerWidth <= 1100;
const navOpen = () => !document.body.classList.contains("nav-closed");
function setNav(open, remember = true) {
  document.body.classList.toggle("nav-closed", !open);
  $("#navBtn").setAttribute("aria-expanded", String(open));
  $("#navBtnText").textContent = open && !narrow() ? "收起导航" : "导航";
  if (remember && !narrow()) try { localStorage.setItem("taleai.nav", open ? "open" : "closed"); } catch (_) {}
}
$("#navBtn").addEventListener("click", () => setNav(!navOpen()));
$("#navClose").addEventListener("click", () => setNav(false));
$("#navScrim").addEventListener("click", () => setNav(false, false));
$("#scrim").addEventListener("click", closeDrawer);
document.querySelectorAll("#nav a").forEach((a) => a.addEventListener("click", () => narrow() && setNav(false, false)));
addEventListener("resize", () => setNav(navOpen(), false));
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "b" && !(e.target.closest && e.target.closest(".monaco-editor"))) { e.preventDefault(); setNav(!navOpen()); }
  if (e.key === "Escape") { closeDrawer(); if (narrow() && navOpen()) setNav(false, false); }
});

/* =====================================================================
   字段表单：模型 / 平台 / 设置三页共用一套（字段来自 fields.py）
   ===================================================================== */
const DOMAINS = ["config", "secrets", "platforms"];
const FORM = { specs: {}, saved: {}, val: {}, loaded: false, query: "" };
const fkey = (d, k) => d + "|" + k;
const isList = (s) => Array.isArray(s.default);
const norm = (s, v) => (isList(s) ? (Array.isArray(v) ? v.join(", ") : String(v ?? "")) : v);
const shown = (s, v) => {
  if (s.sensitive) return v ? "（新密钥）" : "（未改）";
  if (s.type === "bool") return v ? "开" : "关";
  const t = norm(s, v);
  return t === "" || t === null || t === undefined ? "（空）" : String(t);
};
const isDirty = (d, s) => (s.sensitive ? !!FORM.val[fkey(d, s.key)] : String(norm(s, FORM.val[fkey(d, s.key)])) !== String(norm(s, FORM.saved[fkey(d, s.key)])));

async function loadForm(domains = DOMAINS) {
  await Promise.all(domains.map(async (d) => {
    const [f, v] = await Promise.all([getJSON(`/api/settings/fields?domain=${d}`), getJSON(`/api/settings/values?domain=${d}`)]);
    if (!f.ok || !v.ok) throw new Error(f.error || v.error || `读不到 ${d}`);
    FORM.specs[d] = f.fields;
    for (const s of f.fields) {
      const k = fkey(d, s.key), raw = v.values[s.key];
      FORM.saved[k] = s.sensitive ? raw : norm(s, raw);
      FORM.val[k] = s.sensitive ? "" : FORM.saved[k];
    }
  }));
  FORM.loaded = true;
  const name = FORM.saved[fkey("config", "bot.name")];
  if (name) { $("#botName").textContent = name; document.title = `${name} · TaleAI 管理面板`; }
}

/* 每页由哪些字段组成。没被认领的 config 字段落到“设置 → 其他”，新加的配置项不会无处可去 */
const claim = (d, s) => {
  if (d === "platforms") return "platforms";
  if (d === "secrets") return s.key.startsWith("llm.") ? "model" : "settings";
  if (s.key.startsWith("llm.")) return "model";
  return "settings";
};
const PAGE_SECTIONS = {
  model: [
    { title: "网关与模型", pick: (d, s) => (d === "config" && ["llm.provider", "llm.base_url", "llm.model"].includes(s.key)) || d === "secrets" },
    { title: "上下文", note: "每次请求带多少历史、动态部分多大、工具最多调几轮。", pick: (d, s) => d === "config" },
  ],
  platforms: [
    { title: "QQ 连接", note: "地址、端口、令牌在下一次打开 QQ 时生效（关 → 开）。", pick: (d, s) => s.key.startsWith("qq.") && s.key !== "qq.enabled" },
    { title: "网页", note: "端口要重启 TaleAI 才生效。", pick: (d, s) => s.key.startsWith("web.") },
    { title: "其他", pick: () => true },
  ],
  settings: [
    { title: "叫她", pick: (d, s) => s.key.startsWith("bot.") || s.key.startsWith("wake.") },
    { title: "回复节奏", note: "改完下一条消息生效。", pick: (d, s) => s.key.startsWith("reply.") },
    { title: "记忆", note: "衰减与遗忘参数。改完下一轮提炼 / 整合时按新的算。", pick: (d, s) => s.key.startsWith("memory.") },
    { title: "作息", note: "日程到点主动发言。改完下一次检查时生效。", pick: (d, s) => s.key.startsWith("plan.") },
    { title: "其他", pick: () => true },
  ],
};
function sectionsFor(page) {
  const taken = new Set();
  return PAGE_SECTIONS[page].map((sec) => {
    const items = [];
    for (const d of DOMAINS) for (const s of FORM.specs[d] || []) {
      const k = fkey(d, s.key);
      if (taken.has(k) || claim(d, s) !== page || !sec.pick(d, s)) continue;
      taken.add(k); items.push({ d, s });
    }
    return { ...sec, items };
  }).filter((sec) => sec.items.length);
}
const matches = (d, s, q) => !q || [s.label, s.key, s.help || "", shown(s, FORM.val[fkey(d, s.key)])].join(" ").toLowerCase().includes(q.toLowerCase());

function control(d, s, onChange) {
  const k = fkey(d, s.key);
  if (s.sensitive) {
    const box = h("div");
    const paint = (editing) => {
      if (!editing && !FORM.val[k]) {
        const set = FORM.saved[k] && FORM.saved[k].set;
        fill(box, h("span", { text: set ? "已设置" : "未设置" }),
          h("button", { class: "link", type: "button", text: set ? "换一个" : "填一个", style: "margin-left:10px", onclick: () => paint(true) }));
        return;
      }
      const inp = h("input", { type: "password", autocomplete: "new-password", placeholder: "粘贴新的密钥", value: FORM.val[k] || "",
        oninput: () => { FORM.val[k] = inp.value; onChange(); } });
      const eye = h("button", { class: "link", type: "button", text: "显示", style: "margin-left:10px",
        onclick: () => { inp.type = inp.type === "password" ? "text" : "password"; eye.textContent = inp.type === "password" ? "显示" : "隐藏"; } });
      const cancel = h("button", { class: "link", type: "button", text: "不换了", style: "margin-left:10px", onclick: () => { FORM.val[k] = ""; onChange(); paint(false); } });
      fill(box, inp, eye, cancel);
      if (editing) setTimeout(() => inp.focus(), 0);
    };
    paint(false);
    return box;
  }
  if (s.type === "bool" || (s.type === "select" && s.choices.length && s.choices.length <= 4)) {
    const opts = s.type === "bool" ? [[true, "开"], [false, "关"]] : s.choices.map((c) => [c, c]);
    const seg = h("span", { class: "seg" });
    const paint = () => fill(seg, ...opts.map(([v, l]) => h("button", { type: "button", text: l, class: FORM.val[k] === v ? "on" : "",
      onclick: () => { FORM.val[k] = v; paint(); onChange(); } })));
    paint();
    return seg;
  }
  if (s.type === "select") {
    const sel = h("select", { onchange: () => { FORM.val[k] = sel.value; onChange(); } }, s.choices.map((c) => h("option", { value: c, text: c, selected: FORM.val[k] === c })));
    return sel;
  }
  if (s.type === "textarea") {
    const ta = h("textarea", { oninput: () => { FORM.val[k] = ta.value; onChange(); } });
    ta.value = FORM.val[k] ?? "";
    return ta;
  }
  const inp = h("input", { type: s.type === "number" ? "number" : "text", value: FORM.val[k] ?? "",
    oninput: () => { FORM.val[k] = s.type === "number" ? (inp.value === "" ? "" : Number(inp.value)) : inp.value; onChange(); } });
  return inp;
}

function fieldRow(d, s) {
  const k = fkey(d, s.key);
  const st = h("div", { class: "st" });
  const row = h("div", { class: "f", "data-key": k });
  const paintState = () => {
    const dirty = isDirty(d, s);
    row.classList.toggle("is-dirty", dirty);
    const def = norm(s, s.default);
    if (dirty) fill(st, h("span", { class: "uns", text: "未保存" }), " · ",
      h("button", { type: "button", text: "撤回", onclick: () => { FORM.val[k] = s.sensitive ? "" : FORM.saved[k]; rerender(); } }));
    else if (!s.sensitive && def !== "" && def !== null && String(FORM.saved[k]) !== String(def)) fill(st, `已改，默认 ${shown(s, def)}`, h("br"),
      h("button", { type: "button", text: "恢复默认", onclick: () => { FORM.val[k] = def; rerender(); } }));
    else fill(st, );
    paintDirtyBar();
  };
  row.append(
    h("div", null, h("label", null, hl(s.label, FORM.query)), h("span", { class: "fk" }, hl(s.key, FORM.query))),
    h("div", null, control(d, s, paintState), s.help ? h("div", { class: "hint" }, hl(s.help, FORM.query)) : null),
    st,
  );
  row.classList.toggle("hide", !matches(d, s, FORM.query));
  paintState();
  return row;
}

let rerender = () => {};
async function renderFields(page) {
  const intro = {
    model: ["模型", "她用哪个网关、哪个模型说话，带多少上下文。改完下一句生效。"],
    platforms: ["平台", "她在哪些地方能被找到。"],
    settings: ["设置", "怎么叫她、记忆怎么衰减，以及面板外观。"],
  }[page];
  const root = $("#page");
  fill(root, h("div", { class: "page-head" }, h("h1", { text: intro[0] }), h("p", { text: intro[1] })), h("p", { class: "muted", text: "读取中…" }));
  try { if (!FORM.loaded) await loadForm(); }
  catch (e) { root.lastChild.textContent = "读取配置失败：" + e.message; root.lastChild.className = "warn"; return; }
  rerender = () => renderFields(page);
  const form = h("div", { class: "form" });
  const cnt = h("span", { class: "cnt" });
  const search = h("input", { type: "search", placeholder: "在这一页里找，比如 端口、半衰期、唤醒", value: FORM.query,
    oninput: () => { FORM.query = search.value.trim(); paintSearch(); } });
  const groups = [];
  if (page === "platforms") form.append(await qqCard());
  for (const sec of sectionsFor(page)) {
    const rows = sec.items.map(({ d, s }) => ({ d, s, el: fieldRow(d, s) }));
    const g = h("section", { class: "group" }, h("h2", { text: sec.title }), sec.note ? h("p", { text: sec.note }) : null, rows.map((r) => r.el));
    groups.push({ g, rows }); form.append(g);
  }
  if (page === "model") form.append(testGroup());
  if (page === "settings") form.append(appearanceGroup(), aboutGroup());
  const paintSearch = () => {
    let n = 0;
    for (const { g, rows } of groups) {
      let any = false;
      for (const r of rows) {
        const hit = matches(r.d, r.s, FORM.query);
        r.el.classList.toggle("hide", !hit);
        const label = r.el.querySelector("label"), key = r.el.querySelector(".fk");
        fill(label, ...hl(r.s.label, FORM.query)); fill(key, ...hl(r.s.key, FORM.query));
        if (hit) { any = true; n++; }
      }
      g.classList.toggle("hide", !any);
    }
    cnt.textContent = FORM.query ? (n ? `找到 ${n} 项` : "这一页没有") : "";
  };
  fill(root, h("div", { class: "page-head" }, h("h1", { text: intro[0] }), h("p", { text: intro[1] })),
    h("div", { class: "search" }, search, cnt), form);
  paintSearch();
  paintDirtyBar();
}

function appearanceGroup() {
  const seg = h("span", { class: "seg" });
  const paint = () => fill(seg, ...THEMES.map(([v, l]) => h("button", { type: "button", text: l, class: themePref === v ? "on" : "",
    onclick: () => { applyTheme(v); paint(); } })));
  paint();
  return h("section", { class: "group" }, h("h2", { text: "外观" }),
    h("div", { class: "f" }, h("div", null, h("label", { text: "主题" })), h("div", null, seg, h("div", { class: "hint", text: "只存在这个浏览器里；跟随系统时，电脑切换深浅色面板也跟着变" })), h("div")));
}
function testGroup() {
  const out = h("span", { class: "state", style: "margin-left:12px" });
  const btn = h("button", { class: "btn", type: "button", text: "测试已保存的配置", onclick: async () => {
    if (dirtyList().some(({ s }) => s.key.startsWith("llm."))) { out.className = "state bad"; out.textContent = "有改动还没保存，先保存再测"; return; }
    btn.disabled = true; out.className = "state"; out.textContent = "测试中…";
    const { json } = await postJSON("/api/settings/test-llm", {}).catch(() => ({ json: { ok: false, error: "请求没发出去" } }));
    btn.disabled = false;
    out.className = "state" + (json.ok ? "" : " bad");
    out.textContent = json.ok ? `通了，${json.ms} 毫秒，${json.model}` : json.error || "没通";
  } });
  return h("section", { class: "group" }, h("h2", { text: "连通性" }),
    h("div", { class: "f" }, h("div", null, h("label", { text: "测试连接" })),
      h("div", null, btn, out, h("div", { class: "hint", text: "只测保存过的地址和密钥（请求 网关/models）；改了要先保存" })), h("div")));
}
function aboutGroup() {
  const ver = h("span", { class: "mono", text: "…" });
  getJSON("/api/about").then((j) => { ver.textContent = j.ok ? `TaleAI ${j.version}` : "读不到"; }).catch(() => { ver.textContent = "读不到"; });
  return h("section", { class: "group" }, h("h2", { text: "关于" }),
    h("div", { class: "f" }, h("div", null, h("label", { text: "版本" })), h("div", null, ver), h("div")),
    h("div", { class: "f" }, h("div", null, h("label", { text: "旧版设置页" })),
      h("div", null, h("a", { href: "/static/settings.html", text: "/static/settings.html" }), h("div", { class: "hint", text: "面板接管了它的全部功能；旧页先保留一个版本，之后删除" })), h("div")));
}

/* 未保存浮条：所有页面的改动一起算 */
function dirtyList() {
  const out = [];
  for (const d of DOMAINS) for (const s of FORM.specs[d] || []) if (isDirty(d, s)) out.push({ d, s });
  return out;
}
function paintDirtyBar() {
  const bar = $("#dirty");
  if (currentPage === "persona") return;
  const list = dirtyList();
  if (!list.length) { bar.classList.remove("on"); return; }
  fill(bar, h("span", { text: `改了 ${list.length} 项` }),
    h("button", { type: "button", text: "看改动", onclick: () => showFieldChanges(list) }),
    h("button", { type: "button", text: "全部撤回", onclick: () => { for (const { d, s } of list) FORM.val[fkey(d, s.key)] = s.sensitive ? "" : FORM.saved[fkey(d, s.key)]; rerender(); } }),
    h("button", { type: "button", class: "save", text: "保存", onclick: saveFields }));
  bar.classList.add("on");
}
function showFieldChanges(list) {
  openDrawer("这次改了什么", "保存后写进 data/config/，大多数改动下一句对话就生效。",
    h("table", { class: "chg" }, list.map(({ d, s }) => h("tr", null,
      h("td", { class: "k" }, s.label, h("br"), h("span", { class: "mono muted", text: s.key })),
      h("td", null, h("del", { text: shown(s, s.sensitive ? "" : FORM.saved[fkey(d, s.key)]) }), " → ", h("ins", { text: shown(s, FORM.val[fkey(d, s.key)]) }))))),
    h("div", { class: "btns" }, h("button", { class: "btn primary", type: "button", text: "保存", onclick: () => { closeDrawer(); saveFields(); } })));
}
async function saveFields() {
  const list = dirtyList();
  const byDomain = {};
  for (const { d, s } of list) (byDomain[d] = byDomain[d] || {})[s.key] = FORM.val[fkey(d, s.key)];
  const notes = [], errors = [];
  for (const [domain, values] of Object.entries(byDomain)) {
    const { json } = await postJSON("/api/settings/values", { domain, values });
    if (json.ok) notes.push(json.note); else errors.push(json.error || "保存失败");
  }
  try { await loadForm(Object.keys(byDomain)); } catch (_) {}
  rerender(); paintNotice();
  if (errors.length) toast("有改动没保存成功：" + errors.join("；"), { bad: true });
  else toast(`已保存 ${list.length} 项。${[...new Set(notes)].join("；")}`);
}

/* QQ 卡：开关走 /api/platforms/qq（会真的启停服务），状态写成一句话 */
let qqStatus = null;
const qqSentence = (q) => {
  if (!q) return ["读不到 QQ 状态", true];
  if (!q.enabled) return ["关着。群里和私聊的 QQ 消息她都收不到", false];
  if (q.state === "failed") return [`开着，但没启动起来：${q.detail || "原因不明，看看日志"}`, true];
  if (q.state === "starting") return ["正在启动…", false];
  if (q.state === "running" && q.connected) return ["开着，QQ 客户端已经连上", false];
  if (q.state === "running") return ["开着，在等 QQ 客户端连上来", false];
  return [`状态：${q.state}`, false];
};
async function qqCard() {
  try { qqStatus = await getJSON("/api/platforms/qq"); } catch (_) { qqStatus = null; }
  const [text, bad] = qqSentence(qqStatus);
  const seg = h("span", { class: "seg" }, [[true, "开"], [false, "关"]].map(([v, l]) => h("button", { type: "button", text: l,
    class: qqStatus && qqStatus.enabled === v ? "on" : "",
    onclick: async () => {
      if (!qqStatus || qqStatus.enabled === v) return;
      const { json } = await postJSON("/api/platforms/qq", { enabled: v });
      if (!json.ok) toast("QQ 开关没切过去：" + (json.error || ""), { bad: true });
      else toast(v ? "QQ 打开了" : "QQ 关了，群里的消息暂时收不到");
      paintNotice(); rerender();
    } })));
  return h("section", { class: "card" }, h("h2", { text: "QQ" }), seg, h("div", { class: "state" + (bad ? " bad" : ""), text: text }),
    h("div", { class: "hint", text: "QQ 客户端（SnowLuma / NapCat）里把反向 WebSocket 填成 ws://<监听地址>:<端口>/qq；访问令牌两边要一致。" }));
}
async function paintNotice() {
  const n = $("#notice");
  // 第一优先：模型没配好，她说不了话（首次运行时一打开面板就看得到）
  if (!FORM.loaded) { try { await loadForm(); } catch (_) {} }
  const v = (d, k) => FORM.saved[fkey(d, k)];
  const missing = [];
  if (FORM.loaded) {
    if (!v("config", "llm.base_url")) missing.push("网关地址");
    if (!v("config", "llm.model")) missing.push("模型名");
    const key = v("secrets", "llm.api_key");
    if (key && key.set === false) missing.push("密钥");
  }
  if (missing.length) {
    fill(n, h("b", { text: "还没配好模型，她现在说不了话。" }), `缺：${missing.join("、")}。`,
      h("button", { type: "button", text: "去填", onclick: () => { location.hash = "#/model"; } }));
    n.hidden = false; return;
  }
  let q = null;
  try { q = await getJSON("/api/platforms/qq"); } catch (_) {}
  if (!q || !q.enabled || q.state !== "failed") { n.hidden = true; return; }
  fill(n, h("b", { text: "QQ 没启动起来。" }), q.detail || "", h("button", { type: "button", text: "去看看", onclick: () => { location.hash = "#/platforms"; } }));
  n.hidden = false;
}

/* =====================================================================
   人格：在线编辑器（Monaco = VS Code 的编辑器内核）
   ===================================================================== */
const MONACO = "/static/vendor/monaco/vs";
const FILES = [
  { k: "persona", name: "persona.md", path: "data/config/persona.md", dir: "人格", why: "她是谁、怎么想、怎么说话。任何会话都带上。" },
  { k: "global", name: "global.md", path: "data/config/rules/global.md", dir: "聊天规则", why: "任何会话都生效。留空不注入。" },
  { k: "private", name: "private.md", path: "data/config/rules/private.md", dir: "聊天规则", why: "只在私聊和网页聊天里生效。留空不注入。" },
  { k: "group", name: "group.md", path: "data/config/rules/group.md", dir: "聊天规则", why: "只在群聊里生效。留空不注入。" },
  { k: "base", name: "base.md", path: "src/core/llm/prompts/base.md", dir: "框架（只读）", ro: true, why: "输出格式和回应原则。随代码维护，不在面板里改。" },
  { k: "chat", name: "chat.md", path: "src/core/llm/prompts/chat.md", dir: "框架（只读）", ro: true, why: "对话场景约定。随代码维护，不在面板里改。" },
  { k: "view_private", name: "私聊时她读到的", path: "拼接结果 · 私聊", dir: "拼接预览", ro: true, view: "private", why: "框架 → 人设 → 全局规则 → 私聊规则，按注入顺序拼好。显示的是已保存的版本。" },
  { k: "view_group", name: "群聊时她读到的", path: "拼接结果 · 群聊", dir: "拼接预览", ro: true, view: "group", why: "框架 → 人设 → 全局规则 → 群聊规则，按注入顺序拼好。显示的是已保存的版本。" },
];
const EDITABLE = ["persona", "global", "private", "group"];
const fileOf = (k) => FILES.find((f) => f.k === k);
const ED = { files: null, frame: {}, builtin: "", preview: {}, open: ["persona"], active: "persona", diff: false, font: "mono", wrap: true,
  monaco: null, failed: false, editor: null, diffEd: null, models: {}, draft: {}, cur: { l: 1, c: 1 } };

const savedOf = (k) => (ED.files && ED.files[k] ? ED.files[k].content : "");
function liveOf(k) {
  const f = fileOf(k);
  if (f.view) return ED.preview[f.view] || "";
  if (f.ro) return ED.frame[k] || "";
  if (ED.models[k]) return ED.models[k].getValue();
  return k in ED.draft ? ED.draft[k] : savedOf(k);
}
const roNow = (k) => !!fileOf(k).ro || !!(ED.files && ED.files[k] && ED.files[k].error);
const fileDirty = (k) => EDITABLE.includes(k) && !roNow(k) && liveOf(k) !== savedOf(k);
function setLive(k, v) { if (ED.models[k]) ED.models[k].setValue(v); else ED.draft[k] = v; }

let monacoPromise = null;
function loadMonaco() {
  if (monacoPromise) return monacoPromise;
  monacoPromise = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("timeout")), 15000);
    const add = (src, onload) => { const s = document.createElement("script"); s.src = src; s.onload = onload; s.onerror = () => { clearTimeout(timer); reject(new Error("load " + src)); }; document.head.append(s); };
    add(MONACO + "/loader.js", () => {
      window.require.config({ paths: { vs: MONACO } });
      const fail = (e) => { clearTimeout(timer); reject(e); };
      // 中文界面文案是一个 AMD 模块，要先于 editor.main 载入（载不到就用英文，不影响编辑）
      const main = () => window.require(["vs/editor/editor.main"], () => { clearTimeout(timer); resolve(window.monaco); }, fail);
      window.require(["vs/nls.messages.zh-cn"], main, main);
    });
  });
  return monacoPromise;
}
function setupMonaco(m) {
  if (ED.monaco) return;
  ED.monaco = m;
  const base = { "editor.lineHighlightBorder": "#00000000", focusBorder: "#00000000" };
  m.editor.defineTheme("tale", { base: "vs", inherit: true,
    rules: [{ token: "keyword.md", foreground: "3f51a8", fontStyle: "bold" }, { token: "comment", foreground: "948e82", fontStyle: "italic" }],
    colors: { ...base, "editor.background": "#fbfaf7", "editorGutter.background": "#fbfaf7", "minimap.background": "#f5f3ee", "editor.foreground": "#24221e",
      "editorLineNumber.foreground": "#bdb6a8", "editor.lineHighlightBackground": "#f1eee7", "editor.selectionBackground": "#dfe3f3", "editorCursor.foreground": "#24221e",
      "diffEditor.insertedTextBackground": "#b9d8ae66", "diffEditor.removedTextBackground": "#eab8ad66" } });
  m.editor.defineTheme("tale-dark", { base: "vs-dark", inherit: true,
    rules: [{ token: "keyword.md", foreground: "a3b0f0", fontStyle: "bold" }, { token: "comment", foreground: "847e73", fontStyle: "italic" }],
    colors: { ...base, "editor.background": "#191816", "editorGutter.background": "#191816", "minimap.background": "#1c1b19", "editor.foreground": "#e8e4da",
      "editorLineNumber.foreground": "#5a554d", "editor.lineHighlightBackground": "#22211e", "editor.selectionBackground": "#2b3050", "editorCursor.foreground": "#e8e4da",
      "diffEditor.insertedTextBackground": "#3f6b3555", "diffEditor.removedTextBackground": "#7a3a2c55" } });
  for (const k of [...EDITABLE, "base", "chat"]) {
    ED.models[k] = m.editor.createModel(liveOf(k), "markdown", m.Uri.parse(`inmemory://tale/${k}.md`));
    delete ED.draft[k];
    if (EDITABLE.includes(k)) ED.models[k].onDidChangeContent(paintChrome);
  }
}
const edFont = () => (ED.font === "serif" ? '"Noto Serif SC", "Songti SC", serif' : '"Cascadia Mono", Consolas, "Noto Sans SC", monospace');
const edTheme = () => (document.documentElement.dataset.theme === "dark" ? "tale-dark" : "tale");
const edOptions = (ro) => ({ theme: edTheme(), fontFamily: edFont(), fontSize: 14, lineHeight: 26, wordWrap: ED.wrap ? "on" : "off", readOnly: ro,
  minimap: { enabled: true, renderCharacters: false }, automaticLayout: true, scrollBeyondLastLine: false, padding: { top: 12, bottom: 12 },
  unicodeHighlight: { ambiguousCharacters: false, invisibleCharacters: false }, occurrencesHighlight: "off", guides: { indentation: false } });

async function loadPersona() {
  const [files, frame] = await Promise.all([getJSON("/api/persona"), getJSON("/api/persona/frame")]);
  if (!files.ok) throw new Error(files.error || "读不到人格文件");
  ED.files = files.files;
  if (frame.ok) { ED.frame = { base: frame.files.base.content, chat: frame.files.chat.content }; ED.builtin = frame.builtin_persona.content; }
}

function renderPersona() {
  const tree = h("aside", { class: "ide-tree", id: "ideTree" });
  const tabs = h("div", { class: "ide-tabs", id: "ideTabs" });
  const crumb = h("div", { class: "ide-crumb", id: "ideCrumb" });
  const box = h("div", { class: "ide-ed", id: "ideEd" }, h("div", { class: "loading", text: "正在载入…" }));
  const status = h("div", { class: "ide-status", id: "ideStatus" });
  fill($("#page"), 
    h("div", { class: "page-head" }, h("h1", { text: "人格" }), h("p", { text: "直接编辑 data/config/ 下的人格文件。保存后，她说下一句话就按新的来。" }), h("span", { class: "spacer" }),
      h("button", { class: "link", type: "button", id: "ideDiff", onclick: () => { ED.diff = !ED.diff; mountEditor(); } }),
      h("button", { class: "link", type: "button", id: "ideRevert", text: "撤回这个文件", onclick: revertActive }),
      h("button", { class: "link", type: "button", id: "ideBuiltin", text: "恢复内置人设", onclick: restoreBuiltin }),
      h("button", { class: "btn", type: "button", id: "ideSaveAll", text: "全部保存", onclick: () => saveFiles(EDITABLE.filter(fileDirty)) }),
      h("button", { class: "btn primary", type: "button", id: "ideSave", onclick: () => saveFiles([ED.active]) }, "保存 ", h("span", { class: "mono", text: "Ctrl S", style: "opacity:.7;font-size:11px" }))),
    h("div", { class: "ide" }, tree, h("section", { class: "ide-main" }, tabs, crumb, box, status)));
  const go = () => {
    paintChrome();
    if (ED.monaco || ED.failed) { mountEditor(); return; }
    loadMonaco().then((m) => { setupMonaco(m); if (currentPage === "persona") mountEditor(); })
      .catch(() => { ED.failed = true; if (currentPage === "persona") mountEditor(); toast("编辑器内核没载入，先用普通文本框；保存和对比照常能用", { bad: true }); });
  };
  if (ED.files) go();
  else loadPersona().then(go).catch((e) => { fill(box, h("div", { class: "loading warn", text: "读取人格文件失败：" + e.message })); });
}

function paintChrome() {
  const tree = $("#ideTree"); if (!tree || !ED.files) return;
  const rows = [h("div", { class: "th", text: "资源管理器" })];
  let dir = "";
  for (const f of FILES) {
    if (f.dir !== dir) { dir = f.dir; rows.push(h("div", { class: "td", text: dir })); }
    let tag = null;
    const meta = ED.files[f.k];
    if (fileDirty(f.k)) tag = h("span", { class: "tag is-dirty", text: "已改" });
    else if (meta && meta.error) tag = h("span", { class: "tag warn", text: "编码不对" });
    else if (f.view) tag = h("span", { class: "tag", text: "预览" });
    else if (f.ro) tag = h("span", { class: "tag", text: "只读" });
    else if (f.k === "persona" && meta && meta.source === "builtin") tag = h("span", { class: "tag", text: "用内置" });
    else if (meta && meta.injected === false) tag = h("span", { class: "tag", text: "空" });
    rows.push(h("button", { type: "button", class: (f.k === ED.active ? "on " : "") + (f.view ? "vf" : ""), onclick: () => openTab(f.k) }, h("span", { text: f.name }), tag));
  }
  fill(tree, ...rows);

  fill($("#ideTabs"), ...ED.open.map((k) => {
    const f = fileOf(k);
    return h("div", { class: "tab" + (k === ED.active ? " on" : ""), onclick: (e) => { if (!e.target.closest(".x")) openTab(k); } },
      h("span", { text: f.name }), fileDirty(k) ? h("span", { class: "tag is-dirty", text: "已改" }) : roNow(k) ? h("span", { class: "tag", text: "只读" }) : null,
      h("button", { class: "x", type: "button", text: "×", title: "关闭", "aria-label": "关闭 " + f.name, onclick: () => closeTab(k) }));
  }));

  const f = fileOf(ED.active), meta = ED.files[ED.active];
  let why = f.why;
  if (f.k === "persona" && meta && meta.source === "builtin") why = "现在生效的是内置人设。在这里写下内容并保存，就改用这一份。";
  fill($("#ideCrumb"), h("span", { class: "mono", text: f.path }), h("span", { text: why }),
    meta && meta.error ? h("span", { class: "warn", text: meta.error }) : null, h("span", { class: "spacer" }),
    ED.diff && !roNow(f.k) ? h("span", { text: "左边是已保存的，右边是现在的" }) : null);

  const dirtyAll = EDITABLE.filter(fileDirty);
  $("#ideSave").disabled = !fileDirty(ED.active);
  $("#ideSaveAll").disabled = !dirtyAll.length;
  $("#ideRevert").disabled = !fileDirty(ED.active);
  $("#ideDiff").disabled = roNow(ED.active);
  $("#ideDiff").textContent = ED.diff && !roNow(ED.active) ? "回到编辑" : "看改动";
  $("#ideBuiltin").hidden = ED.active !== "persona" || !ED.builtin;
  const text = liveOf(ED.active);
  const fontBtn = (v, l) => h("button", { type: "button", text: l, class: ED.font === v ? "on" : "", onclick: () => { ED.font = v; applyEdOptions(); paintChrome(); } });
  fill($("#ideStatus"), h("span", { text: "Markdown" }), h("span", { text: `第 ${ED.cur.l} 行，第 ${ED.cur.c} 列` }), h("span", { text: `${text.length.toLocaleString("zh-CN")} 字` }),
    dirtyAll.length ? h("span", { class: "warn", text: `${dirtyAll.length} 个文件未保存：${dirtyAll.map((k) => fileOf(k).name).join("、")}` }) : h("span", { text: "全部已保存" }),
    h("span", { class: "spacer" }), h("span", null, "字体 ", fontBtn("mono", "等宽"), " ", fontBtn("serif", "宋体")),
    h("button", { type: "button", text: "自动换行", class: ED.wrap ? "on" : "", onclick: () => { ED.wrap = !ED.wrap; applyEdOptions(); paintChrome(); } }),
    h("span", { text: ED.failed ? "普通文本框" : "Ctrl F 查找 · Ctrl H 替换" }));
}
function applyEdOptions() {
  const o = { fontFamily: edFont(), wordWrap: ED.wrap ? "on" : "off" };
  if (ED.editor) ED.editor.updateOptions(o);
  if (ED.diffEd) { ED.diffEd.getModifiedEditor().updateOptions(o); ED.diffEd.getOriginalEditor().updateOptions(o); }
  const ta = $("#ideEd textarea"); if (ta) ta.style.fontFamily = edFont();
}
function disposeEditor() {
  if (ED.editor) { ED.editor.dispose(); ED.editor = null; }
  if (ED.diffEd) { const orig = ED.diffEd.getModel() && ED.diffEd.getModel().original; ED.diffEd.dispose(); ED.diffEd = null; if (orig) orig.dispose(); }
}
async function mountEditor() {
  const box = $("#ideEd"); if (!box) return;
  disposeEditor();
  const f = fileOf(ED.active);
  if (f.view) {
    fill(box, h("div", { class: "loading", text: "正在拼接…" }));
    const r = await getJSON(`/api/persona/preview?kind=${f.view}`).catch(() => ({ ok: false }));
    ED.preview[f.view] = r.ok ? r.blocks.map((b) => `<!-- ${b.file}${b.edit_as ? " · 可在 " + b.edit_as + " 里改" : " · 只读"} · ${b.tokens} 字 -->\n${b.content}`).join("\n\n") : "（拼接失败：某个文件读不出来）";
    if (ED.active !== f.k) return;
  }
  fill(box, );
  if (ED.failed) { mountTextarea(box, f); paintChrome(); return; }
  const m = ED.monaco, ro = roNow(f.k);
  if (ED.diff && !ro) {
    ED.diffEd = m.editor.createDiffEditor(box, { ...edOptions(false), renderSideBySide: true, useInlineViewWhenSpaceIsLimited: false, originalEditable: false, renderOverviewRuler: false });
    ED.diffEd.setModel({ original: m.editor.createModel(savedOf(f.k), "markdown"), modified: ED.models[f.k] });
    bindEditor(ED.diffEd.getModifiedEditor());
  } else {
    const model = f.view ? m.editor.createModel(liveOf(f.k), "markdown") : ED.models[f.k];
    ED.editor = m.editor.create(box, { ...edOptions(ro), model });
    if (f.view) ED.editor.onDidDispose(() => model.dispose());
    bindEditor(ED.editor);
  }
  paintChrome();
}
function bindEditor(ed) {
  const m = ED.monaco;
  ed.addCommand(m.KeyMod.CtrlCmd | m.KeyCode.KeyS, () => saveFiles([ED.active]));
  ed.onDidChangeCursorPosition((e) => { ED.cur = { l: e.position.lineNumber, c: e.position.column }; paintChrome(); });
  ed.focus();
}
function mountTextarea(box, f) {
  const ta = h("textarea", { spellcheck: "false" });
  ta.value = liveOf(f.k); ta.readOnly = roNow(f.k); ta.style.fontFamily = edFont();
  ta.addEventListener("input", () => { ED.draft[f.k] = ta.value; paintChrome(); });
  ta.addEventListener("keydown", (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); saveFiles([ED.active]); } });
  box.append(ta);
  if (ED.diff && !roNow(f.k)) { ED.diff = false; openDrawer(`${f.name} 改了什么`, "上面是已保存的，下面是现在的。", lineDiff(savedOf(f.k), liveOf(f.k))); }
}
function openTab(k) {
  if (!ED.open.includes(k)) ED.open.push(k);
  ED.active = k; ED.cur = { l: 1, c: 1 };
  if (roNow(k)) ED.diff = false;
  mountEditor();
}
function closeTab(k) {
  ED.open = ED.open.filter((x) => x !== k);
  if (!ED.open.length) ED.open = ["persona"];
  if (ED.active === k) ED.active = ED.open[ED.open.length - 1];
  mountEditor();
}

async function saveFiles(keys) {
  keys = keys.filter(fileDirty);
  if (!keys.length) { toast("没有要保存的改动"); return; }
  const done = [];
  for (const k of keys) {
    const content = liveOf(k);
    const { status, json } = await postJSON(`/api/persona/${k}`, { content, base_version: ED.files[k].version });
    if (status === 200 && json.ok) { ED.files[k] = { ...ED.files[k], ...json, source: k === "persona" ? undefined : ED.files[k].source }; done.push(fileOf(k).name); }
    else if (status === 409) { showConflict(k, json.current, content); }
    else toast(`${fileOf(k).name} 没保存成功：${json.error || "HTTP " + status}`, { bad: true });
  }
  if (done.length) {
    const fresh = await getJSON("/api/persona").catch(() => null);
    if (fresh && fresh.ok) for (const k of EDITABLE) if (!fileDirty(k) || done.includes(fileOf(k).name)) ED.files[k] = fresh.files[k];
    toast(`已保存 ${done.join("、")}，她说下一句话就按新的来`);
  }
  paintChrome();
}
function showConflict(k, current, mine) {
  const f = fileOf(k);
  openDrawer(`${f.name} 在别处被改过`, "你打开之后，这个文件在磁盘上被改了（另一个标签页、编辑器或者别的程序）。你的改动还在编辑器里，没有丢。下面上面是磁盘上的，下面是你的：",
    lineDiff(current.content || "", mine),
    h("div", { class: "btns" },
      h("button", { class: "btn primary", type: "button", text: "用我的覆盖", onclick: async () => {
        const { status, json } = await postJSON(`/api/persona/${k}`, { content: mine, base_version: current.version });
        closeDrawer();
        if (status === 200 && json.ok) { ED.files[k] = { ...ED.files[k], ...json }; toast(`已保存 ${f.name}`); }
        else toast(`${f.name} 又被改了一次，请再试`, { bad: true });
        paintChrome();
      } }),
      h("button", { class: "btn", type: "button", text: "放弃我的，用磁盘上的", onclick: () => { ED.files[k] = { ...ED.files[k], ...current }; setLive(k, current.content || ""); closeDrawer(); mountEditor(); } }),
      h("button", { class: "link", type: "button", text: "先不处理", onclick: closeDrawer })));
}
function revertActive() {
  const k = ED.active; if (!fileDirty(k)) return;
  const cur = liveOf(k);
  setLive(k, savedOf(k)); mountEditor();
  toast(`${fileOf(k).name} 回到了已保存的样子`, { undo: () => { setLive(k, cur); mountEditor(); } });
}
function restoreBuiltin() {
  if (!ED.builtin || !confirm("把内置人设模板填进编辑器？\n不会直接保存：先对比着看，满意再按保存。")) return;
  setLive("persona", ED.builtin); ED.active = "persona"; ED.diff = true;
  if (!ED.open.includes("persona")) ED.open.push("persona");
  mountEditor();
  toast("已填入内置模板，左右对比着看，满意再保存");
}

/* =====================================================================
   路由
   ===================================================================== */
const PAGES = {
  persona: { group: "她", title: "人格", render: renderPersona },
  model: { group: "接入", title: "模型", render: () => renderFields("model") },
  platforms: { group: "接入", title: "平台", render: () => renderFields("platforms") },
  settings: { group: "系统", title: "设置", render: () => renderFields("settings") },
};
let currentPage = null;
function route() {
  const key = location.hash.replace(/^#\/?/, "").split("?")[0];
  const page = PAGES[key] ? key : "persona";
  if (currentPage === "persona" && page !== "persona") disposeEditor();
  currentPage = page;
  document.querySelectorAll("#nav a[data-p]").forEach((a) => {
    const on = a.dataset.p === page;
    a.classList.toggle("on", on);
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  fill($("#crumb"), PAGES[page].group + " / ", h("b", { text: PAGES[page].title }));
  closeDrawer();
  $("#dirty").classList.remove("on");
  window.scrollTo(0, 0);
  PAGES[page].render();
}
addEventListener("hashchange", route);
addEventListener("beforeunload", (e) => {
  if (dirtyList().length || EDITABLE.some(fileDirty)) { e.preventDefault(); e.returnValue = ""; }
});

setNav(navOpen(), false);
route();
paintNotice();
setInterval(paintNotice, 20000);
if (!FORM.loaded) loadForm().catch(() => {});
