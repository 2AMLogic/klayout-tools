"""`klt netlist --block` diagnoses a `.subckt` port order that is not the
schematic's pin order (issue #5473).

A third companion to test_netlist_block.py (the wrapper, continuations and
trailing `.end`) and test_netlist_block_params.py (subcircuit parameters),
kept separate so none of the three crosses the per-file size ratchet.

The oracle is real xschem, as it is for the parameter cases: the measured
behaviour these stubs stand in for is that a sibling `<stem>.sym` -- not the
schematic -- decides the top sheet's port order, and that a schematic pin the
symbol lacks is dropped from the card. The two live cases at the bottom pin
that oracle down; everything above stubs xschem so the comparison itself is
what is under test.
"""

from __future__ import annotations

import shutil

import pytest

from klayout_tools.netlist import run_netlist
from klayout_tools.netlist_port_order import header_ports, schematic_pin_labels
from test_netlist import (  # noqa: F401
    _NETLIST_BODY,
    _STUB_NONZERO_EXIT,
    _write_stub,
)


@pytest.fixture
def argv_log(tmp_path, monkeypatch):
    """Same contract as test_netlist.py's fixture: stubs log their argv here."""
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    monkeypatch.setenv("NETLIST_BODY", _NETLIST_BODY)
    return log


# --------------------------------------------------------------------------- #
# Fixtures: a sheet whose pins are declared in a known file order
# --------------------------------------------------------------------------- #

_PINS = (
    ("iopin.sym", "vdda"),
    ("iopin.sym", "vssa"),
    ("ipin.sym", "inp"),
    ("opin.sym", "outp"),
    ("ipin.sym", "bias"),
)


def _sheet(pins=_PINS, *, prefix: str = "devices/") -> str:
    """An xschem sheet declaring ``pins`` as ``C {...}`` instances, in order."""
    body = "".join(
        f"C {{{prefix}{symbol}}} 0 {i * 40} 0 0 {{name=p{i + 1} lab={lab}}}\n"
        for i, (symbol, lab) in enumerate(pins)
    )
    return (
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\n"
        'K {type=subcircuit\nformat="@name @pinlist @symname"\n'
        'template="name=x1"}\nV {}\nS {}\nE {}\n' + body
    )


def _symbol(names: tuple[str, ...]) -> str:
    """A sibling ``.sym`` declaring ``names`` as pins, in order."""
    body = "".join(
        f"B 5 -22.5 {i * 10 - 2.5} -17.5 {i * 10 + 2.5} {{name={name} dir=inout}}\n"
        for i, name in enumerate(names)
    )
    return (
        "v {xschem version=3.4.7 file_version=1.2}\nG {}\n"
        'K {type=subcircuit\nformat="@name @pinlist @symname"\n'
        'template="name=x1"}\nV {}\nS {}\nE {}\n' + body
    )


def _run(
    tmp_path,
    monkeypatch,
    *,
    ports: str,
    sheet: str | None = None,
    symbol: tuple[str, ...] | None = None,
    body: str | None = None,
):
    """A stubbed ``--block`` run whose export carries ``ports`` on the
    commented header; returns the payload's ``block`` object."""
    schematic = tmp_path / "cell.sch"
    schematic.write_text(sheet if sheet is not None else _sheet(), encoding="utf-8")
    if symbol is not None:
        (tmp_path / "cell.sym").write_text(_symbol(symbol), encoding="utf-8")
    export = body if body is not None else f"**.subckt cell {ports}\n**.ends\n.end"
    monkeypatch.setenv("NETLIST_BODY", export)
    binary = _write_stub(tmp_path, _STUB_NONZERO_EXIT)
    report = run_netlist(
        str(schematic),
        str(tmp_path / "cell.spice"),
        xschem_binary=binary,
        block=True,
    )
    return report["block"]


_DECLARED = ["vdda", "vssa", "inp", "outp", "bias"]


# --------------------------------------------------------------------------- #
# The diagnosis at generation time
# --------------------------------------------------------------------------- #


def test_reordered_sibling_symbol_is_reported_with_both_lists(
    tmp_path, argv_log, monkeypatch
):
    """The reported case: the sibling symbol's pin order wins, silently. The
    warning has to give both lists and name the symbol, because neither is
    visible in the netlist the caller is looking at."""
    block = _run(
        tmp_path,
        monkeypatch,
        ports="vdda vssa inp bias outp",
        symbol=("vdda", "vssa", "inp", "bias", "outp"),
    )

    assert block["port_order"] == {
        "schematic": _DECLARED,
        "emitted": ["vdda", "vssa", "inp", "bias", "outp"],
        "symbol": str(tmp_path / "cell.sym"),
    }
    assert "vdda vssa inp outp bias" in block["warning"]
    assert "vdda vssa inp bias outp" in block["warning"]
    assert "cell.sym" in block["warning"]
    assert "positional contract" in block["warning"]


def test_reordered_ports_without_a_sibling_symbol_are_reported_too(
    tmp_path, argv_log, monkeypatch
):
    """No sibling symbol: the ports follow the schematic's file order, so a
    header that disagrees still rewrites the contract. The warning must not
    blame a symbol that does not exist."""
    block = _run(tmp_path, monkeypatch, ports="bias outp inp vssa vdda")

    assert block["port_order"] == {
        "schematic": _DECLARED,
        "emitted": ["bias", "outp", "inp", "vssa", "vdda"],
        "symbol": None,
    }
    assert "no sibling symbol" in block["warning"].lower()
    assert "sim_pinnumber" in block["warning"]


def test_a_pin_the_symbol_lacks_is_named_as_omitted(tmp_path, argv_log, monkeypatch):
    """The second silent failure: a schematic pin missing from the sibling
    symbol is dropped from the `.subckt` card with no warning at all."""
    block = _run(
        tmp_path,
        monkeypatch,
        ports="vdda vssa inp outp",
        symbol=("vdda", "vssa", "inp", "outp"),
    )

    assert block["port_order"]["emitted"] == ["vdda", "vssa", "inp", "outp"]
    assert "omits the pin 'bias'" in block["warning"]


def test_two_omitted_pins_are_both_named(tmp_path, argv_log, monkeypatch):
    block = _run(
        tmp_path, monkeypatch, ports="vdda vssa inp", symbol=("vdda", "vssa", "inp")
    )

    assert "omits the pins 'outp', 'bias'" in block["warning"]


def test_matching_order_warns_about_nothing(tmp_path, argv_log, monkeypatch):
    """The overwhelmingly common case stays silent, and `port_order` is null
    rather than a redundant copy of an order nobody needs to look at."""
    block = _run(tmp_path, monkeypatch, ports="vdda vssa inp outp bias")

    assert block["port_order"] is None
    assert block["warning"] is None


def test_matching_order_with_a_sibling_symbol_warns_about_nothing(
    tmp_path, argv_log, monkeypatch
):
    block = _run(
        tmp_path,
        monkeypatch,
        ports="vdda vssa inp outp bias",
        symbol=tuple(_DECLARED),
    )

    assert block["port_order"] is None
    assert block["warning"] is None


def test_a_schematic_with_no_pins_is_not_a_drifted_order(
    tmp_path, argv_log, monkeypatch
):
    block = _run(tmp_path, monkeypatch, ports="", sheet=_sheet(pins=()))

    assert block["port_order"] is None
    assert block["warning"] is None


def test_the_warning_rides_beside_a_parameter_warning(tmp_path, argv_log, monkeypatch):
    """`block.warning` is one string: a port-order message must join an
    existing one (issue #5439's) rather than replace it."""
    sheet = _sheet().replace(
        'template="name=x1"', 'template="name=x1 note=\\"a b\\" w=2"'
    )
    block = _run(tmp_path, monkeypatch, ports="bias outp inp vssa vdda", sheet=sheet)

    assert "subcircuit parameter 'note'" in block["warning"]
    assert "positional contract" in block["warning"]


# --------------------------------------------------------------------------- #
# Not comparable: report nothing rather than a false alarm
# --------------------------------------------------------------------------- #


def test_a_bus_pin_label_is_not_comparable(tmp_path, argv_log, monkeypatch):
    """xschem expands `a[1:0]` onto the header, so the pin's `lab` is not a
    token-for-token image of the ports. Not traced; not guessed at."""
    pins = (("ipin.sym", "a[1:0]"), ("opin.sym", "y"))
    block = _run(tmp_path, monkeypatch, ports="a[1] a[0] y", sheet=_sheet(pins=pins))

    assert block["port_order"] is None
    assert block["warning"] is None


def test_a_comma_list_pin_label_is_not_comparable(tmp_path, argv_log, monkeypatch):
    pins = (("ipin.sym", "a,b"), ("opin.sym", "y"))
    block = _run(tmp_path, monkeypatch, ports="a b y", sheet=_sheet(pins=pins))

    assert block["port_order"] is None
    assert block["warning"] is None


def test_unrecognised_pin_symbols_are_not_comparable(tmp_path, argv_log, monkeypatch):
    """A sheet wired with custom pin symbols reads as having no pins at all.
    "No pins, five ports" means klt could not read the schematic, not that
    the header invented five ports -- so it is null, not a warning."""
    pins = tuple(("my_" + symbol, lab) for symbol, lab in _PINS)
    block = _run(
        tmp_path,
        monkeypatch,
        ports="vdda vssa inp bias outp",
        sheet=_sheet(pins=pins),
    )

    assert block["port_order"] is None
    assert block["warning"] is None


def test_a_port_the_schematic_never_declared_is_not_comparable(
    tmp_path, argv_log, monkeypatch
):
    """Same reasoning one pin at a time: an extra emitted port is a pin klt
    failed to read far more often than a pin the symbol grew, and the two are
    indistinguishable from here."""
    block = _run(tmp_path, monkeypatch, ports="vdda vssa inp outp bias extra")

    assert block["port_order"] is None
    assert block["warning"] is None


def test_an_export_that_was_not_block_shaped_is_not_compared(
    tmp_path, argv_log, monkeypatch
):
    """With no commented header to uncomment, the file's first `.subckt` is
    some nested cell's -- comparing the top sheet's pins against it would be
    a guaranteed false positive."""
    block = _run(
        tmp_path,
        monkeypatch,
        ports="",
        body=".subckt other a b\n.ends\n",
    )

    assert block["port_order"] is None
    assert "no commented '**.subckt' header" in block["warning"]


def test_two_pins_sharing_one_label_compare_by_name(tmp_path, argv_log, monkeypatch):
    """A duplicated `lab` is one net on two pins; xschem emits the port once.
    The emitted list is a subset of the declared one, which is the omitted-pin
    shape -- reported, not crashed on."""
    pins = (("ipin.sym", "a"), ("ipin.sym", "a"), ("opin.sym", "y"))
    block = _run(tmp_path, monkeypatch, ports="a y", sheet=_sheet(pins=pins))

    assert block["port_order"]["schematic"] == ["a", "a", "y"]
    assert block["port_order"]["emitted"] == ["a", "y"]


# --------------------------------------------------------------------------- #
# Header shapes the comparison has to survive
# --------------------------------------------------------------------------- #


def test_a_wrapped_header_is_read_across_its_continuations(
    tmp_path, argv_log, monkeypatch
):
    """Issue #3899's wrapped header: the ports live on `*+` continuation
    lines, so reading only the first line would report four phantom omissions."""
    block = _run(
        tmp_path,
        monkeypatch,
        ports="",
        body="**.subckt cell vdda vssa\n*+ inp outp\n*+ bias\n**.ends\n.end",
    )

    assert block["port_order"] is None
    assert block["warning"] is None


def test_restored_parameters_are_not_read_as_ports(tmp_path, argv_log, monkeypatch):
    """Issue #5439 appends `key=value` parameters to the same header. They are
    not ports, and a value carrying a comma is not a bus group either."""
    sheet = _sheet().replace('template="name=x1"', 'template="name=x1 f=max(1,2)"')
    block = _run(tmp_path, monkeypatch, ports="vdda vssa inp outp bias", sheet=sheet)

    assert block["port_order"] is None
    assert block["warning"] is None


def test_header_ports_stops_at_the_parameter_list():
    """The unit the two cases above exercise through the CLI."""
    assert header_ports(".subckt cell a b params: w=2\n") == ["a", "b"]
    assert header_ports(".subckt cell a b\n+ c w=2\n") == ["a", "b", "c"]
    assert header_ports("* no header here\n") is None


def test_schematic_pin_labels_reads_file_order_not_coordinates(tmp_path):
    """Measured: xschem follows the order the pin instances are stored in,
    not canvas position or `name=pN` numbering."""
    shuffled = (
        ("ipin.sym", "inp"),
        ("iopin.sym", "vdda"),
        ("opin.sym", "outp"),
    )
    path = tmp_path / "cell.sch"
    path.write_text(_sheet(pins=shuffled), encoding="utf-8")

    assert schematic_pin_labels(str(path)) == ["inp", "vdda", "outp"]


def test_schematic_pin_labels_ignores_braces_inside_other_elements(tmp_path):
    """Elements are walked, not pattern-matched, so a text label sitting
    between the pins -- escaped brace and all -- cannot be read as one
    component's symbol or another's attributes."""
    sheet = _sheet().replace(
        "V {}", "T {Order sets the .subckt terminal order \\{sic\\}} 0 0 0 0 {layer=4}"
    )
    path = tmp_path / "cell.sch"
    path.write_text(sheet, encoding="utf-8")

    assert schematic_pin_labels(str(path)) == _DECLARED


@pytest.mark.parametrize(
    "sheet",
    [
        pytest.param(_sheet().replace("lab=inp", "name=p3b"), id="pin-without-lab"),
        pytest.param(_sheet().replace("E {}", "E {", 1), id="unbalanced-brace"),
        pytest.param(_sheet() + "C {devices/opin.sym}\n", id="pin-without-attributes"),
    ],
)
def test_a_schematic_that_cannot_be_read_yields_no_pin_list(tmp_path, sheet):
    """Every unreadable shape is `None` -- "do not compare" -- never a
    partial list, which would read as a pile of omitted pins."""
    path = tmp_path / "cell.sch"
    path.write_text(sheet, encoding="utf-8")

    assert schematic_pin_labels(str(path)) is None


def test_an_unreadable_schematic_yields_no_pin_list(tmp_path):
    assert schematic_pin_labels(str(tmp_path / "absent.sch")) is None


# --------------------------------------------------------------------------- #
# real xschem: the oracle the stubs above stand in for
# --------------------------------------------------------------------------- #

_PIN_SYMS = {
    "ipin.sym": ("ipin", "*.ipin @lab", "in"),
    "opin.sym": ("opin", "*.opin @lab", "out"),
    "iopin.sym": ("iopin", "*.iopin @lab", "inout"),
}


def _write_real_fixtures(tmp_path, *, symbol: tuple[str, ...] | None):
    """A five-pin sheet whose pins interleave the three pin types, optionally
    beside a sibling symbol declaring them in a different order."""
    for name, (kind, fmt, direction) in _PIN_SYMS.items():
        (tmp_path / name).write_text(
            f"v {{xschem version=3.4.7 file_version=1.2}}\nG {{}}\n"
            f'K {{type={kind}\nformat="{fmt}"\ntemplate="name=p1 lab=xxx"}}\n'
            f"V {{}}\nS {{}}\nE {{}}\n"
            f"B 5 -1.25 -1.25 1.25 1.25 {{name=p dir={direction}}}\n",
            encoding="utf-8",
        )
    wires = "".join(
        f"N 0 {i * 80} 0 {i * 80 + 40} {{lab={lab}}}\n"
        for i, (_, lab) in enumerate(_PINS)
    )
    (tmp_path / "cell.sch").write_text(_sheet(prefix="") + wires, encoding="utf-8")
    if symbol is not None:
        (tmp_path / "cell.sym").write_text(_symbol(symbol), encoding="utf-8")
    rcfile = tmp_path / "xschemrc"
    rcfile.write_text(f"set XSCHEM_LIBRARY_PATH {tmp_path}\n", encoding="utf-8")
    return str(rcfile), str(tmp_path / "cell.sch")


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
def test_real_xschem_interleaved_pins_without_a_symbol_are_preserved(tmp_path):
    """The reported mechanism, refuted: xschem does not group a top sheet's
    ports by pin type. An interleaved declaration order survives verbatim, so
    there is nothing to warn about."""
    rcfile, schematic = _write_real_fixtures(tmp_path, symbol=None)

    report = run_netlist(
        schematic,
        str(tmp_path / "cell.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )

    written = (tmp_path / "cell.spice").read_text(encoding="utf-8")
    assert ".subckt cell vdda vssa inp outp bias" in written
    assert report["block"]["port_order"] is None
    assert report["block"]["warning"] is None


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
def test_real_xschem_sibling_symbol_reorders_the_ports_and_is_reported(tmp_path):
    """The actual mechanism: the sibling symbol's pin order wins over the
    schematic's, and until now said so nowhere."""
    rcfile, schematic = _write_real_fixtures(
        tmp_path, symbol=("vdda", "vssa", "inp", "bias", "outp")
    )

    report = run_netlist(
        schematic,
        str(tmp_path / "cell.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )

    written = (tmp_path / "cell.spice").read_text(encoding="utf-8")
    assert ".subckt cell vdda vssa inp bias outp" in written
    assert report["block"]["port_order"] == {
        "schematic": _DECLARED,
        "emitted": ["vdda", "vssa", "inp", "bias", "outp"],
        "symbol": str(tmp_path / "cell.sym"),
    }
    assert "cell.sym" in report["block"]["warning"]


@pytest.mark.skipif(shutil.which("xschem") is None, reason="xschem not on PATH")
def test_real_xschem_drops_a_pin_the_symbol_lacks_and_it_is_named(tmp_path):
    """The silent one: the port is gone from the card, the run still exits 0,
    and every positional caller is now short a connection."""
    rcfile, schematic = _write_real_fixtures(
        tmp_path, symbol=("vdda", "vssa", "inp", "outp")
    )

    report = run_netlist(
        schematic,
        str(tmp_path / "cell.spice"),
        rcfile=rcfile,
        block=True,
        provenance_header=False,
    )

    written = (tmp_path / "cell.spice").read_text(encoding="utf-8")
    assert ".subckt cell vdda vssa inp outp\n" in written
    assert report["status"] == "generated"
    assert "omits the pin 'bias'" in report["block"]["warning"]
