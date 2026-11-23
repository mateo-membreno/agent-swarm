"use strict";
// Shared logic for every UI variant. Variants differ only in HTML shell and CSS; they must
// provide: #kinds #list #dashboard #editor #issues #diff, and optionally #jump-btn.
// All DOM is built with createElement/textContent (never innerHTML), so config values
// cannot inject markup.

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k.includes("-") || k === "role") n.setAttribute(k, v === true ? "" : v); // aria-*, data-*
    else n[k] = v;
  }
  for (const kid of kids) if (kid !== null && kid !== undefined) n.append(kid);
  return n;
};

const OVERVIEW = "overview";
const state = { kinds: [], kind: OVERVIEW, name: null, isNew: false, data: {}, listed: [], refs: {}, timer: null, seq: 0, valid: false };

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const json = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, json };
}

const kindOf = (id) => state.kinds.find((k) => k.id === id);
const isEmpty = (v) => v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
const resolve = (schema, root) =>
  schema && schema.$ref ? schema.$ref.replace(/^#\//, "").split("/").reduce((s, p) => s[p], root) : schema;

// ================================================================= form rendering
function renderField(schema, parent, key, root, ctx, required, path) {
  schema = resolve(schema, root);
  const wrap = el("div", { class: "field", "data-path": path });
  const id = `f-${path.replace(/[^a-z0-9]+/gi, "-")}`;
  wrap.append(el("label", { class: required ? "req" : "", textContent: schema.title || key, htmlFor: id }));
  if (schema.description) wrap.append(el("p", { class: "hint", id: `${id}-hint`, textContent: schema.description }));

  const when = schema["x-when"];
  const sib = schema["x-when-sibling"];
  const visible = () => {
    if (when) for (const [k, allowed] of Object.entries(when)) if (!allowed.includes(parent[k])) return false;
    if (sib && isEmpty(parent[sib])) return false;
    return true;
  };
  ctx.refresh.push(() => {
    const show = visible();
    wrap.hidden = !show;
    if (!show && parent[key] !== undefined) delete parent[key]; // prune values of hidden fields
  });

  const set = (v) => { if (isEmpty(v)) delete parent[key]; else parent[key] = v; ctx.changed(); };
  const cur = parent[key];
  const describe = (n) => { if (schema.description) n.setAttribute("aria-describedby", `${id}-hint`); return n; };

  if (schema.enum) {
    const sel = describe(el("select", { id }));
    if (!required) sel.append(el("option", { value: "", textContent: "(not set)" }));
    const labels = schema["x-enum-labels"] || {};
    for (const v of schema.enum) sel.append(el("option", { value: v, textContent: labels[v] || v, selected: cur === v }));
    if (required && cur === undefined) parent[key] = schema.enum[0];
    sel.addEventListener("change", () => set(sel.value));
    wrap.append(sel);
  } else if (schema.type === "boolean") {
    const cb = describe(el("input", { id, type: "checkbox", checked: !!cur }));
    cb.addEventListener("change", () => set(cb.checked));
    wrap.append(cb);
  } else if (schema.type === "integer" || schema.type === "number") {
    const inp = describe(el("input", { id, type: "number", value: cur ?? "", step: schema.type === "integer" ? "1" : "any" }));
    inp.addEventListener("input", () => set(inp.value === "" ? undefined : Number(inp.value)));
    wrap.append(inp);
  } else if (schema.type === "string" && schema["x-ref"]) {
    const names = state.refs[schema["x-ref"]] || [];
    const sel = describe(el("select", { id }));
    sel.append(el("option", { value: "", textContent: names.length ? "(choose)" : `(no ${schema["x-ref"]} yet)` }));
    for (const n of names) sel.append(el("option", { value: n, textContent: n, selected: cur === n }));
    if (cur && !names.includes(cur)) sel.append(el("option", { value: cur, textContent: `${cur} (missing)`, selected: true }));
    sel.addEventListener("change", () => set(sel.value));
    wrap.append(sel);
    if (!names.length) wrap.append(el("button", { class: "link", type: "button", textContent: `Create a ${schema["x-ref"]} first`, onclick: () => go(schema["x-ref"], null, true) }));
  } else if (schema.type === "string") {
    // An existing entry's name is its filename; renaming would silently create a copy.
    const locked = key === "name" && parent === state.data && !state.isNew;
    const inp = describe(el("input", { id, type: "text", value: cur ?? "", spellcheck: false, disabled: locked, autocomplete: "off" }));
    inp.addEventListener("input", () => set(inp.value));
    wrap.append(inp);
  } else if (schema.type === "array" && resolve(schema.items || {}, root).type === "string") {
    const ta = describe(el("textarea", { id, value: (cur || []).join("\n"), placeholder: "one per line", rows: 3 }));
    ta.addEventListener("input", () => set(ta.value.split("\n").map((s) => s.trim()).filter(Boolean)));
    wrap.append(ta);
  } else if (schema.type === "object" && schema.properties) {
    wrap.replaceChildren(renderObject(schema, parent, key, root, ctx, schema.title || key, schema.description, path, wrap));
  } else if (schema.type === "object" && schema.additionalProperties && typeof schema.additionalProperties === "object") {
    wrap.append(renderMap(schema, parent, key, root, ctx, path));
  } else {
    // Free-form values (JSON Schemas, anyOf): raw JSON.
    const ta = describe(el("textarea", { id, value: cur === undefined ? "" : JSON.stringify(cur, null, 2), spellcheck: false, rows: 8 }));
    ta.addEventListener("input", () => {
      if (ta.value.trim() === "") { ta.removeAttribute("aria-invalid"); return set(undefined); }
      try { const v = JSON.parse(ta.value); ta.removeAttribute("aria-invalid"); set(v); }
      catch { ta.setAttribute("aria-invalid", "true"); }
    });
    wrap.append(ta);
  }
  return wrap;
}

function renderObject(schema, parent, key, root, ctx, title, description, path, wrap) {
  if (parent[key] === undefined || typeof parent[key] !== "object") parent[key] = {};
  const obj = parent[key];
  const fs = el("fieldset", { "data-path": path }, el("legend", { textContent: title }));
  if (description) fs.append(el("p", { class: "hint", textContent: description }));
  const req = new Set(schema.required || []);
  for (const [k, sub] of Object.entries(schema.properties)) fs.append(renderField(sub, obj, k, root, ctx, req.has(k), `${path}.${k}`));
  return fs;
}

function renderMap(schema, parent, key, root, ctx, path) {
  if (parent[key] === undefined) parent[key] = {};
  const map = parent[key];
  const valSchema = resolve(schema.additionalProperties, root);
  const body = el("div");
  const rebuild = () => {
    body.replaceChildren();
    for (const name of Object.keys(map)) {
      const section = el("fieldset", { "data-path": `${path}.${name}` }, el("legend", { textContent: name }));
      if (valSchema.type === "object" && valSchema.properties) {
        const req = new Set(valSchema.required || []);
        for (const [k, sub] of Object.entries(valSchema.properties)) {
          section.append(renderField(sub, map[name], k, root, ctx, req.has(k), `${path}.${name}.${k}`));
        }
      } else if (valSchema.type === "array") {
        const ta = el("textarea", { "aria-label": `${name} fallback chain`, value: (map[name] || []).join("\n"), rows: 2, placeholder: "one per line" });
        ta.addEventListener("input", () => { map[name] = ta.value.split("\n").map((s) => s.trim()).filter(Boolean); ctx.changed(); });
        section.append(ta);
      }
      section.append(el("button", {
        class: "ghost danger", type: "button", textContent: `Remove ${name}`,
        onclick: () => { delete map[name]; rebuild(); ctx.changed(); },
      }));
      body.append(section);
    }
    ctx.applyVisibility();
  };
  const nameInput = el("input", { type: "text", placeholder: "new name", "aria-label": "name for the new entry", autocomplete: "off" });
  const add = el("button", { class: "ghost", type: "button", textContent: "Add", onclick: () => {
    const n = nameInput.value.trim();
    if (!n || n in map) return;
    map[n] = valSchema.type === "array" ? [] : {};
    nameInput.value = "";
    rebuild(); ctx.changed();
  } });
  nameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add.click(); } });
  rebuild();
  return el("div", {}, body, el("div", { class: "row" }, nameInput, add));
}

// ================================================================= editor
function seedDefaults(schema) {
  const out = {};
  for (const [k, sub] of Object.entries(schema.properties || {})) if (sub.default !== undefined) out[k] = structuredClone(sub.default);
  return out;
}

function buildForm() {
  const schema = kindOf(state.kind).schema;
  const ctx = {
    refresh: [],
    changed: () => { ctx.applyVisibility(); schedulePreview(); },
    applyVisibility: () => ctx.refresh.forEach((f) => f()),
  };
  const form = el("div", { class: "form" });
  const req = new Set(schema.required || []);
  for (const [k, sub] of Object.entries(schema.properties)) form.append(renderField(sub, state.data, k, schema, ctx, req.has(k), k));
  ctx.applyVisibility();
  return form;
}

async function loadRefs(schema) {
  for (const sub of Object.values(schema.properties || {})) {
    if (sub["x-ref"]) state.refs[sub["x-ref"]] = (await api("GET", `/api/${sub["x-ref"]}`)).json.names || [];
  }
}

async function openEditor(focus = false) {
  document.body.dataset.view = "edit";
  const editor = $("editor");
  const kind = kindOf(state.kind);
  await loadRefs(kind.schema);
  editor.replaceChildren();
  if (!state.name && !state.isNew) {
    editor.append(el("p", { class: "muted empty", textContent: `Select a ${kind.title.toLowerCase()} entry or create a new one.` }));
    return;
  }
  editor.append(el("h2", { tabIndex: -1, textContent: state.isNew ? `New ${(kind.schema.title || kind.title).toLowerCase()}` : state.name }));
  editor.append(buildForm());
  const bar = el("div", { class: "toolbar" }, el("button", { class: "primary", id: "save", textContent: "Save", disabled: true, onclick: save_ }));
  if (!state.isNew) bar.append(el("button", { class: "ghost danger", textContent: "Delete", onclick: delete_ }));
  editor.append(bar);
  if (focus) editor.querySelector("h2").focus({ preventScroll: true });
  schedulePreview(0);
}

function schedulePreview(delay = 250) { clearTimeout(state.timer); state.timer = setTimeout(preview, delay); }

function currentName() {
  return state.data.name || (state.isNew ? "" : state.name);
}

async function preview() {
  const seq = ++state.seq;
  const name = currentName();
  if (!name) { return showIssues([{ path: "name", message: "enter a name" }], [], {}); }
  if (state.isNew && state.listed.includes(name)) {
    return showIssues([{ path: "name", message: "an entry with this name already exists" }], [], {});
  }
  const r = await api("POST", `/api/${state.kind}/${encodeURIComponent(name)}/preview`, state.data);
  if (seq !== state.seq) return; // a newer edit superseded this response
  if (!r.ok) return showIssues([{ path: "name", message: r.json.error || "preview failed" }], [], {});
  showIssues(r.json.errors, r.json.warnings, r.json.derived);
  renderDiff(r.json.diff);
  state.valid = r.json.errors.length === 0;
  const btn = $("save");
  if (btn) btn.disabled = !state.valid || !r.json.diff;
}

function showIssues(errors, warnings, derived) {
  state.valid = false;
  const box = $("issues");
  box.replaceChildren();
  if (!errors.length && !warnings.length) box.append(el("p", { class: "ok", textContent: "Valid." }));
  for (const e of errors) box.append(el("p", { class: "issue err" }, el("strong", { textContent: "Error " }), el("code", { textContent: e.path || "(root)" }), ` ${e.message}`));
  for (const w of warnings) box.append(el("p", { class: "issue warn" }, el("strong", { textContent: "Warning: " }), w));
  for (const [k, v] of Object.entries(derived || {})) {
    const items = [].concat(v).map((x) => (typeof x === "object" ? `${x.path} (${x.max_length})` : x));
    if (items.length) box.append(el("p", { class: "derived muted", textContent: `${k.replace(/_/g, " ")}: ${items.join(", ")}` }));
  }
  placeNotes(errors);
}

// Attach each located error to the field it concerns (longest matching path prefix).
function placeNotes(errors) {
  for (const n of document.querySelectorAll(".field-note")) n.remove();
  const fields = [...document.querySelectorAll("#editor [data-path]")];
  for (const e of errors) {
    const p = e.path || "";
    let best = null;
    for (const f of fields) {
      const fp = f.dataset.path;
      if ((p === fp || p.startsWith(fp + ".")) && (!best || fp.length > best.dataset.path.length)) best = f;
    }
    if (best && best.tagName !== "FIELDSET") best.append(el("p", { class: "field-note", role: "note", textContent: e.message }));
    else if (best) best.querySelector(":scope > legend")?.after(el("p", { class: "field-note", role: "note", textContent: e.message }));
  }
}

function renderDiff(diff) {
  const pre = $("diff");
  pre.replaceChildren();
  if (!diff) { pre.textContent = "No changes."; pre.className = "muted"; return; }
  pre.className = "";
  for (const line of diff.split("\n")) {
    const cls = line.startsWith("+") && !line.startsWith("+++") ? "add" : line.startsWith("-") && !line.startsWith("---") ? "del" : "";
    pre.append(el("span", { class: cls, textContent: line + "\n" }));
  }
}

async function save_() {
  const name = currentName();
  const r = await api("PUT", `/api/${state.kind}/${encodeURIComponent(name)}`, state.data);
  if (!r.ok) return showIssues(r.json.errors || [{ path: "", message: r.json.error || "save failed" }], [], {});
  state.isNew = false; state.name = name;
  await loadList(); await openEditor(true);
  showIssues([], r.json.warnings || [], r.json.derived);
  renderDiff("");
}

async function delete_() {
  if (!confirm(`Delete ${state.name}? Anything referencing it will fail validation on the next deploy.`)) return;
  await api("DELETE", `/api/${state.kind}/${encodeURIComponent(state.name)}`);
  state.name = null; state.data = {};
  await loadList(); await openEditor(); renderDiff(""); showIssues([], [], {});
}

// ================================================================= dashboard
const link = (kind, name, label) => el("button", { class: "link", type: "button", textContent: label ?? name, onclick: () => go(kind, name) });
const dash = (title, count, ...kids) =>
  el("section", { class: "dash-section" },
    el("h2", {}, title, count === null ? null : el("span", { class: "count", textContent: ` ${count}` })), ...kids);
const table = (caption, heads, rows) =>
  el("table", {},
    el("caption", { class: "sr-only", textContent: caption }),
    el("thead", {}, el("tr", {}, ...heads.map((h) => el("th", { scope: "col", textContent: h })))),
    el("tbody", {}, ...rows.map((cells) => el("tr", {}, ...cells.map((c, i) => (i === 0 ? el("th", { scope: "row" }, c) : el("td", {}, c)))))));
const empty = (text, kind) => el("p", { class: "muted empty" }, text, " ", kind ? el("button", { class: "ghost", type: "button", textContent: "Add one", onclick: () => go(kind, null, true) }) : null);
const txt = (v) => (v === undefined || v === null || v === "" ? el("span", { class: "muted", textContent: "none" }) : document.createTextNode(String(v)));

async function showOverview() {
  state.kind = OVERVIEW; state.name = null; state.isNew = false;
  document.body.dataset.view = "overview";
  history.replaceState(null, "", "#overview");
  markNav();
  const r = await api("GET", "/api/overview");
  const o = r.json;
  const root = $("dashboard");
  root.replaceChildren(el("h2", { class: "page-title", tabIndex: -1, textContent: "Overview" }));

  const att = [...o.attention].sort((a, b) => (a.level === "error" ? 0 : 1) - (b.level === "error" ? 0 : 1));
  root.append(dash("Needs attention", att.length,
    att.length
      ? el("ul", { class: "attention" }, ...att.map((a) => el("li", { class: `issue ${a.level === "error" ? "err" : "warn"}` },
          el("strong", { textContent: a.level === "error" ? "Error " : "Warning " }),
          link(a.kind, a.name, `${a.kind}: ${a.name}`),
          a.path ? el("code", { textContent: a.path }) : null, ` ${a.message}`)))
      : el("p", { class: "ok", textContent: "Nothing needs attention. Every entry validates." })));

  root.append(dash("Leaves this machine", o.egress.length,
    o.egress.length
      ? table("Hosts that traffic may leave the machine for", ["Host", "Via"], o.egress.map(([host, via]) => [host, via]))
      : el("p", { class: "ok", textContent: "Nothing is configured to leave this machine." })));

  const inf = o.inference;
  root.append(dash("Inference", `${inf.backends.length} backends, ${inf.models.length} models`,
    inf.backends.length
      ? table("Inference backends", ["Backend", "Kind", "Target", "Concurrency"],
          inf.backends.map((b) => [link("backend", b.name), b.kind, txt(b.target), txt(b.max_concurrency)]))
      : empty("No inference backends configured.", "backend"),
    inf.models.length
      ? table("Model aliases", ["Alias", "Backend", "Model", "Kind", "Agents"],
          inf.models.map((m) => [link("model", m.alias), m.backend, txt(m.model), m.kind,
            m.allowed_agents.length ? m.allowed_agents.join(", ") : el("span", { class: m.hosted ? "warn-text" : "muted", textContent: m.hosted ? "any agent (hosted)" : "any agent" })]))
      : (inf.backends.length ? empty("No model aliases configured. Agents can't use inference until one exists.", "model") : null),
    inf.fallback.length
      ? table("Fallback chains", ["Alias", "Falls back to"], inf.fallback.map((f) => [f.alias, f.chain.join(" then ")]))
      : el("p", { class: "muted", textContent: "No fallback configured: if a backend is down, inference fails." })));

  root.append(dash("APIs", o.apis.length,
    o.apis.length
      ? table("Registered APIs", ["API", "Kind", "Target", "Credential", "Approval", "Rate"],
          o.apis.map((a) => [link("api", a.name), a.kind, a.target, txt(a.credential), a.approval, txt(a.rate)]))
      : empty("No APIs registered.", "api")));

  root.append(dash("Topics", o.topics.length,
    o.topics.length
      ? table("Kafka topics", ["Topic", "Retention", "Partitions", "Key", "Free-text fields"],
          o.topics.map((t) => [link("topic", t.name), t.retention, t.partitions, txt(t.key),
            t.free_text.length ? t.free_text.map((f) => `${f.path} (${f.max_length})`).join(", ") : el("span", { class: "muted", textContent: "none" })]))
      : empty("No topics configured.", "topic")));

  root.append(el("p", { class: "muted todo", textContent: "Not configurable here yet: agents, connectors, pipelines." }));
}

// ================================================================= navigation
function markNav() {
  for (const b of $("kinds").children) b.setAttribute("aria-current", b.dataset.id === state.kind ? "page" : "false");
}

async function loadList() {
  const list = $("list");
  list.replaceChildren();
  if (state.kind === OVERVIEW) return;
  const kind = kindOf(state.kind);
  state.listed = (await api("GET", `/api/${state.kind}`)).json.names || [];
  for (const n of state.listed) {
    list.append(el("button", { class: "item", type: "button", textContent: n, "aria-current": n === state.name ? "true" : "false", onclick: () => selectEntry(n, true) }));
  }
  const by = kind.schema["x-create-by"];
  if (by) {
    // One dropdown picks what to create; the form then shows only that kind's fields.
    const prop = kind.schema.properties[by];
    const labels = prop["x-enum-labels"] || {};
    const noun = (kind.schema.title || kind.title).toLowerCase();
    const sel = el("select", { class: "create", "aria-label": `Create a new ${noun}` }, el("option", { value: "", textContent: `New ${noun}…` }));
    for (const v of prop.enum) sel.append(el("option", { value: v, textContent: labels[v] || v }));
    sel.addEventListener("change", () => { if (sel.value) newEntry(true, { [by]: sel.value }); });
    list.append(sel);
  } else {
    list.append(el("button", { class: "ghost add", type: "button", textContent: "New entry", onclick: () => newEntry(true) }));
  }
}

async function selectEntry(name, focus = false) {
  const r = await api("GET", `/api/${state.kind}/${encodeURIComponent(name)}`);
  state.name = name; state.isNew = false; state.data = r.json;
  history.replaceState(null, "", `#${state.kind}/${encodeURIComponent(name)}`);
  await loadList(); openEditor(focus); renderDiff("");
}

function newEntry(focus = false, preset = {}) {
  const kind = kindOf(state.kind);
  state.name = null;
  state.isNew = true; state.data = { ...seedDefaults(kind.schema), ...preset };
  loadList().then(() => openEditor(focus));
}

async function selectKind(id) {
  if (id === OVERVIEW) return showOverview();
  state.kind = id; state.name = null; state.isNew = false; state.data = {};
  history.replaceState(null, "", `#${id}`);
  markNav();
  await loadList();
  await openEditor(); renderDiff(""); showIssues([], [], {}); $("issues").replaceChildren();
}

async function go(kind, name, create = false, preset = {}) {
  if (kind === OVERVIEW) return showOverview();
  await selectKind(kind);
  if (create) newEntry(true, preset);
  else if (name) await selectEntry(name, true);
}

// ================================================================= jump dialog (/ or Ctrl+K)
async function openJump() {
  let dlg = $("jump");
  if (!dlg) {
    dlg = el("dialog", { id: "jump", "aria-label": "Jump to an entry" });
    document.body.append(dlg);
    dlg.addEventListener("click", (e) => { if (e.target === dlg) dlg.close(); });
  }
  const items = [{ kind: OVERVIEW, name: null, label: "Overview" }];
  for (const k of state.kinds) {
    const names = (await api("GET", `/api/${k.id}`)).json.names || [];
    for (const n of names) items.push({ kind: k.id, name: n, label: `${k.title}: ${n}` });
    const by = k.schema["x-create-by"];
    if (by) {
      const labels = k.schema.properties[by]["x-enum-labels"] || {};
      for (const v of k.schema.properties[by].enum) items.push({ kind: k.id, name: null, create: true, preset: { [by]: v }, label: `${k.title}: new ${labels[v] || v}` });
    } else items.push({ kind: k.id, name: null, create: true, label: `${k.title}: new entry` });
  }
  const input = el("input", { type: "text", placeholder: "Jump to…", "aria-label": "Filter entries", autocomplete: "off" });
  const results = el("ul", { class: "jump-list" });
  const draw = () => {
    const q = input.value.toLowerCase();
    results.replaceChildren();
    for (const it of items.filter((i) => i.label.toLowerCase().includes(q)).slice(0, 12)) {
      results.append(el("li", {}, el("button", { type: "button", class: "item", textContent: it.label, onclick: () => { dlg.close(); go(it.kind, it.name, it.create, it.preset); } })));
    }
  };
  input.addEventListener("input", draw);
  dlg.addEventListener("keydown", (e) => {
    const btns = [...dlg.querySelectorAll("li button")];
    const i = btns.indexOf(document.activeElement);
    if (e.key === "ArrowDown") { e.preventDefault(); (btns[i + 1] || btns[0])?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); (i <= 0 ? input : btns[i - 1]).focus(); }
    else if (e.key === "Enter" && document.activeElement === input) { e.preventDefault(); btns[0]?.click(); }
  });
  dlg.replaceChildren(input, results);
  draw();
  dlg.showModal();
  input.focus();
}

function typing(e) { const t = e.target; return t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName); }
document.addEventListener("keydown", (e) => {
  if ((e.key === "k" && (e.ctrlKey || e.metaKey)) || (e.key === "/" && !typing(e) && !e.ctrlKey && !e.metaKey)) { e.preventDefault(); openJump(); }
});

// ================================================================= boot
(async function init() {
  state.kinds = (await api("GET", "/api/kinds")).json;
  const nav = $("kinds");
  const items = [{ id: OVERVIEW, title: "Overview" }, ...state.kinds];
  for (const k of items) {
    nav.append(el("button", { type: "button", textContent: k.title, "data-id": k.id, "aria-current": "false", onclick: () => selectKind(k.id) }));
  }
  const jb = $("jump-btn");
  if (jb) jb.addEventListener("click", openJump);
  // Deep links: #overview, #api, #topic/email.triaged.v1
  const [hk, hn] = decodeURIComponent(location.hash.slice(1)).split("/");
  if (hk && hk !== OVERVIEW && kindOf(hk)) await go(hk, hn || null);
  else await showOverview();
})();
