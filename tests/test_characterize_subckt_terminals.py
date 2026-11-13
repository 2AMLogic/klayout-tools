"""`characterize._read_subckt_terminals` under the shared ngspice
inline-comment rule (issue #5417).

The header strip used to be ``line.split("$")[0]`` -- any ``$`` ended the
line. A `klt extract` netlist whose top-level ``.subckt`` carries KLayout's
anonymous-net placeholders (``$1``, ``$5``, ...) as pins therefore truncated
to ``.subckt cell`` and raised "declares no terminals". It now shares
``_spice_text.strip_spice_inline_comment`` with every other netlist reader,
which treats only a token-delimited ``$`` as a comment.

The terminal *order* these tests pin is load-bearing: a SPICE ``X`` instance
line is positional, so the generated testbench binds nodes by the position
each terminal appears in here.
"""

from __future__ import annotations

from klayout_tools import characterize


def test_read_subckt_terminals_keeps_klayout_anonymous_net_pins(tmp_path):
    """Issue #5417: `_read_subckt_terminals` stripped the header at *any*
    `$` (`line.split("$")[0]`), so a `klt extract` netlist whose top-level
    `.subckt` carries KLayout's anonymous-net placeholders (`$1`, `$5`, ...)
    as pins truncated to `.subckt cell` and raised "declares no terminals".
    The shared ngspice rule only treats a token-delimited `$` as a comment,
    so those pins survive in declaration order."""
    netlist = tmp_path / "extracted.spice"
    netlist.write_text(
        ".subckt cell $1 $2 VPWR VGND\nR1 $1 $2 1k\n.ends cell\n", encoding="utf-8"
    )
    assert characterize._read_subckt_terminals(str(netlist), "cell") == (
        "$1",
        "$2",
        "VPWR",
        "VGND",
    )


def test_read_subckt_terminals_still_drops_a_real_inline_comment(tmp_path):
    """The flip side of the test above: a genuine ngspice inline comment on
    the `.subckt` header -- `$` or `;` delimited by whitespace -- is still
    removed, including across a `+` continuation."""
    netlist = tmp_path / "schematic.spice"
    netlist.write_text(
        ".subckt cell A Y VPWR\n+ VGND $ power pins last\nR1 A Y 1k\n.ends cell\n",
        encoding="utf-8",
    )
    assert characterize._read_subckt_terminals(str(netlist), "cell") == (
        "A",
        "Y",
        "VPWR",
        "VGND",
    )
