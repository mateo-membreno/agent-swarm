"""Controller -> gateway admin API client."""
from __future__ import annotations

import httpx


class AdminClient:
    def __init__(self, url: str, token: str):
        self._c = httpx.Client(base_url=url, headers={"authorization": f"Bearer {token}"}, timeout=10)

    def health(self) -> dict:
        r = self._c.get("/admin/health")
        r.raise_for_status()
        return r.json()

    def mint(self, agent: dict, envelope: dict, ttl_s: float) -> str:
        inf = agent.get("inference") or {}
        r = self._c.post("/admin/sessions", json={
            "agent": agent["name"], "event_id": envelope["event_id"], "event": envelope,
            "writes_topics": (agent.get("writes") or {}).get("topics", []),
            "models": inf.get("models", []),
            "token_budget": inf.get("max_tokens_per_event", 20000),
            "ttl_s": ttl_s,
        })
        r.raise_for_status()
        return r.json()["token"]

    def revoke(self, token: str) -> None:
        try:
            self._c.post("/admin/sessions/revoke", json={"token": token})
        except httpx.HTTPError:
            pass   # the token expires on its own

    def audit(self, n: int = 50, event_id: str | None = None) -> list[dict]:
        r = self._c.get("/admin/audit", params={"n": n, **({"event_id": event_id} if event_id else {})})
        r.raise_for_status()
        return r.json()["records"]

    def verify_audit(self) -> dict:
        r = self._c.get("/admin/audit/verify")
        r.raise_for_status()
        return r.json()
