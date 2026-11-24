import json
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from configui.app import create_app

H = {"content-type": "application/json"}


@pytest.fixture
def env(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    app = create_app(tmp_path / "config")
    return TestClient(app), tmp_path


def post(c, path, **body):
    return c.post(f"/api/canvas/{path}", content=json.dumps(body), headers=H)


def op(label, *files, base=None):
    return {"id": label, "label": label, "files": [{"kind": k, "name": n, "data": d} for k, n, d in files], "base": base or {}}


def agent(c, name):
    return c.get(f"/api/agent/{name}").json()


# ---------------------------------------------------------------- graph (UI-M0)

def test_graph_is_derived_from_the_yaml(env):
    c, _ = env
    s = post(c, "state", ops=[]).json()
    ids = {n["id"] for n in s["graph"]["nodes"]}
    assert {"agent:summarizer", "topic:news.story.v1", "source:hn_source", "sink:summaries_file"} <= ids
    kinds = {n["id"]: n["kind"] for n in s["graph"]["nodes"]}
    assert kinds["source:hn_source"] == "source" and set(kinds.values()) == {"topic", "agent", "source", "sink"}
    assert kinds["sink:summaries_file"] == "sink"
    edge = {(e["type"], e["from"], e["to"]) for e in s["graph"]["edges"]}
    assert ("trigger", "topic:news.story.v1", "agent:summarizer") in edge
    assert ("writes", "agent:summarizer", "topic:news.summary.v1") in edge
    assert ("publishes", "source:hn_source", "topic:news.story.v1") in edge
    assert ("consumes", "topic:news.summary.v1", "sink:summaries_file") in edge
    assert "frames" not in s["graph"]                       # one graph: no pipelines


def test_reachability_lists_source_to_sink_paths_with_free_text(env):
    c, _ = env
    r = post(c, "state", ops=[]).json()["reach"]
    p = next(x for x in r if x["to"] == "summaries_file" and x["from"] == "hn_source")
    assert any("news.summary.v1.summary" in ch for ch in p["free_text"])


# ---------------------------------------------------------------- staging never touches config/ (U13)

def test_state_applies_staged_changes_without_writing_anything(env):
    c, tmp = env
    before = (tmp / "config" / "agents" / "summarizer.yaml").read_bytes()
    a = {**agent(c, "summarizer"), "concurrency": 3}
    s = post(c, "state", ops=[op("c", ("agent", "summarizer", a))]).json()
    assert next(n for n in s["graph"]["nodes"] if n["id"] == "agent:summarizer")["concurrency"] == 3
    assert (tmp / "config" / "agents" / "summarizer.yaml").read_bytes() == before


# ---------------------------------------------------------------- validation is the store's (U12, U2-ish)

def test_staged_errors_are_reported_per_node(env):
    c, _ = env
    a = {**agent(c, "summarizer"), "trigger": {"topic": "no.such.topic.v1"}}
    issues = post(c, "state", ops=[op("bad", ("agent", "summarizer", a))]).json()["issues"]
    assert any("unknown topic" in e["message"] for e in issues["agent:summarizer"]["errors"])


def test_removing_a_topic_still_in_use_is_an_error_on_its_users(env):
    c, _ = env
    issues = post(c, "state", ops=[op("rm", ("topic", "news.summary.v1", None))]).json()["issues"]
    assert "agent:summarizer" in issues and "sink:summaries_file" in issues


def test_canvas_cannot_write_models(env):
    c, _ = env
    for kind in ("model",):
        r = post(c, "state", ops=[op("x", (kind, "qwen2.5-1.5b-q4", {"name": "qwen2.5-1.5b-q4"}))])
        assert r.status_code == 400 and "config menu" in r.json()["error"]
    assert post(c, "deploy", ops=[op("x", ("model", "qwen2.5-1.5b-q4", None))]).status_code == 400            # U26, server side too


# ---------------------------------------------------------------- diff (U5)

def test_diff_names_every_new_permission_and_path(env):
    c, _ = env
    a = agent(c, "summarizer")
    a["writes"]["topics"] = ["news.summary.v1", "news.story.v1"]
    d = post(c, "diff", ops=[op("w", ("agent", "summarizer", a))]).json()
    assert {"sign": "+", "text": "summarizer  may write  news.story.v1"} in d["capabilities"]
    assert d["files"] == [{"code": "M", "path": "agents/summarizer.yaml"}]
    assert "+" in d["yaml_diff"] and "news.story.v1" in d["yaml_diff"]


def test_a_new_path_to_a_sink_requires_acknowledgement(env):
    c, tmp = env
    sink = {"name": "extra_sink", "type": "file", "topic": "news.story.v1", "path": "data/extra/all.jsonl"}
    ops = [op("s", ("sink", "extra_sink", sink))]
    d = post(c, "diff", ops=ops).json()
    assert d["needs_ack"] and any("hn_source → extra_sink" in r["text"] for r in d["reachability"])
    r = post(c, "deploy", ops=ops)
    assert r.status_code == 409 and r.json()["needs_ack"] and not (tmp / "config" / "sinks" / "extra_sink.yaml").exists()
    assert post(c, "deploy", ops=ops, ack=True).status_code == 200
    assert (tmp / "config" / "sinks" / "extra_sink.yaml").exists()


def test_breaking_schema_changes_are_blocked(env):
    c, _ = env
    t = c.get("/api/topic/news.summary.v1").json()
    t["schema"]["required"].append("bonus")
    t["schema"]["properties"]["bonus"] = {"type": "integer"}
    d = post(c, "diff", ops=[op("b", ("topic", "news.summary.v1", t))]).json()
    assert d["breaking"] and any("required" in s["text"] for s in d["schema"])
    r = post(c, "deploy", ops=[op("b", ("topic", "news.summary.v1", t))], ack=True)
    assert r.status_code == 422 and ".v2" in r.json()["error"]
    ok = c.get("/api/topic/news.summary.v1").json()
    ok["schema"]["properties"]["note"] = {"type": "integer"}                                  # optional addition is compatible
    assert not post(c, "diff", ops=[op("o", ("topic", "news.summary.v1", ok))]).json()["breaking"]


# ---------------------------------------------------------------- deploy (U6, U7, U13)

def test_deploy_changes_one_field_and_keeps_comments(env):
    c, tmp = env
    f = tmp / "config" / "agents" / "summarizer.yaml"
    f.write_text("# summarizers are cheap, keep them numerous\n" + f.read_text().replace("concurrency: 2", "concurrency: 2   # per laptop"))
    base = {"agent/summarizer": post(c, "state", ops=[]).json()["hashes"]["agent/summarizer"]}
    a = {**agent(c, "summarizer"), "concurrency": 3}
    r = post(c, "deploy", ops=[op("c", ("agent", "summarizer", a), base=base)])
    assert r.status_code == 200 and r.json()["written"] == ["agent/summarizer"]
    text = f.read_text()
    assert "# summarizers are cheap, keep them numerous" in text and "concurrency: 3   # per laptop" in text
    new = yaml.safe_load(text)
    assert new == {**yaml.safe_load(f.read_text()), "concurrency": 3} and agent(c, "summarizer")["concurrency"] == 3


def test_deploy_refuses_to_overwrite_an_edit_made_behind_the_canvas(env):
    c, tmp = env
    base = {"agent/summarizer": post(c, "state", ops=[]).json()["hashes"]["agent/summarizer"]}
    f = tmp / "config" / "agents" / "summarizer.yaml"
    f.write_text(f.read_text() + "# edited by hand\n")                                          # someone edits the file
    a = {**agent(c, "summarizer"), "concurrency": 2}
    r = post(c, "deploy", ops=[op("c", ("agent", "summarizer", a), base=base)])
    assert r.status_code == 409 and r.json()["conflicts"] == ["agent/summarizer"]
    assert "# edited by hand" in f.read_text() and "concurrency: 2" in f.read_text()


def test_deploy_is_all_or_nothing(env):
    c, tmp = env
    good = {**agent(c, "summarizer"), "concurrency": 3}
    bad = {**agent(c, "summarizer"), "name": "other", "trigger": {"topic": "nope.v1"}}
    r = post(c, "deploy", ops=[op("a", ("agent", "summarizer", good), ("agent", "other", bad))])
    assert r.status_code == 422 and "agent:other" in r.json()["errors"]
    assert agent(c, "summarizer")["concurrency"] == 2


def test_new_topic_and_agent_in_one_change_deploy_together(env):
    c, _ = env
    schema = {"type": "object", "additionalProperties": False, "required": ["id"],
              "properties": {"id": {"type": "string", "pattern": "^[a-z]{1,8}$"}}}
    topic = {"name": "demo.note.v1", "schema": schema, "retention": "7d", "partitions": 1}
    ag = {**agent(c, "summarizer"), "name": "noter", "writes": {"topics": ["demo.note.v1"]}, "build": "agents-src/llm_agent"}
    ag["inference"] = {"models": ["qwen2.5-1.5b-q4"], "max_tokens_per_event": 1000}
    r = post(c, "deploy", ops=[op("n", ("topic", "demo.note.v1", topic), ("agent", "noter", ag))], ack=True)
    assert r.status_code == 200, r.text
    assert c.get("/api/topic/demo.note.v1").status_code == 200 and c.get("/api/agent/noter").status_code == 200


# ---------------------------------------------------------------- staged list + layout persistence (U15, U8)

def test_staged_list_survives_a_reload_and_discard_clears_it(env):
    c, tmp = env
    ops = [op("c", ("agent", "summarizer", {**agent(c, "summarizer"), "concurrency": 3}))]
    assert c.put("/api/canvas/staged", content=json.dumps({"ops": ops, "pos": 1}), headers=H).status_code == 200
    assert c.get("/api/canvas/staged").json() == {"ops": ops, "pos": 1}
    assert c.delete("/api/canvas/staged").status_code == 200 and c.get("/api/canvas/staged").json() == {"ops": [], "pos": 0}
    assert agent(c, "summarizer")["concurrency"] == 2


def test_layout_is_cosmetic(env):
    c, tmp = env
    lay = {"nodes": {"agent:summarizer": {"x": 10, "y": 20}},
           "viewport": {"x": 1, "y": 2, "zoom": 0.9}}
    assert c.put("/api/canvas/layout", content=json.dumps(lay), headers=H).status_code == 200
    assert c.get("/api/canvas/layout").json()["nodes"]["agent:summarizer"] == {"x": 10, "y": 20}
    (tmp / "config" / "canvas.layout.json").unlink()                                                           # U8
    s = post(c, "state", ops=[]).json()
    assert s["graph"]["nodes"] and c.get("/api/canvas/layout").json() == {}
    assert c.put("/api/canvas/layout", content=json.dumps({"nodes": {"a": {"x": "<script>"}}}), headers=H).status_code == 400


def test_canvas_routes_require_json_and_a_local_host(env):
    c, _ = env
    assert c.post("/api/canvas/state", content="{}", headers={"content-type": "text/plain"}).status_code == 415
    assert c.post("/api/canvas/state", content="{}", headers={**H, "host": "evil.example.com"}).status_code == 400


def test_runtime_status_reflects_whether_the_stack_is_live_and_in_sync(env):
    import time
    c, tmp = env
    assert c.get("/api/canvas/runtime").json() == {"running": False}
    from configui.store import Store
    fp = Store(tmp / "config").fingerprint()
    status = tmp / "data" / "stack.status.json"
    status.parent.mkdir(exist_ok=True)
    status.write_text(json.dumps({"heartbeat": time.time(), "config_hash": fp, "ok": True, "error": "", "changes": {}, "applying": False}))
    assert c.get("/api/canvas/runtime").json()["in_sync"] is True
    a = agent(c, "summarizer")
    assert post(c, "deploy", ops=[op("c", ("agent", "summarizer", {**a, "concurrency": 3}))]).status_code == 200
    assert c.get("/api/canvas/runtime").json()["in_sync"] is False            # config moved on; the stack hasn't applied it yet
    status.write_text(json.dumps({"heartbeat": time.time() - 60, "config_hash": fp, "ok": True}))
    assert c.get("/api/canvas/runtime").json()["running"] is False             # stale heartbeat: not running
