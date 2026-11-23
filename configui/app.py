"""Local web app for editing harness config. Binds to localhost only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .store import KINDS, Store, StoreError

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]


def create_app(config_dir: str | os.PathLike | None = None) -> FastAPI:
    store = Store(config_dir or os.environ.get("HARNESS_CONFIG_DIR", "config"))
    app = FastAPI(title="Harness config", docs_url=None, redoc_url=None, openapi_url=None)
    # Defense against DNS rebinding: only answer for loopback host names.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError):
        body = {"error": str(exc)}
        if exc.status == 422:
            body = json.loads(str(exc))
        return JSONResponse(body, status_code=exc.status)

    async def _json_body(request: Request):
        # Requiring application/json forces a CORS preflight for cross-site pages.
        if "application/json" not in request.headers.get("content-type", ""):
            raise StoreError("content-type must be application/json", 415)
        try:
            return await request.json()
        except ValueError:
            raise StoreError("invalid JSON body") from None

    @app.get("/api/kinds")
    def kinds():
        return [
            {"id": k.id, "title": k.title, "schema": k.schema()}
            for k in KINDS.values()
        ]

    @app.get("/api/overview")
    def overview():
        return store.overview()

    @app.get("/api/{kind}")
    def list_entries(kind: str):
        return {"names": store.list(kind)}

    @app.get("/api/{kind}/{name}")
    def get_entry(kind: str, name: str):
        data = store.read(kind, name)
        if data is None:
            raise StoreError("not found", 404)
        return data

    @app.post("/api/{kind}/{name}/preview")
    async def preview(kind: str, name: str, request: Request):
        return store.preview(kind, name, await _json_body(request))

    @app.put("/api/{kind}/{name}")
    async def save(kind: str, name: str, request: Request):
        return store.save(kind, name, await _json_body(request))

    @app.delete("/api/{kind}/{name}")
    def delete(kind: str, name: str):
        store.delete(kind, name)
        return {"deleted": name}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
