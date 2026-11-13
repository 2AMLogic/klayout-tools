"""Unit tests for ``klayout_tools._spice_text`` -- the SPICE line
pre-processing (``;``/``$`` inline comments, ``+`` continuation folding)
that every netlist-reading ``klt`` verb shares.

Both helpers started as per-module copies: the fold was consolidated first
(issues #1741, #3703), the comment strip once six copies had drifted into
four different rules about what a ``$`` means (issue #5417). The table below
is the record of that one rule, so a seventh near-miss cannot be written
without a failing test.
"""

from __future__ import annotations

import pytest

from klayout_tools._spice_text import (
    fold_spice_continuations,
    strip_spice_inline_comment,
)

#: ``(line, expected)`` for every shape of ngspice inline comment the
#: package's netlist readers meet -- the one rule that replaced six drifted
#: hand-rolled copies (issue #5417).
_INLINE_COMMENT_CASES = [
    # --- no comment marker at all: only right-stripped ---
    ("R1 a b 1k", "R1 a b 1k"),
    ("R1 a b 1k   ", "R1 a b 1k"),
    ("", ""),
    # --- `;` cuts anywhere, with or without surrounding whitespace ---
    ("R1 a b 1k ; load resistor", "R1 a b 1k"),
    ("R1 a b 1k; load resistor", "R1 a b 1k"),
    ("R1 a b 1k;", "R1 a b 1k"),
    ("; whole line", ""),
    # --- `$` delimited on both sides is a comment ---
    ("R1 a b 1k $ load resistor", "R1 a b 1k"),
    ("R1 a b 1k $", "R1 a b 1k"),
    ("R1 a b 1k $ $ nested marker", "R1 a b 1k"),
    ("$ whole line", ""),
    ("$", ""),
    # --- `$` glued to the NEXT token is a variable reference, not a comment ---
    (
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice",
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice",
    ),
    (
        ".lib ${PDK_ROOT}/sky130A/libs.tech/combined/sky130.lib.spice tt",
        ".lib ${PDK_ROOT}/sky130A/libs.tech/combined/sky130.lib.spice tt",
    ),
    # ... and still is not one when a real comment follows it later
    (
        ".include $PDK_ROOT/models.spice $ device models",
        ".include $PDK_ROOT/models.spice",
    ),
    # --- `$` glued to the PREVIOUS token belongs to that token's name ---
    # KLayout writes cards as `X$1`/`M$1`/`C$1` (issue #2972) and anonymous
    # nets as `$1`/`$5` (`extract.py`'s placeholder note).
    ("X$1 a b cap_cmomi", "X$1 a b cap_cmomi"),
    (".subckt cell $1 $2", ".subckt cell $1 $2"),
    ("C$1 $3 $4 1.19e-13 $ parasitic", "C$1 $3 $4 1.19e-13"),
    ("R1 a b 1k$ not a comment", "R1 a b 1k$ not a comment"),
    # --- whichever marker comes FIRST wins ---
    ("R1 a b 1k ; semi $ dollar", "R1 a b 1k"),
    ("R1 a b 1k $ dollar ; semi", "R1 a b 1k"),
    # --- parenthesised waveform arguments are untouched ---
    ("V1 a 0 PULSE(0 1.8 0 1n 1n) $ supply", "V1 a 0 PULSE(0 1.8 0 1n 1n)"),
    # --- leading whitespace is NOT stripped (callers decide) ---
    ("    M1 d g s b nfet $ dev", "    M1 d g s b nfet"),
]


@pytest.mark.parametrize(("line", "expected"), _INLINE_COMMENT_CASES)
def test_strip_spice_inline_comment(line, expected):
    assert strip_spice_inline_comment(line) == expected


def test_strip_spice_inline_comment_is_idempotent():
    """Re-stripping an already-stripped line is a no-op -- `characterize.py`
    applies the helper to lines a caller may have pre-processed."""
    for _line, expected in _INLINE_COMMENT_CASES:
        assert strip_spice_inline_comment(expected) == expected.rstrip()


def test_fold_spice_continuations_empty_input():
    assert fold_spice_continuations([]) == []


def test_fold_spice_continuations_no_continuation_lines():
    lines = ["M1 out in vss vss nfet", ".ends"]
    assert fold_spice_continuations(lines) == lines


def test_fold_spice_continuations_single_continuation():
    lines = [".subckt amp inp inn", "+ out vdd vss"]
    assert fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_multiple_chained_continuations():
    lines = [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8",
        "+ L = 0.15",
        "+ W={2*wu}",
        "+ nf=1",
    ]
    assert fold_spice_continuations(lines) == [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8 L = 0.15 W={2*wu} nf=1"
    ]


def test_fold_spice_continuations_leading_continuation_with_nothing_to_fold_onto():
    """A ``+`` line with no preceding entry is passed through verbatim --
    it is not folded (there is nothing to fold onto) and not dropped or
    raised on. Callers that want an orphan continuation dropped instead
    (``op_sanity.py``) filter this function's return value themselves."""
    lines = ["+ out vdd vss", ".ends"]
    assert fold_spice_continuations(lines) == ["+ out vdd vss", ".ends"]


def test_fold_spice_continuations_leading_whitespace_before_plus():
    lines = [".subckt amp inp inn", "  + out vdd vss"]
    assert fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_preserves_non_continuation_entries_between():
    lines = ["A", "+ a-cont", "B", "+ b-cont"]
    assert fold_spice_continuations(lines) == ["A a-cont", "B b-cont"]
