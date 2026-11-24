"use strict";
// Pipeline canvas, part 1: state, server calls, layout, rendering, pointer interaction.
// Every string that reaches the DOM goes through textContent / setAttribute, never innerHTML: names, filters and
// event data are untrusted, and an XSS here could stage and deploy permission changes (ui-spec §11).

const C = {};
const SVGNS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const h = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k.includes("-") || k === "role") n.setAttribute(k, v === true ? "" : v);
    else n[k] = v;
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid);
  return n;
};
const sv = (tag, attrs = {}, ...kids) => {
  const n = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null && v !== false) n.setAttribute(k, v === true ? "" : v);
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined && kid !== false) n.append(kid);
  return n;
};
const clone = (x) => JSON.parse(JSON.stringify(x));
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---------------------------------------------------------------- state
const S = {
  ops: [], pos: 0,            // staged change list and undo position: the working config is `deployed + ops[0:pos]`
  base: null, work: null,     // server state for the deployed config and for the working config
  layout: { nodes: {}, viewport: null },
  sel: null,                  // {type: "node"|"edge", id}
  view: { x: 40, y: 40, k: 1 },
  reach: false, connect: null, runtime: { running: false }, unavailable: false, conflicts: [], tab: "inspector", seq: 0,
};
C.S = S;

const SIZE = { topic: [176, 46], agent: [196, 58], source: [176, 54], sink: [176, 54] };
const KIND_LABEL = { topic: "topic", agent: "agent", source: "source", sink: "sink" };
C.KIND_LABEL = KIND_LABEL;

async function api(method, path, body) {
  const r = await fetch(path, { method, headers: body ? { "content-type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; e.data = data; throw e; }
  return data;
}
C.api = api;

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast.t);
  toast.t = setTimeout(() => t.classList.remove("show"), 3800);
}
C.toast = toast;

// ---------------------------------------------------------------- server round trips
const active = () => S.ops.slice(0, S.pos);
C.active = active;

async function refresh() {
  const seq = ++S.seq;
  try {
    const work = await api("POST", "/api/canvas/state", { ops: active() });
    if (seq !== S.seq) return;                         // a newer request superseded this one
    S.work = work;
    if (!S.base) S.base = S.ops.length ? await api("POST", "/api/canvas/state", { ops: [] }) : work;
    S.unavailable = false;
  } catch (e) {
    if (seq !== S.seq) return;
    S.unavailable = true;                              // fail closed: no deploy while validation can't run (U9)
  }
  render();
  C.panels && C.panels.update();
}
C.refresh = refresh;

let saveTimer = null;
function persist() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => api("PUT", "/api/canvas/staged", { ops: S.ops, pos: S.pos }).catch(() => {}), 200);
}
let layoutTimer = null;
function saveLayout() {
  clearTimeout(layoutTimer);
  layoutTimer = setTimeout(() => {
    api("PUT", "/api/canvas/layout", { nodes: S.layout.nodes, viewport: { x: S.view.x, y: S.view.y, zoom: S.view.k } }).catch(() => {});
  }, 400);
}

// ---------------------------------------------------------------- staging
C.stage = function stage(label, files, opts = {}) {
  S.ops = S.ops.slice(0, S.pos);                       // a new change drops the redo tail
  const hashes = (S.work && S.work.hashes) || {};
  const base = {};
  for (const f of files) base[`${f.kind}/${f.name}`] = hashes[`${f.kind}/${f.name}`] ?? "";
  const last = S.ops[S.ops.length - 1];
  if (opts.merge && last && last.merge === opts.merge) {
    // a run of edits to one field is a single staged change
    const byKey = new Map(last.files.map((f) => [`${f.kind}/${f.name}`, f]));
    for (const f of files) byKey.set(`${f.kind}/${f.name}`, f);
    last.files = [...byKey.values()];
    last.label = label;
    last.base = { ...base, ...last.base };
  } else {
    S.ops.push({ id: `c${Date.now().toString(36)}${S.ops.length}`, label, merge: opts.merge || null, files, base });
  }
  S.pos = S.ops.length;
  persist();
  return refresh();
};
C.undo = () => { if (S.pos > 0) { S.pos--; persist(); refresh(); } };
C.redo = () => { if (S.pos < S.ops.length) { S.pos++; persist(); refresh(); } };
C.discard = async () => {
  S.ops = []; S.pos = 0; S.conflicts = [];
  await api("DELETE", "/api/canvas/staged").catch(() => {});
  S.base = null;
  await refresh();
};
C.revertAt = (i) => {
  const files = new Set(S.ops[i].files.map((f) => `${f.kind}/${f.name}`));
  const later = S.ops.slice(i + 1, S.pos).some((o) => o.files.some((f) => files.has(`${f.kind}/${f.name}`)));
  if (later) return false;
  S.ops.splice(i, 1);
  S.pos = Math.min(Math.max(S.pos - (i < S.pos ? 1 : 0), 0), S.ops.length);
  persist(); refresh();
  return true;
};
C.canRevert = (i) => {
  const files = new Set(S.ops[i].files.map((f) => `${f.kind}/${f.name}`));
  return !S.ops.slice(i + 1, S.pos).some((o) => o.files.some((f) => files.has(`${f.kind}/${f.name}`)));
};

// ---------------------------------------------------------------- graph model (deployed ∪ working, with change markers)
function model() {
  const wg = S.work.graph, bg = S.base ? S.base.graph : S.work.graph;
  const nodes = new Map();
  const nodeData = (n, inv) => (n.kind === "topic" ? inv.topics[n.name] : inv.entries[n.kind][n.name]);
  for (const n of bg.nodes) nodes.set(n.id, { ...n, status: "removed" });
  for (const n of wg.nodes) {
    const old = nodes.get(n.id);
    let status = "";
    if (S.base) {
      if (!old) status = "added";
      else if (!same(nodeData(n, S.base.inventory), nodeData(n, S.work.inventory))) status = "modified";
    }
    nodes.set(n.id, { ...n, status });
  }
  const edges = new Map();
  for (const e of bg.edges) if (nodes.has(e.from) && nodes.has(e.to)) edges.set(e.id, { ...e, status: "removed" });
  for (const e of wg.edges) edges.set(e.id, { ...e, status: S.base && !edges.has(e.id) ? "added" : "" });
  if (S.ops.length === 0 || S.pos === 0) for (const e of edges.values()) e.status = "";
  return { nodes, edges };
}

// ---------------------------------------------------------------- layout
const COL = 214, ROW = 80, PAD = 26;

function autoLayout(m) {
  const nodes = [...m.nodes.values()];
  const rank = Object.fromEntries(nodes.map((n) => [n.id, 0]));
  const dir = [...m.edges.values()];
  for (let i = 0; i < nodes.length; i++) {
    let changed = false;
    for (const e of dir) if (rank[e.from] + 1 > rank[e.to] && rank[e.from] + 1 <= nodes.length) { rank[e.to] = rank[e.from] + 1; changed = true; }
    if (!changed) break;
  }
  const linked = new Set();
  for (const e of m.edges.values()) { linked.add(e.from); linked.add(e.to); }
  const list = nodes.filter((n) => linked.has(n.id));
  const loose = nodes.filter((n) => !linked.has(n.id)).sort((a, b) => a.kind.localeCompare(b.kind) || a.name.localeCompare(b.name));
  const pos = {}, yOf = {};
  const cols = new Map();
  for (const n of list) (cols.get(rank[n.id]) || cols.set(rank[n.id], []).get(rank[n.id])).push(n);
  let rows = 0;
  for (const c of [...cols.keys()].sort((a, b) => a - b)) {
    const col = cols.get(c);
    const bary = (n) => {
      const ys = [...m.edges.values()].filter((e) => e.to === n.id && yOf[e.from] !== undefined).map((e) => yOf[e.from]);
      return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 1e9;
    };
    col.sort((a, b) => bary(a) - bary(b) || a.name.localeCompare(b.name));
    col.forEach((n, i) => { pos[n.id] = { x: PAD + c * COL, y: PAD + i * ROW }; yOf[n.id] = pos[n.id].y; });
    rows = Math.max(rows, col.length);
  }
  // nodes with no edges yet: a compact grid under the flow
  loose.forEach((n, i) => { pos[n.id] = { x: PAD + (i % 4) * COL, y: PAD + (rows + 1 + Math.floor(i / 4)) * ROW }; });
  return pos;
}

function position(id, auto) { return S.layout.nodes[id] || auto[id] || { x: 0, y: 0 }; }

// ---------------------------------------------------------------- rendering
function issuesFor(n) { return (S.work.issues || {})[`${n.kind}:${n.name}`]; }
C.issuesFor = issuesFor;

function reachSets() {
  if (!S.reach || !S.sel || S.sel.type !== "node") return null;
  const m = S.model;
  const fwd = {}, back = {};
  for (const e of m.edges.values()) if (e.status !== "removed") { (fwd[e.from] ||= []).push(e.to); (back[e.to] ||= []).push(e.from); }
  const walk = (start, adj) => { const seen = new Set([start]); const todo = [start]; while (todo.length) for (const nx of adj[todo.pop()] || []) if (!seen.has(nx)) { seen.add(nx); todo.push(nx); } return seen; };
  const n = m.nodes.get(S.sel.id);
  if (!n) return null;
  if (n.kind === "sink") return walk(n.id, back);
  if (n.kind === "source") return walk(n.id, fwd);
  return new Set([...walk(n.id, back), ...walk(n.id, fwd)]);
}

function render() {
  if (!S.work) return;
  const m = (S.model = model());
  const auto = autoLayout(m);
  S.auto = auto;
  const world = $("world");
  world.replaceChildren();
  world.setAttribute("transform", `translate(${S.view.x} ${S.view.y}) scale(${S.view.k})`);
  const hl = reachSets();
  const el_ = sv("g", { class: "edges" }), nl = sv("g", { class: "nodes" });
  const anchors = new Map([...m.nodes.values()].map((n) => {
    const p = position(n.id, auto), [w, hh] = SIZE[n.kind];
    return [n.id, { x: p.x, y: p.y, w, h: hh }];
  }));
  S.anchors = anchors;

  const topicFree = (id) => { const n = m.nodes.get(id); return n && n.kind === "topic" && (n.free_text || []).length ? n : null; };
  for (const e of m.edges.values()) {
    const a = anchors.get(e.from), b = anchors.get(e.to);
    if (!a || !b) continue;
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2;
    const dx = Math.max(40, Math.abs(x2 - x1) / 2);
    const d = `M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}`;
    const free = S.reach && (topicFree(e.from) || topicFree(e.to));
    const cls = ["edge", e.type, e.status, free ? "freetext" : "", S.sel && S.sel.type === "edge" && S.sel.id === e.id ? "selected" : "",
      hl && !(hl.has(e.from) && hl.has(e.to)) ? "dim" : "", hl && hl.has(e.from) && hl.has(e.to) ? "hl" : ""].filter(Boolean).join(" ");
    const g = sv("g", { class: cls, "data-edge": e.id });
    g.append(sv("path", { class: "line", d }), sv("path", { class: "hit", d }));
    const lbl = (e.filter ? `ƒ ${e.filter}` : "") || (free ? `≤ ${Math.max(...(topicFree(e.from) || topicFree(e.to)).free_text.map((f) => f.max_length || 0))}` : "");
    if (lbl) { const t = sv("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 6, "text-anchor": "middle" }); t.textContent = lbl.length > 34 ? `${lbl.slice(0, 33)}…` : lbl; g.append(t); }
    g.addEventListener("click", (ev) => { ev.stopPropagation(); C.select({ type: "edge", id: e.id }); });
    el_.append(g);
  }

  for (const n of m.nodes.values()) {
    const a = anchors.get(n.id);
    const iss = issuesFor(n) || { errors: [], warnings: [] };
    const cls = ["node", n.kind, n.status, iss.errors.length ? "error" : "",
      S.sel && S.sel.type === "node" && S.sel.id === n.id ? "selected" : "", S.connect && S.connect.from === n.id ? "connecting" : "",
      hl ? (hl.has(n.id) ? "hl" : "dim") : ""].filter(Boolean).join(" ");
    const g = sv("g", { class: cls, "data-node": n.id, transform: `translate(${a.x} ${a.y})`, tabindex: "0", role: "button", "aria-label": `${KIND_LABEL[n.kind]} ${n.name}${iss.errors.length ? ", has errors" : ""}` });
    g.append(sv("rect", { class: "body", width: a.w, height: a.h }), sv("rect", { class: "bar", x: 0, y: 0, width: 4, height: a.h }));
    const name = sv("text", { class: "name", x: 14, y: 22 });
    name.textContent = n.name.length > 21 ? `${n.name.slice(0, 20)}…` : n.name;
    g.append(name);
    if (n.sub) {
      const sub = sv("text", { class: "sub", x: 14, y: 41 });
      const text = (n.sub || "");
      sub.textContent = text.length > 27 ? `${text.slice(0, 26)}…` : text;
      g.append(sub);
    }
    if (n.kind === "agent") {
      const b = sv("g", { class: "badge", transform: `translate(${a.w - 44} 6)` });
      b.append(sv("rect", { width: 38, height: 18 }), sv("text", { x: 19, y: 13, "text-anchor": "middle" }));
      b.lastChild.textContent = `× ${n.concurrency}`;
      g.append(b);
    }
    if (iss.errors.length) { g.append(sv("circle", { class: "errbadge", cx: a.w - 10, cy: a.h - 10, r: 8 })); const t = sv("text", { class: "errbadge-t", x: a.w - 10, y: a.h - 6 }); t.textContent = "!"; g.append(t); }
    else if (iss.warnings.length || n.status === "modified") g.append(sv("circle", { class: "dot", cx: a.w - 10, cy: a.h - 10, r: 4 }));
    if (n.kind !== "sink") {
      const hd = sv("circle", { class: "handle", cx: a.w, cy: a.h / 2, r: 6, "data-handle": n.id });
      hd.addEventListener("mousedown", (ev) => C.startConnect(ev, n.id));
      hd.addEventListener("click", (ev) => { ev.stopPropagation(); });
      g.append(hd);
    }
    if (S.connect) {
      const verdict = S.connect.from === n.id ? null : C.planConnect && C.planConnect(S.connect.from, n.id);
      if (verdict) g.classList.add(verdict.ok || verdict.insert ? "target-ok" : "target-no");
    }
    g.addEventListener("mousedown", (ev) => C.startDrag(ev, n.id));
    g.addEventListener("click", (ev) => C.nodeClick(ev, n.id));
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); C.nodeClick(ev, n.id); } });
    nl.append(g);
  }
  world.append(el_, nl);
  if (S.connect && S.connect.ghost) world.append(sv("path", { class: "ghost", d: S.connect.ghost }));
}
C.render = render;

C.select = (sel) => { S.sel = sel; if (sel) S.tab = "inspector"; render(); C.panels && C.panels.update(); };

// ---------------------------------------------------------------- pointer interaction
function toWorld(ev) {
  const r = $("svg").getBoundingClientRect();
  return { x: (ev.clientX - r.left - S.view.x) / S.view.k, y: (ev.clientY - r.top - S.view.y) / S.view.k };
}
C.toWorld = toWorld;

let drag = null;
C.startDrag = (ev, id) => {
  if (ev.button !== 0 || ev.target.dataset.handle) return;
  const p = position(id, S.auto);
  drag = { id, sx: ev.clientX, sy: ev.clientY, ox: p.x, oy: p.y, moved: false };
};
C.startConnect = (ev, from) => {
  ev.stopPropagation();
  ev.preventDefault();
  S.connect = { from, drag: true, ghost: null, sx: ev.clientX, sy: ev.clientY, moved: false };
  render();
};
let pan = null;
function bind() {
  const svg = $("svg");
  svg.addEventListener("mousedown", (ev) => {
    if (ev.target === svg) pan = { sx: ev.clientX, sy: ev.clientY, ox: S.view.x, oy: S.view.y, moved: false };
  });
  svg.addEventListener("click", (ev) => {
    if (ev.target === svg && !(pan && pan.moved)) { S.connect = null; C.select(null); }
  });
  window.addEventListener("mousemove", (ev) => {
    if (drag) {
      const dx = (ev.clientX - drag.sx) / S.view.k, dy = (ev.clientY - drag.sy) / S.view.k;
      if (!drag.moved && Math.hypot(dx, dy) < 3) return;
      drag.moved = true;
      S.layout.nodes[drag.id] = { x: Math.round(drag.ox + dx), y: Math.round(drag.oy + dy) };
      render();
    } else if (S.connect && S.connect.drag) {
      const w = toWorld(ev), a = S.anchors.get(S.connect.from);
      if (Math.hypot(ev.clientX - S.connect.sx, ev.clientY - S.connect.sy) > 4) S.connect.moved = true;
      const x1 = a.x + a.w, y1 = a.y + a.h / 2;
      S.connect.ghost = `M${x1} ${y1} C${x1 + 60} ${y1} ${w.x - 60} ${w.y} ${w.x} ${w.y}`;
      S.connect.at = w;
      render();
    } else if (pan) {
      const dx = ev.clientX - pan.sx, dy = ev.clientY - pan.sy;
      if (Math.hypot(dx, dy) > 3) pan.moved = true;
      S.view.x = pan.ox + dx; S.view.y = pan.oy + dy;
      $("world").setAttribute("transform", `translate(${S.view.x} ${S.view.y}) scale(${S.view.k})`);
      $("svg").classList.add("panning");
    }
  });
  window.addEventListener("mouseup", (ev) => {
    if (drag) {
      const was = drag; drag = null;
      if (was.moved) { saveLayout(); render(); suppressClick = true; setTimeout(() => { suppressClick = false; }, 0); }
    } else if (S.connect && S.connect.drag) {
      const c = S.connect;
      if (c.moved) {
        const at = toWorld(ev);
        const target = [...S.anchors.entries()].find(([id, a]) => id !== c.from && !a.hidden && at.x >= a.x && at.x <= a.x + a.w && at.y >= a.y && at.y <= a.y + a.h);
        S.connect = null;
        if (target) C.tryConnect(c.from, target[0]); else render();
        suppressClick = true; setTimeout(() => { suppressClick = false; }, 0);
      } else { S.connect = { from: c.from, drag: false }; render(); toast("Now click the node to connect to (Esc cancels)."); }
    }
    if (pan) { if (pan.moved) saveLayout(); $("svg").classList.remove("panning"); setTimeout(() => { pan = null; }, 0); }
  });
  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    zoomAt(ev.deltaY < 0 ? 1.1 : 1 / 1.1, ev.clientX, ev.clientY);
  }, { passive: false });
}
let suppressClick = false;
function zoomAt(f, cx, cy) {
  const r = $("svg").getBoundingClientRect();
  const k = Math.min(2.2, Math.max(0.25, S.view.k * f)), px = (cx ?? r.left + r.width / 2) - r.left, py = (cy ?? r.top + r.height / 2) - r.top;
  S.view.x = px - ((px - S.view.x) / S.view.k) * k; S.view.y = py - ((py - S.view.y) / S.view.k) * k; S.view.k = k;
  $("world").setAttribute("transform", `translate(${S.view.x} ${S.view.y}) scale(${S.view.k})`);
  saveLayout();
}
C.fit = (floor = 0.58) => {
  if (!S.anchors || !S.anchors.size) return;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const a of S.anchors.values()) { x0 = Math.min(x0, a.x); y0 = Math.min(y0, a.y); x1 = Math.max(x1, a.x + a.w); y1 = Math.max(y1, a.y + a.h); }
  const r = $("svg").getBoundingClientRect();
  const w = r.width || 900, hh = r.height || 600;
  const k = Math.min(1.1, Math.max(floor, Math.min((w - 80) / (x1 - x0 + 60), (hh - 80) / (y1 - y0 + 90))));   // never smaller than readable: pan instead
  S.view = { k, x: 40 - (x0 - 30) * k, y: 40 - (y0 - 40) * k };
  render(); saveLayout();
};
C.relayout = () => { S.layout.nodes = {}; render(); saveLayout(); C.fit(0.3); };
C.centerOn = (id) => {
  const a = S.anchors.get(id);
  if (!a) return;
  const r = $("svg").getBoundingClientRect();
  S.view.x = (r.width || 900) / 2 - (a.x + a.w / 2) * S.view.k; S.view.y = (r.height || 600) / 2 - (a.y + a.h / 2) * S.view.k;
  render(); saveLayout();
};

C.nodeClick = (ev, id) => {
  ev.stopPropagation();
  if (suppressClick) return;
  if (S.connect && S.connect.from !== id && !S.connect.drag) { const from = S.connect.from; S.connect = null; C.tryConnect(from, id); return; }
  C.select({ type: "node", id });
};

// ---------------------------------------------------------------- boot
C.boot = async function boot() {
  bind();
  $("undo").addEventListener("click", C.undo);
  $("redo").addEventListener("click", C.redo);
  $("discard").addEventListener("click", () => C.confirmDiscard());
  $("deploy").addEventListener("click", () => C.panels.openDeploy());
  $("fit").addEventListener("click", () => C.fit(0.3));
  $("relayout").addEventListener("click", C.relayout);
  $("zin").addEventListener("click", () => zoomAt(1.2));
  $("zout").addEventListener("click", () => zoomAt(1 / 1.2));
  $("search-btn").addEventListener("click", () => C.panels.openSearch());
  $("reach-toggle").addEventListener("click", () => { S.reach = !S.reach; $("reach-toggle").setAttribute("aria-pressed", String(S.reach)); render(); C.panels.update(); });
  document.querySelectorAll("[data-new]").forEach((b) => b.addEventListener("click", () => C.edit.openNew(b.dataset.new)));
  document.addEventListener("keydown", (ev) => {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(ev.target.tagName) || ev.target.isContentEditable;
    const mod = ev.ctrlKey || ev.metaKey;
    if (mod && ev.key.toLowerCase() === "k") { ev.preventDefault(); C.panels.openSearch(); }
    else if (mod && ev.key.toLowerCase() === "z" && !typing) { ev.preventDefault(); ev.shiftKey ? C.redo() : C.undo(); }
    else if (ev.key === "Escape") { S.connect = null; render(); }
    else if ((ev.key === "Delete" || ev.key === "Backspace") && !typing && S.sel) { ev.preventDefault(); C.edit.deleteSelection(); }
  });
  try {
    const [staged, layout] = await Promise.all([api("GET", "/api/canvas/staged"), api("GET", "/api/canvas/layout")]);
    S.ops = staged.ops || []; S.pos = Math.min(staged.pos ?? S.ops.length, S.ops.length);
    S.layout = { nodes: layout.nodes || {}, viewport: layout.viewport || null };
    if (layout.viewport) S.view = { x: layout.viewport.x, y: layout.viewport.y, k: layout.viewport.zoom || 1 };
  } catch { /* start empty */ }
  S.base = await api("POST", "/api/canvas/state", { ops: [] }).catch(() => null);
  await refresh();
  C.panels.checkConflicts();
  const poll = async () => { try { S.runtime = await api("GET", "/api/canvas/runtime"); } catch { S.runtime = { running: false }; } C.panels.updateLive(); };
  poll();
  setInterval(poll, 3000);
  if (!S.layout.viewport) C.fit();
};
