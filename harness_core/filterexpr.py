"""A tiny, safe expression language for trigger filters.

    payload.urgency == 'high' and payload.score >= 7
    payload.category in ['billing', 'work']
    not (payload.kind == 'spam')

There is no eval(): the expression is tokenised, parsed to a tree, and walked. Unknown paths
evaluate to None. Only the roots `payload`, `type` and `produced_by` are visible.
"""
from __future__ import annotations

import re
from typing import Any

ROOTS = {"payload", "type", "produced_by"}
_TOKEN = re.compile(
    r"""\s*(?:
      (?P<num>-?\d+(?:\.\d+)?)
     |(?P<str>'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")
     |(?P<op>==|!=|<=|>=|<|>|\(|\)|\[|\]|,)
     |(?P<word>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)
    )""",
    re.X,
)
_KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}


class FilterError(ValueError):
    pass


def _tokenise(src: str) -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    pos = 0
    src = src.strip()
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m or m.end() == pos:
            raise FilterError(f"unexpected character at {pos}: {src[pos:pos + 10]!r}")
        pos = m.end()
        if m.group("num") is not None:
            n = m.group("num")
            out.append(("lit", float(n) if "." in n else int(n)))
        elif m.group("str") is not None:
            raw = m.group("str")[1:-1]
            out.append(("lit", re.sub(r"\\(.)", r"\1", raw)))
        elif m.group("op") is not None:
            out.append(("op", m.group("op")))
        else:
            w = m.group("word")
            if w in _KEYWORDS:
                out.append(("kw", w))
            else:
                if w.split(".")[0] not in ROOTS:
                    raise FilterError(f"unknown name '{w}' (use payload.<field>, type or produced_by)")
                out.append(("path", w))
    return out


class _Parser:
    def __init__(self, tokens: list[tuple[str, Any]]):
        self.t = tokens
        self.i = 0

    def peek(self) -> tuple[str, Any] | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> tuple[str, Any]:
        tok = self.peek()
        if tok is None:
            raise FilterError("unexpected end of expression")
        self.i += 1
        return tok

    def is_(self, kind: str, val: Any) -> bool:
        tok = self.peek()
        return tok is not None and tok == (kind, val)

    def parse(self):
        node = self.or_()
        if self.peek() is not None:
            raise FilterError(f"unexpected token {self.peek()[1]!r}")
        return node

    def or_(self):
        node = self.and_()
        while self.is_("kw", "or"):
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.is_("kw", "and"):
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        if self.is_("kw", "not"):
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        left = self.operand()
        tok = self.peek()
        if tok and tok[0] == "op" and tok[1] in ("==", "!=", "<", "<=", ">", ">="):
            self.take()
            return ("cmp", tok[1], left, self.operand())
        if self.is_("kw", "in"):
            self.take()
            return ("in", left, self.operand())
        if self.is_("kw", "not") and self.i + 1 < len(self.t) and self.t[self.i + 1] == ("kw", "in"):
            self.take()
            self.take()
            return ("not", ("in", left, self.operand()))
        return left

    def operand(self):
        kind, val = self.take()
        if kind == "lit":
            return ("lit", val)
        if kind == "path":
            return ("path", val.split("."))
        if kind == "kw" and val in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[val])
        if (kind, val) == ("op", "("):
            node = self.or_()
            if self.take() != ("op", ")"):
                raise FilterError("expected ')'")
            return node
        if (kind, val) == ("op", "["):
            items = []
            while not self.is_("op", "]"):
                items.append(self.operand())
                if self.is_("op", ","):
                    self.take()
            self.take()
            return ("list", items)
        raise FilterError(f"unexpected token {val!r}")


def parse(src: str):
    if not src or not src.strip():
        raise FilterError("empty filter")
    return _Parser(_tokenise(src)).parse()


def _get(path: list[str], env: dict) -> Any:
    cur: Any = env
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _eval(node, env):
    k = node[0]
    if k == "lit":
        return node[1]
    if k == "path":
        return _get(node[1], env)
    if k == "list":
        return [_eval(n, env) for n in node[1]]
    if k == "not":
        return not _eval(node[1], env)
    if k == "and":
        return bool(_eval(node[1], env)) and bool(_eval(node[2], env))
    if k == "or":
        return bool(_eval(node[1], env)) or bool(_eval(node[2], env))
    if k == "in":
        right = _eval(node[2], env)
        return _eval(node[1], env) in right if isinstance(right, (list, str)) else False
    if k == "cmp":
        op, a, b = node[1], _eval(node[2], env), _eval(node[3], env)
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if a is None or b is None or isinstance(a, (bool, str)) != isinstance(b, (bool, str)):
            return False
        try:
            return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        except TypeError:
            return False
    raise FilterError(f"bad node {k}")


def validate(src: str) -> None:
    """Raise FilterError if the expression is not valid."""
    parse(src)


def matches(src: str, envelope: dict) -> bool:
    env = {"payload": envelope.get("payload", {}), "type": envelope.get("type"), "produced_by": envelope.get("produced_by")}
    return bool(_eval(parse(src), env))
