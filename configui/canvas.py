"""Backend for the pipeline canvas (ui-spec.md).

The canvas is a *view and editor over the YAML in config/*, not a second source of truth. Everything here derives
from the same `Store` the config UI and the controller use:

* `state()`    the graph and the validation results for "deployed config + staged changes". Validation is the
               store's own validator run over a scratch copy, so the canvas and `harness plan` can't disagree.
* `diff()`     the capability, reachability and schema differences a deploy would make (§7.2).
* `deploy()`   writes the staged files to config/ in one go, only if the whole result validates (§7.3).

A staged change is a list of whole-file replacements/deletions. Nothing is written to config/ until `deploy()`.
Only topic, agent, source and sink files can be written: there is deliberately no code path that writes an
API manifest (§2 non-goals, U26).
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .store import NAME_RE, Store, StoreError, _dump

WRITABLE = ("topic", "agent", "source", "sink")
READ_ONLY = ("model",)
MAX_BODY = 4_000_000
LAYOUT_FILE = "canvas.layout.json"


class CanvasError(StoreError):
    def __init__(self, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message, status)
        self.extra = extra or {}


# ------------------------------------------------------------------ helpers

def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.is_file() else ""


def _free_text(schema: Any, prefix: str = "") -> list[dict]:
    out: list[dict] = []
    if not isinstance(schema, dict):
        return out
    for k, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict) and sub.get("x-free-text"):
            out.append({"field": prefix + k, "max_length": sub.get("maxLength")})
        out += _free_text(sub, f"{prefix}{k}.")
    return out


def check_ops(ops: Any) -> list[dict]:
    """Shape-check a staged change list. Raises CanvasError on anything unexpected."""
    if not isinstance(ops, list) or len(ops) > 2000:
        raise CanvasError("ops must be a list")
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("files"), list) or not op["files"]:
            raise CanvasError("each change needs a list of files")
        for f in op["files"]:
            if not isinstance(f, dict) or f.get("kind") not in WRITABLE:
                raise CanvasError(f"the canvas can only change {', '.join(WRITABLE)} (not '{(f or {}).get('kind')}'): "
                                  "APIs and models are defined in the config menu (or config/ in the repo), not on the canvas")
            if not isinstance(f.get("name"), str) or not (NAME_RE.match(f["name"]) or f["kind"] == "topic"):
                raise CanvasError("invalid name")
            if f.get("data") is not None and not isinstance(f["data"], dict):
                raise CanvasError("file data must be an object or null")
    return ops


# ------------------------------------------------------------------ graph

def build_graph(store: Store) -> dict:
    """Nodes and edges for everything in config/. Every edge is exactly one manifest field (§4.2)."""
    def safe(kind: str, name: str) -> dict:
        try:
            return store.read(kind, name) or {}
        except Exception:
            return {}

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_edge(typ: str, a: str, b: str, **kw) -> None:
        edges.append({"id": f"{typ}:{a}>{b}", "type": typ, "from": a, "to": b, **kw})

    topics = {t: safe("topic", t) for t in store.list("topic")}
    for t, d in topics.items():
        nodes[f"topic:{t}"] = {"id": f"topic:{t}", "kind": "topic", "name": t,
                               "sub": f"{d.get('retention', '7d')} · {d.get('partitions', 1)}p",
                               "free_text": _free_text(d.get("schema"))}
    for name in store.list("agent"):
        d = safe("agent", name)
        models = ", ".join((d.get("inference") or {}).get("models") or []) or "no inference"
        nodes[f"agent:{name}"] = {"id": f"agent:{name}", "kind": "agent", "name": name, "concurrency": d.get("concurrency", 1),
                                  "sub": f"{models} · {d.get('isolation', 'per_event')}"}
        t = (d.get("trigger") or {}).get("topic")
        if t:
            add_edge("trigger", f"topic:{t}", f"agent:{name}", filter=(d.get("trigger") or {}).get("filter"))
        for w in (d.get("writes") or {}).get("topics") or []:
            add_edge("writes", f"agent:{name}", f"topic:{w}")
    for name in store.list("source"):
        d = safe("source", name)
        host = urlparse(d["url"]).hostname if isinstance(d.get("url"), str) else ""
        nodes[f"source:{name}"] = {"id": f"source:{name}", "kind": "source", "name": name,
                                   "sub": f"{host} · every {d.get('interval', '10m')}"}
        if d.get("topic"):
            add_edge("publishes", f"source:{name}", f"topic:{d['topic']}")
    for name in store.list("sink"):
        d = safe("sink", name)
        sub = d.get("path", "file") if d.get("type") == "file" else f"POST {urlparse(d['url']).hostname if d.get('url') else ''}"
        nodes[f"sink:{name}"] = {"id": f"sink:{name}", "kind": "sink", "name": name, "sub": sub}
        if d.get("topic"):
            add_edge("consumes", f"topic:{d['topic']}", f"sink:{name}")
    edges = [e for e in edges if e["from"] in nodes and e["to"] in nodes]      # a dangling reference shows up as a validation error

    return {"nodes": list(nodes.values()), "edges": edges}


def reach(graph: dict) -> list[dict]:
    """Every (source -> sink) path that exists, with the free-text channels on the way (harness §8.3, laptop-sized)."""
    fwd: dict[str, set[str]] = {}
    back: dict[str, set[str]] = {}
    for e in graph["edges"]:
        fwd.setdefault(e["from"], set()).add(e["to"])
        back.setdefault(e["to"], set()).add(e["from"])

    def closure(start: str, adj: dict[str, set[str]]) -> set[str]:
        seen, todo = {start}, [start]
        while todo:
            for n in adj.get(todo.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        return seen

    by_id = {n["id"]: n for n in graph["nodes"]}
    out = []
    for s in (n for n in graph["nodes"] if n["kind"] == "source"):
        down = closure(s["id"], fwd)
        for t in (n for n in graph["nodes"] if n["kind"] == "sink" and n["id"] in down):
            on_path = down & closure(t["id"], back)
            channels = sorted(f"{by_id[n]['name']}.{c['field']} ≤ {c['max_length']}" for n in on_path
                              if n.startswith("topic:") for c in by_id[n].get("free_text", []))
            out.append({"from": s["name"], "to": t["name"], "free_text": channels})
    return out


# ------------------------------------------------------------------ working copy

class Canvas:
    def __init__(self, store: Store, data_dir: Path):
        self.store = store
        self.data_dir = data_dir
        self.staged_file = data_dir / "canvas.staged.json"

    # -- disk fingerprints (external-edit detection, §9)
    def hashes(self) -> dict[str, str]:
        out = {}
        for kind in WRITABLE:
            for name in self.store.list(kind):
                out[f"{kind}/{name}"] = _hash(self.store._path(self.store.kind(kind), name))
        return out

    def _file(self, store: Store, kind: str, name: str) -> Path:
        return store._path(store.kind(kind), name)

    def materialize(self, ops: list[dict]) -> tuple[Store, str]:
        """A scratch copy of config/ with the staged changes applied (not validated yet). Caller removes the dir."""
        tmp = tempfile.mkdtemp(prefix="canvas-")
        shutil.copytree(self.store.dir, Path(tmp) / "config", ignore=shutil.ignore_patterns("*.tmp", ".*.tmp"))
        scratch = Store(Path(tmp) / "config")
        for op in ops:
            for f in op["files"]:
                p = self._file(scratch, f["kind"], f["name"])
                if f.get("data") is None:
                    p.unlink(missing_ok=True)
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(_dump(f["data"]))
        return scratch, tmp

    def validate_all(self, scratch: Store) -> dict[str, dict]:
        issues: dict[str, dict] = {}
        for kind in WRITABLE:
            for name in scratch.list(kind):
                try:
                    data = scratch.read(kind, name)
                    res = scratch.validate(kind, name, data)
                    errors, warnings = res.errors, res.warnings
                except (StoreError, yaml.YAMLError) as e:
                    errors, warnings = [{"path": "", "message": f"cannot read: {e}"}], []
                if errors or warnings:
                    issues[f"{kind}:{name}"] = {"errors": errors, "warnings": warnings}
        return issues

    # -- /state
    def state(self, ops: list[dict]) -> dict:
        check_ops(ops)
        scratch, tmp = self.materialize(ops)
        try:
            graph = build_graph(scratch)
            return {
                "graph": graph,
                "issues": self.validate_all(scratch),
                "reach": reach(graph),
                "hashes": self.hashes(),
                "inventory": {
                    "models": scratch.list("model"),
                    "agents": scratch.list("agent"), "sources": scratch.list("source"), "sinks": scratch.list("sink"),
                    "topics": {t: scratch.read("topic", t) for t in scratch.list("topic")},
                    "entries": {k: {n: scratch.read(k, n) for n in scratch.list(k)} for k in ("agent", "source", "sink")},
                },
            }
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- the running stack (controller/stack.py writes this file and swaps config in live)
    def runtime(self) -> dict:
        import time
        try:
            st = json.loads((self.data_dir / "stack.status.json").read_text())
        except (OSError, ValueError):
            return {"running": False}
        alive = time.time() - float(st.get("heartbeat", 0)) < 12
        return {"running": alive, "in_sync": alive and st.get("config_hash") == self.store.fingerprint(), "ok": st.get("ok", True),
                "error": st.get("error", ""), "changes": st.get("changes", {}), "applying": bool(st.get("applying"))}

    # -- /diff
    def diff(self, ops: list[dict]) -> dict:
        check_ops(ops)
        scratch, tmp = self.materialize(ops)
        try:
            return self._diff(self.store, scratch, ops)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _diff(self, old: Store, new: Store, ops: list[dict]) -> dict:
        og, ng = build_graph(old), build_graph(new)
        cap: list[dict] = []
        schema: list[dict] = []

        def add(sign: str, text: str, node_id: str | None = None) -> None:
            cap.append({"sign": sign, "text": text})

        def kinds_of(store: Store, kind: str) -> dict[str, dict]:
            return {n: store.read(kind, n) or {} for n in store.list(kind)}

        oa, na = kinds_of(old, "agent"), kinds_of(new, "agent")
        for a in sorted(set(na) - set(oa)):
            add("+", f"{a} added (concurrency {na[a].get('concurrency', 1)})", f"agent:{a}")
        for a in sorted(set(oa) - set(na)):
            add("-", f"{a} removed (consumer group cg.{a} stops being used)", f"agent:{a}")
        for a in sorted(set(oa) & set(na)):
            o, n = oa[a], na[a]
            before, after = set((o.get("writes") or {}).get("topics") or []), set((n.get("writes") or {}).get("topics") or [])
            for x in sorted(after - before):
                add("+", f"{a}  may write  {x}", f"agent:{a}")
            for x in sorted(before - after):
                add("-", f"{a}  may write  {x}", f"agent:{a}")
            if (o.get("trigger") or {}) != (n.get("trigger") or {}):
                add("~", f"{a}  trigger  {_trig(o)} → {_trig(n)}", f"agent:{a}")
            for label, get in (("concurrency", lambda d: d.get("concurrency", 1)),
                               ("models", lambda d: ", ".join((d.get("inference") or {}).get("models") or [])),
                               ("max tokens/event", lambda d: (d.get("inference") or {}).get("max_tokens_per_event")),
                               ("isolation", lambda d: d.get("isolation", "per_event")),
                               ("sandbox", lambda d: json.dumps(d.get("sandbox") or {}, sort_keys=True)),
                               ("retries", lambda d: (d.get("delivery") or {}).get("max_retries")),
                               ("image", lambda d: d.get("image") or d.get("build"))):
                if get(o) != get(n):
                    add("~", f"{a}  {label}  {get(o)} → {get(n)}", f"agent:{a}")
        for kind in ("source", "sink"):
            oc, nc = kinds_of(old, kind), kinds_of(new, kind)
            for c in sorted(set(nc) - set(oc)):
                add("+", _io(c, nc[c]), f"{kind}:{c}")
            for c in sorted(set(oc) - set(nc)):
                add("-", f"{c} removed", f"{kind}:{c}")
            for c in sorted(set(oc) & set(nc)):
                if oc[c] != nc[c]:
                    add("~", f"{c}  {_io(c, oc[c])}  →  {_io(c, nc[c])}", f"{kind}:{c}")
        ot, nt = kinds_of(old, "topic"), kinds_of(new, "topic")
        breaking = False
        for t in sorted(set(nt) - set(ot)):
            schema.append({"sign": "+", "text": t, "breaking": False})
        for t in sorted(set(ot) - set(nt)):
            # Anything still using it would already be a validation error, so removal itself is not a compatibility break.
            schema.append({"sign": "-", "text": f"{t} removed (messages stay on the broker until retention expires)", "breaking": False})
        for t in sorted(set(ot) & set(nt)):
            for line, brk in _schema_changes(ot[t], nt[t]):
                schema.append({"sign": "~", "text": f"{t}  {line}", "breaking": brk})
                breaking = breaking or brk
            for k in ("retention", "partitions", "key"):
                if ot[t].get(k) != nt[t].get(k):
                    schema.append({"sign": "~", "text": f"{t}  {k} {ot[t].get(k)} → {nt[t].get(k)}", "breaking": False})

        old_reach = {(r["from"], r["to"]): r for r in reach(og)}
        reach_changes = []
        needs_ack = False
        for r in reach(ng):
            before = old_reach.get((r["from"], r["to"]))
            if before is None:
                reach_changes.append({"sign": "+", "text": f"{r['from']} → {r['to']}", "free_text": r["free_text"]})
                needs_ack = True
            else:
                for ch in sorted(set(r["free_text"]) - set(before["free_text"])):
                    reach_changes.append({"sign": "+", "text": f"{r['from']} → {r['to']}  new free-text channel: {ch}", "free_text": [ch]})
                    needs_ack = True
        new_pairs = {(r["from"], r["to"]) for r in reach(ng)}
        for (a, b) in sorted(set(old_reach) - new_pairs):
            reach_changes.append({"sign": "-", "text": f"{a} → {b}", "free_text": []})

        files = []
        seen = set()
        for op in ops:
            for f in op["files"]:
                k = (f["kind"], f["name"])
                if k in seen:
                    continue
                seen.add(k)
                p_old = self._file(old, *k)
                p_new = self._file(new, *k)
                if not p_new.is_file() and not p_old.is_file():
                    continue
                if not p_old.is_file():
                    code = "A"
                elif not p_new.is_file():
                    code = "D"
                else:
                    code = "M" if p_old.read_bytes() != p_new.read_bytes() else ""
                if code:
                    files.append({"code": code, "path": f"{old_kind_dir(old, f['kind'])}/{f['name']}.yaml"})
        yaml_diff = _yaml_diff(old, new, files)
        return {"capabilities": cap, "reachability": reach_changes, "schema": schema, "files": files, "yaml_diff": yaml_diff,
                "needs_ack": needs_ack, "breaking": breaking}

    # -- /deploy
    def deploy(self, ops: list[dict], ack: bool) -> dict:
        check_ops(ops)
        scratch, tmp = self.materialize(ops)
        try:
            # 1. nobody edited those files behind the canvas's back (U7): never overwrite a newer version silently
            current = self.hashes()
            conflicts = []
            for op in ops:
                for f in op["files"]:
                    key = f"{f['kind']}/{f['name']}"
                    base = (op.get("base") or {}).get(key)
                    if base is not None and current.get(key, "") != base:
                        conflicts.append(key)
            if conflicts:
                raise CanvasError("Config changed on disk since these changes were staged", 409, {"conflicts": sorted(set(conflicts))})
            # 2. the whole result must validate: the same checks `harness plan` runs
            issues = self.validate_all(scratch)
            errs = {k: v["errors"] for k, v in issues.items() if v["errors"]}
            if errs:
                raise CanvasError("The staged config has errors", 422, {"errors": errs})
            d = self._diff(self.store, scratch, ops)
            if d["breaking"]:
                raise CanvasError("A schema change breaks BACKWARD compatibility. Create a new .v2 topic instead.", 422,
                                  {"breaking": [s for s in d["schema"] if s["breaking"]]})
            if d["needs_ack"] and not ack:
                raise CanvasError("New reachability paths or free-text channels need acknowledgement", 409, {"needs_ack": True})
            # 3. write
            written, deleted = [], []
            final: dict[tuple[str, str], dict | None] = {}
            for op in ops:
                for f in op["files"]:
                    final[(f["kind"], f["name"])] = f.get("data")
            for (kind, name), data in final.items():
                p = self._file(self.store, kind, name)
                if data is None:
                    if p.is_file():
                        p.unlink()
                        deleted.append(f"{kind}/{name}")
                elif _write_preserving(p, data):
                    written.append(f"{kind}/{name}")
            return {"written": sorted(written), "deleted": sorted(deleted), "hashes": self.hashes()}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # -- staged list persistence (§7.1) and layout (§8)
    def load_staged(self) -> dict:
        try:
            d = json.loads(self.staged_file.read_text())
            return d if isinstance(d, dict) and isinstance(d.get("ops"), list) else {"ops": [], "pos": 0}
        except (OSError, ValueError):
            return {"ops": [], "pos": 0}

    def save_staged(self, body: Any) -> None:
        if not isinstance(body, dict) or not isinstance(body.get("ops"), list):
            raise CanvasError("expected {ops, pos}")
        check_ops(body["ops"])
        pos = int(body.get("pos", len(body["ops"])))
        if not 0 <= pos <= len(body["ops"]):
            raise CanvasError("bad undo position")
        self._atomic(self.staged_file, json.dumps({"ops": body["ops"], "pos": pos}))

    def clear_staged(self) -> None:
        self.staged_file.unlink(missing_ok=True)

    def load_layout(self) -> dict:
        try:
            d = json.loads((self.store.dir / LAYOUT_FILE).read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_layout(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise CanvasError("layout must be an object")
        nodes = body.get("nodes") or {}
        for src in (nodes,):
            if not isinstance(src, dict) or len(src) > 2000:
                raise CanvasError("bad layout")
            for k, v in src.items():
                if not isinstance(k, str) or not isinstance(v, dict) or any(
                        not isinstance(x, (int, float)) and not isinstance(x, bool) for x in v.values()):
                    raise CanvasError("bad layout entry")
        clean = {"version": 1, "nodes": nodes}
        if isinstance(body.get("viewport"), dict):
            clean["viewport"] = {k: float(body["viewport"].get(k, 0)) for k in ("x", "y", "zoom")}
        self._atomic(self.store.dir / LAYOUT_FILE, json.dumps(clean, indent=2))

    @staticmethod
    def _atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# ------------------------------------------------------------------ diff helpers

def old_kind_dir(store: Store, kind: str) -> str:
    return store.kind(kind).path


def _trig(d: dict) -> str:
    t = d.get("trigger") or {}
    return t.get("topic", "nothing") + (f" [{t['filter']}]" if t.get("filter") else "")


def _io(name: str, d: dict) -> str:
    if d.get("type") == "http_poll":
        return f"{name}  polls {d.get('url')} every {d.get('interval', '10m')} into {d.get('topic')}"
    if d.get("type") == "http_post":
        return f"{name}  POSTs {d.get('topic')} to {d.get('url')}"
    return f"{name}  writes {d.get('topic')} to {d.get('path')}"


def _schema_changes(old: dict, new: dict) -> list[tuple[str, bool]]:
    """Field-level changes and whether each breaks BACKWARD compatibility (new schema reading old data)."""
    o, n = old.get("schema") or {}, new.get("schema") or {}
    op_, np_ = o.get("properties") or {}, n.get("properties") or {}
    out: list[tuple[str, bool]] = []
    for f in sorted(set(np_) - set(op_)):
        req = f in (n.get("required") or [])
        ft = "  free text" if np_[f].get("x-free-text") else ""
        out.append((f"added field '{f}'{ft}" + (" (required)" if req else ""), req))
    for f in sorted(set(op_) - set(np_)):
        out.append((f"removed field '{f}'", True))
    for f in sorted(set(op_) & set(np_)):
        if op_[f] != np_[f]:
            out.append((f"changed field '{f}'", op_[f].get("type") != np_[f].get("type") or op_[f].get("enum") != np_[f].get("enum")
                        or op_[f].get("pattern") != np_[f].get("pattern")))
        if f in (n.get("required") or []) and f not in (o.get("required") or []):
            out.append((f"'{f}' became required", True))
    return out


def _yaml_diff(old: Store, new: Store, files: list[dict]) -> str:
    import difflib
    chunks = []
    for f in files:
        rel = f["path"]
        po, pn = old.dir / rel, new.dir / rel
        a = po.read_text().splitlines(keepends=True) if po.is_file() else []
        b = pn.read_text().splitlines(keepends=True) if pn.is_file() else []
        chunks.append("".join(difflib.unified_diff(a, b, fromfile=f"a/{rel}" if a else "/dev/null",
                                                   tofile=f"b/{rel}" if b else "/dev/null")))
    return "\n".join(chunks)


# ------------------------------------------------------------------ writing YAML without losing comments (§9)

def _merge(old: Any, new: Any) -> Any:
    """Update `old` (a ruamel round-trip node) so that it equals `new`, touching only what differs. Comments, key
    order and formatting of everything that did not change are kept."""
    if isinstance(old, CommentedMap) and isinstance(new, dict):
        for k in [k for k in old if k not in new]:
            del old[k]
        for k, v in new.items():
            old[k] = _merge(old[k], v) if k in old else v
        return old
    if isinstance(old, CommentedSeq) and isinstance(new, list):
        for i, v in enumerate(new):
            if i < len(old):
                merged = _merge(old[i], v)
                if merged is not old[i]:
                    old[i] = merged
            else:
                old.append(v)
        del old[len(new):]
        return old
    return old if old == new and type(old) is type(new) else new


def _write_preserving(path: Path, data: dict) -> bool:
    """Write `data` to `path`. Returns False when nothing changed. Existing files are edited in place."""
    if path.is_file():
        if yaml.safe_load(path.read_text()) == json.loads(json.dumps(data)):
            return False
        rt = YAML()
        rt.preserve_quotes = True
        rt.indent(mapping=2, sequence=2, offset=0)
        rt.width = 4096
        doc = rt.load(path.read_text())
        if isinstance(doc, CommentedMap):
            merged = _merge(doc, json.loads(json.dumps(data)))
            buf = io.StringIO()
            rt.dump(merged, buf)
            text = buf.getvalue()
            if yaml.safe_load(text) == json.loads(json.dumps(data)):     # belt and braces: never write something different
                Canvas._atomic(path, text)
                return True
    Canvas._atomic(path, _dump(data))
    return True

