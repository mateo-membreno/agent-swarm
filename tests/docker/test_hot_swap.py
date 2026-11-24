"""A deploy is applied to the running stack without a restart, and nothing in flight is lost."""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid

import pytest

from controller.stack import Running, build_plan
from harness_core.bus import KafkaBus
from harness_core.events import make_envelope

from .test_e2e import BOOTSTRAP, ROOT, e2e_config, rows, wait_for

pytestmark = pytest.mark.docker


def test_deploying_while_work_is_running_swaps_the_config_and_loses_nothing(stack, tmp_path):
    import docker
    if not docker.from_env().containers.list(filters={"name": "harness-inference-cpu"}):
        pytest.skip("stub LLM not running (use `harness up --stub`)")
    store = e2e_config(tmp_path / "config")                  # a private config so the real one is never touched
    rp = Running(BOOTSTRAP, stack["admin_url"], stack["admin_token"], ROOT, backoff_s=0.2, store=store)
    rp.start(build_plan(store), store.fingerprint())
    stop = threading.Event()
    threading.Thread(target=rp.watch, args=(store, stop, 0.2, 0.6), daemon=True).start()
    try:
        bus = KafkaBus(BOOTSTRAP, "t")
        run = uuid.uuid4().hex[:8]
        first = [make_envelope("news.story.v1", "t", {"id": f"{run}-a{i}", "title": "before the deploy"}) for i in range(12)]
        for e in first:
            bus.publish("news.story.v1", e, e["payload"]["id"])
        time.sleep(3)                                          # work is in flight
        before = rp.running["agent:summarizer"].thread
        d = store.read("agent", "summarizer")
        store.save("agent", "summarizer", {**d, "concurrency": 3, "instructions": d["instructions"] + " Be brief."})   # what a deploy does
        assert wait_for(lambda: getattr(rp.running.get("agent:summarizer"), "cfg", {}).get("concurrency") == 3, timeout=240), \
            "new config was never applied"
        assert rp.running["agent:summarizer"].thread is not before
        assert wait_for(lambda: all(rows(e["payload"]["id"]) for e in first), timeout=240), "events in flight were lost"
        assert all(len(rows(e["payload"]["id"])) == 1 for e in first)          # exactly once each
        later = [make_envelope("news.story.v1", "t", {"id": f"{run}-b{i}", "title": "after the deploy"}) for i in range(4)]
        for e in later:
            bus.publish("news.story.v1", e, e["payload"]["id"])
        assert wait_for(lambda: all(rows(e["payload"]["id"]) for e in later), timeout=120), "the new config is not processing work"
        status = json.loads((ROOT / "data" / "stack.status.json").read_text())
        assert status["ok"] and status["config_hash"] == store.fingerprint()
    finally:
        stop.set()
        rp.shutdown()
        shutil.rmtree(ROOT / "data" / "results" / "e2e", ignore_errors=True)
