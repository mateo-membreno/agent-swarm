"""`harness` command line."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from configui.store import Store

BOOTSTRAP = os.environ.get("HARNESS_BOOTSTRAP", "127.0.0.1:19092")
ADMIN_URL = os.environ.get("HARNESS_ADMIN_URL", "http://127.0.0.1:18081")


def root() -> Path:
    return Path(os.environ.get("HARNESS_ROOT", ".")).resolve()


def die(msg: str, code: int = 1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def compose_cmd() -> list[str]:
    if shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    die("Docker Compose not found. Install Docker (Docker Desktop, Colima, or Docker Engine) first.")


def admin_token(create: bool = False) -> str:
    f = root() / "data" / "admin_token"
    if f.exists():
        return f.read_text().strip()
    if not create:
        die("no admin token yet; run `harness up` first")
    f.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # created 0600: other local users can't read it
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    return token


def _env() -> dict:
    return {**os.environ, "HARNESS_ADMIN_TOKEN": admin_token(create=True), "HARNESS_UID": str(os.getuid()), "HARNESS_GID": str(os.getgid())}


def cmd_up(args) -> None:
    r = root()
    for d in ("data/gateway", "secrets", "config"):
        (r / d).mkdir(parents=True, exist_ok=True)
    env = _env()
    print("Building images and starting Redpanda and the gateway...")
    build = subprocess.run(["docker", "build", "-q", "-f", "images/base/Dockerfile", "-t", "harness-base:latest", "."],
                           cwd=r, capture_output=True)
    if build.returncode:
        die("could not build the sandbox base image:\n" + build.stderr.decode())
    profiles = ["--profile", "stub"] if args.stub else []
    p = subprocess.run([*compose_cmd(), *profiles, "up", "-d", "--build", "--wait"], cwd=r, env=env)
    if p.returncode:
        die("docker compose failed (see output above)")
    print("\nStack is up. Next: `harness run`   (config UI: `python -m configui`)")


def cmd_down(args) -> None:
    subprocess.run([*compose_cmd(), "--profile", "stub", "down"], cwd=root(), env=_env())


def _admin():
    from .admin import AdminClient
    return AdminClient(ADMIN_URL, admin_token())


def cmd_status(args) -> None:
    out = subprocess.run([*compose_cmd(), "ps", "--format", "table {{.Name}}\t{{.Status}}"],
                         cwd=root(), env=_env(), capture_output=True, text=True)
    print(out.stdout.strip() or "stack is not running")
    try:
        a = _admin()
        print("gateway:", a.health())
        print("audit:  ", a.verify_audit()["message"])
    except Exception as e:
        print(f"gateway unreachable: {type(e).__name__}")


def cmd_plan(args) -> None:
    from .stack import StackError, build_plan
    try:
        plan = build_plan(Store(root() / "config"))
    except StackError as e:
        die(f"config is not valid:\n{e}")
    print("\n".join("  " + line for line in plan.flow()))
    for w in plan.warnings:
        print(f"\n  warning: {w}")


def cmd_run(args) -> None:
    """Run everything in config/. Deploys from the canvas (or any edit to config/) are swapped in while it runs."""
    import threading

    from .stack import Running, StackError, build_plan
    store = Store(root() / "config")
    fp = store.fingerprint()
    try:
        plan = build_plan(store)
    except StackError as e:
        die(f"config is not valid:\n{e}")
    cmd_plan(args)
    rp = Running(BOOTSTRAP, ADMIN_URL, admin_token(), root(), store=store)
    try:
        rp.start(plan, fp)
    except StackError as e:
        die(str(e))
    source = next(iter(plan.sources), "<source>")
    print("\nRunning. Edit config/ or deploy from the canvas and it is applied live. Ctrl-C to stop.")
    print(f"Sources poll on their own schedule. To pull right now:  harness emit {source}")
    signal.signal(signal.SIGTERM, lambda *_: rp.stop.set())
    threading.Thread(target=rp.watch, args=(store, rp.stop), daemon=True, name="watch").start()
    last = ""
    try:
        while not rp.stop.is_set():
            time.sleep(5)
            line = "  ".join(f"{n}: ok={d.stats.succeeded} running={d.stats.in_flight} retry={d.stats.retried} dlq={d.stats.dead_lettered}"
                             for n, d in rp.dispatchers.items())
            sinks = "  ".join(f"{n}: written={s.written}" for n, s in rp.sinks.items())
            print(f"[{time.strftime('%H:%M:%S')}] {line}   {sinks}", flush=True)
            if rp.error != last:
                last = rp.error
                print(f"  config not applied (previous config keeps running): {last}" if last else "  config applied", flush=True)
    except KeyboardInterrupt:
        pass
    print("\nStopping (finishing in-flight events)...")
    rp.shutdown()


def cmd_emit(args) -> None:
    """Poll a source once, right now (new items only; the running stack's own polling shares the same memory of what it sent)."""
    from harness_core.bus import KafkaBus
    from harness_core.events import SchemaRegistry

    from .connectors import HttpSource
    from .dedupe import Dedupe
    store = Store(root() / "config")
    cfg = store.read("source", args.source)
    if not cfg:
        die(f"no source named '{args.source}'")
    (root() / "data").mkdir(exist_ok=True)
    src = HttpSource(cfg, KafkaBus(BOOTSTRAP, "cli"), SchemaRegistry(store), Dedupe(str(root() / "data" / "controller.db")))
    try:
        n = src.poll()
    except Exception as e:
        die(f"poll failed: {e}")
    print(f"published {n} new item(s) to {cfg['topic']} ({src.skipped_seen} already seen, {src.skipped_invalid} rejected)")


def _scan(topics: list[str], limit_s: float = 8.0) -> list[dict]:
    from confluent_kafka import Consumer, KafkaError, TopicPartition
    c = Consumer({"bootstrap.servers": BOOTSTRAP, "group.id": f"cli-scan-{secrets.token_hex(4)}",
                  "enable.auto.commit": False, "log_level": 3})
    found: list[dict] = []
    try:
        meta = c.list_topics(timeout=5).topics
        parts = [TopicPartition(t, p, 0) for t in topics if t in meta for p in meta[t].partitions]
        c.assign(parts)
        deadline, eof = time.time() + limit_s, set()
        while time.time() < deadline and len(eof) < len(parts):
            m = c.poll(0.5)
            if m is None:
                continue
            if m.error():
                if m.error().code() == KafkaError._PARTITION_EOF:
                    eof.add((m.topic(), m.partition()))
                continue
            try:
                found.append(json.loads(m.value()))
            except ValueError:
                pass
    finally:
        c.close()
    return found


def cmd_trace(args) -> None:
    store = Store(root() / "config")
    topics = store.list("topic") + [f"dlq.{a}" for a in [*store.list("agent"), *store.list("sink")]]
    evs = [e for e in _scan(topics) if args.id in (e.get("trace_id"), e.get("event_id"), e.get("causation_id"))]
    if not evs:
        die(f"nothing found for '{args.id}' (is it a trace_id or event_id?)")
    for e in sorted(evs, key=lambda e: e.get("produced_at", "")):
        print(f"{e['produced_at']}  {e['type']:<22} by {e['produced_by']:<14} {e['event_id']}  <- {e.get('causation_id')}")


def cmd_dlq(args) -> None:
    topic = f"dlq.{args.agent}"
    evs = _scan([topic])
    if not evs:
        print(f"{topic} is empty")
    for e in evs:
        p = e["payload"]
        err = p["error"].strip()
        err = ("…" + err[-600:]) if len(err) > 600 else err
        print(f"--- {e['produced_at']}  attempts={p['attempts']}  original={p['original'].get('event_id')}\n{err}")


def cmd_audit(args) -> None:
    a = _admin()
    if args.verify:
        r = a.verify_audit()
        print(("OK: " if r["ok"] else "TAMPERED: ") + r["message"])
        raise SystemExit(0 if r["ok"] else 2)
    for rec in a.audit(args.n, args.event):
        print(json.dumps({k: v for k, v in rec.items() if k not in ("prev", "hash")}))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="harness", description="Run agents in isolated sandboxes.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("up", help="start Redpanda and the gateway")
    s.add_argument("--stub", action="store_true", help="also start a fake LLM at http://inference-cpu:8080 (demo/CI; no real inference)")
    s.set_defaults(fn=cmd_up)
    sub.add_parser("down", help="stop them").set_defaults(fn=cmd_down)
    sub.add_parser("status", help="show stack health").set_defaults(fn=cmd_status)
    sub.add_parser("plan", help="validate everything in config/ and show the flow").set_defaults(fn=cmd_plan)
    sub.add_parser("run", help="run everything in config/ in the foreground; deploys are applied live").set_defaults(fn=cmd_run)
    s = sub.add_parser("emit", help="poll a source once, now")
    s.add_argument("source")
    s.set_defaults(fn=cmd_emit)
    s = sub.add_parser("trace", help="show every event for a trace_id or event_id")
    s.add_argument("id")
    s.set_defaults(fn=cmd_trace)
    s = sub.add_parser("dlq", help="show an agent's dead-letter queue")
    s.add_argument("agent")
    s.set_defaults(fn=cmd_dlq)
    s = sub.add_parser("audit", help="show the gateway audit log")
    s.add_argument("-n", type=int, default=30)
    s.add_argument("--event")
    s.add_argument("--verify", action="store_true", help="check the hash chain")
    s.set_defaults(fn=cmd_audit)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
