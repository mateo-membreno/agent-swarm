import json

import pytest

from configui.store import Store
from harness_core import audit, filterexpr
from harness_core.events import SchemaRegistry, make_envelope

ENV = {"payload": {"urgency": "high", "score": 8, "tags": ["a", "b"]}, "type": "email.triaged.v1", "produced_by": "triager"}


@pytest.mark.parametrize("expr,expected", [
    ("payload.urgency == 'high'", True),
    ("payload.urgency != 'high'", False),
    ("payload.score >= 7 and payload.urgency == 'high'", True),
    ("payload.score < 5 or payload.urgency == 'low'", False),
    ("not (payload.score < 5)", True),
    ("payload.urgency in ['high', 'critical']", True),
    ("payload.urgency not in ['low']", True),
    ("payload.missing == null", True),
    ("payload.missing > 3", False),
    ("payload.score > 'x'", False),
    ("type == 'email.triaged.v1'", True),
])
def test_filter(expr, expected):
    assert filterexpr.matches(expr, ENV) is expected


BAD = ["", "__import__('os')", "payload.x ==", "secrets.x == 1", "payload.x = 1", "(payload.x == 1", "payload.x == 1 garbage"]


@pytest.mark.parametrize("bad", BAD)
def test_filter_rejects(bad):
    with pytest.raises(filterexpr.FilterError):
        filterexpr.validate(bad)


def test_filter_cannot_execute_code():
    with pytest.raises(filterexpr.FilterError):
        filterexpr.parse("payload.x == __import__('os').system('true')")


def test_audit_chain_detects_tampering(tmp_path):
    p = tmp_path / "a.jsonl"
    log = audit.AuditLog(p)
    for i in range(3):
        log.append(op="x", n=i)
    assert audit.verify(p)[0]
    lines = p.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["n"] = 99
    lines[1] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    ok, msg = audit.verify(p)
    assert not ok and "line 2" in msg


def test_audit_detects_deleted_line(tmp_path):
    p = tmp_path / "a.jsonl"
    log = audit.AuditLog(p)
    for i in range(3):
        log.append(n=i)
    lines = p.read_text().splitlines()
    p.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert not audit.verify(p)[0]


def test_audit_resumes_chain_after_restart(tmp_path):
    p = tmp_path / "a.jsonl"
    audit.AuditLog(p).append(n=1)
    audit.AuditLog(p).append(n=2)
    assert audit.verify(p) == (True, "2 records, chain intact")


def test_audit_fails_closed(tmp_path):
    log = audit.AuditLog(tmp_path / "a.jsonl")
    log.path = tmp_path / "missing-dir" / "a.jsonl"
    with pytest.raises(audit.AuditUnavailable):
        log.append(n=1)


def test_envelope_ids_unique_and_ordered():
    a, b = make_envelope("t.x.v1", "agent", {}), make_envelope("t.x.v1", "agent", {})
    assert a["event_id"] != b["event_id"] and a["event_id"].startswith("ev_") and a["trace_id"].startswith("tr_")


def test_schema_registry_validates_shipped_topics():
    reg = SchemaRegistry(Store("config"))
    assert reg.validate("news.story.v1", {}) != []
    assert reg.validate("no.such.topic.v1", {}) == ["topic 'no.such.topic.v1' has no registered schema"]
    assert reg.validate("news.story.v1", "nope") == ["payload must be an object"]
