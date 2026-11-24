"""Integration tests need Docker and a running stack (`harness up`). They skip, not fail, when it isn't there."""
from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

ADMIN_URL = os.environ.get("HARNESS_ADMIN_URL", "http://127.0.0.1:18081")
ROOT = Path(__file__).resolve().parents[2]


def _admin_token() -> str | None:
    if os.environ.get("HARNESS_ADMIN_TOKEN"):
        return os.environ["HARNESS_ADMIN_TOKEN"]
    f = ROOT / "data" / "admin_token"
    return f.read_text().strip() if f.exists() else None


@pytest.fixture(scope="session")
def stack():
    token = _admin_token()
    if not token:
        pytest.skip("no admin token (run `harness up`)")
    try:
        import docker
        docker.from_env().ping()
        r = httpx.get(f"{ADMIN_URL}/admin/health", headers={"authorization": f"Bearer {token}"}, timeout=3)
        r.raise_for_status()
    except Exception as e:
        pytest.skip(f"stack is not running: {type(e).__name__}")
    return {"admin_url": ADMIN_URL, "admin_token": token}


@pytest.fixture(scope="session")
def runner(stack):
    from controller.runner import Runner
    r = Runner()
    r.sweep_orphans()
    yield r
    r.sweep_orphans()


@pytest.fixture
def mint(stack):
    """Mint a session through the real admin API; revoke it afterwards."""
    minted: list[str] = []
    h = {"authorization": f"Bearer {stack['admin_token']}"}

    def _mint(agent="hostile", event_id="ev_test_1", **grant) -> str:
        body = {"agent": agent, "event_id": event_id, "event": {"event_id": event_id, "trace_id": "tr_t", "payload": {}}, **grant}
        r = httpx.post(f"{stack['admin_url']}/admin/sessions", json=body, headers=h)
        r.raise_for_status()
        minted.append(r.json()["token"])
        return minted[-1]

    yield _mint
    for t in minted:
        httpx.post(f"{stack['admin_url']}/admin/sessions/revoke", json={"token": t}, headers=h)
