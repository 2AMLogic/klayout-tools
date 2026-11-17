"""``corners.variants`` parsing and its source-expression evaluator.

Moved verbatim out of ``sim.py`` (issue #5428) so that module stays under its
per-file size ratchet. Holds variant name/slug validation, source resolution
with cycle detection, and :class:`_SourceExprParser` -- the recursive-descent
parser that evaluates a ``sources`` expression without ``eval``.
"""

from __future__ import annotations

import re
from typing import Any

from .sim_errors import SimError
from .sim_slug import _slugify_label

_VARIANT_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_EXPR_TOKEN_RE = re.compile(
    r"\s*(?:(?P<num>\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?)"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_.]*)|(?P<op>[-+*/()]))"
)


def _parse_variants(raw: Any) -> list[tuple[str | None, dict[str, float]]]:
    """Normalize ``corners.variants`` (issue #3252) into ``(name, sources)``
    pairs with every source value resolved to a number.

    ``None`` (axis absent) yields the single nameless, source-less entry so
    :func:`_expand_corners` is byte-identical to its pre-variant behaviour.
    Each variant's ``sources`` map a voltage-source name to a number or to an
    arithmetic expression of the *other* sources in the same variant; the
    resolved numbers are applied with ``alter`` exactly like ``supply_v`` and
    override a same-named ``supply_v`` key.
    """
    if raw is None:
        return [(None, {})]
    if not isinstance(raw, list) or not raw:
        raise SimError("corners.variants must be a non-empty array")
    seen: set[str] = set()
    seen_slugs: dict[str, str] = {}
    out: list[tuple[str | None, dict[str, float]]] = []
    for index, entry in enumerate(raw):
        where = f"corners.variants[{index}]"
        if not isinstance(entry, dict):
            raise SimError(f"{where} must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not _VARIANT_NAME_RE.match(name):
            raise SimError(
                f"{where}.name must be a non-empty string of letters, digits, "
                "'_', '.' or '-'"
            )
        if name in seen:
            raise SimError(f"corners.variants has duplicate name {name!r}")
        seen.add(name)
        # '.' and '-' are both legal in a name but slug to 'p' / 'n', so two
        # distinct names can share an artifact directory -- refuse that.
        slug = _slugify_label(name)
        if slug in seen_slugs:
            raise SimError(
                f"corners.variants names {seen_slugs[slug]!r} and {name!r} "
                f"both map to artifact slug {slug!r}; rename one"
            )
        seen_slugs[slug] = name
        sources = entry.get("sources")
        if not isinstance(sources, dict) or not sources:
            raise SimError(f"{where}.sources must be a non-empty object")
        out.append((name, _resolve_variant_sources(name, sources)))
    return out


def _resolve_variant_sources(variant: str, sources: dict[str, Any]) -> dict[str, float]:
    resolved: dict[str, float] = {}
    visiting: list[str] = []
    lowered = {key.strip().lower(): key for key in sources}

    def resolve(key: str) -> float:
        if key in resolved:
            return resolved[key]
        if key in visiting:
            cycle = " -> ".join([*visiting[visiting.index(key) :], key])
            raise SimError(
                f"corners.variants[{variant!r}].sources has a cyclic "
                f"expression: {cycle}"
            )
        value = sources[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise SimError(
                f"corners.variants[{variant!r}].sources[{key!r}] must be a "
                "number or an arithmetic expression string"
            )
        if isinstance(value, str):
            visiting.append(key)
            try:
                result = _eval_source_expr(variant, key, value, lowered, resolve)
            finally:
                visiting.pop()
        else:
            result = float(value)
        resolved[key] = result
        return result

    for key in sources:
        resolve(key)
    return {key: resolved[key] for key in sources}


def _tokenize_source_expr(where: str, text: str) -> list[tuple[str, str]]:
    """Split ``text`` into ``(kind, lexeme)`` tokens for the source-expression
    grammar, raising :class:`SimError` on any unparseable character."""
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if not text[pos:].strip():
            break
        match = _EXPR_TOKEN_RE.match(text, pos)
        if match is None:
            raise SimError(f"{where}: cannot parse expression {text!r}")
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


class _SourceExprParser:
    """Recursive-descent evaluator for ``+ - * /``, parentheses, numbers, and
    other source names of the same variant (no ``eval``)."""

    def __init__(
        self,
        where: str,
        text: str,
        tokens: list[tuple[str, str]],
        lowered: dict[str, str],
        resolve: Any,
    ) -> None:
        self.where = where
        self.text = text
        self.tokens = tokens
        self.lowered = lowered
        self.resolve = resolve
        self.cursor = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.cursor] if self.cursor < len(self.tokens) else None

    def take(self) -> tuple[str, str]:
        token = self.peek()
        if token is None:
            raise SimError(f"{self.where}: unexpected end of expression {self.text!r}")
        self.cursor += 1
        return token

    def _peek_op(self, ops: str) -> str | None:
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] in ops:
            return token[1]
        return None

    def parse(self) -> float:
        result = self.expr()
        leftover = self.peek()
        if leftover is not None:
            raise SimError(
                f"{self.where}: unexpected {leftover[1]!r} in expression {self.text!r}"
            )
        return result

    def expr(self) -> float:
        value = self.term()
        while (op := self._peek_op("+-")) is not None:
            self.take()
            rhs = self.term()
            value = value + rhs if op == "+" else value - rhs
        return value

    def term(self) -> float:
        value = self.factor()
        while (op := self._peek_op("*/")) is not None:
            self.take()
            rhs = self.factor()
            value = self._apply_muldiv(op, value, rhs)
        return value

    def _apply_muldiv(self, op: str, lhs: float, rhs: float) -> float:
        if op == "*":
            return lhs * rhs
        if rhs == 0:
            raise SimError(f"{self.where}: division by zero in {self.text!r}")
        return lhs / rhs

    def _name(self, tok: str) -> float:
        target = self.lowered.get(tok.lower())
        if target is None:
            raise SimError(
                f"{self.where}: unknown source name {tok!r} in expression "
                f"{self.text!r} (expressions may only reference other sources "
                "of the same variant)"
            )
        return float(self.resolve(target))

    def _group(self) -> float:
        value = self.expr()
        if self.take()[1] != ")":
            raise SimError(f"{self.where}: unbalanced parentheses in {self.text!r}")
        return value

    def factor(self) -> float:
        kind, tok = self.take()
        if kind == "num":
            return float(tok)
        if kind == "name":
            return self._name(tok)
        if tok == "-":
            return -self.factor()
        if tok == "+":
            return self.factor()
        if tok == "(":
            return self._group()
        raise SimError(f"{self.where}: unexpected {tok!r} in expression {self.text!r}")


def _eval_source_expr(
    variant: str,
    key: str,
    text: str,
    lowered: dict[str, str],
    resolve: Any,
) -> float:
    """Evaluate ``text`` (``+ - * /``, parentheses, numbers, and other source
    names of the same variant) without ``eval``."""
    where = f"corners.variants[{variant!r}].sources[{key!r}]"
    tokens = _tokenize_source_expr(where, text)
    return _SourceExprParser(where, text, tokens, lowered, resolve).parse()
