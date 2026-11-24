import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from configui.store import Store
from gateway.app import Gateway, create_admin_app, create_sandbox_app
from gateway.inference import Inference
from gateway.sessions import SessionStore
from harness_core import audit
from harness_core.bus import MemoryBus

ADMIN = "admin-secret-token"
EVENT = {"event_id": "ev_1", "type": "news.story.v1", "trace_id": "tr_1", "payload": {}}
GOOD = {"id": "s1", "title": "t", "summary": "hi", "tag": "work"}


class Rig:
    def __init__(self, tmp_path, handler):
        shutil.copytree("config", tmp_path / "config")
        (tmp_path / "secrets").mkdir()
        (tmp_path / "secrets" / "anthropic_api_key").write_text("sk-SECRET-VALUE\n")
        self.store = Store(tmp_path / "config")
        self.bus = MemoryBus()
        self.audit_path = tmp_path / "audit.jsonl"
        self.requests: list[httpx.Request] = []

        def record(req):
            self.requests.append(req)
            return handler(req)

        inf = Inference(self.store, str(tmp_path / "secrets"), httpx.AsyncClient(transport=httpx.MockTransport(record)))
        self.gw = Gateway(self.store, self.bus, audit.AuditLog(self.audit_path), SessionStore(), inf, ADMIN)
        self.sandbox = TestClient(create_sandbox_app(self.gw))
        self.admin = TestClient(create_admin_app(self.gw))

    def mint(self, agent="summarizer", **kw):
        body = {"agent": agent, "event_id": "ev_1", "event": EVENT, "writes_topics": ["news.summary.v1"],
                "models": ["qwen2.5-1.5b-q4"], **kw}
        r = self.admin.post("/admin/sessions", json=body, headers={"authorization": f"Bearer {ADMIN}"})
        assert r.status_code == 200, r.text
        return {"authorization": f"Bearer {r.json()['token']}"}


def llm_ok(req):
    if "anthropic.com" in str(req.url):
        return httpx.Response(200, json={"content": [{"type": "text", "text": "claude says hi"}],
                                         "usage": {"input_tokens": 3, "output_tokens": 4}})
    return httpx.Response(200, json={"choices": [{"message": {"content": "local says hi"}}],
                                     "usage": {"prompt_tokens": 5, "completion_tokens": 6}})


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path, llm_ok)


def test_requires_valid_token(rig):
    assert rig.sandbox.get("/v1/event").status_code == 401
    assert rig.sandbox.get("/v1/event", headers={"authorization": "Bearer nope"}).status_code == 401


def test_token_is_scoped_to_its_own_event(rig):
    a = rig.mint(agent="summarizer", event_id="ev_A", event={**EVENT, "event_id": "ev_A"})
    b = rig.mint(agent="other", event_id="ev_B", event={**EVENT, "event_id": "ev_B"}, writes_topics=["news.story.v1"])
    assert rig.sandbox.get("/v1/event", headers=a).json()["event_id"] == "ev_A"
    assert rig.sandbox.get("/v1/event", headers=b).json()["event_id"] == "ev_B"
    # B's grant (slack.post) does not leak to A, and A's does not leak to B
    assert rig.sandbox.post("/v1/topics/news.story.v1:publish", headers=a, json={"payload": {}}).status_code == 403
    assert rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=b, json={"payload": GOOD}).status_code == 403


def test_revoked_and_expired_tokens_stop_working(rig):
    h = rig.mint()
    token = h["authorization"].split()[1]
    assert rig.sandbox.get("/v1/event", headers=h).status_code == 200
    rig.admin.post("/admin/sessions/revoke", json={"token": token}, headers={"authorization": f"Bearer {ADMIN}"})
    assert rig.sandbox.get("/v1/event", headers=h).status_code == 401
    h2 = rig.mint(ttl_s=-1)
    assert rig.sandbox.get("/v1/event", headers=h2).status_code == 401


def test_admin_requires_admin_token(rig):
    assert rig.admin.post("/admin/sessions", json={}).status_code == 401
    assert rig.admin.post("/admin/sessions", json={}, headers={"authorization": "Bearer wrong"}).status_code == 401
    # a sandbox token is not an admin token
    assert rig.admin.get("/admin/health", headers=rig.mint()).status_code == 401


def test_publish_ok_sets_envelope_server_side(rig):
    r = rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=rig.mint(),
                         json={"payload": GOOD, "produced_by": "someone_else", "trace_id": "tr_forged", "event_id": "ev_forged"})
    assert r.status_code == 200
    (env,) = rig.bus.messages["news.summary.v1"]
    assert env["produced_by"] == "summarizer" and env["causation_id"] == "ev_1" and env["trace_id"] == "tr_1"
    assert env["event_id"] != "ev_forged" and r.json()["event_id"] == env["event_id"]


def test_publish_denied_for_ungranted_topic(rig):
    r = rig.sandbox.post("/v1/topics/news.story.v1:publish", headers=rig.mint(), json={"payload": {}})
    assert r.status_code == 403 and "news.story.v1" not in rig.bus.messages


def test_publish_rejects_schema_violations(rig):
    bad = {**GOOD, "tag": "x" * 100, "extra": 1}
    r = rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=rig.mint(), json={"payload": bad})
    assert r.status_code == 400 and r.json()["violations"]
    assert rig.bus.messages == {}


def test_publish_surfaces_bus_failure(rig):
    rig.bus.fail = True
    r = rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=rig.mint(), json={"payload": GOOD})
    assert r.status_code == 502


def test_audit_failure_fails_closed(rig):
    h = rig.mint()
    rig.gw.audit.path = rig.audit_path.parent / "gone" / "x.jsonl"
    r = rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=h, json={"payload": GOOD})
    assert r.status_code == 503 and rig.bus.messages == {}


def test_audit_chain_covers_every_decision(rig):
    h = rig.mint()
    rig.sandbox.post("/v1/topics/news.story.v1:publish", headers=h, json={"payload": {}})
    rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=h, json={"payload": GOOD})
    ok, _ = audit.verify(rig.audit_path)
    ops = [json.loads(line)["outcome"] for line in rig.audit_path.read_text().splitlines() if "outcome" in line]
    assert ok and "denied" in ops and "attempt" in ops


def test_infer_local(rig):
    msg = {"model": "qwen2.5-1.5b-q4", "messages": [{"role": "user", "content": "hi"}]}
    r = rig.sandbox.post("/v1/infer", headers=rig.mint(), json=msg)
    assert r.status_code == 200 and r.json()["text"] == "local says hi" and r.json()["usage"] == {"input": 5, "output": 6}
    assert str(rig.requests[0].url) == "http://inference-cpu:8080/v1/chat/completions"


def test_infer_denied_when_not_granted_or_not_allowed(rig):
    msg = {"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x"}]}
    assert rig.sandbox.post("/v1/infer", headers=rig.mint(), json=msg).status_code == 403          # not granted on the session
    # granted on the session, but the model's own allowed_agents (= summarizer) still refuses another agent
    h = rig.mint(agent="other", models=["qwen2.5-1.5b-q4", "claude-sonnet-5-5"])
    not_allowed = rig.sandbox.post("/v1/infer", headers=h, json=msg)
    assert not_allowed.status_code == 403 and rig.requests == []


def test_hosted_call_uses_credential_but_never_returns_it(rig):
    h = rig.mint(models=["claude-sonnet-5-5"])
    r = rig.sandbox.post("/v1/infer", headers=h, json={"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json()["text"] == "claude says hi"
    assert rig.requests[0].headers["x-api-key"] == "sk-SECRET-VALUE"
    assert "sk-SECRET-VALUE" not in r.text and "sk-SECRET-VALUE" not in rig.audit_path.read_text()


def test_token_budget_enforced(rig):
    h = rig.mint(token_budget=10)
    body = {"model": "qwen2.5-1.5b-q4", "messages": [{"role": "user", "content": "hi"}]}
    assert rig.sandbox.post("/v1/infer", headers=h, json=body).status_code == 200   # uses 11
    assert rig.sandbox.post("/v1/infer", headers=h, json=body).status_code == 429


def test_infer_validates_messages(rig):
    r = rig.sandbox.post("/v1/infer", headers=rig.mint(), json={"model": "qwen2.5-1.5b-q4", "messages": "hello"})
    assert r.status_code == 400


def test_explicit_fallback_is_used_only_when_configured(tmp_path):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(503) if "inference-cpu" in str(req.url) else llm_ok(req)

    rig = Rig(tmp_path, handler)
    body = {"model": "qwen2.5-1.5b-q4", "messages": [{"role": "user", "content": "x"}]}
    r = rig.sandbox.post("/v1/infer", headers=rig.mint(), json=body)
    assert r.status_code == 502 and len(calls) == 1          # no fallback configured -> no silent hop to a hosted model
    model = rig.store.read("model", "qwen2.5-1.5b-q4")
    rig.store.save("model", "qwen2.5-1.5b-q4", {**model, "fallback": ["claude-sonnet-5-5"]})
    r = rig.sandbox.post("/v1/infer", headers=rig.mint(models=["qwen2.5-1.5b-q4"]), json=body)
    assert r.status_code == 200 and r.json()["model"] == "claude-sonnet-5-5"


def test_capabilities_listing(rig):
    c = rig.sandbox.get("/v1/capabilities", headers=rig.mint()).json()
    assert c["writes_topics"] == ["news.summary.v1"] and c["models"] == ["qwen2.5-1.5b-q4"] and c["agent"] == "summarizer"


def test_admin_api_refuses_callers_from_the_sandbox_pool(rig):
    client = TestClient(create_admin_app(rig.gw), client=("10.200.3.2", 5555))
    r = client.get("/admin/health", headers={"authorization": f"Bearer {ADMIN}"})   # even WITH the right token
    assert r.status_code == 403


def test_output_ids_are_deterministic_so_retries_dedupe(rig):
    ids = []
    for _ in range(2):                                   # two runs (a retry) of the same trigger event
        h = rig.mint(event_id="ev_same")
        ids.append([rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=h, json={"payload": GOOD}).json()["event_id"]
                    for _ in range(2)])
    assert ids[0] == ids[1]                              # same trigger -> same output ids across attempts
    assert ids[0][0] != ids[0][1]                        # but two publishes within one run stay distinct
    other = rig.mint(event_id="ev_other")
    assert rig.sandbox.post("/v1/topics/news.summary.v1:publish", headers=other, json={"payload": GOOD}).json()["event_id"] != ids[0][0]
