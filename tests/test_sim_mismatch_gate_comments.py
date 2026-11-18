"""Comment and ``+``-continuation handling in the per-instance mismatch gate
and the declaration scanner (issue #5487).

Both readers must fold a netlist exactly as ngspice does, because a mismatch
between them is silent: the gate writes ``mm_ok=0`` somewhere ngspice ignores
(or claims a parameter ngspice never applied) and the report still says
``randomised: false``. Every expectation below was measured on ngspice-47
(2026-11-13) with ``.subckt rr a b foo=1 mm_ok=1`` holding
``R1 a b {1k + mm_ok*1k}`` across 1 V, where ``-1.0e-03`` means ``mm_ok=0``
was honoured and ``-5.0e-04`` means the instance stayed randomised.

The ``$`` / ``;`` end-of-line cases live in
``test_sim_mismatch_gate_sg13g2.py``; this file covers ``//`` and the lines
ngspice drops before it folds continuations.
"""

from klayout_tools import sim_mismatch_declarations as decl
from klayout_tools import sim_mismatch_gate as gate

DECLARED = {"sg13_lv_nmos": {"w", "l", "mm_ok"}}


def _gate(text):
    return gate.gate_netlist(
        text, [], ["xm1"], instance_param="mm_ok", declared_params=DECLARED
    )


def _instance_line(text):
    return next(ln for ln in text.split("\n") if ln.startswith("XM1"))


# --- `//` is an end-of-line comment too -------------------------------------


def test_mm_ok_is_written_ahead_of_a_double_slash_comment():
    # Measured: `X1 n1 0 rr // mirror ref mm_ok=0` draws -5.0e-04 -- ngspice
    # discards everything after `//`, so appending past it leaves the instance
    # randomised while the report says otherwise.
    text, rows = _gate("XM1 d g s b sg13_lv_nmos w=1u // mirror ref\n.end\n")

    line = _instance_line(text)
    assert line == "XM1 d g s b sg13_lv_nmos w=1u mm_ok=0 // mirror ref"
    assert line.index("mm_ok=0") < line.index("//")
    assert rows == [{"instance": "XM1", "model": "sg13_lv_nmos", "randomised": False}]


def test_a_double_slash_comment_needs_no_preceding_whitespace():
    # Unlike `$`, `//` opens a comment mid-token: measured, `X1 n1 0 rr//note
    # mm_ok=0` also draws -5.0e-04.
    text, _rows = _gate("XM1 d g s b sg13_lv_nmos w=1u//nospace\n.end\n")

    assert _instance_line(text) == ("XM1 d g s b sg13_lv_nmos w=1u mm_ok=0 //nospace")


def test_an_mm_ok_inside_a_double_slash_comment_is_not_an_assignment():
    # A commented-out assignment must not be "replaced" in place, which would
    # leave the element itself ungated.
    text, _rows = _gate("XM1 d g s b sg13_lv_nmos w=1u // was mm_ok=1\n.end\n")

    assert _instance_line(text) == (
        "XM1 d g s b sg13_lv_nmos w=1u mm_ok=0 // was mm_ok=1"
    )


def test_split_eol_comment_handles_double_slash_with_quotes_and_braces():
    assert decl.split_eol_comment("X1 a b w=1u // c") == ("X1 a b w=1u ", "// c")
    assert decl.split_eol_comment("X1 a b w=1u//c") == ("X1 a b w=1u", "//c")
    assert decl.split_eol_comment("X1 a b v='a//b' // c") == (
        "X1 a b v='a//b' ",
        "// c",
    )
    assert decl.split_eol_comment("X1 a b m={1//2} // c") == (
        "X1 a b m={1//2} ",
        "// c",
    )


def test_declaration_scan_ignores_a_parameter_named_only_after_a_slash():
    # The mirror-image defect: reading comment prose as a declaration would let
    # the gate write mm_ok=0 onto a subcircuit that never declares it -- the
    # silent case `_require_declared` exists to refuse.
    assert decl.scan_declared_params(
        ".subckt u a b foo=1 // mm_ok=1 default\nR1 a b 1k\n.ends\n", {}
    ) == {"u": {"foo"}}


def test_a_lib_section_definition_with_a_slash_comment_is_not_a_reference():
    # `.lib <section> // note` reaches three tokens, so a comment-blind reader
    # mistakes a section *definition* for a file-bearing `.lib <file>
    # <section>` reference and never scans the section's content.
    assert (
        decl.scan_declared_params(
            ".lib typ // note\n.subckt w a b mm_ok=1\n.ends\n.endl\n", {}
        )
        == {}
    )


# --- lines ngspice drops before folding `+` continuations -------------------
#
# Measured, `X1 n1 0 rr` / <line> / `+ mm_ok=0`: a `*` line, a blank line, a
# `$`-only line and a `//`-only line all still fold the continuation onto the
# element (-1.0e-03); a `;`-only line breaks it and the `+` is discarded with
# no warning (-5.0e-04).

_SPLIT_CASES = ["* note", "", "   ", "$ note", "// note", "   * note"]


def test_a_dropped_line_does_not_break_an_instances_continuation():
    for interrupter in _SPLIT_CASES:
        text, rows = _gate(
            f"XM1 d g s b sg13_lv_nmos\n{interrupter}\n+ w=1u mm_ok=1\n.end\n"
        )

        assert rows == [
            {"instance": "XM1", "model": "sg13_lv_nmos", "randomised": False}
        ], interrupter
        # The folded element carries the continuation's parameters, and the
        # existing mm_ok=1 is replaced rather than left to win.
        assert _instance_line(text) == ("XM1 d g s b sg13_lv_nmos w=1u mm_ok=0"), (
            interrupter
        )
        assert "mm_ok=1" not in text, interrupter
        # The author's line is kept, not deleted by the rewrite.
        assert interrupter in text.split("\n"), interrupter


def test_a_semicolon_only_line_ends_the_instance_as_ngspice_ends_it():
    # ngspice attaches the `+` to the `;` comment and silently discards it, so
    # the element is the first line alone -- klt must gate that, and leave the
    # orphaned continuation exactly as written rather than claim its params.
    text, rows = _gate("XM1 d g s b sg13_lv_nmos\n; note\n+ w=1u mm_ok=1\n.end\n")

    assert rows == [{"instance": "XM1", "model": "sg13_lv_nmos", "randomised": False}]
    assert text.split("\n")[:3] == [
        "XM1 d g s b sg13_lv_nmos mm_ok=0",
        "; note",
        "+ w=1u mm_ok=1",
    ]


def test_declaration_scan_reads_a_continuation_after_a_dropped_line():
    # Same folding gap from the other end: dropping the declaration only ever
    # refuses, so it is the safe direction, but it has the same cause.
    assert decl.scan_declared_params(
        ".subckt v a b foo=1\n* x\n+ mm_ok=1\nR1 a b 1k\n.ends\n", {}
    ) == {"v": {"foo", "mm_ok"}}
    # ...and stops at a `;` line, which ngspice does not fold across.
    assert decl.scan_declared_params(
        ".subckt v a b foo=1\n; x\n+ mm_ok=1\nR1 a b 1k\n.ends\n", {}
    ) == {"v": {"foo"}}


def test_wrapper_mode_keeps_a_dropped_line_inside_a_continuation():
    # The wrapper path shares the folding reader; a `*` line inside a
    # continuation used to split the element there, so the wrapper moved only
    # part of the instance inside and dropped the rest of its parameters.
    text, rows = gate.gate_netlist(
        "XM1 d g s b\n* note\n+ nfet_01v8 w=1u l=0.15u\n.end\n",
        [],
        ["xm1"],
        switch="mc_mm_switch",
    )

    assert rows == [{"instance": "XM1", "model": "nfet_01v8", "randomised": False}]
    assert "XM1 d g s b klt_mmoff_XM1" in text
    assert "* note" in text.split("\n")
    # Every parameter travelled inside the wrapper, none left behind.
    assert "w=1u l=0.15u" in text
    assert "+ nfet_01v8" not in text
