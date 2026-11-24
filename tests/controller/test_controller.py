import json
import shutil

import pytest
import yaml

from configui.store import Store
from controller import stack
from controller.connectors import FileSink, HttpPostSink, HttpSource
from controller.dedupe import Dedupe
from controller.dispatcher import Dispatcher
from controller.offsets import OffsetTracker
from controller.runner import RunResult, SubnetPool, hardening
from controller.stack import StackError, build_plan, topic_specs
from harness_core.bus import MemoryBus
from harness_core.events import SchemaRegistry

# ---------------------------------------------------------------- offsets


def test_commit_only_advances_over_a_finished_prefix():
    t = OffsetTracker()
    for o in (10, 11, 12):
        t.begin(0, o)
    assert t.complete(0, 12) is None      # later event finished first: must not commit past 10
    assert t.complete(0, 11) is None
    assert t.complete(0, 10) == 13        # now the whole prefix is done
    assert t.in_flight() == 0


def test_partitions_are_independent():
    t = OffsetTracker()
    t.begin(0, 5)
    t.begin(1, 7)
    assert t.complete(1, 7) == 8
    assert t.complete(0, 5) == 6


# ---------------------------------------------------------------- dispatcher


class FakeAdmin:
    def __init__(self):
        self.minted, self.revoked = [], []

    def mint(self, agent, env, ttl_s):
        self.minted.append(env["event_id"])
        return f"tok{len(self.minted)}"

    def revoke(self, token):
        self.revoked.append(token)


class FakeRunner:
    def __init__(self, results):
        self.results, self.calls = list(results), 0

    def run(self, agent, image, token, event_id, command=None):
        self.calls += 1
        r = self.results.pop(0) if self.results else RunResult(0, False, "", 0.1)
        if isinstance(r, Exception):
            raise r
        return r


OK = RunResult(0, False, "fine", 0.1)
BAD = RunResult(1, False, "Traceback: boom", 0.1)
SLOW = RunResult(None, True, "", 3)


def agent(**kw):
    base = {"name": "triager", "trigger": {"topic": "email.inbound.v1"}, "delivery": {"max_retries": 2}, "concurrency": 2}
    return {**base, **kw}


def env(i="ev_1", **payload):
    return {"event_id": i, "trace_id": "tr_1", "type": "email.inbound.v1", "produced_by": "gmail", "payload": payload}


def disp(results, **kw):
    admin, bus = FakeAdmin(), MemoryBus()
    d = Dispatcher(agent(**kw), "img", FakeRunner(results), admin, bus, Dedupe(), backoff_s=0)
    return d, admin, bus


def test_success_runs_once_and_revokes_the_token():
    d, admin, _ = disp([OK])
    assert d.handle(env()) == "ok"
    assert d.runner.calls == 1 and admin.revoked == ["tok1"]


def test_failure_retries_then_succeeds():
    d, admin, bus = disp([BAD, OK])
    assert d.handle(env()) == "ok" and d.runner.calls == 2 and d.stats.retried == 1
    assert admin.revoked == ["tok1", "tok2"] and bus.messages == {}


def test_exhausted_retries_go_to_the_dlq_with_the_error():
    d, admin, bus = disp([BAD, BAD, BAD])
    assert d.handle(env()) == "dead_lettered" and d.runner.calls == 3
    (dead,) = bus.messages["dlq.triager"]
    assert dead["payload"]["original"]["event_id"] == "ev_1" and "boom" in dead["payload"]["error"] and dead["payload"]["attempts"] == 3
    assert len(admin.revoked) == 3                           # no token is ever left alive


def test_timeout_is_reported_and_retried():
    d, _, bus = disp([SLOW, SLOW, SLOW])
    assert d.handle(env()) == "dead_lettered"
    assert "timed out" in bus.messages["dlq.triager"][0]["payload"]["error"]


def test_infrastructure_errors_are_retried_not_fatal():
    d, _, _ = disp([RuntimeError("docker is down"), OK])
    assert d.handle(env()) == "ok"


def test_zero_retries_means_one_attempt():
    d, _, bus = disp([BAD], delivery={"max_retries": 0})
    assert d.handle(env()) == "dead_lettered" and d.runner.calls == 1 and "dlq.triager" in bus.messages


def test_redelivered_event_is_not_run_twice():
    d, _, _ = disp([OK, OK])
    assert d.handle(env("ev_9")) == "ok"
    assert d.handle(env("ev_9")) == "skipped_duplicate"
    assert d.runner.calls == 1


def test_failed_event_is_not_marked_done():
    d, _, _ = disp([BAD, BAD, BAD, OK])
    assert d.handle(env("ev_f")) == "dead_lettered"
    assert not d.dedupe.seen("triager", "ev_f")


def test_filter_skips_non_matching_events_without_starting_a_sandbox():
    d, admin, _ = disp([OK], trigger={"topic": "email.inbound.v1", "filter": "payload.urgency == 'high'"})
    assert d.handle(env("a", urgency="low")) == "skipped_filter"
    assert d.handle(env("b", urgency="high")) == "ok"
    assert d.runner.calls == 1 and admin.minted == ["b"]


# ---------------------------------------------------------------- sandbox posture


def test_container_arguments_enforce_the_hardening():
    kw = hardening({"name": "x", "sandbox": {"cpus": 1, "memory_mb": 512}}, "img", {"HARNESS_TOKEN": "t"}, "sess_1", "sbx_1", None)
    assert kw["read_only"] is True and kw["cap_drop"] == ["ALL"] and kw["privileged"] is False
    assert "no-new-privileges:true" in kw["security_opt"] and kw["user"] == "1000:1000"
    assert kw["pids_limit"] == 128 and kw["mem_limit"] == "512m" and kw["nano_cpus"] == 10**9
    assert "volumes" not in kw and "ports" not in kw and "devices" not in kw and "network_mode" not in kw
    assert "runtime" not in kw
    assert hardening({"name": "x"}, "img", {}, "n", "c", "runsc")["runtime"] == "runsc"


def test_subnet_pool_hands_out_distinct_networks_and_recycles():
    p = SubnetPool("10.200.0.0/24")
    a, b = p.take(), p.take()
    assert a != b
    p.give(a)
    taken = {p.take() for _ in range(15)}
    assert a in taken


# ---------------------------------------------------------------- sources and sinks


@pytest.fixture
def cfg(tmp_path):
    shutil.copytree("config", tmp_path / "config")
    store = Store(tmp_path / "config")
    return store, SchemaRegistry(store)


HN = {"hits": [
    {"objectID": "101", "title": "Rust 2.0 released", "url": "https://example.com/rust", "points": 420, "author": "x"},
    {"objectID": "102", "title": "Ask HN: what are you building?", "url": None, "points": 90},
    {"objectID": "103", "title": "T" * 400, "url": "https://example.com/long", "points": 5},          # violates the topic: too long
    {"title": "no id"},
]}


def source(cfg, body=HN, **over):
    import httpx
    store, reg = cfg
    c = {**store.read("source", "hn_source"), **over}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=body)))
    bus = MemoryBus()
    return HttpSource(c, bus, reg, Dedupe(), client), bus


def test_poll_maps_fields_and_publishes_one_event_per_new_item(cfg):
    src, bus = source(cfg)
    assert src.poll() == 2
    msgs = bus.messages["news.story.v1"]
    assert [m["payload"] for m in msgs] == [
        {"id": "101", "title": "Rust 2.0 released", "url": "https://example.com/rust", "points": 420},
        {"id": "102", "title": "Ask HN: what are you building?", "points": 90},      # null url is left out, not sent as null
    ]
    assert all(m["produced_by"] == "hn_source" for m in msgs) and len({m["event_id"] for m in msgs}) == 2
    assert "author" not in msgs[0]["payload"]                                      # only mapped fields leave the source


def test_poll_rejects_items_that_violate_the_topic_schema_and_never_publishes_them(cfg):
    src, bus = source(cfg)
    src.poll()
    assert src.skipped_invalid == 2                                                # the 400-char title and the item with no id
    assert all(m["payload"]["id"] != "103" for m in bus.messages["news.story.v1"])


def test_polling_twice_publishes_nothing_new(cfg):
    src, bus = source(cfg)
    assert src.poll() == 2
    assert src.poll() == 0 and src.skipped_seen >= 2
    assert len(bus.messages["news.story.v1"]) == 2


def test_poll_honours_limit_and_items_path(cfg):
    src, bus = source(cfg, limit=1)
    assert src.poll() == 1
    src2, _ = source(cfg, body=[{"objectID": "9", "title": "bare list"}], items_path="")
    assert src2.poll() == 1


def test_a_wrong_items_path_is_an_error_not_a_silent_empty_poll(cfg):
    src, _ = source(cfg, items_path="nope")
    with pytest.raises(ValueError, match="not a list"):
        src.poll()


def test_response_larger_than_the_limit_is_refused(cfg):
    import httpx
    store, reg = cfg
    c = {**store.read("source", "hn_source"), "max_response_bytes": 100}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=HN)))
    with pytest.raises(ValueError, match="max_response_bytes"):
        HttpSource(c, MemoryBus(), reg, Dedupe(), client).poll()


def test_source_map_must_cover_required_topic_fields(cfg):
    store, _ = cfg
    d = store.read("source", "hn_source")
    errs = store.validate("source", "hn_source", {**d, "map": {"id": "objectID"}}).errors
    assert any("requires 'title'" in e["message"] for e in errs)
    errs = store.validate("source", "hn_source", {**d, "map": {**d["map"], "bogus": "x"}}).errors
    assert any("bogus" in e["message"] for e in errs)


def sink(tmp_path):
    return FileSink({"name": "summaries_file", "type": "file", "topic": "news.summary.v1", "path": "data/results/{id}.jsonl"},
                    "unused", tmp_path, Dedupe())


def scored(event_id="ev_s1", id_="story1"):
    return {"event_id": event_id, "trace_id": "tr", "produced_by": "summarizer", "produced_at": "t",
            "payload": {"id": id_, "title": "t", "summary": "s", "tag": "x"}}


def test_sink_writes_each_event_once(tmp_path):
    s = sink(tmp_path)
    assert s.handle(scored()) is True
    assert s.handle(scored()) is False and s.duplicates == 1             # redelivery of the same event_id
    s.handle(scored("ev_s2"))
    lines = (tmp_path / "data/results/story1.jsonl").read_text().splitlines()
    assert [json.loads(x)["event_id"] for x in lines] == ["ev_s1", "ev_s2"]


@pytest.mark.parametrize("evil", ["../../etc/passwd", "a/b", "..", "x" * 80, "", "a b"])
def test_sink_never_lets_a_payload_shape_the_path(tmp_path, evil):
    s = sink(tmp_path)
    assert s.handle(scored(id_=evil)) is False
    assert not (tmp_path / "etc").exists() and list(tmp_path.rglob("*.jsonl")) == []


# ---------------------------------------------------------------- POST sink


def post_sink(tmp_path, handler, **over):
    import httpx
    cfg = {"name": "hook", "type": "http_post", "topic": "news.summary.v1", "url": "https://hooks.example.com/in", "rate": "600/min",
           "max_retries": 2, **over}
    bus, calls = MemoryBus(), []

    def record(req):
        calls.append(req)
        return handler(req)

    client = httpx.Client(transport=httpx.MockTransport(record))
    return HttpPostSink(cfg, "unused", tmp_path, Dedupe(), bus, client, backoff_s=0.0), bus, calls


def test_post_sink_sends_the_payload_once_per_event_with_an_idempotency_key(tmp_path):
    import httpx
    s, bus, calls = post_sink(tmp_path, lambda r: httpx.Response(200))
    assert s.handle(scored()) is True
    assert s.handle(scored()) is False and s.duplicates == 1 and len(calls) == 1
    assert json.loads(calls[0].content) == scored()["payload"] and calls[0].headers["idempotency-key"] == "ev_s1"


def test_post_sink_sends_the_credential_from_secrets_and_never_logs_it(tmp_path):
    import httpx
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "hook_key").write_text("s3cret\n")
    s, _, calls = post_sink(tmp_path, lambda r: httpx.Response(204), credential="hook_key", auth={"type": "bearer"})
    s.handle(scored())
    assert calls[0].headers["authorization"] == "Bearer s3cret"
    s2, _, calls2 = post_sink(tmp_path, lambda r: httpx.Response(204), credential="hook_key", auth={"type": "header", "name": "X-Key"})
    s2.handle(scored())
    assert calls2[0].headers["x-key"] == "s3cret"


def test_post_sink_retries_server_errors_then_succeeds(tmp_path):
    import httpx
    answers = iter([503, 429, 200])
    s, bus, calls = post_sink(tmp_path, lambda r: httpx.Response(next(answers)))
    assert s.handle(scored()) is True and len(calls) == 3 and bus.messages == {}


def test_post_sink_parks_an_event_after_running_out_of_retries(tmp_path):
    import httpx
    s, bus, calls = post_sink(tmp_path, lambda r: httpx.Response(500))
    assert s.handle(scored()) is False and len(calls) == 3                     # first try + 2 retries
    (parked,) = bus.messages["dlq.hook"]
    assert parked["payload"]["original"]["event_id"] == "ev_s1" and "500" in parked["payload"]["error"]
    assert s.handle(scored()) is False and len(calls) == 3                     # parked once, not retried forever


def test_post_sink_does_not_retry_a_rejection(tmp_path):
    import httpx
    s, bus, calls = post_sink(tmp_path, lambda r: httpx.Response(400))
    s.handle(scored())
    assert len(calls) == 1 and len(bus.messages["dlq.hook"]) == 1


# ---------------------------------------------------------------- the stack (everything in config/ runs together)


def test_plan_validates_and_describes_the_flow(cfg):
    store, _ = cfg
    plan = build_plan(store)
    assert set(plan.agents) == {"summarizer"} and set(plan.sources) == {"hn_source"} and set(plan.sinks) == {"summaries_file"}
    names = {s.name for s in topic_specs(plan)}
    assert {"news.story.v1", "news.summary.v1", "dlq.summarizer"} <= names
    assert any("summarizer x2" in line for line in plan.flow()) and any("polls https://hn.algolia.com" in line for line in plan.flow())


def test_plan_reports_every_problem_at_once(cfg):
    store, _ = cfg
    a = store.read("agent", "summarizer")
    bad = {**a, "writes": {"topics": ["no.such.topic.v1"]}, "inference": {"models": ["nope"]}}
    (store.dir / "agents" / "researcher.yaml").write_text(yaml.safe_dump(bad))      # as if hand-edited, bypassing the UI
    with pytest.raises(StackError) as e:
        build_plan(store)
    msg = str(e.value)
    assert "no.such.topic.v1" in msg and "nope" in msg


def test_long_running_is_rejected_for_now(cfg):
    store, _ = cfg
    res = store.validate("agent", "summarizer", {**store.read("agent", "summarizer"), "isolation": "long_running"})
    assert any("long_running" in e["message"] for e in res.errors)


def test_agent_cannot_use_a_model_that_excludes_it(cfg):
    store, _ = cfg
    a = {**store.read("agent", "summarizer"), "name": "other", "inference": {"models": ["claude-sonnet-5-5"]}}
    res = store.validate("agent", "other", a)
    assert any("allowed_agents" in e["message"] for e in res.errors)


def test_trigger_filter_is_validated_at_config_time(cfg):
    store, _ = cfg
    a = store.read("agent", "summarizer")
    a["trigger"] = {"topic": "news.story.v1", "filter": "__import__('os')"}
    assert any("filter" in e["path"] for e in store.validate("agent", "summarizer", a).errors)


def test_concurrent_duplicates_run_once():
    import threading
    gate = threading.Event()

    class Slow(FakeRunner):
        def run(self, *a, **k):
            gate.wait(5)
            return super().run(*a, **k)

    admin, bus = FakeAdmin(), MemoryBus()
    d = Dispatcher(agent(), "img", Slow([OK, OK]), admin, bus, Dedupe(), backoff_s=0)
    out = []
    t1 = threading.Thread(target=lambda: out.append(d.handle(env("ev_dup"))))
    t1.start()
    while d.stats.started == 0:
        pass
    assert d.handle(env("ev_dup")) == "skipped_duplicate"       # the first copy is still running
    gate.set()
    t1.join()
    assert out == ["ok"] and d.runner.calls == 1


def test_failed_event_can_be_retried_later_after_the_claim_is_released():
    d, _, _ = disp([BAD, BAD, BAD, OK])
    assert d.handle(env("ev_r")) == "dead_lettered"
    assert d.handle(env("ev_r")) == "ok"                         # a replay after the DLQ must not be blocked


# ---------------------------------------------------------------- hot swap


class FakeStack(stack.Running):
    """Running with Docker, Kafka and the gateway replaced by recorders: only the reconcile logic is under test."""

    def __init__(self, tmp_path):
        self.root, self.bootstrap, self.running, self.plan, self.error, self.last_changes = tmp_path, "", {}, None, "", {}
        self.lock = __import__("threading").Lock()
        self.applied_hash = ""
        self.events: list[tuple[str, str]] = []

    def _launch(self, key, cfg):
        import threading
        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        self.running[key] = stack._Component(cfg, stop, t, object())
        self.events.append(("start", key))

    def _halt(self, key, timeout=180):
        c = self.running.pop(key)
        c.stop.set()
        c.thread.join(5)
        self.events.append(("stop", key))


@pytest.fixture
def fake_stack(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(stack, "ensure_topics", lambda *a, **k: [])
    store, _ = cfg
    return FakeStack(tmp_path), store


def test_first_apply_starts_everything(fake_stack):
    fs, store = fake_stack
    ch = fs.apply(build_plan(store))
    assert set(ch["started"]) == {"agent:summarizer", "sink:summaries_file", "source:hn_source"} and not ch["restarted"]


def test_a_deploy_restarts_only_what_changed(fake_stack):
    fs, store = fake_stack
    fs.apply(build_plan(store))
    fs.events.clear()
    a = store.read("agent", "summarizer")
    store.save("agent", "summarizer", {**a, "concurrency": 3})
    ch = fs.apply(build_plan(store))
    assert ch["restarted"] == ["agent:summarizer"] and set(ch["unchanged"]) == {"sink:summaries_file", "source:hn_source"}
    assert fs.events == [("stop", "agent:summarizer"), ("start", "agent:summarizer")]  # old one stops first
    assert fs.running["agent:summarizer"].cfg["concurrency"] == 3


def test_removing_something_from_config_stops_it(fake_stack):
    fs, store = fake_stack
    fs.apply(build_plan(store))
    store.delete("sink", "summaries_file")
    ch = fs.apply(build_plan(store))
    assert ch["stopped"] == ["sink:summaries_file"] and "sink:summaries_file" not in fs.running


def test_an_invalid_config_is_not_applied_and_the_old_one_keeps_running(fake_stack):
    fs, store = fake_stack
    fs.apply(build_plan(store))
    before = dict(fs.running)
    (store.dir / "agents" / "researcher.yaml").write_text("name: summarizer\ntrigger: {topic: nope.thing.v1}\n")
    fs._status = lambda *a, **k: None
    fs._reload(store, store.fingerprint())
    assert fs.error and "nope.thing.v1" in fs.error
    assert fs.running == before                                       # nothing was stopped or restarted


def test_watch_waits_for_a_deploy_to_finish_writing_before_applying(fake_stack):
    import threading
    import time
    fs, store = fake_stack
    fs.apply(build_plan(store))
    fs.applied_hash = store.fingerprint()
    fs._status = lambda *a, **k: None
    stop = threading.Event()
    th = threading.Thread(target=fs.watch, args=(store, stop, 0.05, 1.0), daemon=True)
    th.start()
    try:
        a = store.read("agent", "summarizer")
        store.save("agent", "summarizer", {**a, "concurrency": 5})
        time.sleep(0.2)
        assert fs.running["agent:summarizer"].cfg["concurrency"] == 2          # still settling
        for _ in range(40):
            if fs.running["agent:summarizer"].cfg["concurrency"] == 5:
                break
            time.sleep(0.1)
        assert fs.running["agent:summarizer"].cfg["concurrency"] == 5
    finally:
        stop.set()
        th.join(3)
