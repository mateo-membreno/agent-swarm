"""Per-event sessions. A token is bound to exactly one (agent, event) and one capability grant.

Only the SHA-256 of a token is stored, so reading the database never yields a usable token.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    agent: str
    event_id: str
    event: dict
    writes_topics: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    token_budget: int = 50_000
    tokens_used: int = 0
    pub_counts: dict = field(default_factory=dict)
    expires_at: float = 0.0


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SessionStore:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS sessions (h TEXT PRIMARY KEY, data TEXT NOT NULL, expires REAL NOT NULL)")
            self._db.commit()

    def mint(self, s: Session, ttl_s: float) -> str:
        token = "hst_" + secrets.token_urlsafe(32)
        s.expires_at = time.time() + ttl_s
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES (?,?,?)", (_h(token), json.dumps(s.__dict__), s.expires_at))
            self._db.commit()
        return token

    def get(self, token: str) -> Session | None:
        with self._lock:
            row = self._db.execute("SELECT data, expires FROM sessions WHERE h=?", (_h(token),)).fetchone()
        if not row or row[1] < time.time():
            return None
        return Session(**json.loads(row[0]))

    def charge(self, token: str, tokens: int) -> int:
        """Record usage; returns the new total."""
        with self._lock:
            row = self._db.execute("SELECT data FROM sessions WHERE h=?", (_h(token),)).fetchone()
            if not row:
                return 0
            d = json.loads(row[0])
            d["tokens_used"] += tokens
            self._db.execute("UPDATE sessions SET data=? WHERE h=?", (json.dumps(d), _h(token)))
            self._db.commit()
            return d["tokens_used"]

    def next_index(self, token: str, topic: str) -> int:
        """How many times this session has published to `topic` before now (0 for the first)."""
        with self._lock:
            row = self._db.execute("SELECT data FROM sessions WHERE h=?", (_h(token),)).fetchone()
            if not row:
                return 0
            d = json.loads(row[0])
            counts = d.setdefault("pub_counts", {})
            i = counts.get(topic, 0)
            counts[topic] = i + 1
            self._db.execute("UPDATE sessions SET data=? WHERE h=?", (json.dumps(d), _h(token)))
            self._db.commit()
            return i

    def revoke(self, token: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM sessions WHERE h=?", (_h(token),))
            self._db.commit()
            return cur.rowcount > 0

    def purge_expired(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            self._db.commit()
