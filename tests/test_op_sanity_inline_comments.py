"""`op_sanity`'s netlist pre-processing under the shared ngspice
inline-comment rule (issue #5417).

``op_sanity`` hand-rolled the strip as ``find(";")``/``find("$ ")``, with no
leading-whitespace requirement and no end-of-line case. That left a bare
trailing ``$`` in the card as a token, cut at a ``$`` glued to the previous
token (mangling KLayout's ``X$1``-style card names), and truncated
``.include $PDK_ROOT/...`` to a bare ``.include``. All three now follow
``_spice_text.strip_spice_inline_comment``.
"""

from __future__ import annotations

from klayout_tools import op_sanity


def test_logical_lines_follows_the_shared_ngspice_comment_rule():
    """Issue #5417: `op_sanity` hand-rolled the comment strip as
    ``find(";")``/``find("$ ")``, which (a) left a bare trailing ``$`` in
    the card as a token and (b) cut at a ``$`` glued to the previous token,
    mangling KLayout's ``X$1``-style card names. Both now follow the shared
    rule, and the ``$VAR`` include it also used to truncate survives whole.
    """
    text = (
        "* a full-line comment\n"
        "\n"
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice\n"
        "R1 a b 1k $ load\n"
        "R2 a b 1k $\n"
        "R3 a b 1k ; load\n"
        "X$1 a b cap_cmomi\n"
        "$ a whole-line dollar comment\n"
        "M1 d g s b nfet\n"
        "+ w=1u l=0.15u $ geometry\n"
    )
    assert op_sanity._logical_lines(text) == [
        ".include $PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice",
        "R1 a b 1k",
        "R2 a b 1k",
        "R3 a b 1k",
        "X$1 a b cap_cmomi",
        "M1 d g s b nfet w=1u l=0.15u",
    ]


def test_expand_includes_drops_a_dollar_variable_include_it_can_now_see():
    """`expand_includes` has always refused a ``$VAR`` reference, but
    `op_sanity._logical_lines` used to hand it the truncated card. Now that
    the card arrives whole, the refusal is the path actually taken -- the
    card is dropped, not passed through as a literal ``.include`` line."""
    lines = op_sanity._logical_lines(".include $PDK_ROOT/models.spice\nR1 a b 1k\n")
    expanded = op_sanity.expand_includes(
        lines, "/nonexistent", op_sanity.IncludeState("/nonexistent")
    )
    assert expanded == ["R1 a b 1k"]
