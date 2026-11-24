"""Local web app for editing harness config. Binds to localhost only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .canvas import Canvas, CanvasError
from .store import KINDS, Store, StoreError

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]", "testserver"]


def create_app(config_dir: str | os.PathLike | None = None) -> FastAPI:
    store = Store(config_dir or os.environ.get("HARNESS_CONFIG_DIR", "config"))
    app = FastAPI(title="Harness config", docs_url=None, redoc_url=None, openapi_url=None)
    # Defense against DNS rebinding: only answer for loopback host names.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

    canvas = Canvas(store, Path(os.environ.get("HARNESS_DATA_DIR") or store.dir.parent / "data"))

    @app.exception_handler(StoreError)
    async def _store_error(_: Request, exc: StoreError):
        body = {"error": str(exc)}
        if isinstance(exc, CanvasError):
            body.update(exc.extra)
        elif exc.status == 422:
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

    # -- pipeline canvas (registered before the generic /api/{kind} routes)
    @app.post("/api/canvas/state")
    async def canvas_state(request: Request):
        return canvas.state((await _json_body(request)).get("ops", []))

    @app.post("/api/canvas/diff")
    async def canvas_diff(request: Request):
        return canvas.diff((await _json_body(request)).get("ops", []))

    @app.post("/api/canvas/joins:keys")
    async def canvas_join_keys(request: Request):
        body = await _json_body(request)
        inputs = body.get("inputs", [])
        if not isinstance(inputs, list) or not all(isinstance(i, str) for i in inputs):
            raise StoreError("inputs must be a list of topic names")
        return canvas.join_keys(body.get("ops", []), inputs)

    @app.post("/api/canvas/deploy")
    async def canvas_deploy(request: Request):
        body = await _json_body(request)
        return canvas.deploy(body.get("ops", []), bool(body.get("ack")))

    @app.get("/api/canvas/runtime")
    def canvas_runtime():
        return canvas.runtime()

    @app.get("/api/canvas/staged")
    def canvas_staged():
        return canvas.load_staged()

    @app.put("/api/canvas/staged")
    async def canvas_save_staged(request: Request):
        canvas.save_staged(await _json_body(request))
        return {"ok": True}

    @app.delete("/api/canvas/staged")
    def canvas_clear_staged():
        canvas.clear_staged()
        return {"ok": True}

    @app.get("/api/canvas/layout")
    def canvas_layout():
        return canvas.load_layout()

    @app.put("/api/canvas/layout")
    async def canvas_save_layout(request: Request):
        canvas.save_layout(await _json_body(request))
        return {"ok": True}

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
