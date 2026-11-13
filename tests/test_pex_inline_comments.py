"""`klt pex`'s netlist pre-processing under the shared ngspice inline-comment
rule (issue #5417).

``pex._logical_lines`` used to hand-roll the comment strip as ``\\s\\$|;``,
which truncated ``.include $PDK_ROOT/...`` to a bare ``.include`` -- a ``$``
glued to the next token is a shell-style variable reference, not a comment.
It now shares ``_paths.strip_spice_inline_comment`` with every other netlist
reader in the package, which makes the two consumers built on it --
``_has_include_card`` and ``_expand_reference_includes`` -- see such a card
at all. These tests pin that, and pin that a *genuine* ``$``/``;`` comment is
still stripped.
"""

from __future__ import annotations

from klayout_tools.pex import _has_include_card, _logical_lines, _model_mismatch


def test_logical_lines_keeps_a_dollar_variable_include_target():
    """Issue #5417: `pex._logical_lines` used `\\s\\$|;`, which truncated
    `.include $PDK_ROOT/...` to a bare `.include` -- a `$` glued to the next
    token is a variable reference, not an ngspice inline comment. It now
    shares `strip_spice_inline_comment`, so the card survives whole (and a
    *real* `$`/`;` comment is still stripped)."""
    text = (
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice\n"
        ".lib ${PDK_ROOT}/sky130A/libs.tech/combined/sky130.lib.spice tt\n"
        "R1 a b 1k $ load\n"
        "R2 a b 1k $\n"
        "R3 a b 1k ; load\n"
    )
    assert _logical_lines(text) == [
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice",
        ".lib ${PDK_ROOT}/sky130A/libs.tech/combined/sky130.lib.spice tt",
        "R1 a b 1k",
        "R2 a b 1k",
        "R3 a b 1k",
    ]


def test_has_include_card_sees_a_dollar_variable_include():
    """The consequence of the truncation above: `_has_include_card` returned
    `False` for a netlist whose only `.include` used a `$VAR` path, so
    `_model_mismatch` could not reach its "unresolved include" clause --
    the one case that clause exists for (it names `` `$VAR` path ``
    explicitly)."""
    text = ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice\n"
    assert _has_include_card(text) is True
    assert _has_include_card("R1 a b 1k $ not an include\n") is False


def test_model_mismatch_names_unread_dollar_variable_include(tmp_path):
    """Issue #5417, end to end: a reference netlist that is nothing but a
    `$VAR` `.include` yields no device cards, so the `model_mismatch`
    `detail` must say the divergence may only reflect the unread include
    rather than asserting a genuine model-set mismatch."""
    reference = tmp_path / "schematic_dut.spice"
    reference.write_text(
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice\n"
    )
    extracted = tmp_path / "extracted.spice"
    extracted.write_text(".SUBCKT TOP A Y VSS\nXn1 Y A VSS VSS nfet_01v8\n.ENDS TOP\n")
    mismatch = _model_mismatch(
        reference_netlist=str(reference), extracted_netlist=str(extracted)
    )
    assert mismatch is not None
    assert "`$VAR` path" in mismatch["detail"]
    assert "electrically unrelated" not in mismatch["detail"]
