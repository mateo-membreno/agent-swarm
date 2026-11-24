"""The gateway: the only door out of a sandbox (plan.md §9).

Two separate FastAPI apps share one `Gateway` state object:
  sandbox_app  - bearer = per-event session token; reachable only on a sandbox's private network
  admin_app    - bearer = admin token; published on host loopback only; used by the controller
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import re
from dataclasses import dataclass, field

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from configui.store import Store
from harness_core.audit import AuditLog, AuditUnavailable
from harness_core.bus import Bus, BusError
from harness_core.events import SchemaRegistry, derived_id, make_envelope

from .inference import Inference, InferenceError
from .sessions import Session, SessionStore

MAX_TOKENS_CAP = 8192


@dataclass
class Gateway:
    store: Store
    bus: Bus
    audit: AuditLog
    sessions: SessionStore
    inference: Inference
    admin_token: str
    schemas: SchemaRegistry = field(init=False)
    http: httpx.AsyncClient = field(default_factory=lambda: httpx.AsyncClient(timeout=15, follow_redirects=False))

    def __post_init__(self):
        self.schemas = SchemaRegistry(self.store)

    def record(self, **rec):
        """Write an audit line. Raises HTTP 503 (fail closed) if it cannot be written."""
        try:
            return self.audit.append(**rec)
        except AuditUnavailable:
            raise HTTPException(503, "audit log unavailable; refusing the operation") from None


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "missing bearer token")
    return authorization[7:].strip()


def create_sandbox_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="harness gateway", docs_url=None, redoc_url=None, openapi_url=None)

    def session(authorization: str | None = Header(default=None)) -> tuple[str, Session]:
        token = _bearer(authorization)
        s = gw.sessions.get(token)
        if s is None:
            raise HTTPException(401, "invalid or expired session token")
        return token, s

    @app.get("/v1/event")
    def event(auth=Depends(session)):
        _, s = auth
        return s.event

    @app.get("/v1/capabilities")
    def capabilities(auth=Depends(session)):
        _, s = auth
        return {"agent": s.agent, "writes_topics": s.writes_topics, "models": s.models,
                "token_budget": s.token_budget, "tokens_used": s.tokens_used}

    @app.post("/v1/infer")
    async def infer(request: Request, auth=Depends(session)):
        token, s = auth
        body = await request.json()
        alias, messages = body.get("model"), body.get("messages")
        if alias not in s.models:
            gw.record(op="infer", agent=s.agent, event_id=s.event_id, model=alias, outcome="denied")
            raise HTTPException(403, f"model '{alias}' is not granted to this agent")
        if not isinstance(messages, list) or not messages or not all(
                isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and isinstance(m.get("content"), str)
                for m in messages):
            raise HTTPException(400, "messages must be a non-empty list of {role, content}")
        if s.tokens_used >= s.token_budget:
            gw.record(op="infer", agent=s.agent, event_id=s.event_id, model=alias, outcome="budget_exceeded")
            raise HTTPException(429, "per-event token budget exhausted")
        max_tokens = max(1, min(int(body.get("max_tokens") or 1024), MAX_TOKENS_CAP))
        try:
            out = await gw.inference.complete(alias, s.agent, messages, max_tokens)
        except InferenceError as e:
            gw.record(op="infer", agent=s.agent, event_id=s.event_id, model=alias, outcome="error", detail=str(e))
            raise HTTPException(e.status, str(e)) from None
        used = out["usage"]["input"] + out["usage"]["output"]
        gw.sessions.charge(token, used)
        gw.record(op="infer", agent=s.agent, event_id=s.event_id, model=out["model"], tokens=used, outcome="ok")
        return out

    @app.post("/v1/topics/{target}")
    async def publish(target: str, request: Request, auth=Depends(session)):
        token, s = auth
        if not target.endswith(":publish"):
            raise HTTPException(404, "not found")
        topic = target[: -len(":publish")]
        if topic not in s.writes_topics:
            gw.record(op="publish", agent=s.agent, event_id=s.event_id, topic=topic, outcome="denied")
            raise HTTPException(403, f"agent '{s.agent}' may not write to '{topic}'")
        body = await request.json()
        payload = body.get("payload") if isinstance(body, dict) else None
        errors = gw.schemas.validate(topic, payload)
        if errors:
            gw.record(op="publish", agent=s.agent, event_id=s.event_id, topic=topic, outcome="schema_rejected")
            return JSONResponse({"error": "payload violates the topic schema", "violations": errors}, status_code=400)
        # produced_by, causation_id and trace_id are set here, never taken from the agent (invariant I5).
        idx = gw.sessions.next_index(token, topic)
        env = make_envelope(topic, s.agent, payload, causation_id=s.event_id, trace_id=s.event.get("trace_id"),
                            event_id=derived_id(s.event_id, s.agent, topic, idx))
        gw.record(op="publish", agent=s.agent, event_id=s.event_id, topic=topic, new_event_id=env["event_id"], outcome="attempt")
        try:
            await asyncio.to_thread(gw.bus.publish, topic, env, gw.schemas.partition_key(topic, payload))
        except BusError as e:
            gw.record(op="publish", agent=s.agent, event_id=s.event_id, topic=topic, new_event_id=env["event_id"], outcome="bus_error")
            raise HTTPException(502, f"bus error: {e}") from None
        return {"event_id": env["event_id"]}

    return app


SANDBOX_POOL = ipaddress.ip_network("10.200.0.0/16")   # must match controller.runner.SubnetPool


def create_admin_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="harness gateway admin", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_sandbox_callers(request: Request, call_next):
        # The admin port shares the gateway's interfaces, so a sandbox can route to it. Refuse by source address,
        # before the token is even looked at.
        try:
            peer = ipaddress.ip_address(request.client.host) if request.client else None
        except ValueError:
            peer = None
        if peer is not None and peer in SANDBOX_POOL:
            return JSONResponse({"detail": "admin API is not available to sandboxes"}, status_code=403)
        return await call_next(request)

    def admin(authorization: str | None = Header(default=None)):
        if not hmac.compare_digest(_bearer(authorization), gw.admin_token):
            raise HTTPException(401, "bad admin token")

    @app.get("/admin/health", dependencies=[Depends(admin)])
    def health():
        ok = getattr(gw.bus, "healthy", lambda: True)()
        return {"ok": bool(ok), "bus": bool(ok)}

    @app.post("/admin/sessions", dependencies=[Depends(admin)])
    async def mint(request: Request):
        b = await request.json()
        for k in ("agent", "event_id", "event"):
            if k not in b:
                raise HTTPException(400, f"missing {k}")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,62}", b["agent"]):
            raise HTTPException(400, "bad agent name")
        s = Session(agent=b["agent"], event_id=b["event_id"], event=b["event"], writes_topics=b.get("writes_topics", []),
                    models=b.get("models", []), token_budget=int(b.get("token_budget", 50_000)))
        gw.record(op="session_minted", agent=s.agent, event_id=s.event_id, writes=s.writes_topics, models=s.models)
        return {"token": gw.sessions.mint(s, float(b.get("ttl_s", 900)))}

    @app.post("/admin/sessions/revoke", dependencies=[Depends(admin)])
    async def revoke(request: Request):
        b = await request.json()
        return {"revoked": gw.sessions.revoke(b.get("token", ""))}

    @app.get("/admin/audit", dependencies=[Depends(admin)])
    def audit_tail(n: int = 50, event_id: str | None = None):
        import json as _json
        lines = [_json.loads(x) for x in gw.audit.path.read_text().splitlines() if x.strip()]
        if event_id:
            lines = [x for x in lines if x.get("event_id") == event_id or x.get("new_event_id") == event_id]
        return {"records": lines[-max(1, min(n, 1000)):]}

    @app.get("/admin/audit/verify", dependencies=[Depends(admin)])
    def audit_verify():
        from harness_core.audit import verify
        ok, msg = verify(gw.audit.path)
        return {"ok": ok, "message": msg}

    @app.post("/admin/reload", dependencies=[Depends(admin)])
    def reload():
        gw.schemas.reload()
        return {"ok": True}

    return app
