"""Inference forwarding: model -> HTTP call. Credentials never leave this module."""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

import httpx

from configui.store import Store

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class InferenceError(Exception):
    def __init__(self, message: str, status: int = 502, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class Inference:
    def __init__(self, store: Store, secrets_dir: str, client: httpx.AsyncClient | None = None):
        self.store = store
        self.secrets_dir = Path(secrets_dir)
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(120, connect=5), follow_redirects=False)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.queue_timeout = 300.0

    def _secret(self, name: str) -> str:
        p = (self.secrets_dir / name).resolve()
        if self.secrets_dir.resolve() not in p.parents or not p.is_file():
            raise InferenceError(f"credential '{name}' is not available to the gateway", 502)
        return p.read_text().strip()

    def _sem(self, model: dict) -> asyncio.Semaphore:
        n = int(model.get("max_concurrency") or 4)
        return self._sems.setdefault(model["name"], asyncio.Semaphore(n))

    def chain(self, alias: str, agent: str) -> list[dict]:
        """Primary then explicit fallbacks, each a model. Enforces allowed_agents on every hop."""
        out, seen, name = [], set(), alias
        todo = [alias]
        while todo:
            name = todo.pop(0)
            if name in seen:
                continue
            seen.add(name)
            model = self.store.read("model", name)
            if not model:
                raise InferenceError(f"unknown model '{name}'", 404)
            allowed = model.get("allowed_agents")
            if allowed is not None and agent not in allowed:
                raise InferenceError(f"agent '{agent}' may not use model '{name}'", 403)
            out.append(model)
            todo.extend(model.get("fallback") or [])
        return out

    async def complete(self, alias: str, agent: str, messages: list[dict], max_tokens: int) -> dict:
        last: InferenceError | None = None
        for model in self.chain(alias, agent):
            try:
                return {**await self._one(model, messages, max_tokens), "model": model["name"]}
            except InferenceError as e:
                last = e
                if not e.retryable:
                    raise
        raise last or InferenceError("no model available", 502)

    async def _one(self, model: dict, messages: list[dict], max_tokens: int) -> dict:
        sem = self._sem(model)
        try:
            await asyncio.wait_for(sem.acquire(), self.queue_timeout)
        except TimeoutError:
            raise InferenceError(f"model '{model['name']}' is saturated (queue timeout)", 503, retryable=True) from None
        try:
            if model["provider"] == "anthropic":
                return await self._anthropic(model, messages, max_tokens)
            return await self._openai(model, messages, max_tokens)
        finally:
            sem.release()

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            r = await self.client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            raise InferenceError(f"model server unreachable: {type(e).__name__}", 502, retryable=True) from None
        if r.status_code >= 500 or r.status_code == 429:
            raise InferenceError(f"model server returned {r.status_code}", 502, retryable=True)
        if r.status_code >= 400:
            raise InferenceError(f"model server rejected the request ({r.status_code})", 502)
        return r.json()

    async def _anthropic(self, model, messages, max_tokens) -> dict:
        key = self._secret(model["credential"])
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        body = {"model": model["model"], "max_tokens": max_tokens, "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            body["system"] = system
        d = await self._post(ANTHROPIC_URL, {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, body)
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
        u = d.get("usage", {})
        return {"text": text, "usage": {"input": u.get("input_tokens", 0), "output": u.get("output_tokens", 0)}}

    async def _openai(self, model, messages, max_tokens) -> dict:
        base = model.get("endpoint") or model.get("base_url")
        if not base:
            raise InferenceError(f"model '{model['name']}' has no endpoint", 502)
        if model["provider"] != "local" and urlparse(base).hostname != model.get("egress_host"):
            raise InferenceError("model URL host does not match its egress_host", 502)
        headers = {}
        if model.get("credential"):
            headers["authorization"] = "Bearer " + self._secret(model["credential"])
        d = await self._post(base.rstrip("/") + "/chat/completions", headers,
                             {"model": model["model"], "messages": messages, "max_tokens": max_tokens})
        try:
            text = d["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise InferenceError("model server returned an unexpected response", 502) from None
        u = d.get("usage", {})
        return {"text": text, "usage": {"input": u.get("prompt_tokens", 0), "output": u.get("completion_tokens", 0)}}
