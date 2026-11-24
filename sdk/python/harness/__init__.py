"""Agent SDK. Standard library only, so a sandbox needs no pip and no internet.

    import harness
    ev = harness.event()                       # the triggering event envelope
    text = harness.infer("qwen2.5-1.5b-q4", "Summarise: ...")
    harness.publish("research.idea.v1", {"run_id": ev["payload"]["run_id"], ...})

The only thing this module can reach is the gateway. Everything else (the broker, the internet,
other sandboxes) is not routable from inside the sandbox.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

__all__ = ["HarnessError", "event", "capabilities", "infer", "publish", "instructions", "output_schema"]


class HarnessError(Exception):
    def __init__(self, status: int, body):
        super().__init__(f"gateway returned {status}: {body}")
        self.status = status
        self.body = body


def _request(method: str, path: str, body: dict | None = None, timeout: float = 300):
    base = os.environ.get("HARNESS_GATEWAY", "http://gateway:8080")
    token = os.environ["HARNESS_TOKEN"]
    req = urllib.request.Request(
        base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
        headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw)
        except ValueError:
            detail = raw
        raise HarnessError(e.code, detail) from None


def event() -> dict:
    return _request("GET", "/v1/event")


def capabilities() -> dict:
    return _request("GET", "/v1/capabilities")


def instructions() -> str:
    """The `instructions` text from this agent's manifest (empty if none)."""
    return os.environ.get("HARNESS_INSTRUCTIONS", "")


def output_schema() -> dict:
    """JSON Schema of the first topic this agent writes to (empty if it writes to none)."""
    return json.loads(os.environ.get("HARNESS_OUTPUT_SCHEMA") or "{}")


def infer(model: str, prompt: str | list, *, system: str | None = None, max_tokens: int = 1024) -> str:
    """Ask a model (by id, as in the agent's inference.models) for a completion. `prompt`: a string or {role, content} list."""
    messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return _request("POST", "/v1/infer", {"model": model, "messages": messages, "max_tokens": max_tokens})["text"]


def publish(topic: str, payload: dict) -> str:
    """Publish to a topic this agent is allowed to write. Returns the new event id."""
    return _request("POST", f"/v1/topics/{topic}:publish", {"payload": payload})["event_id"]
