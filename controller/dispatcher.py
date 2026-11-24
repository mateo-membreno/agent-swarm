"""Turns events on a topic into sandbox runs for one agent (plan.md §11).

Kafka is the queue. We only poll when a slot is free, so a burst waits in the topic (visible as consumer lag)
instead of piling up as idle sandboxes. Offsets are committed only over a finished prefix, after the agent
exited 0 (its publishes were already acknowledged by the gateway). Failures retry with backoff, then go to
`dlq.<agent>` with the error attached.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from harness_core import filterexpr
from harness_core.events import make_envelope

from .dedupe import Dedupe
from .offsets import OffsetTracker

log = logging.getLogger("harness.dispatcher")


@dataclass
class Stats:
    started: int = 0
    succeeded: int = 0
    skipped_filter: int = 0
    skipped_duplicate: int = 0
    retried: int = 0
    dead_lettered: int = 0
    in_flight: int = 0
    max_in_flight: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def bump(self, **kw) -> None:
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, getattr(self, k) + v)
            self.max_in_flight = max(self.max_in_flight, self.in_flight)


class Dispatcher:
    def __init__(self, agent: dict, image: str, runner, admin, bus, dedupe: Dedupe, *, bootstrap: str | None = None,
                 backoff_s: float = 2.0):
        self.agent = agent
        self.name = agent["name"]
        self.image = image
        self.runner, self.admin, self.bus, self.dedupe = runner, admin, bus, dedupe
        self.bootstrap = bootstrap
        self.concurrency = int(agent.get("concurrency", 1))
        self.max_retries = int((agent.get("delivery") or {}).get("max_retries", 2))
        self.backoff_s = backoff_s
        self.topic = agent["trigger"]["topic"]
        self.filter = agent["trigger"].get("filter")
        self.timeout = int((agent.get("sandbox") or {}).get("timeout_per_event_s", 120))
        self.stats = Stats()
        self._claimed: set[str] = set()      # events running right now (guards against concurrent duplicates)
        self._claim_lock = threading.Lock()

    # -- one event, start to finish; pure of Kafka so it can be tested directly
    def handle(self, env: dict) -> str:
        """Returns one of: ok, skipped_filter, skipped_duplicate, dead_lettered."""
        if self.filter and not filterexpr.matches(self.filter, env):
            self.stats.bump(skipped_filter=1)
            return "skipped_filter"
        with self._claim_lock:
            if self.dedupe.seen(self.name, env["event_id"]) or env["event_id"] in self._claimed:
                self.stats.bump(skipped_duplicate=1)
                return "skipped_duplicate"
            self._claimed.add(env["event_id"])
        try:
            return self._attempts(env)
        finally:
            with self._claim_lock:
                self._claimed.discard(env["event_id"])

    def _attempts(self, env: dict) -> str:
        last_error = ""
        for attempt in range(self.max_retries + 1):
            if attempt:
                self.stats.bump(retried=1)
                time.sleep(min(self.backoff_s * 2 ** (attempt - 1), 30))
            token = self.admin.mint(self.agent, env, ttl_s=self.timeout + 60)
            self.stats.bump(started=1, in_flight=1)
            try:
                res = self.runner.run(self.agent, self.image, token, env["event_id"])
            except Exception as e:  # infrastructure failure (Docker down, etc.) is retried like an agent failure
                last_error = f"runner error: {type(e).__name__}: {e}"
                log.warning("%s attempt %d for %s failed: %s", self.name, attempt + 1, env["event_id"], last_error)
            else:
                if res.ok:
                    self.dedupe.mark(self.name, env["event_id"])
                    self.stats.bump(succeeded=1)
                    return "ok"
                why = f"timed out after {self.timeout}s" if res.timed_out else f"exit code {res.exit_code}"
                last_error = f"{why}\n{res.logs}"
                log.warning("%s attempt %d for %s failed: %s", self.name, attempt + 1, env["event_id"], why)
            finally:
                self.admin.revoke(token)
                self.stats.bump(in_flight=-1)
        self.dead_letter(env, last_error, self.max_retries + 1)
        return "dead_lettered"

    def dead_letter(self, env: dict | None, error: str, attempts: int, raw: bytes | None = None) -> None:
        self.stats.bump(dead_lettered=1)
        payload = {"agent": self.name, "attempts": attempts, "error": error[-3000:],
                   "original": env if env is not None else {"raw": (raw or b"").decode(errors="replace")[:2000]}}
        dlq = make_envelope(f"dlq.{self.name}", "controller", payload, causation_id=(env or {}).get("event_id"),
                            trace_id=(env or {}).get("trace_id"))
        self.bus.publish(f"dlq.{self.name}", dlq, None)
        log.error("dead-lettered event %s for %s after %d attempts", (env or {}).get("event_id"), self.name, attempts)

    # -- the Kafka loop
    def run(self, stop: threading.Event) -> None:
        from confluent_kafka import KafkaError, TopicPartition

        from harness_core.bus import make_consumer

        consumer = make_consumer(self.bootstrap, f"cg.{self.name}", [self.topic], **{"log_level": 3})
        slots = threading.BoundedSemaphore(self.concurrency)
        done: queue.Queue[tuple[int, int]] = queue.Queue()
        tracker = OffsetTracker()
        pool = ThreadPoolExecutor(self.concurrency, thread_name_prefix=f"d-{self.name}")

        def work(msg) -> None:
            try:
                try:
                    env = json.loads(msg.value())
                    if not isinstance(env, dict) or "event_id" not in env:
                        raise ValueError("not an envelope")
                except ValueError:
                    self.dead_letter(None, "unparseable message", 0, msg.value())
                else:
                    self.handle(env)
            except Exception:
                log.exception("dispatcher error for %s", self.name)
            finally:
                done.put((msg.partition(), msg.offset()))
                slots.release()

        def drain() -> None:
            commits: dict[int, int] = {}
            while True:
                try:
                    p, o = done.get_nowait()
                except queue.Empty:
                    break
                nxt = tracker.complete(p, o)
                if nxt is not None:
                    commits[p] = nxt
            if commits:
                consumer.commit(offsets=[TopicPartition(self.topic, p, o) for p, o in commits.items()], asynchronous=False)

        try:
            while not stop.is_set():
                drain()
                if not slots.acquire(timeout=0.5):
                    continue                       # at capacity: leave the backlog in Kafka
                msg = consumer.poll(1.0)
                if msg is None or (msg.error() and msg.error().code() == KafkaError._PARTITION_EOF):
                    slots.release()
                    continue
                if msg.error():
                    slots.release()
                    log.warning("consumer error: %s", msg.error())
                    continue
                tracker.begin(msg.partition(), msg.offset())
                pool.submit(work, msg)
        finally:
            pool.shutdown(wait=True)
            drain()
            consumer.close()
