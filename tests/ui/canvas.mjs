// Drives the real canvas (canvas.html + its three scripts) in jsdom against a running server on a scratch config.
// Usage: BASE_URL=http://127.0.0.1:8788/ node tests/ui/canvas.mjs   (needs: npm ci in tests/ui)
import { JSDOM } from "jsdom";
const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8788/";
const html = await (await fetch(BASE + "canvas.html")).text();
const scripts = await Promise.all(["topicname.js", "canvas-core.js", "canvas-edit.js", "canvas-panels.js"].map(async (f) => (await fetch(BASE + f)).text()));
const dom = new JSDOM(html.replace(/<script[^>]*>[\s\S]*?<\/script>/g, ""), { url: BASE + "canvas.html", runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.fetch = (u, o) => fetch(new URL(u, BASE), o);
window.structuredClone = structuredClone;
window.eval(scripts.join("\n") + "\nwindow.C = C;");

const doc = window.document, C = window.C, S = C.S;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const settle = () => sleep(500);
const fail = [];
const check = (name, ok, extra = "") => { console.log(ok ? "PASS" : "FAIL", name, extra); if (!ok) fail.push(name); };
const ev = (type, init = {}) => new window.MouseEvent(type, { bubbles: true, cancelable: true, ...init });
const click = (el, init) => el.dispatchEvent(ev("click", init));
const change = (el, v) => { if (v !== undefined) el.value = v; el.dispatchEvent(new window.Event("change", { bubbles: true })); };
const node = (id) => doc.querySelector(`[data-node="${id}"]`);
const nodes = () => [...doc.querySelectorAll("[data-node]")].map((n) => n.dataset.node);
const toolbar = () => doc.getElementById("staged-count").textContent;

await C.boot();
await settle();

// ---- read-only graph (UI-M0)
check("renders the whole chain", nodes().includes("agent:summarizer") && nodes().includes("topic:news.story.v1") && nodes().includes("source:hn_source") && nodes().includes("sink:summaries_file"), nodes().length + " nodes");
check("agent is drawn once", nodes().filter((n) => n === "agent:summarizer").length === 1);
check("one graph: no pipeline frames", doc.querySelectorAll(".frame").length === 0 && !doc.getElementById("focus"));
check("agent shows its concurrency badge", node("agent:summarizer").textContent.includes("× 2"));
check("only four building blocks in the palette", [...doc.querySelectorAll("[data-new]")].map((b) => b.dataset.new).join() === "topic,agent,source,sink");
check("toolbar says whether the stack is live", doc.getElementById("live").textContent.includes("Not running"));
check("toolbar says up to date", toolbar() === "Up to date with deployed" && doc.getElementById("deploy").hidden);
check("no node is red on the shipped config", doc.querySelectorAll(".node.error").length === 0);

const insp = () => doc.getElementById("inspector");

// ---- the source and sink inspectors expose every field
click(node("source:hn_source")); await settle();
check("source inspector shows its URL and field map", [...insp().querySelectorAll("input")].some((i) => i.value.startsWith("https://hn.algolia.com")) && insp().textContent.includes("Field map"));
click(node("sink:summaries_file")); await settle();
check("sink inspector shows its file", [...insp().querySelectorAll("input")].some((i) => i.value === "data/results/summaries.jsonl"));

// ---- stage a change (U13, U6, U14, U15, U16)
click(node("agent:summarizer"));
await settle();
check("agent inspector shows the stepper", !!doc.getElementById("conc-out") && doc.getElementById("conc-out").textContent === "2");
const plus = [...insp().querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === "Increase concurrency");
click(plus); await settle(); click([...insp().querySelectorAll("button")].find((b) => b.getAttribute("aria-label") === "Increase concurrency")); await settle();
check("two clicks on the stepper merge into one staged change", S.ops.length === 1 && toolbar() === "1 staged change", toolbar());
check("badge shows 4", node("agent:summarizer").textContent.includes("× 4"));
check("modified node is marked", node("agent:summarizer").classList.contains("modified") || node("agent:summarizer").querySelector(".dot"));
const onDisk = await (await fetch(BASE + "api/agent/summarizer")).json();
check("config/ is untouched while staged", onDisk.concurrency === 2);
check("deploy and discard appear", !doc.getElementById("deploy").hidden && !doc.getElementById("discard").hidden);
check("staged list was saved to the server", (await (await fetch(BASE + "api/canvas/staged")).json()).ops.length === 1);

click(doc.getElementById("undo")); await settle();
check("undo returns to deployed", node("agent:summarizer").textContent.includes("× 2") && toolbar() === "Up to date with deployed", toolbar());
click(doc.getElementById("redo")); await settle();
check("redo restores the edit", node("agent:summarizer").textContent.includes("× 4"));

// ---- invalid edge: topic -> topic is rejected before anything is staged
C.tryConnect("topic:news.story.v1", "topic:news.summary.v1"); await settle();
check("topic→topic is rejected, nothing staged", S.ops.length === 1 && doc.getElementById("toast").textContent.includes("Topics connect through"));

// ---- a valid edge: the summarizer may also write news.story.v1, then undo it
C.tryConnect("agent:summarizer", "topic:news.story.v1"); await settle();
check("adding a write edge stages a change", S.ops.length === 2);
check("a new edge is drawn and marked", !!doc.querySelector('[data-edge="writes:agent:summarizer>topic:news.story.v1"].added'));
click(doc.getElementById("undo")); await settle();
check("undo removed it", S.ops.length === 2 && S.pos === 1);

// ---- staged panel lists the change, deploy review shows the capability diff
click([...insp().querySelectorAll(".tabs button")].find((b) => b.textContent.startsWith("Staged changes"))); await settle();
check("staged panel lists the concurrency change", insp().textContent.includes("concurrency"));
click(doc.getElementById("deploy")); await settle();
const modal = () => doc.querySelector("#modal-root .modal");
check("deploy review opens with capability changes", modal()?.textContent.includes("summarizer  concurrency  2 → 4"), modal()?.textContent.slice(0, 160));
check("review lists the file", modal().textContent.includes("M  config/agents/summarizer.yaml"));
click(doc.querySelector("#modal-root button.ghost")); // Cancel
check("cancel keeps changes staged", S.pos === 1 && !modal());

// ---- discard (U16)
click(doc.getElementById("discard")); await settle();
click([...doc.querySelectorAll("#modal-root button")].find((b) => b.textContent === "Discard all")); await settle();
check("discard returns to deployed and writes nothing", S.ops.length === 0 && toolbar() === "Up to date with deployed" && node("agent:summarizer").textContent.includes("× 2"));
check("server staged list cleared", (await (await fetch(BASE + "api/canvas/staged")).json()).ops.length === 0);

// ---- reachability overlay (UI-M3 subset)
click(doc.getElementById("reach-toggle")); await settle();
click(node("sink:summaries_file")); await settle();
check("selecting a sink highlights everything that feeds it", doc.querySelectorAll(".node.hl").length === 5 && doc.querySelectorAll(".node.dim").length === 0, String(doc.querySelectorAll(".node.hl").length));
check("inspector lists the paths", insp().textContent.includes("hn_source → summaries_file"));
check("summary chip appears", doc.getElementById("reach-summary").textContent.includes("free-text"));
click(doc.getElementById("reach-toggle"));

// ---- untrusted text is never markup (U10, spirit)
C.stage("evil", [{ kind: "agent", name: "summarizer", data: { ...(await (await fetch(BASE + "api/agent/summarizer")).json()), description: "<img src=x onerror=window.__pwned=1><script>window.__pwned=1</script>" } }]);
await settle(); click(node("agent:summarizer")); await settle();
check("markup in a description stays text", !window.__pwned && !doc.querySelector("#inspector img, #inspector script") && doc.querySelector("#inspector input").value.includes("<img"));
await C.discard(); await settle();

// ---- search
C.panels.openSearch();
const q = doc.querySelector("#modal-root input"); q.value = "summarizer"; q.dispatchEvent(new window.Event("input", { bubbles: true }));
check("search finds a node", [...doc.querySelectorAll("#modal-root .jump-list button")].some((b) => b.textContent.includes("summarizer")));
q.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await settle();
check("enter selects it", S.sel && S.sel.id === "agent:summarizer");

// ---- creating a topic and an agent from the palette, then deploying (U1-ish)
doc.querySelector('[data-new="topic"]').click();
doc.getElementById("ask-name").value = "demo.note.v1";
doc.querySelector("#modal-root form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await settle();
check("new topic appears and is staged", nodes().includes("topic:demo.note.v1") && S.ops.length === 1);
check("new topic is valid", !node("topic:demo.note.v1").classList.contains("error"));
doc.querySelector('[data-new="topic"]').click();
doc.getElementById("ask-name").value = "!!!";
doc.querySelector("#modal-root form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
check("a name with no letters or digits is refused in the dialog", doc.querySelector("#modal-root .field-note:not([hidden])")?.textContent.includes("at least one letter"));
doc.getElementById("ask-name").value = "News stories!";
doc.getElementById("ask-name").dispatchEvent(new window.Event("change", { bubbles: true }));
check("the dialog shows the name free text will be saved as", doc.querySelector("#hint-name")?.textContent === "Saved as news_stories.v1", doc.querySelector("#hint-name")?.textContent);
doc.querySelector("#modal-root form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await settle();
check("free text becomes a valid topic name", nodes().includes("topic:news_stories.v1") && S.ops.length === 2);
click(doc.getElementById("undo")); await settle();
check("and it can be undone", !nodes().includes("topic:news_stories.v1") && S.pos === 1);

click(node("topic:demo.note.v1")); await settle();
const fieldName = insp().querySelector("input[aria-label='Field name']");
fieldName.value = "note"; const kind = insp().querySelector("select[aria-label='Field type']"); kind.value = "free"; change(kind);
insp().querySelector("input[aria-label='Field detail']").value = "";
// a redraw of the inspector (runtime poll, refresh) must not reset the half-filled form back to "enum"
C.panels.update(); C.panels.updateLive();
check("a half-filled add-field form survives a redraw", insp().querySelector("select[aria-label='Field type']").value === "free" && insp().querySelector("input[aria-label='Field name']").value === "note");
click([...insp().querySelectorAll("button")].find((b) => b.textContent === "Add field")); await settle();
const t = await (async () => (S.work.inventory.topics["demo.note.v1"]))();
check("a free-text field always gets a maxLength and the free-text marker", t.schema.properties.note["x-free-text"] === true && t.schema.properties.note.maxLength === 280, JSON.stringify(t.schema.properties.note));
check("schema stays closed", t.schema.additionalProperties === false);

// deploy it for real on the scratch config
click(doc.getElementById("deploy")); await settle();
const needsAck = doc.getElementById("ack");
if (needsAck) { needsAck.checked = true; needsAck.dispatchEvent(new window.Event("change", { bubbles: true })); }
const go = [...doc.querySelectorAll("#modal-root button.primary")].find((b) => b.textContent === "Deploy");
check("deploy button is enabled", go && !go.disabled);
click(go); await settle(); await settle();
check("deployed: staged cleared and the topic is on disk", S.ops.length === 0 && (await fetch(BASE + "api/topic/demo.note.v1")).status === 200);
check("toolbar back to up to date", toolbar() === "Up to date with deployed");

// ---- a new agent from the palette carries its instructions; agents only talk through topics
doc.querySelector('[data-new="agent"]').click();
doc.getElementById("ask-name").value = "tagger";
doc.getElementById("ask-instructions").value = "Tag each summary.";
doc.querySelector("#modal-root form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true })); await settle();
const tagger = S.work.inventory.entries.agent.tagger;
check("new agent uses the built-in LLM agent and keeps its instructions", tagger && tagger.build === "agents-src/llm_agent" && tagger.instructions === "Tag each summary.", JSON.stringify(tagger));
C.tryConnect("agent:summarizer", "agent:tagger"); await settle();
check("agent→agent offers 'insert topic between'", doc.querySelector("#modal-root .modal h2")?.textContent.includes("Insert a topic"));
doc.querySelector("#modal-root button[type=button]").click();
click(node("agent:tagger")); await settle();
check("the inspector shows the instructions", insp().querySelector("textarea[aria-label='Instructions']")?.value === "Tag each summary.");
await C.discard(); await settle();

// ---- validation unavailable blocks deploy (U9)
C.stage("x", [{ kind: "agent", name: "summarizer", data: { ...(await (await fetch(BASE + "api/agent/summarizer")).json()), concurrency: 3 } }]); await settle();
S.unavailable = true; C.panels.update();
check("toolbar says validation unavailable", toolbar().includes("Validation unavailable"));
click(doc.getElementById("deploy")); await settle();
check("deploy is blocked, not attempted", modal()?.textContent.includes("deploying is blocked"));
doc.querySelector("#modal-root button").click();
S.unavailable = false;

// ---- external edit (U7): file changes behind the canvas's back
await fetch(BASE + "api/agent/summarizer", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ ...(await (await fetch(BASE + "api/agent/summarizer")).json()), description: "edited elsewhere" }) });
await C.refresh(); C.panels.checkConflicts();
check("banner announces the external edit", !doc.getElementById("banner").hidden && doc.getElementById("banner").textContent.includes("Config changed on disk"));
click(doc.getElementById("deploy")); await settle();
const go2 = [...doc.querySelectorAll("#modal-root button.primary")].find((b) => b.textContent === "Deploy");
click(go2); await settle(); await settle();
check("deploy refuses to overwrite it", (await (await fetch(BASE + "api/agent/summarizer")).json()).description === "edited elsewhere" && S.ops.length === 1);

console.log(fail.length ? `\n${fail.length} FAILED: ${fail.join("; ")}` : "\nall canvas checks passed");
process.exit(fail.length ? 1 : 0);
