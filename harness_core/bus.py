"""Kafka-API bus access (Redpanda). The gateway and controller use this; sandboxes never do."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Protocol


class BusError(RuntimeError):
    pass


class Bus(Protocol):
    def publish(self, topic: str, value: dict, key: str | None = None) -> None: ...


class MemoryBus:
    """In-process bus for tests."""

    def __init__(self) -> None:
        self.messages: dict[str, list[dict]] = {}
        self.fail = False
        self._lock = threading.Lock()

    def publish(self, topic: str, value: dict, key: str | None = None) -> None:
        if self.fail:
            raise BusError("bus unavailable")
        with self._lock:
            self.messages.setdefault(topic, []).append(value)


class KafkaBus:
    def __init__(self, bootstrap: str, client_id: str = "harness"):
        from confluent_kafka import Producer

        self._p = Producer({
            "bootstrap.servers": bootstrap, "client.id": client_id,
            "acks": "all", "enable.idempotence": True, "message.timeout.ms": 15000,
        })

    def publish(self, topic: str, value: dict, key: str | None = None) -> None:
        result: list = []
        self._p.produce(
            topic, json.dumps(value).encode(), key=key.encode() if key else None,
            on_delivery=lambda err, msg: result.append(err),
        )
        while not result:
            self._p.poll(0.1)
        if result[0] is not None:
            raise BusError(str(result[0]))

    def healthy(self) -> bool:
        try:
            self._p.list_topics(timeout=3)
            return True
        except Exception:
            return False


@dataclass
class TopicSpec:
    name: str
    partitions: int = 1
    retention_ms: int = 7 * 24 * 3600 * 1000


def parse_retention(text: str) -> int:
    n, unit = int(text[:-1]), text[-1]
    return n * (3600_000 if unit == "h" else 86_400_000)


def ensure_topics(bootstrap: str, specs: list[TopicSpec]) -> list[str]:
    """Create any missing topics. Returns the names that were created."""
    from confluent_kafka.admin import AdminClient, NewTopic

    admin = AdminClient({"bootstrap.servers": bootstrap})
    existing = set(admin.list_topics(timeout=10).topics)
    todo = [NewTopic(s.name, s.partitions, 1, config={"retention.ms": str(s.retention_ms)}) for s in specs if s.name not in existing]
    created: list[str] = []
    if not todo:
        return created
    for name, fut in admin.create_topics(todo).items():
        try:
            fut.result()
            created.append(name)
        except Exception as e:
            if "already exists" not in str(e).lower():
                raise BusError(f"cannot create topic {name}: {e}") from e
    return created


def make_consumer(bootstrap: str, group: str, topics: list[str], **extra):
    from confluent_kafka import Consumer

    c = Consumer({
        "bootstrap.servers": bootstrap, "group.id": group, "enable.auto.commit": False,
        "auto.offset.reset": "earliest", "max.poll.interval.ms": 3_600_000, **extra,
    })
    c.subscribe(topics)
    return c
