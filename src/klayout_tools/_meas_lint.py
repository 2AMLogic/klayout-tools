"""Lint of arithmetic expressions out of `.meas` signal positions (issue #5651).

ngspice's ``.MEASURE`` accepts only a single vector reference (``v(node)``,
``i(source)``, or a bare vector name) in every signal position -- the
``WHEN``/``FIND``/``AVG``/``MAX``/``TRIG``/``TARG`` position of a ``.meas``
card. A bare arithmetic expression over vectors there passes request
validation and then fails *every* corner with ngspice's "no such vector",
while the same quantity wrapped as ``par('...')`` works in the same card
(measured on ngspice-47): ngspice lifts the quoted expression into an
internal ``pa_NN`` helper vector.

This module finds such an expression in a card and raises the refusal,
before any corner runs. It is a self-contained unit with no other caller;
:func:`klayout_tools.sim_validate._validate_meas_card` calls
:func:`check_meas_signal_position` on every ``.meas`` card it validates,
and ``sim_steps`` passes ``control_scope=True`` for ``analysis_steps[]``
cards (emitted as ``meas`` commands inside a generated ``.control`` block).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sim_errors import SimError

#: A ``par('...')`` expression span in a `.meas` card (issue #5651). ngspice
#: lifts the quoted expression into an internal ``pa_NN`` helper vector, so a
#: wrapped expression *is* a single vector reference as far as a signal
#: position is concerned. Matched on the original card both to blank those
#: spans out of the adjacency scan (length-preserving, so offsets still index
#: the original) and to unwrap them again when the refusal quotes the
#: author's own expression. Case-insensitive: ngspice card syntax is.
_MEAS_PAR_SPAN_RE = re.compile(r"par\s*\(\s*'[^']*'\s*\)", re.IGNORECASE)

#: A ``v(...)``/``i(...)``-style vector reference in a `.meas` card, with its
#: contents captured so a two-node ``v(a,b)`` can be detected (ngspice
#: resolves no such vector in a `.meas` signal position -- issue #5651).
#: Run on the par-blanked card, so references inside a ``par('...')`` span
#: are not seen here.
_MEAS_VECTOR_REF_RE = re.compile(r"\b[vViI]\s*\(([^)]*)\)")

#: ngspice's line-continuation marker: a ``+`` as the first non-blank
#: character of a line (measured working on a ``when`` card, issue #5651).
#: Blanked out (length-preserving) before the adjacency scan so the
#: continuation ``+`` is not read as an arithmetic operator.
_MEAS_LINE_CONTINUATION_RE = re.compile(r"(?m)^[ \t]*\+")

#: ngspice's two-node voltage reference: ``v(a,b)``. Accepted in a
#: ``print``/plot context but resolved as no vector in a `.meas` signal
#: position (issue #5651); the expressible equivalent is ``v(a)-v(b)``.
_MEAS_TWO_NODE_REF_RE = re.compile(r"([vViI])\s*\(\s*([^,()\s]+)\s*,\s*([^()]+?)\s*\)")

#: The arithmetic operators a `.meas` signal-position expression may be
#: built from.
_MEAS_ARITH_OPS = ("+", "-", "*", "/")

#: Words of a `.meas` card that are keywords, never functions: a signal
#: position's keyword (``when``, ``find``, ``avg``, ...) and the card's
#: modifiers. One of these right before a ``(`` must not be read as the
#: callee of a call, or a span widened over the "call" swallows the keyword
#: and the suggested ``par('when (v(a)-v(b))')`` fatals ngspice-47 with
#: ``no such function 'when'`` (issue #5651).
_MEAS_CARD_KEYWORDS = frozenset(
    {
        "when", "find", "avg", "max", "min", "rms", "integ", "pp", "deriv",
        "min_at", "max_at", "trig", "targ", "param", "at", "val", "from",
        "to", "td", "cross", "rise", "fall", "meas", "measure", "tran",
        "dc", "ac", "sp", "noise",
    }
)  # fmt: skip


#: One operator-merged run of adjacent references in a `.meas` card: its
#: ``(start, end)`` span in the original card, how many references merged
#: into it, and whether any of them held a comma (a two-node ``v(a,b)``).
@dataclass
class _MeasGroup:
    start: int
    end: int
    count: int = 1
    comma: bool = False


def _blank_par_spans_and_continuations(spice: str) -> str:
    """A length-preserving copy of *spice* with every ``par('...')`` span and
    every line-continuation ``+`` blanked to spaces.

    Offsets in the copy index the original card, while operators *inside* a
    ``par(...)`` expression and continuation markers are invisible to the
    adjacency scan (issue #5651: ``when par('v(a)-v(b)')=0.3`` works, and a
    ``+``-continued ``when`` card is legal).
    """
    blanked = _MEAS_PAR_SPAN_RE.sub(lambda m: " " * len(m.group(0)), spice)
    return _MEAS_LINE_CONTINUATION_RE.sub(lambda m: m.group(0)[:-1] + " ", blanked)


def _par_span_inner(span: str) -> str:
    """The quoted expression of a ``par('...')`` span, without the wrapper."""
    return span[span.index("'") + 1 : span.rindex("'")]


def _unwrap_par_spans(text: str) -> str:
    """Replace every ``par('...')`` span in *text* with its quoted
    expression, so a suggested rewrite holds one ``par('...')`` wrapper
    around the whole expression (``par('v(a)-v(b)')-v(c)`` ->
    ``v(a)-v(b)-v(c)``)."""
    return _MEAS_PAR_SPAN_RE.sub(lambda m: _par_span_inner(m.group(0)), text)


def _two_node_difference(text: str) -> str:
    """``v(a,b)`` -- ngspice's two-node voltage -- as the ``v(a)-v(b)`` a
    ``par('...')`` wrapper can hold (issue #5651: ``v(a,b)`` resolves no
    such vector in a `.meas` signal position). Text that is not exactly a
    two-node reference is returned unchanged."""
    match = _MEAS_TWO_NODE_REF_RE.fullmatch(text.strip())
    if match is None:
        return text
    func, first, second = match.group(1), match.group(2), match.group(3)
    return f"{func}({first})-{func}({second})"


def _balanced_close(spice: str, open_at: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at *open_at*, or ``None`` when
    the group never closes."""
    depth = 0
    for index in range(open_at, len(spice)):
        if spice[index] == "(":
            depth += 1
        elif spice[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _callee_start(spice: str, open_at: int) -> int | None:
    """Start of the callee identifier immediately before the ``(`` at
    *open_at* (spaces allowed) -- ``None`` when the group has no callee and
    is a bare parenthesised group. A `.meas` card keyword (``when``,
    ``avg``, ...) sitting right before the ``(`` is not a callee: the group
    after it is a bare group, so the keyword stays outside any ``par('...')``
    span built from it."""
    cursor = open_at
    while cursor > 0 and spice[cursor - 1].isspace():
        cursor -= 1
    match = re.search(r"[A-Za-z0-9_.]+$", spice[:cursor])
    if match is None or match.group(0).lower() in _MEAS_CARD_KEYWORDS:
        return None
    return match.start()


def _token_end(spice: str, start: int) -> int | None:
    """End index of the operand token beginning at *start* -- a quoted span,
    a parenthesised group, a call (``abs(v(b))``: an identifier run followed
    by its balanced group, spaces allowed before it), or a bare
    number/identifier run -- or ``None`` when no operand starts there."""
    if start >= len(spice):
        return None
    ch = spice[start]
    if ch == "'":
        quote_close = spice.find("'", start + 1)
        return None if quote_close == -1 else quote_close + 1
    if ch == "(":
        close = _balanced_close(spice, start)
        return None if close is None else close + 1
    match = re.match(r"[A-Za-z0-9_.]+", spice[start:])
    if match is None:
        return None
    end = start + match.end()
    cursor = end
    while cursor < len(spice) and spice[cursor].isspace():
        cursor += 1
    if cursor < len(spice) and spice[cursor] == "(":
        close = _balanced_close(spice, cursor)
        if close is not None:
            return close + 1
    return end


def _token_start(spice: str, end: int) -> int | None:
    """Start index of the operand token whose last character is at *end*,
    or ``None`` when no operand ends there. A group that closes at *end*
    and carries a callee (``abs(v(b))``) absorbs it, so the token spans the
    whole call."""
    if end < 0:
        return None
    ch = spice[end]
    if ch == "'":
        open_quote = spice.rfind("'", 0, end)
        return None if open_quote == -1 else open_quote
    if ch == ")":
        depth = 0
        for index in range(end, -1, -1):
            if spice[index] == ")":
                depth += 1
            elif spice[index] == "(":
                depth -= 1
                if depth == 0:
                    callee = _callee_start(spice, index)
                    return index if callee is None else callee
        return None
    match = re.search(r"[A-Za-z0-9_.]+$", spice[: end + 1])
    return None if match is None else match.start()


def _extend_left_over_operand(spice: str, start: int) -> tuple[int, str]:
    """Take one left step: ``again`` retries after widening, ``stop`` ends
    the whole walk, and ``pass`` moves on to the right side."""
    left = start
    while left > 0 and spice[left - 1].isspace():
        left -= 1
    if left <= 0 or spice[left - 1] not in _MEAS_ARITH_OPS:
        return start, "pass"
    cursor = left - 1
    while cursor > 0 and spice[cursor - 1].isspace():
        cursor -= 1
    found = _token_start(spice, cursor - 1)
    if found is None or spice[found:cursor].strip().lower() in _MEAS_CARD_KEYWORDS:
        # No operand precedes the operator, only a card keyword or nothing.
        # Absorb a leading unary sign without swallowing the keyword, then
        # stop the whole walk (including the right side), as before.
        return (left - 1 if spice[left - 1] in "+-" else start), "stop"
    return found, "again"


def _extend_right_over_operand(spice: str, end: int) -> tuple[int, bool]:
    """Widen *end* over one operand bound by an arithmetic operator,
    returning whether the walk may continue."""
    right = end
    while right < len(spice) and spice[right].isspace():
        right += 1
    if right >= len(spice) or spice[right] not in _MEAS_ARITH_OPS:
        return end, False
    cursor = right + 1
    while cursor < len(spice) and spice[cursor].isspace():
        cursor += 1
    found = _token_end(spice, cursor)
    return (end, False) if found is None else (found, True)


def _extend_over_operands(spice: str, start: int, end: int) -> tuple[int, int]:
    """Widen a lone flagged reference over the operand it is bound to by an
    outer arithmetic operator (``2*v(a)`` or ``v(a)*2``), so the refusal's
    example rewrite wraps the whole local expression in ``par('...')``
    rather than only the reference."""
    while True:
        start, verdict = _extend_left_over_operand(spice, start)
        if verdict == "again":
            continue
        if verdict == "stop":
            return start, end
        end, again = _extend_right_over_operand(spice, end)
        if not again:
            return start, end


def _widen_over_enclosing_call(spice: str, start: int, end: int) -> tuple[int, int]:
    """Widen a flagged expression span over the balanced groups enclosing it
    (``abs(v(a)-v(b))``, nested calls too), so the refusal's rewrite wraps
    the whole local expression -- ``par('abs(v(a)-v(b))')``, the form
    ngspice accepts -- rather than ``abs(par('v(a)-v(b)'))``, which fatals
    on every corner. A bare parenthesised group (no callee, or a `.meas`
    keyword such as ``when`` before it) is widened over too, parentheses
    included and the keyword left outside: ``when par('(v(a)-v(b))')=0.1``
    works, while ``when (par('v(a)-v(b)'))=0.1`` errors on ngspice-47."""
    while True:
        depth = 0
        widened = False
        for index in range(start - 1, -1, -1):
            ch = spice[index]
            if ch == ")":
                depth += 1
            elif ch == "(":
                if depth > 0:
                    depth -= 1
                    continue
                close = _balanced_close(spice, index)
                if close is None or close < end:
                    continue
                callee = _callee_start(spice, index)
                start, end = (index if callee is None else callee), close + 1
                widened = True
                break
        if not widened:
            return start, end


def _find_meas_signal_expression(spice: str) -> tuple[int, int, str] | None:
    """Locate an arithmetic expression in a `.meas` card's signal position.

    The rule fires on exactly that and nothing else (issue #5651, every case
    measured on ngspice-47): references or ``par('...')`` spans connected
    by ``+ - * /`` operators (spaces ignored), a comma inside a reference
    (the two-node ``v(a,b)``), or a lone reference bound to an operand by
    an outer operator (``2*v(a)``).

    Returns ``(start, end, text)`` -- the expression's span in *spice* and
    its verbatim text, widened over any enclosing balanced call so the
    quoted rewrite holds the whole local expression -- or ``None`` when
    every reference stands alone:
    ``when v(a)=0.5``, ``when a=0.5`` (a bare identifier left-hand side is
    legal), ``i(V1)=-0.0005`` (the ``-`` binds a number), and the #2826
    right-hand-side cases ``when v(a)=0.5*1.62`` / ``find v(a) at='1u+2n'``
    (operators between *numbers*, never adjacent to a reference) all stay
    unflagged.
    """
    blanked = _blank_par_spans_and_continuations(spice)
    items: list[tuple[int, int, bool]] = []  # (start, end, comma inside ref)
    for match in _MEAS_VECTOR_REF_RE.finditer(blanked):
        items.append((match.start(), match.end(), "," in match.group(1)))
    for match in _MEAS_PAR_SPAN_RE.finditer(spice):
        items.append((match.start(), match.end(), False))
    if not items:
        return None
    items.sort()
    # Merge operator-connected neighbours into expression groups.
    groups: list[_MeasGroup] = []
    for start, end, comma in items:
        if groups:
            between = blanked[groups[-1].end : start].strip()
            if between and all(op in _MEAS_ARITH_OPS for op in between):
                groups[-1].end = end
                groups[-1].count += 1
                continue
        groups.append(_MeasGroup(start, end, comma=comma))
    for group in groups:
        if group.count >= 2 or group.comma:
            lo, hi = _widen_over_enclosing_call(spice, group.start, group.end)
            return (lo, hi, spice[lo:hi])
        before = blanked[: group.start].rstrip()
        after = blanked[group.end :].lstrip()
        if before.endswith(_MEAS_ARITH_OPS) or after.startswith(_MEAS_ARITH_OPS):
            lo, hi = _extend_over_operands(spice, group.start, group.end)
            lo, hi = _widen_over_enclosing_call(spice, lo, hi)
            return (lo, hi, spice[lo:hi])
    return None


def _splice_replacement(spice: str, start: int, end: int, replacement: str) -> str:
    """*spice* with ``spice[start:end]`` replaced by *replacement* (a
    ``par('...')`` call or a ``vdiff`` name, both identifier-initial). A
    space goes in when the character before the span is an identifier
    character, so ``when(v(a)-v(b))`` rewrites to ``when par('...')`` and
    never to the unknown token ``whenpar(``."""
    gap = " " if start > 0 and re.match(r"[A-Za-z0-9_.]", spice[start - 1]) else ""
    return f"{spice[:start]}{gap}{replacement}{spice[end:]}"


def check_meas_signal_position(
    name: str, spice: str, control_scope: bool = False
) -> None:
    """Refuse a `.meas` card carrying an arithmetic expression in a signal
    position (issue #5651); return silently when every reference stands
    alone.

    The refusal is raised before any corner runs (``--dry-run`` included),
    quotes the author's own expression and its ``par('...')`` rewrite, and
    points at ``measurements[].crossings`` for the cases that form serves
    better (a crossing *series*, or a supply-relative ``level``) with its
    count-vs-time trade-off stated. With ``control_scope=True``
    (``analysis_steps[]`` cards, emitted as ``meas`` commands inside the
    generated ``.control`` block) the remedy named is instead an
    intermediate ``expr`` measurement referenced by name: ``par('...')``
    is a file-scope feature ngspice rejects in control scope, where a
    ``let`` helper line is the working route. The expression rule itself is
    deliberately narrow -- see :func:`_find_meas_signal_expression`.
    """
    found = _find_meas_signal_expression(spice)
    if found is None:
        return
    start, end, expression = found
    inner = _two_node_difference(_unwrap_par_spans(expression))
    rewrite = _splice_replacement(spice, start, end, f"par('{inner}')")
    if control_scope:
        reference = _splice_replacement(spice, start, end, "vdiff")
        raise SimError(
            f"measurement '{name}': a control-scope meas command's signal "
            "position takes a single vector reference, not the arithmetic "
            f"expression '{expression}' -- ngspice resolves no such vector "
            "and the card fails on every corner. The file-scope expression "
            "wrapper is not accepted inside a .control block; define the "
            "expression as an intermediate measurement declared before this "
            f'card ({{"name": "vdiff", "expr": "{inner}", "intermediate": '
            "true} -- `klt sim` emits it as a `let vdiff = "
            f"{inner}` control line in declared order) and reference that "
            f"name in the card instead: `{reference}`"
        )
    raise SimError(
        f"measurement '{name}': a .meas signal position takes a single "
        "vector reference, not the arithmetic expression "
        f"'{expression}' -- ngspice resolves no such vector and the card "
        "fails on every corner. Wrap the whole expression in a par('...') "
        f"span: `{rewrite}`. For a whole crossing series, or a "
        'supply-relative level ({"frac_of": "vdd", "fraction": 0.5}) that '
        "`klt sim` resolves per corner, a measurements[].crossings entry "
        "also fits -- but a crossings entry's value is the *count* of "
        "crossings, not a time (the crossing times are reported on its "
        "crossings.times[] array)"
    )
