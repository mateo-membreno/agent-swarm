// Drives the real UI (index.html + app.js) in jsdom against a running server and checks the
// create-by dropdown, field swapping and save flow.
// Usage: BASE_URL=http://127.0.0.1:8787/ node tests/ui/drive.mjs   (needs: npm ci in tests/ui)
import { JSDOM } from "jsdom";
const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8787/";
const html = await (await fetch(BASE)).text();
const js = (await (await fetch(BASE + "topicname.js")).text()) + "\n" + (await (await fetch(BASE + "app.js")).text());
const dom = new JSDOM(html.replace(/<script[^>]*><\/script>/g, ""), { url: BASE, runScripts: "outside-only", pretendToBeVisual: true });
const { window } = dom;
window.fetch = (u, o) => fetch(new URL(u, BASE), o);
window.structuredClone = structuredClone;
window.eval(js);

const doc = window.document;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const settle = () => sleep(600);
const fail = [];
const check = (name, ok, extra = "") => { console.log(ok ? "PASS" : "FAIL", name, extra); if (!ok) fail.push(name); };
const visible = (sel) => [...doc.querySelectorAll(sel)].filter((n) => !n.closest("[hidden]"));
const labelsShown = () => visible("#editor .field > label").map((l) => l.textContent.replace(" *", "").trim());
const click = (el) => el.dispatchEvent(new window.Event("click", { bubbles: true }));
const choose = (sel, v) => { sel.value = v; sel.dispatchEvent(new window.Event("change", { bubbles: true })); };
const type = (el, v) => { el.value = v; el.dispatchEvent(new window.Event("input", { bubbles: true })); };

await settle();
check("overview renders", doc.body.dataset.view === "overview" && doc.querySelector("#dashboard table"));
const nav = [...doc.querySelectorAll("#kinds button")].map((b) => b.textContent);
check("nav lists every kind", nav.join(",") === "Overview,Models,Topics,Agents,Sources,Sinks", nav.join(","));
check("current nav item is marked", doc.querySelector('#kinds [aria-current="page"]')?.textContent === "Overview");

// Models: one kind covers local and hosted; the list offers a create dropdown, not a big form
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Models"));
await settle();
const create = doc.querySelector("#list select.create");
check("models page has a create dropdown", !!create);
const opts = [...create.options].map((o) => o.textContent).join("|");
check("dropdown offers local, Anthropic, other hosted", opts === "New model…|Local (runs on this machine)|Anthropic (Claude, hosted)|Hosted, OpenAI-compatible", opts);
check("existing models are listed by their real ids", [...doc.querySelectorAll("#list button.item")].map((b) => b.textContent.trim()).sort().join(",") === "claude-sonnet-5-5,qwen2.5-1.5b-q4");

choose(create, "anthropic");
await settle();
const hostedLabels = labelsShown();
check("hosted form shows hosted fields", ["credential", "daily_spend_cap_usd", "provider", "model"].every((l) => hostedLabels.includes(l)), hostedLabels.join(","));
check("hosted form hides local-only fields", !hostedLabels.includes("endpoint"), hostedLabels.join(","));
check("save disabled while invalid", doc.getElementById("save").disabled === true);

const provSel = [...doc.querySelectorAll("#editor select")].find((s) => [...s.options].some((o) => o.value === "local"));
choose(provSel, "local");
await settle();
const localLabels = labelsShown();
check("switching provider swaps fields", localLabels.includes("endpoint") && !localLabels.includes("credential"), localLabels.join(","));

type(doc.querySelector('[data-path="name"] input'), "test-local-model");
type(doc.querySelector('[data-path="model"] input'), "/models/test.gguf");
type(doc.querySelector('[data-path="endpoint"] input'), "http://inference-cpu:8080/v1");
await settle();
check("valid local model enables save", doc.getElementById("save").disabled === false, doc.getElementById("issues").textContent.slice(0, 120));
click(doc.getElementById("save"));
await settle();
const names = (await (await fetch(BASE + "api/model")).json()).names;
check("model was saved", names.includes("test-local-model"), names.join(","));

// Topics: free text in the name box is saved as a valid topic name
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Topics"));
await settle();
click([...doc.querySelectorAll("#list button")].find((b) => b.textContent === "New entry"));
await settle();
type(doc.querySelector('[data-path="name"] input'), "News stories!");
await settle();
check("topic name free text is shown as the name it will be saved as", doc.querySelector('[data-path="name"] .name-note')?.textContent.includes("Saved as news_stories.v1"), doc.querySelector('[data-path="name"]')?.textContent);
check("and the preview accepts it (no name error)", !/invalid name/.test(doc.getElementById("issues").textContent), doc.getElementById("issues").textContent.slice(0, 120));

// Agents: references are pickers, and opening an entry must not make it look edited
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Agents"));
await settle();
click([...doc.querySelectorAll("#list button.item")].find((b) => b.textContent.trim() === "summarizer"));
await settle();
const topicSel = doc.querySelector('[data-path="trigger.topic"] select');
check("agent trigger topic is a dropdown of real topics", topicSel && [...topicSel.options].some((o) => o.value === "news.story.v1"));
const writes = [...doc.querySelectorAll('[data-path="writes.topics"] input[type=checkbox]')];
check("writes.topics is a checklist with the saved topic ticked", writes.length > 1 && writes.filter((c) => c.checked).length === 1);
const instr = doc.querySelector('[data-path="instructions"] textarea');
check("the agent form has every field, including instructions", instr && instr.value.includes("summarise"), instr && instr.value.slice(0, 40));
const modelBoxes = [...doc.querySelectorAll('[data-path="inference.models"] label')].map((l) => l.textContent.trim());
check("model picker lists real model ids", modelBoxes.includes("qwen2.5-1.5b-q4") && modelBoxes.includes("claude-sonnet-5-5"), modelBoxes.join(","));
check("an untouched agent shows no changes (save disabled)", doc.getElementById("save").disabled === true, doc.getElementById("diff").textContent.slice(0, 120));
const target = writes.find((c) => !c.checked);
target.checked = true;
target.dispatchEvent(new window.Event("change", { bubbles: true }));
await settle();
check("ticking another topic enables save", doc.getElementById("save").disabled === false);

// Sinks: pick what the sink does from a dropdown, and the form swaps fields
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Sinks"));
await settle();
const cc = doc.querySelector("#list select.create");
const sinkOpts = cc && [...cc.options].map((o) => o.textContent).join("|");
check("sinks page offers a create dropdown", sinkOpts === "New sink…|Write to a file|POST to an API", sinkOpts);
choose(cc, "http_post");
await settle();
const postLabels = labelsShown();
check("POST sink shows url, credential and rate, not path", ["url", "credential", "rate", "timeout"].every((l) => postLabels.includes(l)) && !postLabels.includes("path"), postLabels.join(","));

// Sources: the shipped source is listed and its map is editable
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Sources"));
await settle();
check("the shipped source is listed", [...doc.querySelectorAll("#list button.item")].some((b) => b.textContent.trim() === "hn_source"));

// clean up the entry the test created
await fetch(BASE + "api/model/test-local-model", { method: "DELETE" });
console.log(fail.length ? `\n${fail.length} FAILED: ${fail.join("; ")}` : "\nall checks passed");
process.exit(fail.length ? 1 : 0);
