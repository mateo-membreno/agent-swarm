"use strict";
(() => {
// Pipeline canvas, part 2: turning gestures into staged changes. Every edge is exactly one manifest field (ui-spec §4.2),
// so connecting writes that field and deleting the edge removes it. Nothing here touches disk: C.stage() only queues.

const E = (C.edit = {});
const S = C.S;
const KIND_DIR = { topic: "topic", agent: "agent", source: "source", sink: "sink" };
const NAME_RE = /^[a-z][a-z0-9_]{1,62}$/;
const EMPTY_SCHEMA = () => ({ type: "object", additionalProperties: false, required: [], properties: {} });

const node = (id) => S.model.nodes.get(id);
const entryOf = (n) => {
  const inv = S.work.inventory;
  const e = n.kind === "topic" ? inv.topics[n.name] : inv.entries[KIND_DIR[n.kind]][n.name];
  return e ? clone(e) : null;
};
const getEntry = (kind, name) => {
  const inv = S.work.inventory;
  const e = kind === "topic" ? inv.topics[name] : inv.entries[kind] && inv.entries[kind][name];
  return e ? clone(e) : null;
};
E.entryOf = entryOf;
E.getEntry = getEntry;
const file = (kind, name, data) => ({ kind, name, data });

// ---------------------------------------------------------------- field edits from the inspector (merged per field)
E.patch = (n, mutate, label, mergeKey) => {
  const data = entryOf(n);
  if (!data) return;
  mutate(data);
  if (same(data, entryOf(n))) return;
  return C.stage(label, [file(KIND_DIR[n.kind], n.name, data)], { merge: mergeKey ? `${n.id}:${mergeKey}` : null });
};
E.patchEntry = (kind, name, mutate, label, mergeKey) => {
  const data = getEntry(kind, name);
  if (!data) return;
  mutate(data);
  if (same(data, getEntry(kind, name))) return;
  return C.stage(label, [file(kind, name, data)], { merge: mergeKey ? `${kind}:${name}:${mergeKey}` : null });
};

// ---------------------------------------------------------------- connection rules (§4.3)
C.planConnect = function planConnect(fromId, toId) {
  const f = node(fromId), t = node(toId);
  if (!f || !t || fromId === toId) return { ok: false, reason: "Pick a different node." };
  const no = (reason) => ({ ok: false, reason });
  const pair = `${f.kind}>${t.kind}`;
  const out = (label, files) => ({ ok: true, label, files });
  switch (pair) {
    case "source>topic": {
      const c = entryOf(f);
      if (c.topic === t.name) return no(`${f.name} already publishes to ${t.name}.`);
      c.topic = t.name;
      return out(`${f.name} publishes to ${t.name}`, [file("source", f.name, c)]);
    }
    case "topic>agent": {
      const a = entryOf(t);
      if (a.trigger && a.trigger.topic === f.name) return no(`${t.name} already reads ${f.name}.`);
      if (a.trigger && a.trigger.topic) return no(`${t.name} reads one topic in this version (${a.trigger.topic}). Remove that edge first.`);
      a.trigger = { topic: f.name };
      return out(`${t.name} triggers on ${f.name}`, [file("agent", t.name, a)]);
    }
    case "agent>topic": {
      const a = entryOf(f);
      a.writes = a.writes || {};
      a.writes.topics = a.writes.topics || [];
      if (a.writes.topics.includes(t.name)) return no(`${f.name} already writes ${t.name}.`);
      a.writes.topics.push(t.name);
      return out(`${f.name} may write ${t.name}`, [file("agent", f.name, a)]);
    }
    case "topic>sink": {
      const c = entryOf(t);
      if (c.topic === f.name) return no(`${t.name} already reads ${f.name}.`);
      c.topic = f.name;
      return out(`${t.name} reads ${f.name}`, [file("sink", t.name, c)]);
    }
    case "agent>agent":
      return { ok: false, insert: true, reason: "Agents talk only through topics. Insert a topic between them?" };
    default:
      if (t.kind === "source") return no("A source brings data in from outside. Draw from it to a topic.");
      if (pair === "topic>topic") return no("Topics connect through agents, sources or sinks, not directly.");
      if (f.kind === "agent") return no("An agent writes to topics. Connect it to a topic.");
      if (t.kind === "agent") return no("An agent reads a topic. Connect a topic to it.");
      if (f.kind === "sink") return no("A sink is an end point.");
      return no(`A ${f.kind} cannot connect to a ${t.kind}.`);
  }
};

C.tryConnect = function tryConnect(fromId, toId) {
  const plan = C.planConnect(fromId, toId);
  S.connect = null;
  if (plan.insert) { C.render(); return E.openInsertBetween(fromId, toId); }
  if (!plan.ok) { C.render(); return C.toast(plan.reason); }
  C.stage(plan.label, plan.files);
};

E.openInsertBetween = (aId, bId) => {
  const a = node(aId), b = node(bId);
  E.ask({
    title: "Insert a topic between agents",
    intro: `${a.name} and ${b.name} can't be wired directly: agents talk only through typed topics. This creates a topic, lets ${a.name} write it and makes ${b.name} read it.`,
    fields: [{ key: "topic", label: "New topic name", type: "text", value: `${a.name} handoff`, hint: "Any text. It is saved as a valid topic name." }],
    submit: "Insert topic",
    onChange: (v, f) => f.setHint("topic", `Saved as ${window.topicName(v.topic) || "(enter a name)"}`),
    onSubmit: (raw) => {
      const v = { topic: window.topicName(raw.topic) };
      if (!v.topic) return "Enter a name with at least one letter or digit.";
      if (getEntry("topic", v.topic)) return `Topic ${v.topic} already exists.`;
      const ad = entryOf(a), bd = entryOf(b);
      ad.writes = ad.writes || {};
      ad.writes.topics = [...(ad.writes.topics || []), v.topic];
      if (bd.trigger && bd.trigger.topic) return `${b.name} already reads ${bd.trigger.topic}. Remove that edge first.`;
      bd.trigger = { topic: v.topic };
      C.stage(`Insert topic ${v.topic} between ${a.name} and ${b.name}`,
        [file("topic", v.topic, { name: v.topic, schema: EMPTY_SCHEMA(), retention: "7d", partitions: 1 }), file("agent", a.name, ad), file("agent", b.name, bd)]);
    },
  });
};

// ---------------------------------------------------------------- deleting
E.deleteNode = (n) => {
  const files = [file(KIND_DIR[n.kind], n.name, null)];
  if (n.kind === "topic") {
    for (const [an, a] of Object.entries(S.work.inventory.entries.agent || {})) {
      if (((a.writes || {}).topics || []).includes(n.name)) { const d = clone(a); d.writes.topics = d.writes.topics.filter((x) => x !== n.name); files.push(file("agent", an, d)); }
    }
  }
  C.stage(`Remove ${n.kind} ${n.name}`, files);
  C.select(null);
};

E.deleteEdge = (e) => {
  const f = node(e.from), t = node(e.to);
  const change = (n, fn, label) => { const d = entryOf(n); fn(d); C.stage(label, [file(KIND_DIR[n.kind], n.name, d)]); };
  switch (e.type) {
    case "trigger": return change(t, (d) => { delete d.trigger; }, `Remove trigger ${f.name} → ${t.name}`);
    case "writes": return change(f, (d) => { d.writes.topics = d.writes.topics.filter((x) => x !== t.name); }, `${f.name} may no longer write ${t.name}`);
    case "publishes": return change(f, (d) => { delete d.topic; }, `${f.name} no longer publishes to ${t.name}`);
    case "consumes": return change(t, (d) => { delete d.topic; }, `${t.name} no longer reads ${f.name}`);
    default: return null;
  }
};

E.deleteSelection = () => {
  const sel = S.sel;
  if (!sel) return;
  if (sel.type === "node") return E.deleteNode(node(sel.id));
  if (sel.type === "edge") { const e = S.model.edges.get(sel.id); C.select(null); return e && E.deleteEdge(e); }
};

// ---------------------------------------------------------------- modal forms
C.closeModal = () => { $("modal-root").replaceChildren(); if (E.restoreFocus) E.restoreFocus.focus && E.restoreFocus.focus(); };
C.openModal = (content, opts = {}) => {
  E.restoreFocus = document.activeElement;
  const scrim = h("div", { class: "scrim", onmousedown: (ev) => { if (ev.target === scrim) C.closeModal(); } },
    h("div", { class: `modal${opts.wide ? " wide" : ""}`, role: "dialog", "aria-modal": "true", "aria-label": opts.title || "Dialog" }, content));
  scrim.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { ev.stopPropagation(); C.closeModal(); } });
  $("modal-root").replaceChildren(scrim);
  const first = scrim.querySelector("input, select, textarea, button");
  if (first) first.focus();
  return scrim;
};

/** Small form dialog. fields: {key,label,type:"text|number|select|checks|textarea",value,options:[{value,label}],hint}. */
E.ask = ({ title, intro, fields, submit = "Create", onSubmit, onChange }) => {
  const inputs = {};
  const err = h("p", { class: "field-note", hidden: true, role: "alert" });
  const body = fields.map((f) => {
    let input;
    if (f.type === "select") input = h("select", { id: `ask-${f.key}` }, ...(f.options || []).map((o) => h("option", { value: o.value, textContent: o.label ?? o.value })));
    else if (f.type === "checks") {
      input = h("div", { class: "checks", id: `ask-${f.key}` }, ...(f.options || []).map((o) => h("label", { class: "check" }, h("input", { type: "checkbox", value: o.value, checked: (f.value || []).includes(o.value) }), o.label ?? o.value)));
    } else if (f.type === "textarea") input = h("textarea", { id: `ask-${f.key}`, rows: 5, value: f.value ?? "" });
    else input = h("input", { type: f.type === "number" ? "number" : "text", id: `ask-${f.key}`, value: f.value ?? "" });
    if (f.type === "select" && f.value !== undefined) input.value = f.value;
    inputs[f.key] = input;
    const hint = h("p", { class: "hint", id: `hint-${f.key}`, textContent: f.hint || "" });
    return h("div", { class: "field" }, h("label", { for: `ask-${f.key}`, textContent: f.label }), hint, input);
  });
  const values = () => Object.fromEntries(fields.map((f) => {
    const i = inputs[f.key];
    if (f.type === "checks") return [f.key, [...i.querySelectorAll("input:checked")].map((c) => c.value)];
    return [f.key, f.type === "number" ? Number(i.value) : i.value.trim()];
  }));
  const api_ = {
    values,
    setOptions: (key, options, value) => { const s = inputs[key]; s.replaceChildren(...options.map((o) => h("option", { value: o.value, textContent: o.label ?? o.value }))); if (value !== undefined) s.value = value; },
    setHint: (key, text) => { $(`hint-${key}`).textContent = text; },
  };
  const form = h("form", { onsubmit: (ev) => {
    ev.preventDefault();
    const msg = onSubmit(values(), api_);
    if (msg) { err.textContent = msg; err.hidden = false; } else C.closeModal();
  } }, h("h2", { textContent: title }), intro ? h("p", { class: "muted", textContent: intro }) : null, ...body, err,
  h("div", { class: "row end" }, h("button", { class: "ghost", type: "button", textContent: "Cancel", onclick: C.closeModal }), h("button", { class: "primary", type: "submit", textContent: submit })));
  if (onChange) form.addEventListener("change", () => onChange(values(), api_));
  C.openModal(form, { title });
  if (onChange) onChange(values(), api_);
  return api_;
};

// ---------------------------------------------------------------- creating things
const topicOptions = () => Object.keys(S.work.inventory.topics).sort().map((t) => ({ value: t }));
const nameField = (hint) => ({ key: "name", label: "Name", type: "text", hint: hint || "lowercase letters, digits, underscore" });
const taken = (kind, name) => !!getEntry(kind, name);

E.openNew = (type) => {
  if (!S.work) return;
  if (type === "topic") {
    return E.ask({
      title: "New topic", intro: "Starts with a strict empty schema. Add fields from the inspector.",
      fields: [{ ...nameField("Any text, e.g. News stories. It is saved as a valid topic name."), value: "" }, { key: "description", label: "Description", type: "text" }],
      onChange: (v, f) => f.setHint("name", `Saved as ${window.topicName(v.name) || "(enter a name)"}`),
      onSubmit: (raw) => {
        const v = { ...raw, name: window.topicName(raw.name) };
        if (!v.name) return "Enter a name with at least one letter or digit.";
        if (taken("topic", v.name)) return `The topic ${v.name} already exists.`;
        C.stage(`Add topic ${v.name}`, [file("topic", v.name, { name: v.name, ...(v.description ? { description: v.description } : {}), schema: EMPTY_SCHEMA(), retention: "7d", partitions: 1 })]);
      },
    });
  }
  if (type === "agent") {
    const topics = topicOptions();
    if (!topics.length) return C.toast("Add a topic first: an agent needs one to read.");
    return E.ask({
      title: "New agent", intro: "Describe what it should do with each event. It reads one topic, asks the model, and writes to topics you connect afterwards.",
      fields: [nameField(), { key: "trigger", label: "Reads topic", type: "select", options: topics },
        { key: "model", label: "Model", type: "select", options: [{ value: "", label: "(no inference)" }, ...S.work.inventory.models.map((m) => ({ value: m }))], value: S.work.inventory.models[0] || "" },
        { key: "instructions", label: "Instructions", type: "textarea", hint: "In plain words, e.g. Summarise each story in one sentence." }],
      onSubmit: (v) => {
        if (!NAME_RE.test(v.name)) return "Use lowercase letters, digits and underscore (2 to 63 characters).";
        if (taken("agent", v.name)) return "That agent already exists.";
        const data = { name: v.name, build: "agents-src/llm_agent", ...(v.instructions ? { instructions: v.instructions } : {}), trigger: { topic: v.trigger }, writes: { topics: [] },
          inference: { ...(v.model ? { models: [v.model] } : {}), max_tokens_per_event: 20000 }, isolation: "per_event", concurrency: 1,
          sandbox: { cpus: 0.5, memory_mb: 256, timeout_per_event_s: 120 }, delivery: { max_retries: 3 } };
        C.stage(`Add agent ${v.name}`, [file("agent", v.name, data)]);
      },
    });
  }
  if (type === "source") {
    const topics = topicOptions();
    if (!topics.length) return C.toast("Add a topic first: a source publishes to one.");
    return E.ask({
      title: "New source", intro: "Polls a public https API on a schedule and publishes each new item to a topic. Set the field mapping in the inspector.",
      fields: [nameField(), { key: "url", label: "URL to poll", type: "text", value: "https://", hint: "public API, no key" },
        { key: "topic", label: "Publishes to", type: "select", options: topics },
        { key: "id_field", label: "Item id field", type: "text", value: "id", hint: "the item field that is unique, e.g. objectID" },
        { key: "interval", label: "Every", type: "text", value: "10m", hint: "30s, 10m, 1h" }],
      onSubmit: (v) => {
        if (!NAME_RE.test(v.name)) return "Use lowercase letters, digits and underscore (2 to 63 characters).";
        if (taken("source", v.name)) return "That source already exists.";
        if (!/^https:\/\/[^\s]+$/.test(v.url)) return "The URL must start with https://";
        if (!/^[1-9][0-9]*(s|m|h)$/.test(v.interval)) return "Interval looks like 30s, 10m or 1h.";
        const props = Object.keys((((getEntry("topic", v.topic) || {}).schema) || {}).properties || {});
        const map = Object.fromEntries(props.map((p) => [p, p === "id" ? v.id_field : p]));
        C.stage(`Add source ${v.name}`, [file("source", v.name, { name: v.name, type: "http_poll", url: v.url, topic: v.topic, interval: v.interval, items_path: "", id_field: v.id_field, map, limit: 30 })]);
      },
    });
  }
  if (type === "sink") {
    const topics = topicOptions();
    if (!topics.length) return C.toast("Add a topic first.");
    return E.ask({
      title: "New sink", intro: "Takes events off a topic and either appends them to a file or POSTs each one to an API.",
      fields: [nameField(), { key: "topic", label: "Reads topic", type: "select", options: topics },
        { key: "type", label: "Does", type: "select", options: [{ value: "file", label: "Write to a file" }, { value: "http_post", label: "POST to an API" }] },
        { key: "path", label: "File (if writing)", type: "text", value: "data/results/out.jsonl", hint: "under data/, ends in .jsonl" },
        { key: "url", label: "URL (if POSTing)", type: "text", value: "", hint: "https only. Add a credential in the inspector if it needs one." }],
      onSubmit: (v) => {
        if (!NAME_RE.test(v.name)) return "Use lowercase letters, digits and underscore (2 to 63 characters).";
        if (taken("sink", v.name)) return "That sink already exists.";
        if (v.type === "http_post" && !/^https:\/\/[^\s]+$/.test(v.url)) return "The URL must start with https://";
        const data = v.type === "file" ? { name: v.name, type: "file", topic: v.topic, path: v.path } : { name: v.name, type: "http_post", topic: v.topic, url: v.url };
        C.stage(`Add sink ${v.name}`, [file("sink", v.name, data)]);
      },
    });
  }
};
})();
