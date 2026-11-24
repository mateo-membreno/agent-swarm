"""The whole system, for real: Redpanda, the gateway, sandboxes, dispatchers, a connector sink.

Needs `harness up --stub` (the stub LLM answers as http://inference-cpu:8080).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from configui.store import Store
from controller.cli import _scan
from controller.stack import Running, build_plan
from harness_core.bus import KafkaBus
from harness_core.events import make_envelope

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = "127.0.0.1:19092"


def wait_for(cond, timeout=90, every=0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(every)
    return None


def rows(story_id: str) -> list[dict]:
    f = ROOT / "data" / "results" / "e2e" / f"{story_id}.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def e2e_config(dest: Path) -> Store:
    """The shipped chain minus the source (these tests publish stories themselves, so nothing calls the internet),
    with the sink pointed at a folder the test owns."""
    shutil.copytree(ROOT / "config", dest)
    (dest / "sources" / "hn_source.yaml").unlink()
    sink = dest / "sinks" / "summaries_file.yaml"
    sink.write_text(sink.read_text().replace("data/results/summaries.jsonl", "data/results/e2e/{id}.jsonl"))
    return Store(dest)


@pytest.fixture(scope="module")
def pipeline(stack, tmp_path_factory):
    import docker
    if not docker.from_env().containers.list(filters={"name": "harness-inference-cpu"}):
        pytest.skip("stub LLM not running (use `harness up --stub`)")
    store = e2e_config(tmp_path_factory.mktemp("cfg") / "config")
    rp = Running(BOOTSTRAP, stack["admin_url"], stack["admin_token"], ROOT, backoff_s=0.2, store=store)
    rp.start(build_plan(store))
    yield rp
    rp.shutdown()
    shutil.rmtree(ROOT / "data" / "results" / "e2e", ignore_errors=True)


def story(sid: str, title: str = "A story about databases", **extra) -> dict:
    return make_envelope("news.story.v1", "test", {"id": sid, "title": title, "points": 7, **extra})


@pytest.fixture
def send(pipeline):
    bus = KafkaBus(BOOTSTRAP, "test")

    def go(n: int = 1, title: str = "A story about databases") -> list[dict]:
        run = uuid.uuid4().hex[:8]
        envs = [story(f"{run}-{i}", title) for i in range(n)]
        for e in envs:
            bus.publish("news.story.v1", e, e["payload"]["id"])
        return envs
    return go


def test_stories_become_summaries_end_to_end(pipeline, send):
    envs = send(8)
    assert wait_for(lambda: all(rows(e["payload"]["id"]) for e in envs)), "not every story was summarised"
    for e in envs:
        (r,) = rows(e["payload"]["id"])
        p = r["payload"]
        assert p["id"] == e["payload"]["id"] and p["title"] == e["payload"]["title"]       # copied by the agent, not retyped by the model
        assert p["summary"].startswith("Stub summary of:") and p["tag"].startswith("stub")
        assert r["produced_by"] == "summarizer" and r["trace_id"] == e["trace_id"]           # the trace follows the story through


def test_concurrency_cap_is_respected_and_used(pipeline, send):
    peak = 0
    stop = threading.Event()

    def sample():
        nonlocal peak
        while not stop.is_set():
            n = subprocess.run(["docker", "ps", "-q", "--filter", "label=harness.agent=summarizer"], capture_output=True, text=True)
            peak = max(peak, len(n.stdout.split()))
            time.sleep(0.1)

    t = threading.Thread(target=sample)
    t.start()
    envs = send(12)
    wait_for(lambda: all(rows(e["payload"]["id"]) for e in envs), timeout=120)
    stop.set()
    t.join()
    cap = pipeline.plan.agents["summarizer"]["concurrency"]
    assert 1 < peak <= cap, f"peak {peak} with cap {cap}"


def test_duplicate_delivery_produces_one_result(pipeline):
    """At-least-once means the same event can arrive twice. It must still be processed once."""
    bus = KafkaBus(BOOTSTRAP, "test")
    env = story(f"dup-{uuid.uuid4().hex[:8]}")
    for _ in range(2):
        bus.publish("news.story.v1", env, env["payload"]["id"])
    assert wait_for(lambda: len(rows(env["payload"]["id"])) >= 1)
    time.sleep(6)                                                 # give a wrongly-processed duplicate time to appear
    assert len(rows(env["payload"]["id"])) == 1
    assert pipeline.dispatchers["summarizer"].stats.skipped_duplicate >= 1


def test_failing_agent_retries_then_dead_letters_with_the_error(pipeline):
    bus = KafkaBus(BOOTSTRAP, "test")
    env = story(f"poison-{uuid.uuid4().hex[:8]}", "__FAIL__")
    d = pipeline.dispatchers["summarizer"]
    before = d.stats.dead_lettered
    bus.publish("news.story.v1", env, env["payload"]["id"])
    assert wait_for(lambda: d.stats.dead_lettered > before, timeout=90)
    dead = [e for e in _scan(["dlq.summarizer"]) if e["payload"]["original"]["event_id"] == env["event_id"]]
    assert dead and dead[0]["payload"]["attempts"] == 3
    assert rows(env["payload"]["id"]) == []


def test_a_bad_event_does_not_block_the_good_ones(pipeline, send):
    bus = KafkaBus(BOOTSTRAP, "test")
    bad = story("poison-x", "__FAIL__")
    bus.publish("news.story.v1", bad, "poison-x")
    envs = send(6)
    assert wait_for(lambda: all(rows(e["payload"]["id"]) for e in envs), timeout=150), "good events were stuck behind a poison event"


def test_gateway_audit_chain_is_intact_after_all_of_it(stack):
    import httpx
    r = httpx.get(f"{stack['admin_url']}/admin/audit/verify", headers={"authorization": f"Bearer {stack['admin_token']}"}).json()
    assert r["ok"], r


def test_no_sandboxes_or_networks_are_left_running(pipeline):
    import docker
    time.sleep(2)
    d = docker.from_env()
    assert d.containers.list(all=True, filters={"label": "harness.sandbox=1"}) == []
    assert d.networks.list(filters={"label": "harness.sandbox=1"}) == []
