// Drives the real UI (index.html + app.js) in jsdom against a running server and checks the
// create-by dropdown, field swapping, backend picker and save flow.
// Usage: BASE_URL=http://127.0.0.1:8787/ node tests/ui/drive.mjs   (needs: npm ci in tests/ui)
import { JSDOM } from "jsdom";
const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8787/";
const html = await (await fetch(BASE)).text();
const js = await (await fetch(BASE + "app.js")).text();
const dom = new JSDOM(html.replace('<script src="app.js"></script>', ""), { url: BASE, runScripts: "outside-only", pretendToBeVisual: true });
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
check("nav lists Overview, APIs, Backends, Models, Topics", nav.join(",") === "Overview,APIs,Backends,Models,Topics", nav.join(","));
check("current nav item is marked", doc.querySelector('#kinds [aria-current="page"]')?.textContent === "Overview");

// Backends: the list offers a dropdown, not a big form
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Backends"));
await settle();
const create = doc.querySelector("#list select.create");
check("backends page has a create dropdown", !!create);
check("dropdown offers GPU, CPU, hosted", [...create.options].map((o) => o.textContent).join("|") === "New backend…|Local GPU (vLLM)|Local CPU (llama.cpp)|Hosted API", [...create.options].map((o) => o.textContent).join("|"));
check("existing backends are listed", doc.querySelectorAll("#list button.item").length === 2);

// Create a hosted backend: only the hosted fields should show
choose(create, "hosted_api");
await settle();
const hostedLabels = labelsShown();
check("hosted form shows hosted fields", ["credential", "egress_host", "daily_spend_cap_usd", "provider"].every((l) => hostedLabels.includes(l)), hostedLabels.join(","));
check("hosted form hides local-only fields", !hostedLabels.includes("endpoint") && !hostedLabels.includes("gpus") && !hostedLabels.includes("threads"), hostedLabels.join(","));
check("kind is preset to hosted_api", doc.querySelector("#editor select").value === "hosted_api");
check("save disabled while invalid", doc.getElementById("save").disabled === true);

// Switch to GPU through the form's own kind select: fields swap
const kindSel = [...doc.querySelectorAll("#editor select")].find((s) => [...s.options].some((o) => o.value === "local_gpu"));
choose(kindSel, "local_gpu");
await settle();
const gpuLabels = labelsShown();
check("switching kind swaps fields", gpuLabels.includes("endpoint") && gpuLabels.includes("gpus") && !gpuLabels.includes("credential"), gpuLabels.join(","));

// Fill in a valid local GPU backend and save it
type(doc.querySelector('[data-path="name"] input'), "local-gpu");
type(doc.querySelector('[data-path="endpoint"] input'), "http://inference-gpu:8000/v1");
await settle();
check("valid GPU backend enables save", doc.getElementById("save").disabled === false, doc.getElementById("issues").textContent.slice(0, 120));
click(doc.getElementById("save"));
await settle();
const names = (await (await fetch(BASE + "api/backend")).json()).names;
check("backend was saved", names.includes("local-gpu"), names.join(","));

// Models: backend is a picker of existing backends
click([...doc.querySelectorAll("#kinds button")].find((b) => b.textContent === "Models"));
await settle();
click([...doc.querySelectorAll("#list button")].find((b) => b.textContent === "New entry"));
await settle();
const backendSel = doc.querySelector('[data-path="backend"] select');
check("model backend is a dropdown of existing backends", backendSel && [...backendSel.options].map((o) => o.value).filter(Boolean).sort().join(",") === "anthropic,local-cpu,local-gpu", backendSel && [...backendSel.options].map((o) => o.value).join(","));
type(doc.querySelector('[data-path="name"] input'), "bad alias!");
await settle();
check("bad alias name reports an error", /invalid name|name/.test(doc.getElementById("issues").textContent), doc.getElementById("issues").textContent.slice(0, 100));

// clean up the entry the test created
await fetch(BASE + "api/backend/local-gpu", { method: "DELETE" });
console.log(fail.length ? `\n${fail.length} FAILED: ${fail.join("; ")}` : "\nall checks passed");
process.exit(fail.length ? 1 : 0);
