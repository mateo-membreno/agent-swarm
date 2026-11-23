import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from configui.app import create_app
from configui.store import Store, StoreError

REPO_CONFIG = Path(__file__).parent.parent / "config"

API = {
    "name": "weather_lookup",
    "kind": "read",
    "backend": {"type": "http", "url": "https://api.example.com/v1/weather", "method": "GET"},
    "credential": "weather_key",
    "auth": {"type": "bearer"},
    "args_schema": {"type": "object", "additionalProperties": False, "properties": {"city": {"type": "string", "maxLength": 80}}},
    "limits": {"rate": "10/min", "timeout": "10s", "max_response_bytes": 65536},
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
    assert s.list("api")
    for name in s.list("api"):
        assert s.validate("api", name, s.read("api", name)).errors == []
    for kind in ("backend", "model"):
        assert s.list(kind)
        for name in s.list(kind):
            assert s.validate(kind, name, s.read(kind, name)).errors == []
    assert s.list("topic")
    for name in s.list("topic"):
        assert s.validate("topic", name, s.read("topic", name)).errors == []


# ------------------------------------------------------------ api
def test_valid_api_derives_egress_host(store):
    res = store.validate("api", "weather_lookup", API)
    assert res.errors == []
    assert res.derived["egress_host"] == "api.example.com"


def test_api_requires_https_and_method_for_http(store):
    d = copy.deepcopy(API)
    d["backend"] = {"type": "http", "url": "http://insecure.example.com"}
    assert store.validate("api", "weather_lookup", d).errors


def test_api_args_schema_must_forbid_extra_properties(store):
    d = copy.deepcopy(API)
    del d["args_schema"]["additionalProperties"]
    assert "args_schema" in paths(store.validate("api", "weather_lookup", d))


def test_api_fixed_args_cannot_overlap_agent_args(store):
    d = copy.deepcopy(API)
    d["fixed_args"] = {"city": "Paris"}
    assert "fixed_args" in paths(store.validate("api", "weather_lookup", d))


def test_api_name_must_match_entry(store):
    assert "name" in paths(store.validate("api", "other_name", API))


def test_credential_needs_auth_method(store):
    d = copy.deepcopy(API)
    del d["auth"]
    assert "auth" in paths(store.validate("api", "weather_lookup", d))


def test_effect_without_approval_warns(store):
    d = copy.deepcopy(API)
    d.update(kind="effect")
    d["backend"]["method"] = "POST"
    res = store.validate("api", "weather_lookup", d)
    assert res.errors == [] and any("approval" in w for w in res.warnings)


def test_secret_literal_is_rejected(store):
    d = copy.deepcopy(API)
    d["description"] = "key is sk-ant-abcdefghijklmnop1234"
    res = store.validate("api", "weather_lookup", d)
    assert any("secret" in e["message"] for e in res.errors)


@pytest.mark.parametrize("bad", ["../evil", "Bad Name", "a", "x/y", ""])
def test_path_traversal_names_rejected(store, bad):
    with pytest.raises(StoreError):
        store.save("api", bad, API)


# ------------------------------------------------------------ inference: backends + models
CPU = {"name": "local-cpu", "kind": "local_cpu", "endpoint": "http://inference-cpu:8080/v1", "threads": 8, "max_concurrency": 2}
HOSTED = {
    "name": "anthropic", "kind": "hosted_api", "provider": "anthropic", "credential": "anthropic_api_key",
    "egress_host": "api.anthropic.com", "daily_spend_cap_usd": 10,
}
GPU = {"name": "local-gpu", "kind": "local_gpu", "endpoint": "http://inference-gpu:8000/v1", "gpus": "auto"}
SMALL = {"name": "small-local", "backend": "local-cpu", "model": "/models/q.gguf"}
STRONG = {"name": "strong-hosted", "backend": "anthropic", "model": "claude-sonnet-5-5", "allowed_agents": ["notifier"]}


@pytest.fixture
def inf(store):
    """A store with a CPU backend, a hosted backend and one alias on each."""
    store.save("backend", "local-cpu", CPU)
    store.save("backend", "anthropic", HOSTED)
    store.save("model", "small-local", SMALL)
    store.save("model", "strong-hosted", STRONG)
    return store


def test_hosted_backend_requires_spend_cap_and_credential(store):
    d = {k: v for k, v in HOSTED.items() if k not in ("daily_spend_cap_usd", "credential")}
    assert store.validate("backend", "anthropic", d).errors


def test_local_backend_requires_endpoint(store):
    d = {k: v for k, v in CPU.items() if k != "endpoint"}
    assert store.validate("backend", "local-cpu", d).errors


def test_openai_compatible_requires_base_url(store):
    d = dict(HOSTED, provider="openai_compatible")
    assert store.validate("backend", "anthropic", d).errors


def test_gpu_backend_accepts_auto_and_list(store):
    for gpus in ("auto", [0, 1]):
        assert store.validate("backend", "local-gpu", dict(GPU, gpus=gpus)).errors == []


def test_hosted_backend_reports_egress_host(store):
    assert store.validate("backend", "anthropic", HOSTED).derived["egress_host"] == "api.anthropic.com"


def test_alias_must_reference_existing_backend(store):
    assert "backend" in paths(store.validate("model", "small-local", SMALL))


def test_valid_alias(inf):
    assert inf.validate("model", "small-local", SMALL).errors == []


def test_model_names_may_contain_dots(inf):
    inf.save("model", "qwen2.5-7b-instruct", dict(SMALL, name="qwen2.5-7b-instruct"))
    assert "qwen2.5-7b-instruct" in inf.list("model")


def test_no_fallback_by_default(inf):
    assert inf.read("model", "small-local").get("fallback") is None
    assert inf.overview()["inference"]["fallback"] == []


def test_fallback_to_hosted_warns_about_data_leaving_machine(inf):
    res = inf.validate("model", "small-local", dict(SMALL, fallback=["strong-hosted"]))
    assert res.errors == [] and any("hosted" in w for w in res.warnings)


def test_fallback_cycle_and_self_rejected(inf):
    inf.save("model", "strong-hosted", dict(STRONG, fallback=["small-local"]))
    assert any("cycle" in e["message"] for e in inf.validate("model", "small-local", dict(SMALL, fallback=["strong-hosted"])).errors)
    assert inf.validate("model", "small-local", dict(SMALL, fallback=["small-local"])).errors


def test_fallback_to_unknown_alias_rejected(inf):
    assert "fallback" in paths(inf.validate("model", "small-local", dict(SMALL, fallback=["nope"])))


def test_fallback_kind_mismatch_rejected(inf):
    inf.save("model", "emb", {"name": "emb", "backend": "local-cpu", "model": "/m/bge.gguf", "kind": "embedding"})
    assert inf.validate("model", "small-local", dict(SMALL, fallback=["emb"])).errors


def test_hosted_alias_without_allowlist_warns(inf):
    d = {k: v for k, v in STRONG.items() if k != "allowed_agents"}
    assert any("allowed_agents" in w for w in inf.validate("model", "strong-hosted", d).warnings)


def test_alias_is_invalid_if_its_backend_is_deleted(inf):
    inf.delete("backend", "local-cpu")
    errors = [a for a in inf.overview()["attention"] if a["level"] == "error"]
    assert any(a["name"] == "small-local" and a["path"] == "backend" for a in errors)


# ------------------------------------------------------------ save / diff
def test_save_is_atomic_roundtrip_and_diff(store):
    p = store.preview("api", "weather_lookup", API)
    assert p["diff"].startswith("--- /dev/null") and not p["exists"]
    store.save("api", "weather_lookup", API)
    assert store.read("api", "weather_lookup") == API
    assert store.list("api") == ["weather_lookup"]
    d = copy.deepcopy(API)
    d["limits"]["rate"] = "5/min"
    diff = store.preview("api", "weather_lookup", d)["diff"]
    assert "-  rate: 10/min" in diff and "+  rate: 5/min" in diff
    assert not list(store.dir.rglob("*.tmp"))


def test_invalid_save_writes_nothing(store):
    d = copy.deepcopy(API)
    d["kind"] = "bogus"
    with pytest.raises(StoreError):
        store.save("api", "weather_lookup", d)
    assert store.list("api") == []


# ------------------------------------------------------------ http
def test_http_flow(client):
    assert {k["id"] for k in client.get("/api/kinds").json()} == {"api", "backend", "model", "topic"}
    r = client.post("/api/api/weather_lookup/preview", json=API)
    assert r.status_code == 200 and r.json()["errors"] == []
    assert client.put("/api/api/weather_lookup", json=API).status_code == 200
    assert client.get("/api/api").json() == {"names": ["weather_lookup"]}
    assert client.get("/api/api/weather_lookup").json()["name"] == "weather_lookup"
    bad = dict(API, kind="bogus")
    r = client.put("/api/api/weather_lookup", json=bad)
    assert r.status_code == 422 and r.json()["errors"]
    assert client.delete("/api/api/weather_lookup").status_code == 200
    assert client.get("/api/api/weather_lookup").status_code == 404


def test_requires_json_content_type(client):
    r = client.put("/api/api/weather_lookup", content="x", headers={"content-type": "text/plain"})
    assert r.status_code == 415


def test_rejects_foreign_host_header(client):
    assert client.get("/api/kinds", headers={"host": "evil.example.com"}).status_code == 400


def test_unknown_kind_404(client):
    assert client.get("/api/agents").status_code == 404


def test_static_ui_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Harness config" in r.text


# ------------------------------------------------------------ topics
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
    assert o["counts"]["topic"] >= 3 and o["counts"]["api"] >= 1
    assert [a for a in o["attention"] if a["level"] == "error"] == []
    assert {"api.anthropic.com"} <= {host for host, _ in o["egress"]}
    assert any(m["hosted"] for m in o["inference"]["models"])
    assert o["inference"]["fallback"] == []


def test_overview_surfaces_invalid_and_unreadable_files(tmp_path):
    (tmp_path / "apis").mkdir()
    (tmp_path / "apis" / "broken.yaml").write_text("name: broken\nkind: bogus\n")
    (tmp_path / "apis" / "garbled.yaml").write_text("a: [unclosed\n")
    (tmp_path / "apis" / "Bad Name.yaml").write_text("name: x\n")
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
