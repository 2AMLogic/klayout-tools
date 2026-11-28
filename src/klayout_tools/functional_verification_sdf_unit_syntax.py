"""Read SDF time units and retained delay targets, preserving original
identifiers, and the parenthesis matchers every SDF pass shares."""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

from .functional_verification_base import FunctionalVerificationError

#: A time unit with its ``1``/``10``/``100`` multiplier, in any spacing or
#: case, optionally followed by a precision: a Verilog `` `timescale ``
#: argument, or an SDF header written ``(TIMESCALE 1ns/1ps)``.
_TIME_SUFFIX = r"(?:ms|us|ns|ps|fs|s)"
_MULTIPLIER = r"[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
_TIME_UNIT_RE = re.compile(
    rf"({_MULTIPLIER})\s*({_TIME_SUFFIX})(?:\s*/\s*({_MULTIPLIER})\s*{_TIME_SUFFIX})?",
    re.IGNORECASE,
)

#: Each unit's length in femtoseconds, the finest unit either format knows.
_UNIT_FS = {"s": 10**15, "ms": 10**12, "us": 10**9, "ns": 10**6, "ps": 10**3, "fs": 1}

# Mask lexemes that may contain parentheses or apparent keywords. Offsets in
# the mask still index the original text, including quoted CELLTYPE values.
_PAREN = re.compile(r"[()]")
_SDF_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|//[^\n]*|/\*.*?\*/|\\.', re.DOTALL)
_SDF_TIMESCALE_RE = re.compile(r"\(\s*TIMESCALE\b([^)]*)\)", re.IGNORECASE)
_SDF_DELAYFILE_RE = re.compile(r"\(\s*DELAYFILE\b", re.IGNORECASE)
_SDF_DIVIDER = re.compile(r"\(\s*DIVIDER\s+([./])\s*\)", re.IGNORECASE)
_SDF_CELL_RE = re.compile(r"\(\s*CELL\b", re.IGNORECASE)
_SDF_DELAY_RE = re.compile(r"\(\s*DELAY\b", re.IGNORECASE)
_SDF_INTERCONNECT_RE = re.compile(r"\(\s*INTERCONNECT\b", re.IGNORECASE)
_SDF_CELLTYPE_RE = re.compile(r'\(\s*CELLTYPE\s*"([^"]*)"', re.IGNORECASE)
_SDF_INSTANCE_RE = re.compile(r"\(\s*INSTANCE\b\s*([^)]*?)\s*\)", re.IGNORECASE)


def _sdf_syntax(text: str) -> str:
    def mask(match: re.Match[str]) -> str:
        value = match.group()
        if value.startswith('"'):
            return '"' + "x" * (len(value) - 2) + '"'
        if value.startswith("\\"):
            return "xx"
        return " " * len(value)

    return _SDF_LITERAL.sub(mask, text)


class _Target(NamedTuple):
    celltype: str | None
    instance: str | None
    divider: str
    interconnect: bool


def _integral_multiplier(written: str) -> int | None:
    """Read an exact positive integer, including SDF real-number spellings."""
    try:
        value = Decimal(written)
    except InvalidOperation:
        return None
    # Limit expansion of an exponent beyond the simulator's real-number range.
    if "e" in written.lower() and value.adjusted() > sys.float_info.max_10_exp:
        return None
    integral = value.to_integral_value()
    if value <= 0 or value != integral:
        return None
    try:
        return int(format(integral, "f"))
    except ValueError:  # Python's limit on integer string conversions.
        return None


def _parse_time_unit(unit: str) -> tuple[str, int] | None:
    """``("<multiplier><unit>", length in femtoseconds)``, or ``None`` when
    ``unit`` is not a positive time unit."""
    match = _TIME_UNIT_RE.fullmatch(unit.strip())
    if match is None:
        return None
    amount = _integral_multiplier(match[1])
    if amount is None or (
        match[3] is not None and _integral_multiplier(match[3]) is None
    ):
        return None
    unit = match[2].lower()
    return f"{amount}{unit}", amount * _UNIT_FS[unit]


def _unit_fs(unit: str) -> int:
    """A canonical unit's length in femtoseconds."""
    parsed = _parse_time_unit(unit)
    assert parsed is not None, unit
    return parsed[1]


def _same_time_unit(left: str, right: str) -> bool:
    """Whether two time units are the same length of time. ``1ns`` and
    ``1000ps`` are; ``10ps`` and ``1ns`` are not -- the multiplier is part of
    the unit."""
    parsed = (_parse_time_unit(left), _parse_time_unit(right))
    return (
        parsed[0] is not None and parsed[1] is not None and parsed[0][1] == parsed[1][1]
    )


def _sdf_timescale(text: str) -> str:
    """The SDF file's ``TIMESCALE`` unit as ``"<multiplier><unit>"``, or
    ``"1ns"`` when it has none (the SDF standard's default). Refuses a
    ``TIMESCALE`` that names no time unit: the unit of every delay in the
    file would be unknown."""
    unit = None
    for start, end in _file_clauses(text):
        if _SDF_CELL_RE.match(text, start):
            break
        match = _SDF_TIMESCALE_RE.fullmatch(text, start, end)
        if match:
            parsed = _header_unit(match[1].strip())
            if unit is not None and not _same_time_unit(unit, parsed):
                raise FunctionalVerificationError(
                    "options.sdf: conflicting TIMESCALE headers"
                )
            unit = parsed
    return unit or "1ns"


def _header_unit(written: str) -> str:
    parsed = _parse_time_unit(written)
    if parsed is None:
        raise FunctionalVerificationError(
            "options.sdf: cannot read the SDF's TIMESCALE "
            f"{written!r} as a time unit such as 1ns, 10ps or 100ps, so "
            "the unit of every delay in the file is unknown"
        )
    return parsed[0]


def _sdf_clause_end(text: str, open_index: int) -> int | None:
    """Index just past the clause opening at ``text[open_index]``, or ``None``
    when its parentheses never balance (the simulator reports that).

    The same answer as :func:`._sdf_matching_paren`, visiting each ``)`` in
    turn instead of each character: the nesting depth at a ``)`` is the depth
    left by the previous one plus the ``(`` in between, which ``str.count``
    takes in one call. It matters because a post-route SDF has a clause per
    ``CELL`` and this preflight walks three of them per cell: on the shared
    loop a synthetic 70 MB, 400k-cell file took 16 s, against 2 s this way,
    where one existing pass over it takes 7 s. A running total per window
    cannot replace it: the depth can return to zero and rise again inside one
    window.
    """
    depth, previous = 1, open_index
    while True:
        close = text.find(")", previous + 1)
        if close < 0:
            return None
        depth += text.count("(", previous + 1, close)
        if depth == 1:
            return close + 1
        depth, previous = depth - 1, close


def _sdf_matching_paren(text: str, open_index: int) -> int:
    """Return the index of the ``)`` that closes the ``(`` at ``open_index``,
    balancing nested parens -- what ``functional_verification_sdf`` uses
    throughout instead of a regex, since an SDF ``(DELAY (ABSOLUTE ...))``
    clause nests arbitrarily and a non-greedy regex would truncate at the
    first inner ``)``.

    At a ``(`` -- every caller's case -- this is :func:`_sdf_clause_end`,
    whose ``str.find``/``str.count`` walk is several times faster on a
    large post-route SDF. Off a ``(`` the two answers differ, so the
    original character walk is kept for that case.
    """
    if open_index < len(text) and text[open_index] == "(":
        end = _sdf_clause_end(text, open_index)
        if end is not None:
            return end - 1
    else:
        depth = 0
        index = open_index
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    return index
            index += 1
    raise FunctionalVerificationError(
        "options.sdf: unbalanced parentheses while scanning the SDF file for "
        "bit-selected top-level-port INTERCONNECT entries (issue #1619) -- "
        "the file is not well-formed SDF"
    )


def _sdf_cell_target(
    text: str, syntax: str, start: int, end: int
) -> tuple[str | None, str]:
    """``(CELLTYPE, INSTANCE)`` of the ``CELL`` at ``[start:end]``. A
    ``CELL`` with no quoted ``CELLTYPE`` yields ``None``; an empty
    ``(INSTANCE)`` yields ``""``."""
    celltype = _SDF_CELLTYPE_RE.search(syntax, start, end)
    instance = _SDF_INSTANCE_RE.search(syntax, start, end)
    declared = text[slice(*celltype.span(1))] if celltype else None
    return declared, text[slice(*instance.span(1))] if instance else ""


def _children(syntax: str, position: int) -> Iterator[tuple[int, int]]:
    """Walk direct child clauses from just inside the parent's opening paren."""
    while (paren := _PAREN.search(syntax, position)) and paren[0] == "(":
        end = _sdf_clause_end(syntax, paren.start())
        if end is None:
            return
        yield paren.start(), end
        position = end


def _file_clauses(syntax: str) -> Iterator[tuple[int, int]]:
    if delayfile := _SDF_DELAYFILE_RE.search(syntax):
        yield from _children(syntax, delayfile.end())


def _delay_kinds(syntax: str, start: int, end: int) -> set[bool]:
    """An INTERCONNECT uses the annotator's unit. Other entries use the cell's."""
    return {
        _SDF_INTERCONNECT_RE.match(syntax, entry) is not None
        for delay in _SDF_DELAY_RE.finditer(syntax, start, end)
        for group, _ in _children(syntax, delay.end())
        for entry, _ in _children(syntax, group + 1)
    }


def _sdf_delay_targets(text: str, syntax: str) -> list[_Target]:
    """Targets with retained delays, including DELAYs at the annotation root."""
    targets: list[_Target] = []
    divider = "."
    for start, end in _file_clauses(syntax):
        if _SDF_CELL_RE.match(syntax, start):
            kinds = _delay_kinds(syntax, start, end)
            if kinds:
                cell = _sdf_cell_target(text, syntax, start, end)
                targets.extend(_Target(*cell, divider, kind) for kind in kinds)
        elif _SDF_DELAY_RE.match(syntax, start):
            targets.extend(
                _Target(None, None, divider, kind)
                for kind in _delay_kinds(syntax, start, end)
            )
        elif match := _SDF_DIVIDER.fullmatch(syntax, start, end):
            divider = match[1]
    return targets
