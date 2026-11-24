"""Event envelope and topic-schema validation (plan.md §6.3, §6.4)."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from configui.store import Store

MAX_PAYLOAD_BYTES = 64 * 1024


def new_id(prefix: str) -> str:
    """Time-ordered id, e.g. ev_0193a4b2c1d7f3a9c2b1."""
    return f"{prefix}_{int(time.time() * 1000):011x}{secrets.token_hex(5)}"


def derived_id(causation_id: str, agent: str, topic: str, index: int) -> str:
    """Deterministic output id. A retried run of the same trigger event yields the same ids, so downstream
    dedupe on event_id collapses the duplicates that at-least-once delivery would otherwise create."""
    h = hashlib.sha256(f"{causation_id}|{agent}|{topic}|{index}".encode()).hexdigest()[:20]
    return f"ev_d{h}"


def make_envelope(topic: str, produced_by: str, payload: dict, causation_id: str | None = None, trace_id: str | None = None,
                  event_id: str | None = None) -> dict:
    return {
        "event_id": event_id or new_id("ev"),
        "type": topic,
        "produced_by": produced_by,
        "produced_at": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "causation_id": causation_id,
        "trace_id": trace_id or new_id("tr"),
        "payload": payload,
    }


class SchemaRegistry:
    """Topic schemas live in config/topics (edited by the config UI); this loads and applies them."""

    def __init__(self, store: Store):
        self.store = store
        self._validators: dict[str, tuple[float, Draft202012Validator, dict]] = {}

    def reload(self) -> None:
        self._validators.clear()

    def topic(self, name: str) -> dict | None:
        try:
            return self.store.read("topic", name)
        except Exception:  # unreadable config is treated as "no such topic": fail closed
            return None

    def _validator(self, name: str) -> tuple[Draft202012Validator, dict] | None:
        entry = self.topic(name)
        if not entry or not isinstance(entry.get("schema"), dict):
            return None
        cached = self._validators.get(name)
        if cached and cached[2] is entry:
            return cached[1], entry
        v = Draft202012Validator(entry["schema"], format_checker=FormatChecker())
        self._validators[name] = (time.time(), v, entry)
        return v, entry

    def validate(self, name: str, payload: Any) -> list[str]:
        """Return a list of violations; empty means valid. Unknown topics are a violation."""
        found = self._validator(name)
        if found is None:
            return [f"topic '{name}' has no registered schema"]
        if not isinstance(payload, dict):
            return ["payload must be an object"]
        if len(json.dumps(payload).encode()) > MAX_PAYLOAD_BYTES:
            return [f"payload exceeds {MAX_PAYLOAD_BYTES} bytes"]
        v, _ = found
        errs = sorted(v.iter_errors(payload), key=lambda e: list(e.absolute_path))
        return [f".{'.'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in errs]

    def partition_key(self, name: str, payload: dict) -> str | None:
        entry = self.topic(name) or {}
        k = entry.get("key")
        return str(payload[k]) if k and k in payload else None
