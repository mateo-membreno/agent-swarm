"""Append-only, hash-chained JSONL audit log (plan.md §15, invariant I7).

Each line commits to the previous one, so editing or deleting a line breaks the chain.
append() raises AuditUnavailable if the line cannot be durably written: callers must then
refuse the operation (fail closed).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


class AuditUnavailable(RuntimeError):
    pass


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            last = self._last_line()
            if last:
                rec = json.loads(last)
                self._seq, self._prev = rec["seq"], rec["hash"]
        except (OSError, ValueError, KeyError) as e:
            raise AuditUnavailable(f"cannot open audit log: {e}") from e

    def _last_line(self) -> str | None:
        last = None
        with open(self.path, "rb") as f:
            for line in f:
                if line.strip():
                    last = line
        return last.decode() if last else None

    def append(self, **record) -> dict:
        with self._lock:
            body = {"seq": self._seq + 1, "ts": round(time.time(), 3), "prev": self._prev, **record}
            h = _digest(self._prev, body)
            line = json.dumps({**body, "hash": h}, sort_keys=True, separators=(",", ":")) + "\n"
            try:
                with open(self.path, "a") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError as e:
                raise AuditUnavailable(f"audit write failed: {e}") from e
            self._seq, self._prev = body["seq"], h
            return {**body, "hash": h}


def verify(path: str | os.PathLike) -> tuple[bool, str]:
    """Check the whole chain. Returns (ok, message)."""
    prev, seq = GENESIS, 0
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                h = rec.pop("hash")
            except (ValueError, KeyError):
                return False, f"line {n}: unreadable"
            if rec.get("prev") != prev:
                return False, f"line {n}: chain broken (previous hash mismatch)"
            if rec.get("seq") != seq + 1:
                return False, f"line {n}: sequence gap"
            if _digest(prev, rec) != h:
                return False, f"line {n}: record was modified"
            prev, seq = h, rec["seq"]
    return True, f"{seq} records, chain intact"
