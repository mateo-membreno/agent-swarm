"""Remember which (agent, trigger event) pairs already completed, so redelivery doesn't re-run them."""
from __future__ import annotations

import sqlite3
import threading


class Dedupe:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS done (agent TEXT, event_id TEXT, PRIMARY KEY (agent, event_id))")
            self._db.commit()

    def seen(self, agent: str, event_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM done WHERE agent=? AND event_id=?", (agent, event_id)).fetchone() is not None

    def mark(self, agent: str, event_id: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO done VALUES (?,?)", (agent, event_id))
            self._db.commit()
