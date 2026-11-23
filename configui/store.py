"""File-backed config store: schema + semantic validation, diff preview, atomic save.

Every configurable kind is a JSON Schema plus an optional semantic validator
(cross-field checks JSON Schema can't express). Adding a kind = add a schema
file and one entry in KINDS.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator, SchemaError

SCHEMA_DIR = Path(__file__).parent / "schemas"
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,62}$")

# Values that look like real secrets. Config holds vault *names*, never secrets.
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{8,}|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class StoreError(Exception):
    """Raised for bad kind/name requests (maps to HTTP 4xx)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Result:
    errors: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    derived: dict[str, Any] = field(default_factory=dict)


Semantic = Callable[[str, dict, Path], Result]


@dataclass(frozen=True)
class Kind:
    id: str
    title: str
    path: str  # directory under config/, one <name>.yaml per entry
    schema_file: str
    semantic: Semantic | None = None
    name_re: re.Pattern = NAME_RE
    name_hint: str = "lowercase letters, digits, underscore; start with a letter"

    def schema(self) -> dict:
        return json.loads((SCHEMA_DIR / self.schema_file).read_text())


# ---------------------------------------------------------------- semantics

def _semantic_api(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    args = data.get("args_schema")
    if isinstance(args, dict):
        try:
            Draft202012Validator.check_schema(args)
        except SchemaError as e:
            r.errors.append({"path": "args_schema", "message": f"not a valid JSON Schema: {e.message}"})
        else:
            if args.get("type") == "object" and args.get("additionalProperties") is not False:
                r.errors.append({"path": "args_schema", "message": "object schemas must set additionalProperties: false"})
            if args.get("type") != "object":
                r.errors.append({"path": "args_schema", "message": "must describe an object (type: object)"})
    fixed = data.get("fixed_args")
    if isinstance(fixed, dict) and isinstance(args, dict) and isinstance(args.get("properties"), dict):
        clash = sorted(set(fixed) & set(args["properties"]))
        if clash:
            r.errors.append({"path": "fixed_args", "message": f"also settable by the agent via args_schema: {', '.join(clash)}"})
    backend = data.get("backend") or {}
    if backend.get("type") == "http" and isinstance(backend.get("url"), str):
        host = urlparse(backend["url"]).hostname
        if host:
            r.derived["egress_host"] = host
        else:
            r.errors.append({"path": "backend.url", "message": "no host in URL"})
        if data.get("credential") and not data.get("auth"):
            r.errors.append({"path": "auth", "message": "a credential is set but no auth method says how to inject it"})
    if data.get("auth") and not data.get("credential"):
        r.errors.append({"path": "credential", "message": "auth is set but no credential"})
    if data.get("kind") == "effect":
        if data.get("approval", "none") == "none":
            r.warnings.append(
                "Effect API without approval: agents can trigger this effect unattended. "
                "Prefer a sink connector, or set approval: required."
            )
    elif data.get("approval") == "required":
        r.warnings.append("approval has no effect on read APIs.")
    if data.get("kind") == "read" and backend.get("type") == "http" and backend.get("method") not in (None, "GET"):
        r.warnings.append(f"Read API uses {backend.get('method')}; make sure it has no side effects.")
    if not data.get("limits"):
        r.warnings.append("No limits set; add rate, timeout and max_response_bytes.")
    return r


BACKEND_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


def _semantic_backend(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    if data.get("kind") == "local_gpu" and data.get("gpus") is None:
        r.warnings.append("No gpus setting; defaulting to auto.")
    if data.get("kind") == "hosted_api" and data.get("egress_host"):
        r.derived["egress_host"] = data["egress_host"]
    return r


def _semantic_model(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    store = Store(config_dir)

    def read(kind_id: str, entry: str) -> dict | None:
        try:
            return store.read(kind_id, entry)
        except (StoreError, yaml.YAMLError):
            return None

    backend = read("backend", data.get("backend", ""))
    if backend is None:
        r.errors.append({"path": "backend", "message": f"unknown backend '{data.get('backend')}'"})
    hosted = bool(backend) and backend.get("kind") == "hosted_api"
    if hosted and not data.get("allowed_agents"):
        r.warnings.append("Hosted alias with no allowed_agents: every agent may send content off the machine.")
    kind = data.get("kind", "chat")
    for target in data.get("fallback") or []:
        path = "fallback"
        if target == name:
            r.errors.append({"path": path, "message": "cannot fall back to itself"})
            continue
        t = read("model", target)
        if t is None:
            r.errors.append({"path": path, "message": f"unknown model alias '{target}'"})
            continue
        if t.get("kind", "chat") != kind:
            r.errors.append({"path": path, "message": f"'{target}' is a different kind (chat/embedding) than this alias"})
        tb = read("backend", t.get("backend", ""))
        if tb and tb.get("kind") == "hosted_api" and not hosted:
            r.warnings.append(f"Fallback to '{target}' sends data to a hosted API if this backend is down.")
    # A chain that leads back here is a cycle.
    seen: set[str] = set()
    stack = list(data.get("fallback") or [])
    while stack:
        cur = stack.pop()
        if cur == name:
            r.errors.append({"path": "fallback", "message": "fallback cycle"})
            break
        if cur in seen:
            continue
        seen.add(cur)
        nxt = read("model", cur)
        stack += (nxt or {}).get("fallback") or []
    return r


TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[1-9][0-9]*$")
RESERVED_TOPIC_PREFIXES = ("dlq.", "harness.")
RESERVED_TOPICS = {"jobs.completed.v1"}


def _walk_topic_schema(schema: Any, path: str, r: Result, free_text: list[dict]) -> None:
    """Enforce plan.md §6.3: closed objects, typed fields, bounded free text."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            r.errors.append({"path": path, "message": "objects must set additionalProperties: false"})
        for key, sub in (schema.get("properties") or {}).items():
            _walk_topic_schema(sub, f"{path}.properties.{key}", r, free_text)
    elif schema.get("type") == "array":
        _walk_topic_schema(schema.get("items"), f"{path}.items", r, free_text)
    elif schema.get("type") == "string":
        typed = any(k in schema for k in ("enum", "const", "pattern", "format"))
        marked = schema.get("x-free-text") is True
        if marked and "maxLength" not in schema:
            r.errors.append({"path": path, "message": "x-free-text fields must set maxLength"})
        elif not typed and not marked:
            r.errors.append(
                {"path": path, "message": "string must be typed (enum, pattern or format) or marked x-free-text: true with a maxLength"}
            )
        if marked:
            short = path.removeprefix("schema.properties.").replace(".properties.", ".")
            free_text.append({"path": short, "max_length": schema.get("maxLength")})


def _semantic_topic(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    if name.startswith(RESERVED_TOPIC_PREFIXES) or name in RESERVED_TOPICS:
        r.errors.append({"path": "name", "message": "reserved for system topics (dlq.*, harness.*, jobs.completed.v1)"})
    schema = data.get("schema")
    if isinstance(schema, dict):
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as e:
            r.errors.append({"path": "schema", "message": f"not a valid JSON Schema: {e.message}"})
            return r
        if schema.get("type") != "object":
            r.errors.append({"path": "schema.type", "message": "topic payloads must be objects (type: object)"})
        if "$id" in schema and schema["$id"] != name:
            r.errors.append({"path": "schema.$id", "message": f"must equal the topic name '{name}' when present"})
        free_text: list[dict] = []
        _walk_topic_schema(schema, "schema", r, free_text)
        r.derived["free_text"] = free_text
        if not schema.get("required"):
            r.warnings.append("Schema has no required fields: consumers can receive empty payloads.")
        key = data.get("key")
        props = schema.get("properties") or {}
        if key and key not in props:
            r.errors.append({"path": "key", "message": f"'{key}' is not a property of the schema"})
        if data.get("partitions", 1) > 1 and not key:
            r.warnings.append("Several partitions without a key: ordering is not preserved per entity.")
    return r


KINDS: dict[str, Kind] = {
    k.id: k
    for k in [
        Kind("api", "APIs", "apis", "api.json", _semantic_api),
        Kind("backend", "Backends", "inference/backends", "backend.json", _semantic_backend, BACKEND_RE,
             "lowercase letters, digits, hyphen; start with a letter, e.g. local-gpu"),
        Kind("model", "Models", "inference/models", "model.json", _semantic_model, MODEL_RE,
             "letters, digits, dot, underscore, hyphen, e.g. qwen2.5-7b-instruct"),
        Kind("topic", "Topics", "topics", "topic.json", _semantic_topic, TOPIC_RE,
             "<domain>.<event>.v<N>, e.g. email.triaged.v1"),
    ]
}


# ---------------------------------------------------------------- helpers

def _find_secrets(value: Any, path: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, str):
        if SECRET_RE.search(value):
            hits.append(path or "(root)")
    elif isinstance(value, dict):
        for k, v in value.items():
            hits += _find_secrets(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            hits += _find_secrets(v, f"{path}[{i}]")
    return hits


def _dump(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False, allow_unicode=True)


class Store:
    def __init__(self, config_dir: str | os.PathLike):
        self.dir = Path(config_dir).resolve()

    # -- resolution
    def kind(self, kind_id: str) -> Kind:
        try:
            return KINDS[kind_id]
        except KeyError:
            raise StoreError(f"unknown kind '{kind_id}'", 404) from None

    def _path(self, kind: Kind, name: str) -> Path:
        if not kind.name_re.match(name):
            raise StoreError(f"invalid name (use {kind.name_hint})")
        p = (self.dir / kind.path / f"{name}.yaml").resolve()
        if (self.dir / kind.path).resolve() not in p.parents:
            raise StoreError("invalid name")
        return p

    # -- reads
    def list(self, kind_id: str) -> list[str]:
        kind = self.kind(kind_id)
        d = self.dir / kind.path
        return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []

    def read(self, kind_id: str, name: str) -> dict | None:
        kind = self.kind(kind_id)
        p = self._path(kind, name)
        if not p.is_file():
            return None
        data = yaml.safe_load(p.read_text())
        return data if isinstance(data, dict) else {}

    # -- dashboard
    def overview(self) -> dict:
        """Everything configured, summarised, plus every error and warning across all entries."""
        attention: list[dict] = []
        egress: list[dict] = []

        def d(x: Any) -> dict:
            return x if isinstance(x, dict) else {}

        def load(kind_id: str, name: str) -> tuple[dict, Result]:
            try:
                data = d(self.read(kind_id, name))
                res = self.validate(kind_id, name, data)
            except (StoreError, yaml.YAMLError) as e:
                attention.append({"kind": kind_id, "name": name, "level": "error", "path": "", "message": f"unreadable: {e}"})
                return {}, Result()
            for e in res.errors:
                attention.append({"kind": kind_id, "name": name, "level": "error", "path": e["path"], "message": e["message"]})
            for w in res.warnings:
                attention.append({"kind": kind_id, "name": name, "level": "warning", "path": "", "message": w})
            return data, res

        apis = []
        for name in self.list("api"):
            a, res = load("api", name)
            b = d(a.get("backend"))
            if b.get("type") == "http":
                host = res.derived.get("egress_host") or "?"
                target = f"{b.get('method', '?')} {host}"
                egress.append({"host": host, "via": f"API {name}"})
            else:
                target = f"adapter {b.get('name', '?')}"
            apis.append({
                "name": name, "kind": a.get("kind"), "description": a.get("description", ""), "target": target,
                "credential": a.get("credential"), "approval": a.get("approval", "none"), "rate": d(a.get("limits")).get("rate"),
            })

        backends = []
        for name in self.list("backend"):
            b, _ = load("backend", name)
            backends.append({
                "name": name,
                "kind": b.get("kind"),
                "target": b.get("endpoint") or b.get("egress_host"),
                "max_concurrency": b.get("max_concurrency"),
            })
            if b.get("kind") == "hosted_api" and b.get("egress_host"):
                egress.append({"host": b["egress_host"], "via": f"inference backend {name}"})
        backend_kind = {b["name"]: b["kind"] for b in backends}
        models = []
        fallback = []
        for alias in self.list("model"):
            m, _ = load("model", alias)
            models.append({
                "alias": alias, "backend": m.get("backend"), "model": m.get("model"), "kind": m.get("kind", "chat"),
                "allowed_agents": m.get("allowed_agents") or [], "hosted": backend_kind.get(m.get("backend")) == "hosted_api",
            })
            if m.get("fallback"):
                fallback.append({"alias": alias, "chain": m["fallback"]})

        topics = []
        for name in self.list("topic"):
            t, res = load("topic", name)
            topics.append({
                "name": name, "description": t.get("description", ""), "retention": t.get("retention", "7d"),
                "partitions": t.get("partitions", 1), "key": t.get("key"), "free_text": res.derived.get("free_text", []),
            })

        return {
            "counts": {"api": len(apis), "topic": len(topics), "backends": len(backends), "models": len(models)},
            "apis": apis,
            "inference": {"backends": backends, "models": models, "fallback": fallback},
            "topics": topics,
            "egress": sorted({(e["host"], e["via"]) for e in egress}),
            "attention": attention,
        }

    # -- validation + preview
    def validate(self, kind_id: str, name: str, data: Any) -> Result:
        kind = self.kind(kind_id)
        self._path(kind, name)  # name check
        result = Result()
        if not isinstance(data, dict):
            result.errors.append({"path": "", "message": "must be an object"})
            return result
        validator = Draft202012Validator(kind.schema())
        for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
            result.errors.append({"path": ".".join(str(p) for p in err.absolute_path), "message": err.message})
        for path in _find_secrets(data):
            result.errors.append({"path": path, "message": "looks like a secret. Store it in the vault and reference it by name."})
        if not result.errors and kind.semantic:  # semantic checks assume a schema-valid shape
            sem = kind.semantic(name, data, self.dir)
            result.errors += sem.errors
            result.warnings += sem.warnings
            result.derived.update(sem.derived)
        return result

    def preview(self, kind_id: str, name: str, data: Any) -> dict:
        kind = self.kind(kind_id)
        res = self.validate(kind_id, name, data)
        p = self._path(kind, name)
        old = p.read_text() if p.is_file() else ""
        new = _dump(data) if isinstance(data, dict) else ""
        diff = "".join(
            difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{p.name}" if old else "/dev/null",
                tofile=f"b/{p.name}",
            )
        )
        return {"errors": res.errors, "warnings": res.warnings, "derived": res.derived, "diff": diff, "yaml": new, "exists": bool(old)}

    # -- writes
    def save(self, kind_id: str, name: str, data: Any) -> dict:
        kind = self.kind(kind_id)
        res = self.validate(kind_id, name, data)
        if res.errors:
            raise StoreError(json.dumps({"errors": res.errors}), 422)
        p = self._path(kind, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_dump(data))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return {"warnings": res.warnings, "derived": res.derived}

    def delete(self, kind_id: str, name: str) -> None:
        kind = self.kind(kind_id)
        p = self._path(kind, name)
        if not p.is_file():
            raise StoreError("not found", 404)
        p.unlink()
