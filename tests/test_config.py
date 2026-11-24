import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from configui.app import create_app
from configui.store import Store, StoreError

REPO_CONFIG = Path(__file__).parent.parent / "config"

TOPIC = {
    "name": "email.triaged.v1",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message_id", "urgency"],
        "properties": {
            "message_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
            "urgency": {"enum": ["low", "normal", "high"]},
            "summary": {"type": "string", "maxLength": 280, "x-free-text": True},
        },
    },
    "retention": "7d",
    "partitions": 1,
}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(tmp_path))


def paths(res):
    return {e["path"] for e in res.errors}


# ------------------------------------------------------------ shipped config
def test_shipped_config_is_valid():
    s = Store(REPO_CONFIG)
    for kind in ("model", "topic", "agent", "source", "sink"):
        assert s.list(kind)
        for name in s.list(kind):
            assert s.validate(kind, name, s.read(kind, name)).errors == []


# ------------------------------------------------------------ sources and sinks
STORY = {"name": "news.story.v1", "schema": {"type": "object", "additionalProperties": False, "required": ["id", "title"], "properties": {
    "id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"}, "title": {"type": "string", "maxLength": 200, "x-free-text": True}}},
    "retention": "7d", "partitions": 1, "key": "id"}
SOURCE = {"name": "feed", "type": "http_poll", "url": "https://api.example.com/v1/items", "topic": "news.story.v1", "interval": "5m",
          "items_path": "items", "id_field": "uid", "map": {"id": "uid", "title": "headline"}}
POST_SINK = {"name": "webhook", "type": "http_post", "topic": "news.story.v1", "url": "https://hooks.example.com/in",
             "credential": "hook_key", "auth": {"type": "bearer"}}


@pytest.fixture
def flow(store):
    store.save("topic", "news.story.v1", STORY)
    return store


def test_source_derives_egress_host(flow):
    res = flow.validate("source", "feed", SOURCE)
    assert res.errors == [] and res.derived["egress_host"] == "api.example.com"


def test_source_url_must_be_https(flow):
    assert "url" in paths(flow.validate("source", "feed", dict(SOURCE, url="http://api.example.com/x")))


def test_source_map_must_match_the_topic(flow):
    assert any("requires 'title'" in e["message"] for e in flow.validate("source", "feed", dict(SOURCE, map={"id": "uid"})).errors)
    assert "map.bogus" in paths(flow.validate("source", "feed", dict(SOURCE, map={**SOURCE["map"], "bogus": "x"})))


def test_source_needs_an_existing_topic(store):
    assert "topic" in paths(store.validate("source", "feed", SOURCE))


def test_name_must_match_entry(flow):
    assert "name" in paths(flow.validate("source", "other_name", SOURCE))


def test_http_post_sink_warns_that_data_leaves_the_machine_and_reports_its_host(flow):
    res = flow.validate("sink", "webhook", POST_SINK)
    assert res.errors == [] and res.derived["egress_host"] == "hooks.example.com" and any("leaves this machine" in w for w in res.warnings)


def test_http_post_sink_credential_and_auth_go_together(flow):
    assert "auth" in paths(flow.validate("sink", "webhook", {k: v for k, v in POST_SINK.items() if k != "auth"}))
    assert "credential" in paths(flow.validate("sink", "webhook", {k: v for k, v in POST_SINK.items() if k != "credential"}))


def test_file_sink_path_placeholders_must_be_topic_fields(flow):
    ok = {"name": "out", "type": "file", "topic": "news.story.v1", "path": "data/results/{id}.jsonl"}
    assert flow.validate("sink", "out", ok).errors == []
    assert "path" in paths(flow.validate("sink", "out", dict(ok, path="data/results/{nope}.jsonl")))
    assert "path" in paths(flow.validate("sink", "out", dict(ok, path="data/../x/{id}.jsonl")))


def test_there_is_no_api_or_connector_kind(store):
    for kind in ("api", "connector", "backend"):
        with pytest.raises(StoreError):
            store.list(kind)


# ------------------------------------------------------------ models (where a model runs is part of the model)
SMALL = {"name": "small", "provider": "local", "model": "/models/q.gguf", "endpoint": "http://inference-cpu:8080/v1",
         "max_concurrency": 2}
STRONG = {"name": "claude-sonnet-5-5", "provider": "anthropic", "model": "claude-sonnet-5-5", "credential": "anthropic_api_key",
          "daily_spend_cap_usd": 10, "allowed_agents": ["notifier"]}
OPENAI = {"name": "gpt-x", "provider": "openai_compatible", "model": "gpt-x", "base_url": "https://api.openai.example/v1",
          "egress_host": "api.openai.example", "credential": "openai_key", "daily_spend_cap_usd": 5}


@pytest.fixture
def inf(store):
    """A store with one local and one hosted model."""
    store.save("model", "small", SMALL)
    store.save("model", "claude-sonnet-5-5", STRONG)
    return store


def test_hosted_model_requires_spend_cap_and_credential(store):
    d = {k: v for k, v in STRONG.items() if k not in ("daily_spend_cap_usd", "credential")}
    assert store.validate("model", "claude-sonnet-5-5", d).errors


def test_local_model_requires_endpoint(store):
    d = {k: v for k, v in SMALL.items() if k != "endpoint"}
    assert store.validate("model", "small", d).errors


def test_openai_compatible_requires_base_url_and_matching_egress_host(store):
    d = {k: v for k, v in OPENAI.items() if k != "base_url"}
    assert store.validate("model", "gpt-x", d).errors
    assert store.validate("model", "gpt-x", OPENAI).errors == []
    assert "egress_host" in paths(store.validate("model", "gpt-x", dict(OPENAI, egress_host="evil.example")))


def test_anthropic_model_reports_its_egress_host(store):
    assert store.validate("model", "claude-sonnet-5-5", STRONG).derived["egress_host"] == "api.anthropic.com"


def test_there_is_no_separate_backend_kind(store):
    assert "backend" not in {k.id for k in store.kinds()} if hasattr(store, "kinds") else True
    with pytest.raises(StoreError):
        store.list("backend")


def test_valid_alias(inf):
    assert inf.validate("model", "small", SMALL).errors == []


def test_model_names_may_contain_dots(inf):
    inf.save("model", "qwen2.5-7b-instruct", dict(SMALL, name="qwen2.5-7b-instruct"))
    assert "qwen2.5-7b-instruct" in inf.list("model")


def test_no_fallback_by_default(inf):
    assert inf.read("model", "small").get("fallback") is None
    assert inf.overview()["inference"]["fallback"] == []


def test_fallback_to_hosted_warns_about_data_leaving_machine(inf):
    res = inf.validate("model", "small", dict(SMALL, fallback=["claude-sonnet-5-5"]))
    assert res.errors == [] and any("hosted" in w for w in res.warnings)


def test_fallback_cycle_and_self_rejected(inf):
    inf.save("model", "claude-sonnet-5-5", dict(STRONG, fallback=["small"]))
    assert any("cycle" in e["message"] for e in inf.validate("model", "small", dict(SMALL, fallback=["claude-sonnet-5-5"])).errors)
    assert inf.validate("model", "small", dict(SMALL, fallback=["small"])).errors


def test_fallback_to_unknown_alias_rejected(inf):
    assert "fallback" in paths(inf.validate("model", "small", dict(SMALL, fallback=["nope"])))


def test_fallback_kind_mismatch_rejected(inf):
    inf.save("model", "emb", {"name": "emb", "provider": "local", "endpoint": "http://x:1/v1", "model": "/m/bge.gguf", "kind": "embedding"})
    assert inf.validate("model", "small", dict(SMALL, fallback=["emb"])).errors


def test_hosted_alias_without_allowlist_warns(inf):
    d = {k: v for k, v in STRONG.items() if k != "allowed_agents"}
    assert any("allowed_agents" in w for w in inf.validate("model", "claude-sonnet-5-5", d).warnings)


# ------------------------------------------------------------ save / diff
def test_save_is_atomic_roundtrip_and_diff(store):
    p = store.preview("topic", "email.triaged.v1", TOPIC)
    assert p["diff"].startswith("--- /dev/null") and not p["exists"]
    store.save("topic", "email.triaged.v1", TOPIC)
    assert store.read("topic", "email.triaged.v1") == TOPIC
    assert store.list("topic") == ["email.triaged.v1"]
    d = copy.deepcopy(TOPIC)
    d["retention"] = "3d"
    diff = store.preview("topic", "email.triaged.v1", d)["diff"]
    assert "-retention: 7d" in diff and "+retention: 3d" in diff
    assert not list(store.dir.rglob("*.tmp"))


def test_invalid_save_writes_nothing(store):
    d = copy.deepcopy(TOPIC)
    d["retention"] = "forever"
    with pytest.raises(StoreError):
        store.save("topic", "email.triaged.v1", d)
    assert store.list("topic") == []


# ------------------------------------------------------------ http
def test_http_flow(client):
    assert {k["id"] for k in client.get("/api/kinds").json()} == {"model", "topic", "agent", "source", "sink"}
    r = client.post("/api/topic/email.triaged.v1/preview", json=TOPIC)
    assert r.status_code == 200 and r.json()["errors"] == []
    assert client.put("/api/topic/email.triaged.v1", json=TOPIC).status_code == 200
    assert client.get("/api/topic").json() == {"names": ["email.triaged.v1"]}
    assert client.get("/api/topic/email.triaged.v1").json()["name"] == "email.triaged.v1"
    r = client.put("/api/topic/email.triaged.v1", json=dict(TOPIC, retention="forever"))
    assert r.status_code == 422 and r.json()["errors"]
    assert client.delete("/api/topic/email.triaged.v1").status_code == 200
    assert client.get("/api/topic/email.triaged.v1").status_code == 404


def test_requires_json_content_type(client):
    r = client.put("/api/topic/email.triaged.v1", content="x", headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_rejects_foreign_host_header(client):
    assert client.get("/api/kinds", headers={"host": "evil.example.com"}).status_code == 400


def test_unknown_kind_404(client):
    assert client.get("/api/agents").status_code == 404


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Harness config" in r.text


# ------------------------------------------------------------ topics
def test_valid_topic_reports_free_text_channels(store):
    res = store.validate("topic", "email.triaged.v1", TOPIC)
    assert res.errors == []
    assert res.derived["free_text"] == [{"path": "summary", "max_length": 280}]


@pytest.mark.parametrize("bad", ["email", "Email.triaged.v1", "email.triaged", "email.triaged.v0", "../x.y.v1", "a/b.c.v1"])
def test_topic_name_must_follow_convention(store, bad):
    with pytest.raises(StoreError):
        store.validate("topic", bad, dict(TOPIC, name=bad))


@pytest.mark.parametrize("reserved", ["dlq.email_triage.v1", "harness.audit.v1", "jobs.completed.v1"])
def test_reserved_topic_names_rejected(store, reserved):
    assert "name" in paths(store.validate("topic", reserved, dict(TOPIC, name=reserved)))


def test_topic_schema_must_forbid_extra_properties(store):
    d = copy.deepcopy(TOPIC)
    del d["schema"]["additionalProperties"]
    assert "schema" in paths(store.validate("topic", "email.triaged.v1", d))


def test_untyped_string_rejected_unless_marked_free_text(store):
    d = copy.deepcopy(TOPIC)
    d["schema"]["properties"]["note"] = {"type": "string", "maxLength": 50}
    assert "schema.properties.note" in paths(store.validate("topic", "email.triaged.v1", d))
    d["schema"]["properties"]["note"]["x-free-text"] = True
    assert store.validate("topic", "email.triaged.v1", d).errors == []


def test_free_text_requires_max_length(store):
    d = copy.deepcopy(TOPIC)
    del d["schema"]["properties"]["summary"]["maxLength"]
    assert "schema.properties.summary" in paths(store.validate("topic", "email.triaged.v1", d))


def test_nested_objects_are_checked(store):
    d = copy.deepcopy(TOPIC)
    d["schema"]["properties"]["meta"] = {"type": "object", "properties": {"x": {"enum": ["a"]}}}
    assert "schema.properties.meta" in paths(store.validate("topic", "email.triaged.v1", d))


def test_topic_key_must_be_a_schema_property(store):
    d = dict(copy.deepcopy(TOPIC), key="nope")
    assert "key" in paths(store.validate("topic", "email.triaged.v1", d))
    d["key"] = "message_id"
    assert store.validate("topic", "email.triaged.v1", d).errors == []


def test_partitions_without_key_warns(store):
    res = store.validate("topic", "email.triaged.v1", dict(copy.deepcopy(TOPIC), partitions=4))
    assert res.errors == [] and any("ordering" in w for w in res.warnings)


def test_topic_bounds(store):
    assert store.validate("topic", "email.triaged.v1", dict(copy.deepcopy(TOPIC), partitions=0)).errors
    assert store.validate("topic", "email.triaged.v1", dict(copy.deepcopy(TOPIC), retention="forever")).errors


def test_topic_with_dots_roundtrips_on_disk(store):
    store.save("topic", "email.triaged.v1", TOPIC)
    assert store.list("topic") == ["email.triaged.v1"]
    assert store.read("topic", "email.triaged.v1")["name"] == "email.triaged.v1"


# ------------------------------------------------------------ overview
def test_overview_of_shipped_config():
    o = Store(REPO_CONFIG).overview()
    assert o["counts"]["topic"] >= 2 and o["counts"]["source"] >= 1 and o["counts"]["sink"] >= 1
    assert [a for a in o["attention"] if a["level"] == "error"] == []
    assert {"api.anthropic.com", "hn.algolia.com"} <= {host for host, _ in o["egress"]}
    assert any(m["hosted"] for m in o["inference"]["models"])
    assert o["inference"]["fallback"] == []


def test_overview_surfaces_invalid_and_unreadable_files(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "broken.yaml").write_text("name: broken\ntype: bogus\n")
    (tmp_path / "sources" / "garbled.yaml").write_text("a: [unclosed\n")
    (tmp_path / "sources" / "Bad Name.yaml").write_text("name: x\n")
    o = Store(tmp_path).overview()
    levels = {(a["name"], a["level"]) for a in o["attention"]}
    assert ("broken", "error") in levels and ("garbled", "error") in levels and ("Bad Name", "error") in levels


def test_overview_http(client):
    client.put("/api/topic/email.triaged.v1", json=TOPIC)
    o = client.get("/api/overview").json()
    assert o["counts"]["topic"] == 1 and o["topics"][0]["free_text"][0]["path"] == "summary"


# ------------------------------------------------------------ UI shell
STATIC = Path(__file__).parent.parent / "configui" / "static"
REQUIRED_IDS = ["kinds", "list", "dashboard", "editor", "issues", "diff"]


def test_shell_provides_every_element_the_script_needs():
    html = (STATIC / "index.html").read_text()
    for ident in REQUIRED_IDS:
        assert f'id="{ident}"' in html, f"missing #{ident}"
    assert 'src="app.js"' in html and 'href="base.css"' in html and 'href="theme.css"' in html


def test_assets_are_served(client):
    for path in ("/", "/app.js", "/base.css", "/theme.css"):
        assert client.get(path).status_code == 200, path
