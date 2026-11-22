"""Precedence and sign handling for suggested ngspice measurement expressions."""

from __future__ import annotations

import re

#: The arithmetic operators a `.meas` signal-position expression may be
#: built from.
ARITH_OPS = ("+", "-", "*", "/")

#: A number written with an exponent (``1e-3``): its sign belongs to the
#: operand, so the operator scan neutralises it first (issue #6444).
_MEAS_EXPONENT_NUMBER_RE = re.compile(r"(?<![\w.])(?:\d+\.?\d*|\.\d+)[eE][+-]\d+")


def _top_level_operators(text: str) -> set[str]:
    """The binary arithmetic operators of *text* outside every parenthesis.
    A sign with no operand before it (``-v(a)``) is unary and not counted."""
    found: set[str] = set()
    depth = 0
    previous = ""  # the last non-space character seen
    for ch in _MEAS_EXPONENT_NUMBER_RE.sub("0", text):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch in ARITH_OPS and depth == 0 and previous not in ("", *ARITH_OPS, "("):
            found.add(ch)
        if not ch.isspace():
            previous = ch
    return found


def needs_parentheses(inner: str, prefix: str, after: str) -> bool:
    """Whether *inner*, put after *prefix* and before *after*
    (the neighbours of the text it replaces, an operator or not), must keep
    parentheses to mean what it meant as one operand: a ``+``/``-`` inside
    it rebinds under an outer ``-``, ``*`` or ``/`` before it or ``*``/``/``
    after it, and a ``*``/``/`` inside it under an outer ``/`` before it
    (``2/(a*b)`` is not ``2/a*b``)."""
    operators = _top_level_operators(inner)
    # A chain of unary signs can hide the operator binding this operand.
    if operators and re.search(r"[-+*/]\s*[-+]\s*$", prefix):
        return True
    before = prefix.rstrip()[-1:]
    if operators & {"+", "-"}:
        return before in ("-", "*", "/") or after in ("*", "/")
    return bool(operators) and before == "/"


# Vector names and scientific literals can contain signs of their own.
_CONTROL_TOKENS = re.compile(
    r"\b[vi]\s*\([^)]*\)|"
    + _MEAS_EXPONENT_NUMBER_RE.pattern
    + r"|(?P<op>[-+*/])(?P<gap>\s*)(?P<signs>[+-](?:\s*[+-])*)",
    re.IGNORECASE,
)


def control_expression(expression: str) -> str:
    """Fold signs on right operands because ngspice control rejects unary plus."""

    def fold(match: re.Match[str]) -> str:
        op = match["op"]
        if op is None:
            return match[0]
        gap = match["gap"]
        minus_count = match["signs"].count("-")
        if op in ("*", "/"):
            return op + gap + ("-" if minus_count % 2 else "")
        # Only rewrite the right operand of a binary operator.
        if expression[: match.start()].rstrip()[-1:] in ("", "(", ","):
            return match[0]
        minus_count += op == "-"
        return ("-" if minus_count % 2 else "+") + gap

    return _CONTROL_TOKENS.sub(fold, expression)
