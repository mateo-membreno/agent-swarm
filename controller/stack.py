"""The running stack: everything in config/ runs together, and a deploy swaps it in without a restart.

`build_plan` validates every source, agent and sink. `Running.apply` reconciles what is running with a plan: it starts
what is new, restarts only what changed (finishing in-flight events first, so nothing is cut off), and stops what was
removed. `Running.watch` does that automatically whenever config/ changes, which is how a canvas deploy takes effect.
A config that doesn't validate is never applied: the previous one keeps running and the error is reported.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from configui.store import Store
from harness_core.bus import KafkaBus, TopicSpec, ensure_topics, parse_retention
from harness_core.events import SchemaRegistry

from .admin import AdminClient
from .connectors import FileSink, HttpPostSink, HttpSource
from .dedupe import Dedupe
from .dispatcher import Dispatcher
from .runner import Runner

log = logging.getLogger("harness.stack")
STATUS_FILE = Path("data") / "stack.status.json"


class StackError(Exception):
    pass


@dataclass
class Plan:
    agents: dict[str, dict]
    sources: dict[str, dict]
    sinks: dict[str, dict]
    topics: dict[str, dict]
    warnings: list[str] = field(default_factory=list)

    def flow(self) -> list[str]:
        lines = [f"[{c['name']}] polls {c['url']} every {c.get('interval', '10m')} -> {c['topic']}" for c in self.sources.values()]
        for a in self.agents.values():
            outs = ", ".join((a.get("writes") or {}).get("topics", [])) or "(nothing)"
            models = ", ".join((a.get("inference") or {}).get("models", [])) or "no inference"
            lines.append(f"{a['trigger']['topic']}  -> ({a['name']} x{a.get('concurrency', 1)}, {models}) -> {outs}")
        for c in self.sinks.values():
            lines.append(f"{c['topic']}  -> [{c['name']}] -> {c['path'] if c['type'] == 'file' else 'POST ' + c['url']}")
        return lines


def build_plan(store: Store) -> Plan:
    """Load and validate everything. Raises StackError listing every problem."""
    problems: list[str] = []
    warnings: list[str] = []

    def load(kind: str, name: str) -> dict:
        data = store.read(kind, name) or {}
        res = store.validate(kind, name, data)
        problems.extend(f"{kind} '{name}': {e['path']}: {e['message']}" for e in res.errors)
        warnings.extend(f"{kind} '{name}': {w}" for w in res.warnings)
        return data

    agents = {a: load("agent", a) for a in store.list("agent")}
    sources = {c: load("source", c) for c in store.list("source")}
    sinks = {c: load("sink", c) for c in store.list("sink")}
    if not (agents or sources or sinks):
        problems.append("nothing is configured yet: add a source, an agent or a sink")
    topics: dict[str, dict] = {}
    produced: set[str] = set()
    for a in agents.values():
        for t in [(a.get("trigger") or {}).get("topic")] + (a.get("writes") or {}).get("topics", []):
            if t:
                topics[t] = store.read("topic", t) or {}
        produced |= set((a.get("writes") or {}).get("topics", []))
    for c in [*sources.values(), *sinks.values()]:
        if c.get("topic"):
            topics[c["topic"]] = store.read("topic", c["topic"]) or {}
    produced |= {c.get("topic") for c in sources.values()}
    for a in agents.values():
        t = (a.get("trigger") or {}).get("topic")
        if t and t not in produced:
            warnings.append(f"agent '{a['name']}' reads '{t}', but nothing produces it (add a source for it).")
    if problems:
        raise StackError("\n".join(problems))
    return Plan(agents, sources, sinks, topics, warnings)


def topic_specs(plan: Plan) -> list[TopicSpec]:
    specs = [TopicSpec(n, int(t.get("partitions", 1)), parse_retention(t.get("retention", "7d"))) for n, t in plan.topics.items()]
    parked = [*plan.agents, *(n for n, c in plan.sinks.items() if c["type"] == "http_post")]
    return specs + [TopicSpec(f"dlq.{a}", 1, parse_retention("30d")) for a in parked]


@dataclass
class _Component:
    cfg: dict
    stop: threading.Event
    thread: threading.Thread
    obj: object


class Running:
    def __init__(self, bootstrap: str, admin_url: str, admin_token: str, root: str | Path = ".", backoff_s: float = 2.0,
                 store: Store | None = None):
        self.bootstrap, self.root = bootstrap, Path(root)
        self.store = store or Store(self.root / "config")
        self.running: dict[str, _Component] = {}           # "agent:summarizer" / "sink:file" / "source:hn"
        self.plan: Plan | None = None
        (self.root / "data").mkdir(parents=True, exist_ok=True)
        self.runner = Runner()
        self.bus = KafkaBus(bootstrap, "controller")
        self.admin = AdminClient(admin_url, admin_token)
        self.dedupe = Dedupe(str(self.root / "data" / "controller.db"))
        self.backoff_s = backoff_s
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.applied_hash = ""
        self.error = ""
        self.last_changes: dict = {}

    # -- compatibility views used by the CLI and tests
    @property
    def dispatchers(self) -> dict[str, Dispatcher]:
        return {k.split(":", 1)[1]: c.obj for k, c in list(self.running.items()) if k.startswith("agent:")}

    @property
    def sinks(self) -> dict[str, FileSink]:
        return {k.split(":", 1)[1]: c.obj for k, c in list(self.running.items()) if k.startswith("sink:")}

    def start(self, plan: Plan, fingerprint: str = "") -> dict:
        if not self.admin.health().get("ok"):
            raise StackError("gateway is not healthy; run `harness up`")
        (self.root / "data").mkdir(exist_ok=True)
        self.runner.sweep_orphans()
        changes = self.apply(plan)
        self.applied_hash = fingerprint
        return changes

    # -- reconcile
    def _desired(self, plan: Plan) -> dict[str, dict]:
        want = {}
        for n, a in plan.agents.items():
            out = ((a.get("writes") or {}).get("topics") or [None])[0]
            # the built-in LLM agent is told the shape of what it must produce; a change to that shape restarts it
            want[f"agent:{n}"] = {**a, "_output": {"topic": out, "schema": (plan.topics.get(out) or {}).get("schema")}} if out else a
        for n, c in plan.sources.items():
            want[f"source:{n}"] = {**c, "_topic": plan.topics.get(c["topic"])}           # a topic schema change restarts the source
        want.update({f"sink:{n}": c for n, c in plan.sinks.items()})
        return want

    def _launch(self, key: str, cfg: dict) -> None:
        kind = key.split(":", 1)[0]
        if kind == "agent":
            if cfg.get("isolation") == "long_running":
                raise StackError(f"agent '{cfg['name']}': long_running is not available in this version")
            image = self.runner.ensure_image(cfg, str(self.root))
            obj = Dispatcher(cfg, image, self.runner, self.admin, self.bus, self.dedupe, bootstrap=self.bootstrap, backoff_s=self.backoff_s)
        elif kind == "source":
            obj = HttpSource(cfg, self.bus, SchemaRegistry(self.store), self.dedupe)
        elif cfg["type"] == "http_post":
            obj = HttpPostSink(cfg, self.bootstrap, self.root, self.dedupe, self.bus)
        else:
            obj = FileSink(cfg, self.bootstrap, self.root, self.dedupe)
        stop = threading.Event()
        t = threading.Thread(target=obj.run, args=(stop,), name=key, daemon=True)
        t.start()
        self.running[key] = _Component(cfg, stop, t, obj)

    def _halt(self, key: str, timeout: float = 180) -> None:
        c = self.running.pop(key)
        c.stop.set()                                         # stops polling; events already running are allowed to finish
        c.thread.join(timeout)

    def apply(self, plan: Plan) -> dict:
        with self.lock:
            ensure_topics(self.bootstrap, topic_specs(plan))
            want = self._desired(plan)
            changes = {"started": [], "restarted": [], "stopped": [], "unchanged": []}
            for key in [k for k in self.running if k not in want]:
                self._halt(key)
                changes["stopped"].append(key)
            for key, cfg in want.items():
                cur = self.running.get(key)
                if cur is None:
                    self._launch(key, cfg)
                    changes["started"].append(key)
                elif cur.cfg != cfg:
                    self._halt(key)
                    self._launch(key, cfg)
                    changes["restarted"].append(key)
                else:
                    changes["unchanged"].append(key)
            self.plan = plan
            self.last_changes = changes
            return changes

    # -- hot swap
    def watch(self, store: Store, stop: threading.Event, poll_s: float = 1.0, settle_s: float = 1.5) -> None:
        """Apply config changes as they land. A deploy writes several files, so wait until they stop changing."""
        applied = self.applied_hash
        pending, since, beat = None, 0.0, 0.0
        while not stop.is_set():
            now = time.time()
            fp = store.fingerprint()
            if fp != applied:
                if fp != pending:
                    pending, since = fp, now
                elif now - since >= settle_s:
                    self._reload(store, fp)
                    applied, pending = fp, None
            if now - beat >= 3:
                self._status(applied, applying=pending is not None)
                beat = now
            stop.wait(poll_s)

    def _reload(self, store: Store, fp: str) -> None:
        try:
            changes = self.apply(build_plan(store))
            self.error = ""
            log.info("applied config: %s", {k: v for k, v in changes.items() if v and k != "unchanged"})
        except Exception as e:   # an invalid or unbuildable config never replaces the one that is running
            self.error = str(e)
            log.error("config not applied; previous config keeps running: %s", e)
        self.applied_hash = fp
        self._status(fp)

    def _status(self, fp: str, applying: bool = False) -> None:
        path = self.root / STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"heartbeat": time.time(), "config_hash": self.applied_hash or fp, "ok": not self.error, "error": self.error[:2000],
                "applying": applying, "changes": self.last_changes, "running": sorted(self.running)}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        os.replace(tmp, path)

    def shutdown(self, timeout: float = 30) -> None:
        self.stop.set()
        for key in list(self.running):
            self._halt(key, timeout)
        try:
            (self.root / STATUS_FILE).unlink(missing_ok=True)
        except OSError:
            pass
        self.runner.sweep_orphans()
