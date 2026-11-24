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

MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


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

    hosted = data.get("provider") in ("anthropic", "openai_compatible")
    host = "api.anthropic.com" if data.get("provider") == "anthropic" else data.get("egress_host")
    if hosted and host:
        r.derived["egress_host"] = host
    base_host = urlparse(data["base_url"]).hostname if data.get("base_url") else None
    if data.get("provider") == "openai_compatible" and base_host and base_host != data.get("egress_host"):
        r.errors.append({"path": "egress_host", "message": "must equal the host of base_url"})
    if hosted and not data.get("allowed_agents"):
        r.warnings.append("Hosted model with no allowed_agents: every agent may send content off the machine.")
    kind = data.get("kind", "chat")
    for target in data.get("fallback") or []:
        path = "fallback"
        if target == name:
            r.errors.append({"path": path, "message": "cannot fall back to itself"})
            continue
        t = read("model", target)
        if t is None:
            r.errors.append({"path": path, "message": f"unknown model '{target}'"})
            continue
        if t.get("kind", "chat") != kind:
            r.errors.append({"path": path, "message": f"'{target}' is a different kind (chat/embedding) than this model"})
        if t.get("provider") != "local" and not hosted:
            r.warnings.append(f"Fallback to '{target}' sends data to a hosted API if this model is down.")
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


TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*\.v[1-9][0-9]*$")
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



def _reader(config_dir: Path) -> Callable[[str, str], dict | None]:
    store = Store(config_dir)

    def read(kind_id: str, entry: str) -> dict | None:
        try:
            return store.read(kind_id, entry)
        except (StoreError, yaml.YAMLError):
            return None

    return read


def _semantic_agent(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    read = _reader(config_dir)
    t = data.get("trigger") or {}
    if read("topic", t.get("topic", "")) is None:
        r.errors.append({"path": "trigger.topic", "message": f"unknown topic '{t.get('topic')}'"})
    if t.get("filter"):
        from harness_core import filterexpr
        try:
            filterexpr.validate(t["filter"])
        except filterexpr.FilterError as e:
            r.errors.append({"path": "trigger.filter", "message": str(e)})
    trig = {t.get("topic")}
    for i, topic in enumerate((data.get("writes") or {}).get("topics") or []):
        if read("topic", topic) is None:
            r.errors.append({"path": f"writes.topics.{i}", "message": f"unknown topic '{topic}'"})
        if topic in trig:
            r.warnings.append(f"Writes to '{topic}', which is also its trigger: every event it publishes starts another run (a loop).")
    hosted = []
    for i, alias in enumerate((data.get("inference") or {}).get("models") or []):
        m = read("model", alias)
        if m is None:
            r.errors.append({"path": f"inference.models.{i}", "message": f"unknown model '{alias}'"})
            continue
        allowed = m.get("allowed_agents")
        if allowed is not None and name not in allowed:
            r.errors.append({"path": f"inference.models.{i}", "message": f"'{alias}' does not list '{name}' in allowed_agents"})
        if m.get("provider") != "local":
            hosted.append(alias)
    if hosted:
        r.warnings.append(f"Sends event content to a hosted API via: {', '.join(hosted)}.")
    if data.get("isolation") == "long_running":
        r.errors.append({"path": "isolation", "message": "long_running is not available in this version"})
    if not (data.get("writes") or {}).get("topics"):
        r.warnings.append("This agent writes to no topic, so its work has no visible effect.")
    return r


def _topic_props(read, topic_name: str | None) -> tuple[dict | None, dict]:
    topic = read("topic", topic_name) if topic_name else None
    return topic, (((topic or {}).get("schema") or {}).get("properties")) or {}


def _semantic_source(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    topic, props = _topic_props(_reader(config_dir), data.get("topic"))
    if data.get("topic") and topic is None:
        r.errors.append({"path": "topic", "message": f"unknown topic '{data['topic']}'"})
    host = urlparse(data["url"]).hostname if isinstance(data.get("url"), str) else None
    if host:
        r.derived["egress_host"] = host
    elif data.get("url"):
        r.errors.append({"path": "url", "message": "no host in URL"})
    if topic is not None:
        mapped = data.get("map") or {}
        for k in sorted(set(mapped) - set(props)):
            r.errors.append({"path": f"map.{k}", "message": f"'{k}' is not a field of topic '{data['topic']}'"})
        for k in sorted(set((topic.get("schema") or {}).get("required") or []) - set(mapped)):
            r.errors.append({"path": "map", "message": f"topic '{data['topic']}' requires '{k}' but the map does not set it"})
    return r


def _semantic_sink(name: str, data: dict, config_dir: Path) -> Result:
    r = Result()
    if data.get("name") != name:
        r.errors.append({"path": "name", "message": f"must equal the entry name '{name}'"})
    topic, props = _topic_props(_reader(config_dir), data.get("topic"))
    if data.get("topic") and topic is None:
        r.errors.append({"path": "topic", "message": f"unknown topic '{data['topic']}'"})
    if data.get("type") == "file" and data.get("path"):
        if ".." in data["path"]:
            r.errors.append({"path": "path", "message": "must not contain '..'"})
        for ph in re.findall(r"{([a-z_]+)}", data["path"]):
            if ph not in props:
                r.errors.append({"path": "path", "message": f"'{{{ph}}}' is not a field of topic '{data.get('topic')}'"})
    if data.get("type") == "http_post":
        host = urlparse(data["url"]).hostname if isinstance(data.get("url"), str) else None
        if host:
            r.derived["egress_host"] = host
        elif data.get("url"):
            r.errors.append({"path": "url", "message": "no host in URL"})
        if data.get("auth") and not data.get("credential"):
            r.errors.append({"path": "credential", "message": "auth is set but no credential"})
        if data.get("credential") and not data.get("auth"):
            r.errors.append({"path": "auth", "message": "a credential is set but no auth says how to send it"})
        r.warnings.append(f"Sends the events on '{data.get('topic')}' to {host or 'an external URL'}: that data leaves this machine.")
    return r


KINDS: dict[str, Kind] = {
    k.id: k
    for k in [
        Kind("model", "Models", "inference/models", "model.json", _semantic_model, MODEL_RE,
             "letters, digits, dot, underscore, hyphen, e.g. qwen2.5-7b-instruct"),
        Kind("topic", "Topics", "topics", "topic.json", _semantic_topic, TOPIC_RE,
             "a name ending in .v<N>, e.g. news_stories.v1 (the UI turns free text into this)"),
        Kind("agent", "Agents", "agents", "agent.json", _semantic_agent),
        Kind("source", "Sources", "sources", "source.json", _semantic_source),
        Kind("sink", "Sinks", "sinks", "sink.json", _semantic_sink),
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


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper, value):
    """Multi-line text (an agent's instructions) is written as a readable block, not one quoted line."""
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _str)


def _dump(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True)


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

    def fingerprint(self) -> str:
        """A hash of every config file, so the running stack can tell when a deploy has changed something."""
        import hashlib
        h = hashlib.sha256()
        for kind in KINDS.values():
            d = self.dir / kind.path
            for p in sorted(d.glob("*.yaml")) if d.is_dir() else []:
                h.update(f"{kind.id}/{p.name}".encode())
                h.update(p.read_bytes())
        return h.hexdigest()[:16]

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

        sources, sinks = [], []
        for name in self.list("source"):
            d_, res = load("source", name)
            sources.append({"name": name, "type": d_.get("type"), "target": res.derived.get("egress_host"), "topic": d_.get("topic"),
                            "interval": d_.get("interval", "10m")})
            if res.derived.get("egress_host"):
                egress.append({"host": res.derived["egress_host"], "via": f"source {name}"})
        for name in self.list("sink"):
            d_, res = load("sink", name)
            sinks.append({"name": name, "type": d_.get("type"), "topic": d_.get("topic"),
                          "target": res.derived.get("egress_host") or d_.get("path")})
            if res.derived.get("egress_host"):
                egress.append({"host": res.derived["egress_host"], "via": f"sink {name}"})

        models = []
        fallback = []
        for name in self.list("model"):
            m, res = load("model", name)
            models.append({
                "name": name, "provider": m.get("provider"), "model": m.get("model"), "kind": m.get("kind", "chat"),
                "target": m.get("endpoint") or res.derived.get("egress_host"), "max_concurrency": m.get("max_concurrency"),
                "allowed_agents": m.get("allowed_agents") or [], "hosted": m.get("provider") != "local",
            })
            if m.get("provider") != "local" and res.derived.get("egress_host"):
                egress.append({"host": res.derived["egress_host"], "via": f"model {name}"})
            if m.get("fallback"):
                fallback.append({"name": name, "chain": m["fallback"]})

        topics = []
        for name in self.list("topic"):
            t, res = load("topic", name)
            topics.append({
                "name": name, "description": t.get("description", ""), "retention": t.get("retention", "7d"),
                "partitions": t.get("partitions", 1), "key": t.get("key"), "free_text": res.derived.get("free_text", []),
            })

        return {
            "counts": {"source": len(sources), "sink": len(sinks), "topic": len(topics), "models": len(models)},
            "sources": sources,
            "sinks": sinks,
            "inference": {"models": models, "fallback": fallback},
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
