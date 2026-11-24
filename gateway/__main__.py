"""Run both gateway listeners. Configured by environment (set by compose.yaml)."""
from __future__ import annotations

import asyncio
import os

import uvicorn

from configui.store import Store
from harness_core.audit import AuditLog
from harness_core.bus import KafkaBus

from .app import Gateway, create_admin_app, create_sandbox_app
from .inference import Inference
from .sessions import SessionStore


def build() -> Gateway:
    data = os.environ.get("DATA_DIR", "/data")
    store = Store(os.environ.get("CONFIG_DIR", "/config"))
    admin_token = os.environ.get("HARNESS_ADMIN_TOKEN", "")
    if len(admin_token) < 32:
        raise SystemExit("HARNESS_ADMIN_TOKEN is missing or too short; start the stack with `harness up`")
    return Gateway(
        store=store, bus=KafkaBus(os.environ.get("BOOTSTRAP", "redpanda:9092"), "gateway"),
        audit=AuditLog(os.path.join(data, "audit", "gateway.jsonl")),
        sessions=SessionStore(":memory:"),   # sessions are short-lived; a restart invalidates them by design
        inference=Inference(store, os.environ.get("SECRETS_DIR", "/secrets")), admin_token=admin_token,
    )


async def main() -> None:
    gw = build()
    servers = [
        uvicorn.Server(uvicorn.Config(create_sandbox_app(gw), host="0.0.0.0", port=8080, log_level="warning")),
        uvicorn.Server(uvicorn.Config(create_admin_app(gw), host="0.0.0.0", port=8081, log_level="warning")),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
