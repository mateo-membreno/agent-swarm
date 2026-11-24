"""Commit offsets only up to the lowest event that is still running.

Events finish out of order when concurrency > 1. Committing a later offset before an earlier one finishes would
lose the earlier event if the controller crashed, so we advance the commit point only over a finished prefix.
"""
from __future__ import annotations


class OffsetTracker:
    def __init__(self) -> None:
        self._open: dict[int, dict[int, bool]] = {}   # partition -> {offset: done}, in begin() order

    def begin(self, partition: int, offset: int) -> None:
        self._open.setdefault(partition, {})[offset] = False

    def complete(self, partition: int, offset: int) -> int | None:
        """Mark done. Returns the next offset to commit for this partition, or None if the commit point didn't move."""
        slots = self._open.get(partition, {})
        if offset not in slots:
            return None
        slots[offset] = True
        commit = None
        for off in list(slots):
            if not slots[off]:
                break
            del slots[off]
            commit = off + 1
        return commit

    def in_flight(self) -> int:
        return sum(1 for s in self._open.values() for done in s.values() if not done)
