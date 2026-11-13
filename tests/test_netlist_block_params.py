"""`klt netlist --block` restores a cell's subcircuit parameters (issue #5439).

A companion to test_netlist_block.py (which covers the wrapper, continuations
and trailing `.end`), kept separate so neither file crosses the per-file size
ratchet. The oracle these cases are written against is real xschem: the
`--block` top-level `.subckt` card must be the card xschem itself writes for
that cell when a parent sheet instantiates it.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from klayout_tools.netlist import run_netlist
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_stub,
    _written,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch) -> Path:
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


# --------------------------------------------------------------------------- #
# Subcircuit parameters on the restored header (issue #5439)
# --------------------------------------------------------------------------- #

#: A top sheet whose devices reference subcircuit parameters. The stub writes
#: this verbatim, so the only thing under test is the header rewrite.
_PARAM_BODY = "* stub netlist\n**.subckt block a b\nR1 a b wstv m=1\n**.ends\n.end"


def _write_cell(tmp_path: Path, k_block: str, name: str = "block.sch") -> str:
    """An xschem sheet whose symbol attributes are ``k_block``."""
    path = tmp_path / name
    path.write_text(
        f"v {{xschem version=3.4.7 file_version=1.2}}\nG {{}}\nK {{{k_block}}}\n"
        "V {}\nS {}\nE {}\n",
        encoding="utf-8",
    )
    return str(path)


def _subckt_line(written: str) -> str:
    """The restored ``.subckt`` header line of a block export."""
    return next(line for line in written.splitlines() if line.startswith(".subckt"))


def _block_header(tmp_path, monkeypatch, k_block, *, body=_PARAM_BODY, sym=None):
    """``(report, written)`` of a ``--block`` run over a cell whose own
    ``K {}`` block is ``k_block`` and whose sibling ``.sym`` (when ``sym`` is
    given) carries ``sym``."""
    monkeypatch.setenv("NETLIST_BODY", body)
    schematic = _write_cell(tmp_path, k_block)
    if sym is not None:
        (tmp_path / "block.sym").write_text(
            f"v {{xschem version=3.4.7 file_version=1.2}}\nG {{}}\nK {{{sym}}}\n"
            "V {}\nS {}\nE {}\n",
            encoding="utf-8",
        )
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)
    return report, output.read_text(encoding="utf-8")


def test_block_mode_restores_template_parameters_from_the_schematic(
    tmp_path, argv_log, monkeypatch
):
    """The emitted block declares the parameters its device cards reference,
    so an including deck no longer fails on `unknown parameter`."""
    report, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 wstv=0.42 lstv=2 cld=0.5f"'
    )

    assert _subckt_line(written) == ".subckt block a b wstv=0.42 lstv=2 cld=0.5f"
    assert report["block"]["subckt_uncommented"] is True
    assert report["block"]["warning"] is None


def test_block_mode_prefers_the_sibling_symbols_template(
    tmp_path, argv_log, monkeypatch
):
    """Source precedence matches what a parent instantiating the cell gets:
    the sibling `.sym` wins over the schematic's own `K {}` block."""
    _, written = _block_header(
        tmp_path,
        monkeypatch,
        'template="name=x1 wstv=0.42 cld=0.5f"',
        sym='template="name=x1 wstv=0.11 cld=3f"',
    )

    assert _subckt_line(written) == ".subckt block a b wstv=0.11 cld=3f"


def test_block_mode_sibling_symbol_without_parameters_wins_too(
    tmp_path, argv_log, monkeypatch
):
    """Precedence is the *source*, not "whichever has parameters": a
    name-only sibling template means no parameters, exactly as xschem emits
    for that cell as a child."""
    _, written = _block_header(
        tmp_path,
        monkeypatch,
        'template="name=x1 wstv=0.42 cld=0.5f"',
        sym='template="name=x1"',
    )

    assert _subckt_line(written) == ".subckt block a b"


@pytest.mark.parametrize(
    "k_block",
    ["", "type=subcircuit", 'template="name=x1"', 'template=""'],
    ids=["empty", "no-template", "name-only", "empty-template"],
)
def test_block_mode_without_parameters_is_byte_identical_to_today(
    tmp_path, argv_log, monkeypatch, k_block
):
    """A cell with nothing to declare keeps today's output, byte for byte."""
    report, written = _block_header(tmp_path, monkeypatch, k_block)

    assert written == _written(
        "* stub netlist\n.subckt block a b\nR1 a b wstv m=1\n.ends\n"
    )
    assert report["block"]["warning"] is None


def test_block_mode_puts_parameters_after_the_last_continuation_line(
    tmp_path, argv_log, monkeypatch
):
    """A wrapped port list (issue #3899) keeps every port: the parameters go
    after the last continuation's ports, never into the middle of the list."""
    body = (
        "* stub netlist\n**.subckt block a b\n*+ c d\n**+ e f\n"
        "R1 a b wstv m=1\n**.ends\n.end"
    )
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 wstv=0.42"', body=body
    )

    assert "\n.subckt block a b\n+ c d\n+ e f wstv=0.42\n" in written


def test_block_mode_does_not_double_parameters_already_on_the_header(
    tmp_path, argv_log, monkeypatch
):
    """Forward guard: a future xschem that restores the parameters itself
    must not get a second copy appended."""
    body = (
        "* stub netlist\n**.subckt block a b wstv=0.42\nR1 a b wstv m=1\n**.ends\n.end"
    )
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 wstv=0.42"', body=body
    )

    assert _subckt_line(written) == ".subckt block a b wstv=0.42"


def test_block_mode_does_not_double_parameters_on_a_continuation_line(
    tmp_path, argv_log, monkeypatch
):
    body = (
        "* stub netlist\n**.subckt block a b\n*+ c wstv=0.42\nR1 a b wstv m=1\n"
        "**.ends\n.end"
    )
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 wstv=0.42"', body=body
    )

    assert "\n.subckt block a b\n+ c wstv=0.42\n" in written


def test_block_mode_drops_the_keys_xschem_drops(tmp_path, argv_log, monkeypatch):
    """`name` and `spiceprefix` never reach a `.subckt` card, and neither
    does a token named by the symbol's own `extra` attribute."""
    _, written = _block_header(
        tmp_path,
        monkeypatch,
        'extra="VSS" template="name=x1 spiceprefix=X w=1u VSS=0 nf=2"',
    )

    assert _subckt_line(written) == ".subckt block a b w=1u nf=2"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("name=x1 m=1 w=2", ".subckt block a b  w=2"),
        ("name=x1 w=2 m=1", ".subckt block a b w=2"),
        ("name=x1 m=0.5 w=2", ".subckt block a b .5 w=2"),
        ("name=x1 m=7u w=2", ".subckt block a b u w=2"),
        ("name=x1 m=M1 w=2", ".subckt block a b m=M1 w=2"),
        ("name=x1 m=1 w=2 m=3", ".subckt block a b  w=2 m=3"),
    ],
    ids=["int", "trailing", "fraction", "suffixed", "non-numeric", "only-first"],
)
def test_block_mode_strips_a_numeric_m_exactly_as_xschem_does(
    tmp_path, argv_log, monkeypatch, template, expected
):
    """xschem's `spice.awk` removes the first ` m=<digits>` from a `.subckt`
    card ("m= is a multiplier not a param"). Replicated verbatim, prefix
    match and all, so a block header is token-identical to the card xschem
    writes for the same cell as a child."""
    _, written = _block_header(tmp_path, monkeypatch, f'template="{template}"')

    assert _subckt_line(written) == expected


def test_block_mode_reads_a_multi_line_and_escaped_template(
    tmp_path, argv_log, monkeypatch
):
    """xschem's attribute strings may span lines and backslash-escape an
    embedded quote or brace."""
    _, written = _block_header(
        tmp_path,
        monkeypatch,
        'type=subcircuit\nformat="@name @pinlist @symname"\n'
        'template="name=x1 wstv=0.42\n  expr=\\{a+b\\} tag=\\"hi\\""',
    )

    assert _subckt_line(written) == ".subckt block a b wstv=0.42 expr={a+b} tag=hi"


def test_block_mode_keeps_a_quoted_template_value_with_spaces(
    tmp_path, argv_log, monkeypatch
):
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 note=\\"a b\\" w=2"'
    )

    assert _subckt_line(written) == ".subckt block a b note=a b w=2"


def test_block_mode_warns_and_keeps_output_when_the_symbol_is_unparsable(
    tmp_path, argv_log, monkeypatch
):
    """An unterminated `K {` must not fail the run: today's output is kept
    and the gap is reported in `block.warning`."""
    monkeypatch.setenv("NETLIST_BODY", _PARAM_BODY)
    schematic = _write_cell(tmp_path, 'template="name=x1 w=2"')
    (tmp_path / "block.sym").write_text(
        'v {xschem version=3.4.7}\nK {template="name=x1 w=2"\n', encoding="utf-8"
    )
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    assert output.read_text(encoding="utf-8") == _written(
        "* stub netlist\n.subckt block a b\nR1 a b wstv m=1\n.ends\n"
    )
    warning = report["block"]["warning"]
    assert "unterminated 'K {...}'" in warning
    assert "block.sym" in warning


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_block_mode_warns_and_keeps_output_when_the_symbol_is_unreadable(
    tmp_path, argv_log, monkeypatch
):
    monkeypatch.setenv("NETLIST_BODY", _PARAM_BODY)
    schematic = _write_cell(tmp_path, 'template="name=x1 w=2"')
    sym = tmp_path / "block.sym"
    sym.write_text('K {template="name=x1 w=2"}\n', encoding="utf-8")
    sym.chmod(0o000)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"

    report = run_netlist(schematic, str(output), xschem_binary=binary, block=True)

    assert output.read_text(encoding="utf-8") == _written(
        "* stub netlist\n.subckt block a b\nR1 a b wstv m=1\n.ends\n"
    )
    assert "could not read the symbol attributes" in report["block"]["warning"]


def test_block_mode_warning_keeps_the_not_testbench_shaped_gap_too(
    tmp_path, argv_log, monkeypatch
):
    """A run that is both unshaped and unreadable reports both gaps."""
    monkeypatch.setenv("NETLIST_BODY", "* stub\n**.subckt block a b\n.something x\n")
    schematic = _write_cell(tmp_path, "")
    (tmp_path / "block.sym").write_text("K {template=\n", encoding="utf-8")
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)

    report = run_netlist(
        schematic, str(tmp_path / "o.spice"), xschem_binary=binary, block=True
    )

    warning = report["block"]["warning"]
    assert "no commented '**.ends' found to uncomment" in warning
    assert "unterminated 'K {...}'" in warning


def test_restored_parameter_values_keep_their_commas(tmp_path, argv_log, monkeypatch):
    """The bus-port rewrite (issue #4490) must not reach into the parameter
    list: spacing the comma in `f=max(1,2)` would corrupt the expression."""
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 f=max(1,2) w=2"'
    )

    assert _subckt_line(written) == ".subckt block a b f=max(1,2) w=2"


def test_bus_ports_are_still_spaced_when_parameters_follow(
    tmp_path, argv_log, monkeypatch
):
    """The port part of the same header still gets the #4490 rewrite."""
    body = "* stub netlist\n**.subckt block p[1],p[0] q\nR1 q 0 wstv m=1\n**.ends\n.end"
    _, written = _block_header(
        tmp_path, monkeypatch, 'template="name=x1 f=max(1,2)"', body=body
    )

    assert _subckt_line(written) == ".subckt block p[1] p[0] q f=max(1,2)"


def _check_block(tmp_path, monkeypatch, committed: str) -> dict:
    """``--check --block`` of a parameterised cell against ``committed``."""
    monkeypatch.setenv("NETLIST_BODY", _PARAM_BODY)
    schematic = _write_cell(tmp_path, 'template="name=x1 wstv=0.42"')
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    output = tmp_path / "block.spice"
    output.write_text(_written(committed), encoding="utf-8")
    return run_netlist(
        schematic, str(output), xschem_binary=binary, block=True, check=True
    )


def test_check_block_reports_drift_for_a_committed_header_without_parameters(
    tmp_path, argv_log, monkeypatch
):
    """Same rule as the #4490 port rewrite: `--check` compares against the
    parameter-carrying form, so a stale committed block artifact drifts."""
    report = _check_block(
        tmp_path,
        monkeypatch,
        "* stub netlist\n.subckt block a b\nR1 a b wstv m=1\n.ends\n",
    )

    assert report["status"] == "drifted"
    assert "+.subckt block a b wstv=0.42" in report["drift"]["diff"]


def test_check_block_is_clean_for_a_committed_header_with_parameters(
    tmp_path, argv_log, monkeypatch
):
    report = _check_block(
        tmp_path,
        monkeypatch,
        "* stub netlist\n.subckt block a b wstv=0.42\nR1 a b wstv m=1\n.ends\n",
    )

    assert report["status"] == "match"


# --------------------------------------------------------------------------- #
# real xschem: the block card is the card xschem writes for the same cell
# --------------------------------------------------------------------------- #

_PIN_SYMS = {
    "pin_in.sym": 'K {type=ipin\nformat="*.ipin @lab"\ntemplate="name=p1 lab=xxx"}',
    "pin_out.sym": 'K {type=opin\nformat="*.opin @lab"\ntemplate="name=p1 lab=xxx"}',
}


def _write_real_fixtures(tmp_path, *, cell: str, template: str, sibling: bool):
    """A two-pin cell plus a parent that instantiates it, for a real xschem
    run. Returns ``(rcfile, cell_sch, parent_sch)``.

    With ``sibling=True`` the parameters live in a sibling ``<cell>.sym`` and
    the parent instantiates that symbol; otherwise they live in the cell's
    own ``K {}`` block and the parent instantiates the ``.sch`` directly.
    """
    for name, k_block in _PIN_SYMS.items():
        (tmp_path / name).write_text(
            f"v {{xschem version=3.4.7 file_version=1.2}}\nG {{}}\n{k_block}\n"
            "V {}\nS {}\nE {}\nB 5 -1.25 -1.25 1.25 1.25 {name=p dir=inout}\n",
            encoding="utf-8",
        )
    sym_k = 'type=subcircuit\nformat="@name @pinlist @symname"\n'
    (tmp_path / f"{cell}.sch").write_text(
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\n"
        f'K {{{sym_k}template="{"name=x1" if sibling else template}"}}\n'
        "V {}\nS {}\nE {}\nN 0 0 0 40 {lab=A}\nN 0 80 0 120 {lab=B}\n"
        "C {pin_in.sym} 0 0 0 0 {name=p1 lab=A}\n"
        "C {pin_out.sym} 0 120 0 0 {name=p2 lab=B}\n",
        encoding="utf-8",
    )
    if sibling:
        (tmp_path / f"{cell}.sym").write_text(
            "v {xschem version=3.4.7 file_version=1.2}\nG {}\n"
            f'K {{{sym_k}template="{template}"}}\n'
            "V {}\nS {}\nE {}\nB 5 -22.5 -2.5 -17.5 2.5 {name=A dir=in}\n"
            "B 5 17.5 -2.5 22.5 2.5 {name=B dir=out}\n",
            encoding="utf-8",
        )
    instance = f"{cell}.sym" if sibling else f"{cell}.sch"
    parent = tmp_path / f"parent_{cell}.sch"
    parent.write_text(
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
        f"C {{{instance}}} 200 0 0 0 {{name=x1}}\n",
        encoding="utf-8",
    )
    rcfile = tmp_path / "xschemrc"
    rcfile.write_text(f"set XSCHEM_LIBRARY_PATH {tmp_path}\n", encoding="utf-8")
    return str(rcfile), str(tmp_path / f"{cell}.sch"), str(parent)


def _real_card(netlist: str, cell: str) -> list[str]:
    """The ``.subckt`` card for ``cell``, as tokens (name, ports, params)."""
    pattern = re.compile(rf"[ \t]*\.subckt[ \t]+{re.escape(cell)}\b", re.IGNORECASE)
    text = Path(netlist).read_text(encoding="utf-8")
    return next(line.split() for line in text.splitlines() if pattern.match(line))


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
@pytest.mark.parametrize("sibling", [False, True], ids=["sch-K-block", "sibling-sym"])
def test_block_card_matches_what_xschem_writes_for_the_same_cell(tmp_path, sibling):
    """The oracle, against real xschem: the `--block` top-level card must be
    the card xschem itself writes for that cell when a parent instantiates
    it. Covers both parameter sources (issue #5439)."""
    template = "name=x1 wstv=0.42 lstv=2 cld=0.5f"
    rcfile, cell_sch, parent_sch = _write_real_fixtures(
        tmp_path, cell="cell", template=template, sibling=sibling
    )

    run_netlist(
        cell_sch,
        str(tmp_path / "block.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )
    run_netlist(
        parent_sch,
        str(tmp_path / "parent.spice"),
        rcfile=rcfile,
        provenance_header=False,
    )

    block_card = _real_card(str(tmp_path / "block.spice"), "cell")
    child_card = _real_card(str(tmp_path / "parent.spice"), "cell")
    assert block_card == child_card
    # ...and it is not the parameter-less card the defect produced.
    assert block_card == [".subckt", "cell", "A", "B"] + template.split()[1:]
