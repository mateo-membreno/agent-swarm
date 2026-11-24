"""Trusted edge components. They run no model and no agent code (plan.md §12, §21.3)."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

import httpx

from harness_core.bus import make_consumer
from harness_core.events import SchemaRegistry, make_envelope

from .dedupe import Dedupe

log = logging.getLogger("harness.connectors")
SAFE_PART = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def parse_duration(text: str) -> float:
    """'30s' / '10m' / '2h' -> seconds."""
    m = re.fullmatch(r"([1-9][0-9]*)(s|m|h)", str(text))
    if not m:
        raise ValueError(f"bad duration '{text}' (use e.g. 30s, 10m, 1h)")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def _dig(obj, path: str):
    """Follow a dotted path through dicts and lists; None when any step is missing."""
    for part in [p for p in path.split(".") if p]:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


class HttpSource:
    """Polls a public HTTP API and publishes one event per new item. Trusted code, no model: the response is
    mapped field by field into the topic's schema, so nothing the API returns reaches a topic unless it validates."""

    def __init__(self, cfg: dict, bus, schemas: SchemaRegistry, dedupe: Dedupe, client: httpx.Client | None = None):
        self.cfg, self.bus, self.schemas, self.dedupe = cfg, bus, schemas, dedupe
        self.max_bytes = int(cfg.get("max_response_bytes", 1_048_576))
        self.timeout = parse_duration(cfg.get("timeout", "10s"))
        self.client = client or httpx.Client(timeout=self.timeout, follow_redirects=False)
        self.published = 0
        self.skipped_invalid = 0
        self.skipped_seen = 0
        self.last_error = ""

    def _fetch(self) -> object:
        with self.client.stream("GET", self.cfg["url"], headers={"accept": "application/json"}) as r:
            r.raise_for_status()
            body = bytearray()
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > self.max_bytes:
                    raise ValueError(f"response from {self.cfg['name']} is larger than max_response_bytes ({self.max_bytes})")
        return json.loads(bytes(body))

    def _items(self, data) -> list[dict]:
        items = _dig(data, self.cfg.get("items_path", "")) if self.cfg.get("items_path") else data
        if not isinstance(items, list):
            raise ValueError(f"'{self.cfg.get('items_path') or '(top level)'}' in the response is not a list")
        return [i for i in items if isinstance(i, dict)][: int(self.cfg.get("limit", 30))]

    def poll(self) -> int:
        """Fetch once, publish every item not seen before. Returns how many were published."""
        topic, key = self.cfg["topic"], f"source:{self.cfg['name']}"
        n = 0
        for item in self._items(self._fetch()):
            ident = _dig(item, self.cfg["id_field"])
            if ident is None:
                self.skipped_invalid += 1
                continue
            ident = str(ident)
            if self.dedupe.seen(key, ident):
                self.skipped_seen += 1
                continue
            payload = {t: v for t, f in self.cfg["map"].items() if (v := _dig(item, f)) is not None}
            errors = self.schemas.validate(topic, payload)
            if errors:
                self.skipped_invalid += 1
                log.warning("source %s: item %s violates %s: %s", self.cfg["name"], ident, topic, "; ".join(errors))
                self.dedupe.mark(key, ident)               # a bad item stays bad; don't retry it every poll
                continue
            self.bus.publish(topic, make_envelope(topic, self.cfg["name"], payload), self.schemas.partition_key(topic, payload))
            self.dedupe.mark(key, ident)                   # marked after publishing: a crash in between re-sends, never drops
            n += 1
        self.published += n
        return n

    def run(self, stop: threading.Event) -> None:
        every = parse_duration(self.cfg.get("interval", "10m"))
        while not stop.is_set():
            try:
                n = self.poll()
                self.last_error = ""
                log.info("source %s: %d new item(s)", self.cfg["name"], n)
            except (httpx.HTTPError, ValueError, OSError) as e:
                self.last_error = str(e)
                log.error("source %s: poll failed: %s", self.cfg["name"], e)
            stop.wait(every)


def consume(bootstrap: str, cfg: dict, handle, stop: threading.Event) -> None:
    """Read a sink's topic and hand each event to `handle`. An event is committed only once it was handled, so a crash
    or an unexpected error re-delivers it instead of losing it."""
    consumer = make_consumer(bootstrap, f"cg.sink.{cfg['name']}", [cfg["topic"]], log_level=3)
    try:
        while not stop.is_set():
            msg = consumer.poll(1.0)
            if msg is None or msg.error():
                continue
            try:
                handle(json.loads(msg.value()))
            except (ValueError, KeyError, OSError, httpx.HTTPError):
                log.exception("sink %s failed on offset %s", cfg["name"], msg.offset())
                continue                       # not committed: it will be retried after a restart
            consumer.commit(message=msg, asynchronous=False)
    finally:
        consumer.close()


class FileSink:
    """Appends each event on a topic to a JSONL file, once per event_id."""

    def __init__(self, cfg: dict, bootstrap: str, root: str | Path, dedupe: Dedupe):
        self.cfg, self.bootstrap, self.dedupe = cfg, bootstrap, dedupe
        self.root = Path(root).resolve()
        self.written = 0
        self.duplicates = 0

    def _target(self, payload: dict) -> Path | None:
        parts = {}
        for ph in re.findall(r"{([a-z_]+)}", self.cfg["path"]):
            v = str(payload.get(ph, ""))
            if not SAFE_PART.match(v):
                return None                      # never let a payload value shape a filesystem path
            parts[ph] = v
        p = (self.root / self.cfg["path"].format(**parts)).resolve()
        return p if (self.root / "data").resolve() in p.parents else None

    def handle(self, env: dict) -> bool:
        key = f"sink:{self.cfg['name']}"
        if self.dedupe.seen(key, env["event_id"]):
            self.duplicates += 1
            return False
        target = self._target(env.get("payload", {}))
        if target is None:
            log.error("sink %s: refusing unsafe or incomplete path for event %s", self.cfg["name"], env["event_id"])
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        line = {k: env.get(k) for k in ("event_id", "trace_id", "produced_by", "produced_at", "payload")}
        with open(target, "a") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
            f.flush()
        self.dedupe.mark(key, env["event_id"])
        self.written += 1
        return True

    def run(self, stop: threading.Event) -> None:
        consume(self.bootstrap, self.cfg, self.handle, stop)


class HttpPostSink:
    """POSTs each event's payload as JSON to a fixed https URL, once per event_id (the id is also sent as an
    Idempotency-Key). 429 and 5xx are retried with backoff; anything else, or running out of retries, parks the event
    on dlq.<sink> with the reason, so nothing is silently dropped and one bad event never blocks the rest."""

    def __init__(self, cfg: dict, bootstrap: str, root: str | Path, dedupe: Dedupe, bus, client: httpx.Client | None = None,
                 backoff_s: float = 1.0):
        self.cfg, self.bootstrap, self.dedupe, self.bus, self.backoff_s = cfg, bootstrap, dedupe, bus, backoff_s
        self.root = Path(root)
        self.client = client or httpx.Client(timeout=parse_duration(cfg.get("timeout", "10s")), follow_redirects=False)
        n, unit = re.fullmatch(r"([0-9]+)/(sec|min|hour)", cfg.get("rate", "30/min")).groups()
        self.min_gap = {"sec": 1, "min": 60, "hour": 3600}[unit] / int(n)
        self._last = 0.0
        self.sent = self.duplicates = self.parked = 0

    def _headers(self, env: dict) -> dict:
        h = {"content-type": "application/json", "idempotency-key": env["event_id"]}
        name = self.cfg.get("credential")
        if name:
            secret = (self.root / "secrets" / name).read_text().strip()      # read per request: rotating a key needs no restart
            auth = self.cfg.get("auth") or {"type": "bearer"}
            if auth["type"] == "bearer":
                h["authorization"] = f"Bearer {secret}"
            else:
                h[auth["name"]] = secret
        return h

    def _park(self, env: dict, error: str, attempts: int) -> None:
        dlq = make_envelope(f"dlq.{self.cfg['name']}", "controller", {"error": error[:500], "attempts": attempts, "original": env},
                            causation_id=env["event_id"], trace_id=env.get("trace_id"))
        self.bus.publish(dlq["type"], dlq, env["event_id"])
        self.parked += 1
        log.error("sink %s parked event %s: %s", self.cfg["name"], env["event_id"], error)

    def handle(self, env: dict) -> bool:
        key = f"sink:{self.cfg['name']}"
        if self.dedupe.seen(key, env["event_id"]):
            self.duplicates += 1
            return False
        retries = int(self.cfg.get("max_retries", 3))
        error = ""
        for attempt in range(retries + 1):
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.client.post(self.cfg["url"], headers=self._headers(env), content=json.dumps(env["payload"]))
            except httpx.HTTPError as e:
                error = f"request failed: {type(e).__name__}"
            else:
                if r.status_code < 300:
                    self.dedupe.mark(key, env["event_id"])
                    self.sent += 1
                    return True
                error = f"server answered {r.status_code}"
                if r.status_code != 429 and r.status_code < 500:
                    break                                          # the server rejected it; retrying the same body won't help
            time.sleep(self.backoff_s * (2 ** attempt))
        self._park(env, error, attempt + 1)
        self.dedupe.mark(key, env["event_id"])
        return False

    def run(self, stop: threading.Event) -> None:
        consume(self.bootstrap, self.cfg, self.handle, stop)
