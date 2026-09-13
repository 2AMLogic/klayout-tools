"""Unit tests for the shared helpers in ``klayout_tools._paths``.

These are internal (leading-underscore) helpers with no public re-export;
the module-level docstrings in ``_paths.py`` and in each duplicating
caller (``pex.py``, ``op_sanity.py``, ``netlist_normalize.py``) explain
why they were pulled out. This file covers the ones not already
exercised indirectly through a public verb's own test suite.
"""

from __future__ import annotations

from klayout_tools._paths import _fold_spice_continuations


def test_fold_spice_continuations_empty_input():
    assert _fold_spice_continuations([]) == []


def test_fold_spice_continuations_no_continuation_lines():
    lines = ["M1 out in vss vss nfet", ".ends"]
    assert _fold_spice_continuations(lines) == lines


def test_fold_spice_continuations_single_continuation():
    lines = [".subckt amp inp inn", "+ out vdd vss"]
    assert _fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_multiple_chained_continuations():
    lines = [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8",
        "+ L = 0.15",
        "+ W={2*wu}",
        "+ nf=1",
    ]
    assert _fold_spice_continuations(lines) == [
        "XM1 out in 0 0 sky130_fd_pr__pfet_01v8 L = 0.15 W={2*wu} nf=1"
    ]


def test_fold_spice_continuations_leading_continuation_with_nothing_to_fold_onto():
    """A ``+`` line with no preceding entry is passed through verbatim --
    it is not folded (there is nothing to fold onto) and not dropped or
    raised on. Callers that want an orphan continuation dropped instead
    (``op_sanity.py``) filter this function's return value themselves."""
    lines = ["+ out vdd vss", ".ends"]
    assert _fold_spice_continuations(lines) == ["+ out vdd vss", ".ends"]


def test_fold_spice_continuations_leading_whitespace_before_plus():
    lines = [".subckt amp inp inn", "  + out vdd vss"]
    assert _fold_spice_continuations(lines) == [".subckt amp inp inn out vdd vss"]


def test_fold_spice_continuations_preserves_non_continuation_entries_between():
    lines = ["A", "+ a-cont", "B", "+ b-cont"]
    assert _fold_spice_continuations(lines) == ["A a-cont", "B b-cont"]
