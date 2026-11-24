"use strict";
(() => {
// Pipeline canvas, part 3: inspector, staged-changes list, deploy review, search, banners.
// All text is set with textContent. Values from config (names, filters, payload templates) are untrusted.

const P = (C.panels = {});
const S2 = C.S;
const KINDDIR = { topic: "topic", agent: "agent", source: "source", sink: "sink" };

const fieldBox = (label, input, hint) => h("div", { class: "field" }, h("label", { textContent: label }, null), hint ? h("p", { class: "hint", textContent: hint }) : null, input);
const sign = (s) => h("span", { class: `sign ${s === "+" ? "plus" : s === "-" ? "minus" : "tilde"}`, textContent: s });

// ---------------------------------------------------------------- toolbar, legend
/** The "is the stack running my config" label. Polled every few seconds, so it must never rebuild the inspector
 *  (that would reset a half-filled form). */
P.updateLive = function updateLive() {
  const live = $("live");
  const rt = S2.runtime || {};
  if (!rt.running) { live.textContent = "Not running: `harness run` applies deploys live"; live.className = "muted"; }
  else if (!rt.ok) { live.textContent = `Live, but the last config wasn't applied: ${rt.error.split("\n")[0]}`; live.className = "warn-text"; }
  else if (!rt.in_sync || rt.applying) { live.textContent = "Live: applying the new config…"; live.className = "muted"; }
  else { live.textContent = "Live: running the deployed config"; live.className = "ok"; }
};

P.update = function update() {
  const n = S2.pos;
  $("staged-count").textContent = S2.unavailable ? "Validation unavailable: deploy is blocked"
    : n ? `${n} staged change${n === 1 ? "" : "s"}` : "Up to date with deployed";
  $("staged-count").className = S2.unavailable ? "warn-text" : "muted";
  $("undo").disabled = S2.pos === 0;
  $("redo").disabled = S2.pos >= S2.ops.length;
  $("discard").hidden = S2.ops.length === 0;
  $("deploy").hidden = n === 0;
  P.updateLive();
  const legend = $("legend");
  if (!legend.children.length) {
    for (const [cls, text, color] of [["topic", "Topic", "var(--n-topic)"], ["agent", "Agent × concurrency", "var(--n-agent)"], ["conn", "Source, sink", "var(--n-conn)"],
      ["added", "Staged: added", "var(--ok)"], ["bad", "Has errors", "var(--err)"]]) {
      legend.append(h("li", {}, h("i", { style: `border-color:${color}` }), text));
    }
  }
  const rs = $("reach-summary");
  if (S2.reach && S2.work) {
    const r = S2.work.reach;
    const ch = new Set(r.flatMap((x) => x.free_text)).size;
    rs.textContent = `${new Set(r.map((x) => x.from)).size} source(s) reach ${new Set(r.map((x) => x.to)).size} sink(s) · ${ch} free-text channel(s). Select a source or sink to trace it.`;
  } else rs.textContent = "";
  P.renderInspector();
};

// ---------------------------------------------------------------- inspector
const issueList = (iss) => {
  if (!iss || (!iss.errors.length && !iss.warnings.length)) return null;
  return h("div", { class: "issues" }, h("ul", {},
    ...iss.errors.map((e) => h("li", { class: "err", textContent: `${e.path ? `${e.path}: ` : ""}${e.message}` })),
    ...iss.warnings.map((w) => h("li", { class: "warn", textContent: w }))));
};

function textInput(value, onCommit, opts = {}) {
  const i = h("input", { type: opts.type || "text", value: value ?? "", id: opts.id });
  if (opts.min !== undefined) i.min = opts.min;
  if (opts.max !== undefined) i.max = opts.max;
  i.addEventListener("change", () => onCommit(opts.type === "number" ? (i.value === "" ? undefined : Number(i.value)) : i.value));
  return i;
}
function selectInput(value, options, onCommit) {
  const s = h("select", {}, ...options.map((o) => h("option", { value: o.value, textContent: o.label ?? o.value })));
  s.value = value ?? "";
  s.addEventListener("change", () => onCommit(s.value));
  return s;
}
function checkGroup(all, chosen, onCommit) {
  return h("div", { class: "checks" }, ...all.map((v) => {
    const c = h("input", { type: "checkbox", value: v, checked: chosen.includes(v) });
    c.addEventListener("change", () => onCommit(all.filter((x) => (x === v ? c.checked : chosen.includes(x)))));
    return h("label", { class: "check" }, c, v);
  }));
}

P.renderInspector = function renderInspector() {
  const root = $("inspector");
  root.replaceChildren();
  if (!S2.work) return;
  const tabs = h("div", { class: "tabs", role: "tablist" },
    h("button", { type: "button", role: "tab", "aria-selected": String(S2.tab === "inspector"), textContent: "Inspector", onclick: () => { S2.tab = "inspector"; P.renderInspector(); } }),
    h("button", { type: "button", role: "tab", "aria-selected": String(S2.tab === "changes"), textContent: `Staged changes (${S2.pos})`, onclick: () => { S2.tab = "changes"; P.renderInspector(); } }));
  root.append(tabs);
  if (S2.tab === "changes") return root.append(changesPanel());
  const sel = S2.sel;
  if (!sel) return root.append(overviewPanel());
  if (sel.type === "node") {
    const n = S2.model.nodes.get(sel.id);
    return n ? root.append(...nodePanel(n)) : root.append(overviewPanel());
  }
  if (sel.type === "edge") {
    const e = S2.model.edges.get(sel.id);
    return e ? root.append(...edgePanel(e)) : root.append(overviewPanel());
  }
};

function overviewPanel() {
  const g = S2.work.graph;
  const count = (k) => g.nodes.filter((n) => n.kind === k).length;
  const errs = Object.entries(S2.work.issues).filter(([, v]) => v.errors.length);
  return h("div", {}, h("h2", { textContent: "Canvas" }),
    h("dl", {}, ...[["agents", count("agent")], ["topics", count("topic")], ["sources", count("source")], ["sinks", count("sink")]]
      .flatMap(([k, v]) => [h("dt", { textContent: k }), h("dd", { textContent: String(v) })])),
    errs.length ? h("div", {}, h("h2", { textContent: "Errors" }), h("ul", { class: "issues" }, ...errs.map(([k, v]) => h("li", { class: "err" },
      h("button", { class: "link", type: "button", textContent: k.replace(":", " "), onclick: () => { const [kind, name] = k.split(":"); const nd = [...S2.model.nodes.values()].find((x) => x.name === name && (KINDDIR[x.kind] === kind)); if (nd) { C.select({ type: "node", id: nd.id }); C.centerOn(nd.id); } } }),
      `: ${v.errors[0].message}`)))) : h("p", { class: "ok", textContent: "No validation errors." }),
    h("p", { class: "hint", textContent: "Select a node or edge to inspect it. Edits are staged and only reach config/ when you deploy." }));
}

function head(n, kindText) {
  return [h("div", { class: "kind", textContent: kindText }), h("h1", { textContent: n.name })];
}

function nodePanel(n) {
  const out = [...head(n, C.KIND_LABEL[n.kind])];
  const iss = C.issuesFor(n);
  const il = issueList(iss);
  if (il) out.push(il);
  const E2 = C.edit;
  const data = E2.entryOf(n);
  if (!data) return out;
  const patch = (fn, label, merge) => E2.patch(n, fn, label, merge);
  if (n.kind === "agent") out.push(...agentFields(n, data, patch));
  else if (n.kind === "topic") out.push(...topicFields(n, data, patch));
  else if (n.kind === "source") out.push(...sourceFields(n, data, patch));
  else if (n.kind === "sink") out.push(...sinkFields(n, data, patch));
  out.push(h("div", { class: "toolbar" }, h("button", { class: "ghost danger", type: "button", textContent: `Remove ${n.name}`, onclick: () => E2.deleteNode(n) })));
  if (S2.reach && (n.kind === "source" || n.kind === "sink")) out.push(reachSection(n));
  return out;
}

function reachSection(n) {
  const rows = S2.work.reach.filter((r) => (n.kind === "sink" ? r.to : r.from) === n.name);
  return h("div", {}, h("h2", { textContent: n.kind === "sink" ? "Sources that can reach this sink" : "Sinks this source can reach" }),
    rows.length ? h("ul", {}, ...rows.map((r) => h("li", {}, `${r.from} → ${r.to}`, r.free_text.length ? h("div", { class: "hint", textContent: `free-text channels: ${r.free_text.join(", ")}` }) : null)))
      : h("p", { class: "muted", textContent: "No path." }));
}

function agentFields(n, d, patch) {
  const out = [];
  out.push(fieldBox("Description", textInput(d.description, (v) => patch((x) => { if (v) x.description = v; else delete x.description; }, `${n.name} description`, "description"))));
  const ins = h("textarea", { rows: 7, "aria-label": "Instructions", value: d.instructions ?? "", maxLength: 4000 });
  ins.addEventListener("change", () => patch((x) => { if (ins.value.trim()) x.instructions = ins.value; else delete x.instructions; }, `${n.name} instructions edited`, "instructions"));
  out.push(fieldBox("Instructions", ins, "What this agent does with each event, in plain words. The built-in agent (agents-src/llm_agent) follows it; custom code ignores it."));
  const conc = d.concurrency ?? 1;
  const step = (delta) => patch((x) => { x.concurrency = Math.max(1, Math.min(64, (x.concurrency ?? 1) + delta)); }, `${n.name} concurrency ${conc} → ${Math.max(1, Math.min(64, conc + delta))}`, "concurrency");
  out.push(fieldBox("Concurrency (sandboxes at once)", h("div", { class: "stepper" },
    h("button", { class: "ghost", type: "button", "aria-label": "Decrease concurrency", textContent: "−", onclick: () => step(-1) }),
    h("output", { id: "conc-out", textContent: String(conc) }),
    h("button", { class: "ghost", type: "button", "aria-label": "Increase concurrency", textContent: "+", onclick: () => step(1) })),
  "Per-event agents run up to this many events in parallel; it is a cap, not N always-on copies."));
  const topic = d.trigger && S2.work.inventory.topics[d.trigger.topic];
  if (topic && topic.key && (topic.partitions || 1) < conc) {
    out.push(h("p", { class: "hint warn-text" }, `${d.trigger.topic} is keyed by '${topic.key}' with ${topic.partitions || 1} partition(s), so per-key ordering limits real parallelism. `,
      h("button", { class: "link", type: "button", textContent: `Raise partitions to ${conc}`, onclick: () => C.edit.patchEntry("topic", d.trigger.topic, (x) => { x.partitions = conc; }, `${d.trigger.topic} partitions → ${conc}`) })));
  }
  const maxTok = (d.inference || {}).max_tokens_per_event ?? 20000;
  out.push(h("p", { class: "hint", textContent: `Peak inference load: ${conc} × ${maxTok.toLocaleString("en-US")} = ${(conc * maxTok).toLocaleString("en-US")} tokens in flight.` }));
  out.push(fieldBox("Models", checkGroup(S2.work.inventory.models, (d.inference || {}).models || [], (chosen) => patch((x) => { x.inference = x.inference || {}; if (chosen.length) x.inference.models = chosen; else delete x.inference.models; }, `${n.name} models: ${chosen.join(", ") || "none"}`))));
  out.push(fieldBox("Max tokens per event", textInput(maxTok, (v) => patch((x) => { x.inference = x.inference || {}; x.inference.max_tokens_per_event = v; }, `${n.name} max tokens → ${v}`, "tokens"), { type: "number", min: 1 })));
  const sb = d.sandbox || {};
  out.push(h("fieldset", {}, h("legend", { textContent: "Sandbox" }),
    fieldBox("CPUs", textInput(sb.cpus, (v) => patch((x) => { x.sandbox = x.sandbox || {}; x.sandbox.cpus = v; }, `${n.name} cpus → ${v}`, "cpus"), { type: "number", min: 0.1 })),
    fieldBox("Memory (MB)", textInput(sb.memory_mb, (v) => patch((x) => { x.sandbox = x.sandbox || {}; x.sandbox.memory_mb = v; }, `${n.name} memory → ${v} MB`, "mem"), { type: "number", min: 64 })),
    fieldBox("Timeout per event (s)", textInput(sb.timeout_per_event_s, (v) => patch((x) => { x.sandbox = x.sandbox || {}; x.sandbox.timeout_per_event_s = v; }, `${n.name} timeout → ${v}s`, "timeout"), { type: "number", min: 1 }))));
  out.push(fieldBox("Retries before dead-letter", textInput((d.delivery || {}).max_retries, (v) => patch((x) => { x.delivery = x.delivery || {}; x.delivery.max_retries = v; }, `${n.name} retries → ${v}`, "retries"), { type: "number", min: 0, max: 10 })));
  out.push(h("dl", {}, h("dt", { textContent: "isolation" }), h("dd", { textContent: `${d.isolation || "per_event"} (long_running isn't available yet)` }),
    h("dt", { textContent: d.image ? "image" : "build" }), h("dd", { textContent: d.image || d.build || "none" }),
    h("dt", { textContent: "reads topic" }), h("dd", { textContent: (d.trigger || {}).topic || "nothing" })));
  return out;
}

function schemaSummary(schema) {
  const props = (schema || {}).properties || {};
  const req = new Set((schema || {}).required || []);
  return Object.entries(props).map(([k, v]) => {
    const kind = v["x-free-text"] ? `free text ≤ ${v.maxLength}` : v.enum ? `enum ${v.enum.join("|")}` : v.pattern ? `string /${v.pattern}/` : v.type + (v.minimum !== undefined || v.maximum !== undefined ? ` ${v.minimum ?? ""}..${v.maximum ?? ""}` : "");
    return { k, kind, required: req.has(k), free: !!v["x-free-text"] };
  });
}

function topicFields(n, d, patch) {
  const out = [];
  out.push(fieldBox("Description", textInput(d.description, (v) => patch((x) => { if (v) x.description = v; else delete x.description; }, `${n.name} description`, "description"))));
  out.push(fieldBox("Retention", textInput(d.retention, (v) => patch((x) => { x.retention = v; }, `${n.name} retention → ${v}`, "retention"), {}), "e.g. 72h or 7d. This is also the replay window."));
  out.push(fieldBox("Partitions", textInput(d.partitions ?? 1, (v) => patch((x) => { x.partitions = v; }, `${n.name} partitions → ${v}`, "partitions"), { type: "number", min: 1, max: 64 })));
  const props = Object.keys((d.schema || {}).properties || {});
  out.push(fieldBox("Partition key", selectInput(d.key || "", [{ value: "", label: "(none)" }, ...props.map((p) => ({ value: p }))], (v) => patch((x) => { if (v) x.key = v; else delete x.key; }, `${n.name} key → ${v || "none"}`, "key"))));
  out.push(h("h2", { textContent: "Schema" }));
  const rows = schemaSummary(d.schema);
  out.push(rows.length ? h("dl", {}, ...rows.flatMap((r) => [h("dt", { textContent: r.k + (r.required ? " *" : "") }), h("dd", { textContent: r.kind }, h("button", { class: "link", type: "button", textContent: "  remove", onclick: () => patch((x) => { delete x.schema.properties[r.k]; x.schema.required = (x.schema.required || []).filter((q) => q !== r.k); }, `${n.name} removed field '${r.k}'`) }))]))
    : h("p", { class: "muted", textContent: "No fields yet." }));
  out.push(addFieldForm(n, patch));
  const raw = h("textarea", { rows: 8, spellcheck: false, value: JSON.stringify(d.schema, null, 2), "aria-label": "Schema JSON" });
  const msg = h("p", { class: "field-note", hidden: true });
  out.push(h("details", {}, h("summary", { textContent: "Raw JSON schema" }), raw, msg,
    h("button", { class: "ghost", type: "button", textContent: "Apply JSON", onclick: () => {
      try { const s = JSON.parse(raw.value); msg.hidden = true; patch((x) => { x.schema = s; }, `${n.name} schema edited (JSON)`); } catch (e) { msg.textContent = `Not valid JSON: ${e.message}`; msg.hidden = false; }
    } })));
  return out;
}

const fieldDrafts = {};   // a half-filled "Add a field" form survives the inspector being redrawn (refresh, tab focus)

function addFieldForm(n, patch) {
  const draft = (fieldDrafts[n.id] ||= { name: "", type: "enum", detail: "", required: true });
  const name = h("input", { type: "text", "aria-label": "Field name", placeholder: "field_name", value: draft.name });
  const type = h("select", { "aria-label": "Field type" }, ...[["enum", "enum"], ["pattern", "string with pattern"], ["id", "ID"], ["integer", "integer (min,max)"], ["number", "number (min,max)"], ["free", "free text (max length)"]]
    .map(([v, l]) => h("option", { value: v, textContent: l })));
  type.value = draft.type;
  const detail = h("input", { type: "text", "aria-label": "Field detail", placeholder: "a,b,c", value: draft.detail });
  const hints = { enum: "values, comma-separated", pattern: "regular expression, e.g. ^[a-z]{1,32}$", id: "", integer: "min,max", number: "min,max", free: "max length (default 280)" };
  const required = h("input", { type: "checkbox", checked: draft.required });
  const err = h("p", { class: "field-note", hidden: true });
  const upd = () => { detail.placeholder = hints[type.value]; detail.disabled = type.value === "id"; };
  const remember = () => { Object.assign(draft, { name: name.value, type: type.value, detail: detail.value, required: required.checked }); };
  for (const el_ of [name, type, detail, required]) el_.addEventListener(el_ === type || el_ === required ? "change" : "input", remember);
  type.addEventListener("change", upd); upd();
  const add = () => {
    err.hidden = true;
    const k = name.value.trim();
    if (!/^[a-z][a-z0-9_]*$/.test(k)) { err.textContent = "Field names: lowercase letters, digits, underscore."; err.hidden = false; return; }
    let schema;
    const t = type.value, d = detail.value.trim();
    if (t === "enum") { const v = d.split(",").map((s) => s.trim()).filter(Boolean); if (!v.length) { err.textContent = "Give at least one value."; err.hidden = false; return; } schema = { type: "string", enum: v }; }
    else if (t === "pattern") { try { new RegExp(d); } catch { err.textContent = "That isn't a valid pattern."; err.hidden = false; return; } if (!d) { err.textContent = "A pattern is required."; err.hidden = false; return; } schema = { type: "string", pattern: d }; }
    else if (t === "id") schema = { type: "string", pattern: "^[A-Za-z0-9_-]{1,64}$" };
    else if (t === "free") { const m = d === "" ? 280 : Number(d); if (!Number.isInteger(m) || m < 1 || m > 100000) { err.textContent = "Max length must be a whole number from 1 to 100000."; err.hidden = false; return; } schema = { type: "string", maxLength: m, "x-free-text": true }; }
    else { const [lo, hi] = d.split(",").map((s) => s.trim()); schema = { type: t }; if (lo !== undefined && lo !== "") schema.minimum = Number(lo); if (hi !== undefined && hi !== "") schema.maximum = Number(hi); if ([schema.minimum, schema.maximum].some(Number.isNaN)) { err.textContent = "Use numbers, like 1,10."; err.hidden = false; return; } }
    patch((x) => {
      x.schema = x.schema || { type: "object", additionalProperties: false, required: [], properties: {} };
      if (x.schema.properties[k]) { err.textContent = `Field '${k}' exists.`; err.hidden = false; return; }
      delete fieldDrafts[n.id];                                  // added: the next form starts clean
      x.schema.properties[k] = schema;
      x.schema.additionalProperties = false;
      if (required.checked) x.schema.required = [...(x.schema.required || []), k];
    }, `${n.name} added field '${k}'${t === "free" ? " (free text)" : ""}`);
  };
  return h("details", { open: true }, h("summary", { textContent: "Add a field" }),
    h("div", { class: "row" }, name, type), h("div", { class: "row" }, detail), h("label", { class: "check" }, required, " required"), err,
    h("div", { class: "toolbar" }, h("button", { class: "ghost", type: "button", textContent: "Add field", onclick: add })),
    h("p", { class: "hint", textContent: "Free text always gets a max length and is marked x-free-text. Objects stay closed (additionalProperties: false)." }));
}

function jsonField(label, value, onCommit, hint) {
  const t = h("textarea", { rows: 4, spellcheck: false, value: value === undefined ? "" : JSON.stringify(value, null, 2), "aria-label": label });
  const msg = h("p", { class: "field-note", hidden: true });
  t.addEventListener("change", () => {
    try { const v = t.value.trim() ? JSON.parse(t.value) : undefined; msg.hidden = true; onCommit(v); } catch (e) { msg.textContent = `Not valid JSON: ${e.message}`; msg.hidden = false; }
  });
  return h("div", { class: "field" }, h("label", { textContent: label }), hint ? h("p", { class: "hint", textContent: hint }) : null, t, msg);
}

function sourceFields(n, d, patch) {
  const set = (key, label) => (v) => patch((x) => { if (v === "" || v === undefined) delete x[key]; else x[key] = v; }, `${n.name} ${label} → ${v}`, key);
  return [h("p", { class: "hint", textContent: "Polls a public API on a schedule and publishes each new item to the topic. Items already sent are skipped." }),
    fieldBox("URL", textInput(d.url, set("url", "url"), {}), "https only, public API (no key)."),
    fieldBox("Every", textInput(d.interval, set("interval", "interval"), {}), "30s, 10m, 1h."),
    fieldBox("List is at", textInput(d.items_path, (v) => patch((x) => { x.items_path = v; }, `${n.name} list path → ${v}`, "items_path"), {}), "Where the list of items is in the response, e.g. hits. Empty if the response is the list."),
    fieldBox("Item id field", textInput(d.id_field, set("id_field", "id field"), {}), "The item field that is unique, e.g. objectID."),
    fieldBox("Max items per poll", textInput(d.limit, set("limit", "limit"), { type: "number", min: 1, max: 500 })),
    fieldBox("Timeout", textInput(d.timeout, set("timeout", "timeout"), {}), "e.g. 10s."),
    fieldBox("Max response bytes", textInput(d.max_response_bytes, set("max_response_bytes", "max response"), { type: "number", min: 1 })),
    jsonField("Field map (topic field: item field)", d.map, (v) => patch((x) => { x.map = v || {}; }, `${n.name} field map edited`), "Each topic field gets the value at that path in the item. Missing values are left out."),
    h("dl", {}, h("dt", { textContent: "publishes" }), h("dd", { textContent: d.topic || "nothing yet" }))];
}
function sinkFields(n, d, patch) {
  const set = (key, label) => (v) => patch((x) => { if (v === "" || v === undefined) delete x[key]; else x[key] = v; }, `${n.name} ${label} → ${v}`, key);
  const out = [h("p", { class: "hint", textContent: d.type === "http_post" ? "POSTs each event's payload as JSON, once per event id. Failures park the event on dlq." : "Appends each event on a topic to a JSONL file, once per event id." })];
  if (d.type === "http_post") {
    out.push(fieldBox("URL", textInput(d.url, set("url", "url"), {}), "https only. The data on this topic leaves the machine."),
      fieldBox("Credential name", textInput(d.credential, (v) => patch((x) => { if (v) { x.credential = v; x.auth = x.auth || { type: "bearer" }; } else { delete x.credential; delete x.auth; } }, `${n.name} credential`, "credential"), {}),
        "A file in secrets/. Never the secret itself."),
      fieldBox("Rate", textInput(d.rate, set("rate", "rate"), {}), "e.g. 30/min."),
      fieldBox("Timeout", textInput(d.timeout, set("timeout", "timeout"), {}), "e.g. 10s."),
      fieldBox("Retries", textInput(d.max_retries, set("max_retries", "retries"), { type: "number", min: 0, max: 10 })));
  } else {
    out.push(fieldBox("File", textInput(d.path, (v) => patch((x) => { x.path = v; }, `${n.name} path → ${v}`, "path"), {}), "Under data/, ending .jsonl. {field} is replaced by that payload field."));
  }
  out.push(h("dl", {}, h("dt", { textContent: "reads" }), h("dd", { textContent: d.topic || "nothing yet" })));
  return out;
}

function edgePanel(e) {
  const f = S2.model.nodes.get(e.from), t = S2.model.nodes.get(e.to);
  const desc = { trigger: `${t.name} reads ${f.name}`, writes: `${f.name} may write ${t.name}`, publishes: `${f.name} publishes to ${t.name}`,
    consumes: `${t.name} reads ${f.name}` }[e.type];
  const out = [h("div", { class: "kind", textContent: `${e.type} edge` }), h("h1", { textContent: desc }), h("p", { class: "hint", textContent: "This edge is one manifest field. Removing it removes the field." })];
  if (e.type === "trigger") {
    const a = C.edit.entryOf(t);
    out.push(fieldBox("Filter", textInput((a.trigger || {}).filter, (v) => C.edit.patch(t, (x) => { if (v) x.trigger.filter = v; else delete x.trigger.filter; }, `${t.name} filter ${v ? `→ ${v}` : "removed"}`, "filter")),
      "e.g. payload.urgency == 'high'. Events that don't match are skipped. Filter preview needs sample events from the broker, which this version does not serve."));
    const il = issueList(C.issuesFor(t));
    if (il) out.push(il);
  }
  out.push(h("div", { class: "toolbar" }, h("button", { class: "ghost danger", type: "button", textContent: "Remove edge", onclick: () => { C.select(null); C.edit.deleteEdge(e); } })));
  return out;
}

// ---------------------------------------------------------------- staged changes (§7.1)
function changesPanel() {
  const box = h("div", {});
  if (!S2.ops.length) return h("p", { class: "muted", textContent: "No staged changes. Everything shown is what's deployed." });
  box.append(h("ul", { id: "changes-list" }, ...S2.ops.map((op, i) => {
    const undone = i >= S2.pos;
    return h("li", { class: undone ? "muted" : "" },
      h("span", {}, h("span", { class: "grp", textContent: `${op.files.map((f) => `${f.kind}/${f.name}`).join(", ")} ` }), h("br"), `${undone ? "(undone) " : ""}${op.label}`),
      undone ? null : h("button", { class: "link", type: "button", textContent: "Revert", disabled: !C.canRevert(i), title: C.canRevert(i) ? "" : "A later change depends on this one", onclick: () => C.revertAt(i) }));
  })));
  return box;
}

// ---------------------------------------------------------------- discard, banners, conflicts
C.confirmDiscard = () => {
  C.openModal(h("div", {}, h("h2", { textContent: "Discard all staged changes?" }), h("p", { textContent: `${S2.pos} change${S2.pos === 1 ? "" : "s"} will be dropped. Nothing was written to config/, so nothing on disk changes.` }),
    h("div", { class: "row end" }, h("button", { class: "ghost", type: "button", textContent: "Keep them", onclick: C.closeModal }),
      h("button", { class: "primary", type: "button", textContent: "Discard all", onclick: () => { C.closeModal(); C.discard(); } }))), { title: "Discard" });
};

P.checkConflicts = function checkConflicts() {
  const cur = (S2.work && S2.work.hashes) || {};
  const bad = new Set();
  for (const op of C.active()) for (const f of op.files) { const k = `${f.kind}/${f.name}`; if (op.base && k in op.base && (cur[k] ?? "") !== op.base[k]) bad.add(k); }
  S2.conflicts = [...bad];
  const b = $("banner");
  if (!bad.size) { b.hidden = true; b.replaceChildren(); return; }
  b.hidden = false;
  b.className = "banner err";
  b.replaceChildren(h("strong", { textContent: "Config changed on disk." }), h("span", { textContent: `${[...bad].join(", ")} changed outside the canvas since you staged edits to it.` }),
    h("button", { class: "ghost", type: "button", textContent: "Keep my changes (overwrite)", onclick: () => {
      for (const op of S2.ops) for (const f of op.files) { const k = `${f.kind}/${f.name}`; if (bad.has(k)) op.base[k] = cur[k] ?? ""; }
      persist_(); P.checkConflicts();
    } }),
    h("button", { class: "ghost danger", type: "button", textContent: "Discard staged changes", onclick: () => C.discard().then(() => P.checkConflicts()) }));
};
const persist_ = () => C.api("PUT", "/api/canvas/staged", { ops: S2.ops, pos: S2.pos }).catch(() => {});

// ---------------------------------------------------------------- deploy review (§7.2, §7.3)
P.openDeploy = async function openDeploy() {
  if (S2.unavailable) {
    return C.openModal(h("div", {}, h("h2", { textContent: "Validation unavailable" }), h("p", { textContent: "The checker could not be reached, so deploying is blocked rather than guessing." }),
      h("div", { class: "row end" }, h("button", { class: "ghost", type: "button", textContent: "Close", onclick: C.closeModal }))), { title: "Validation unavailable" });
  }
  const errs = Object.entries(S2.work.issues).filter(([, v]) => v.errors.length);
  if (errs.length) {
    return C.openModal(h("div", {}, h("h2", { textContent: "Fix these before deploying" }),
      h("ul", { class: "issues" }, ...errs.flatMap(([k, v]) => v.errors.map((e) => h("li", { class: "err", textContent: `${k.replace(":", " ")}: ${e.path ? `${e.path}: ` : ""}${e.message}` })))),
      h("div", { class: "row end" }, h("button", { class: "ghost", type: "button", textContent: "Close", onclick: C.closeModal }))), { title: "Errors", wide: true });
  }
  let d;
  try { d = await C.api("POST", "/api/canvas/diff", { ops: C.active() }); } catch (e) { return C.toast(`Could not build the review: ${e.message}`); }
  const section = (title, rows) => (rows.length ? [h("h3", { textContent: title }), ...rows] : []);
  const row = (s, t, extra) => h("div", { class: "diff-row" }, sign(s), h("span", { textContent: t }), extra ? h("span", { class: "muted", textContent: `  ${extra}` }) : null);
  const ack = h("input", { type: "checkbox", id: "ack" });
  const review = h("div", {},
    ...section("Capability changes", d.capabilities.map((r) => row(r.sign, r.text))),
    ...section("Reachability changes", d.reachability.map((r) => row(r.sign, r.text, r.free_text.length ? `free-text: ${r.free_text.join(", ")}` : ""))),
    ...section("Schema changes", d.schema.map((r) => row(r.sign, r.text, r.breaking ? "BREAKING" : "backward compatible"))),
    ...section("Files", d.files.map((f) => h("div", { class: "diff-row" }, h("code", { textContent: `${f.code}  config/${f.path}` })))));
  const yaml = h("pre", { class: "yamldiff", hidden: true, textContent: d.yaml_diff || "No file changes." });
  const go = h("button", { class: "primary", type: "button", textContent: "Deploy", disabled: d.needs_ack || d.breaking });
  ack.addEventListener("change", () => { go.disabled = d.breaking || !ack.checked; });
  const note = h("p", { class: "muted", textContent: "Deploy writes these files to config/. If `harness run` is running, it swaps the new config in live: it finishes events already in flight, then restarts only the agents that changed." });
  const status = h("p", { class: "field-note", hidden: true, role: "alert" });
  go.addEventListener("click", async () => {
    go.disabled = true;
    try {
      const r = await C.api("POST", "/api/canvas/deploy", { ops: C.active(), ack: ack.checked });
      C.closeModal();
      S2.ops = []; S2.pos = 0; S2.conflicts = [];
      await C.api("DELETE", "/api/canvas/staged").catch(() => {});
      S2.base = null;
      await C.refresh();
      P.checkConflicts();
      C.toast(`Deployed ${r.written.length + r.deleted.length} file(s). Restart \`harness run\` to apply.`);
    } catch (e) {
      go.disabled = false;
      status.hidden = false;
      status.textContent = e.status === 409 && e.data && e.data.conflicts ? `Config changed on disk: ${e.data.conflicts.join(", ")}. Nothing was overwritten.` : e.message;
      if (e.status === 409 && e.data && e.data.conflicts) P.checkConflicts();
    }
  });
  const tabs = h("div", { class: "tabs", role: "tablist" },
    h("button", { type: "button", role: "tab", "aria-selected": "true", textContent: "Review", onclick: (ev) => { review.hidden = false; yaml.hidden = true; tabs.children[0].setAttribute("aria-selected", "true"); tabs.children[1].setAttribute("aria-selected", "false"); ev.preventDefault(); } }),
    h("button", { type: "button", role: "tab", "aria-selected": "false", textContent: "View YAML diff", onclick: () => { review.hidden = true; yaml.hidden = false; tabs.children[0].setAttribute("aria-selected", "false"); tabs.children[1].setAttribute("aria-selected", "true"); } }));
  const warn = d.breaking ? h("p", { class: "field-note", textContent: "A schema change breaks BACKWARD compatibility. Create a new .v2 topic instead; deploy is blocked." }) : null;
  const ackLine = d.needs_ack ? h("label", { class: "checkline" }, ack, h("span", { textContent: "I've reviewed the new reachability paths and free-text channels above." })) : null;
  C.openModal(h("div", {}, h("h2", { textContent: `Deploy ${S2.pos} staged change${S2.pos === 1 ? "" : "s"}` }), tabs, review, yaml, warn, ackLine, note, status,
    h("div", { class: "row end" }, h("button", { class: "ghost", type: "button", textContent: "Cancel", onclick: C.closeModal }), go)), { title: "Deploy review", wide: true });
};

// ---------------------------------------------------------------- search (Ctrl/Cmd+K)
P.openSearch = function openSearch() {
  if (!S2.model) return;
  const items = [...S2.model.nodes.values()].map((n) => ({ label: n.name, kind: C.KIND_LABEL[n.kind], sel: { type: "node", id: n.id } }))
;
  const input = h("input", { type: "text", placeholder: "Jump to a node", "aria-label": "Search" });
  const list = h("ul", { class: "jump-list" });
  const go = (it) => { C.closeModal(); C.select(it.sel); if (it.sel.type === "node") C.centerOn(it.sel.id); };
  const fill = () => {
    const q = input.value.trim().toLowerCase();
    const shown = items.filter((i) => i.label.toLowerCase().includes(q)).slice(0, 30);
    list.replaceChildren(...shown.map((i) => h("li", {}, h("button", { type: "button", onclick: () => go(i) }, h("code", { textContent: i.label }), `  ${i.kind}`))));
    fill.first = shown[0];
  };
  input.addEventListener("input", fill);
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && fill.first) go(fill.first); });
  fill();
  C.openModal(h("div", {}, input, list), { title: "Search" });
};

// the canvas should notice edits made behind its back when the tab regains focus (§9)
window.addEventListener("focus", async () => { if (S2.work) { await C.refresh(); P.checkConflicts(); } });
})();
