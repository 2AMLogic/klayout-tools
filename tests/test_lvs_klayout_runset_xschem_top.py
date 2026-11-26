"""Issue #7918: ``klayout-runset`` reference preparation for commented xschem
top headers (``**.subckt`` / ``*+`` / ``**.ends``) and headerless implicit
``.TOP`` decks, including real KLayout ``align`` verification."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import klayout.db as kdb
import pytest

import lvs_tmp_repo
from helpers import lvs_runset_fixtures as fx
from klayout_tools.lvs import LvsError, rerun_lvs_report, run_lvs
from klayout_tools.lvs_runset_reference import convert_reference

tmp_path = lvs_tmp_repo.tmp_path

MINIMAL_ALIGN_DECK = """\
source($input, *($top ? [$top] : []))
schematic($schematic)
report_lvs($report)
act = input(1, 0)
poly = input(2, 0)
bulk = input(9, 0)
gate = act & poly
sd = act - poly
extract_devices(mos4("NMOS"), { "SD" => sd, "G" => gate, "tG" => poly, "W" => bulk })
connect(sd, sd)
connect(poly, poly)
connect(gate, poly)
connect(bulk, bulk)
netlist
align
compare
"""

XSCHEM_HIER_SPICE = (
    "** sch_path: /tmp/my_block.sch\n"
    "**.subckt my_block a y\n"
    "*+ vdd vss\n"
    "X0 a y vdd vss my_leaf\n"
    "**.ends\n"
    "\n"
    ".subckt my_leaf a y vdd vss\n"
    "M1 a y vss vdd NMOS L=0.2u W=0.4u\n"
    ".ends\n"
    ".end\n"
)

HEADERLESS_IMPLICIT_SPICE = (
    "* headerless implicit top\n"
    "X0 a y vdd vss my_leaf\n"
    "+ \n"
    ".subckt my_leaf a y vdd vss\n"
    "M1 a y vss vdd NMOS L=0.2u W=0.4u\n"
    ".ends\n"
    ".end\n"
)


def make_hierarchical_gds(path: Path, top_name: str = "lay_block") -> None:
    ly = kdb.Layout()
    ly.dbu = 0.001
    leaf = ly.create_cell("my_leaf")
    act, poly, bulk = ly.layer(1, 0), ly.layer(2, 0), ly.layer(9, 0)
    leaf.shapes(bulk).insert(kdb.Box(-1000, -1000, 3000, 2000))
    leaf.shapes(act).insert(kdb.Box(0, 0, 1000, 400))
    leaf.shapes(poly).insert(kdb.Box(400, -100, 600, 500))
    top = ly.create_cell(top_name)
    top.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans()))
    ly.write(str(path))


def _real_align_request(
    tmp_path: Path, spice: str, *, layout_top: str = "lay_block", **ref_kw
) -> tuple[dict, Path]:
    gds = tmp_path / "hier.gds"
    make_hierarchical_gds(gds, top_name=layout_top)
    deck = tmp_path / "align.lvs"
    deck.write_text(MINIMAL_ALIGN_DECK)
    ref = tmp_path / "ref.sp"
    ref.write_bytes(spice.encode("utf-8"))
    req = {
        "engine": "klayout-runset",
        "layout": {"file": str(gds), "top": layout_top},
        "reference": {"netlist": str(ref), **ref_kw},
        "deck_file": str(deck),
    }
    return req, ref


@pytest.fixture
def fake_klayout(monkeypatch, tmp_path):
    return fx.install_fake_klayout(monkeypatch, tmp_path)


def run_real_align_suite(tmp_path: Path) -> None:
    for selector in ({}, {"top": "my_block"}, {"top": ".TOP"}):
        sub = tmp_path / f"case_{selector.get('top', 'omitted')}"
        sub.mkdir()
        req, ref = _real_align_request(sub, XSCHEM_HIER_SPICE, **selector)
        before = ref.read_bytes()
        report = run_lvs(json.dumps(req))
        assert ref.read_bytes() == before
        assert report["status"] == "match", report
        assert report["counts"]["pins"]["reference"] == 4
        assert report["counts"]["devices"]["matched"] == 1

    imp_dir = tmp_path / "implicit"
    imp_dir.mkdir()
    req, ref = _real_align_request(imp_dir, HEADERLESS_IMPLICIT_SPICE, top=".TOP")
    before = ref.read_bytes()
    report = run_lvs(json.dumps(req))
    assert ref.read_bytes() == before
    assert report["status"] == "match", report
    assert report["counts"]["pins"]["reference"] == 0
    assert report["counts"]["devices"]["matched"] == 1


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
def test_real_klayout_align_continued_xschem_and_implicit_top(tmp_path):
    run_real_align_suite(tmp_path)


@pytest.mark.skipif(shutil.which("klayout") is None, reason="klayout binary absent")
@pytest.mark.parametrize("layout_top", ["lay_block", "my_block"])
def test_real_klayout_unrelated_selector_never_selects_wrapper(tmp_path, layout_top):
    req, _ = _real_align_request(
        tmp_path, XSCHEM_HIER_SPICE, layout_top=layout_top, top="unrelated_top"
    )
    with pytest.raises(LvsError, match="schematic counterpart"):
        run_lvs(json.dumps(req))


@pytest.mark.parametrize(
    ("spice", "ref_kw"),
    [
        (
            "**.subckt my_block a y\n*+ vdd vss\nX0 a y vdd vss my_leaf\n"
            ".subckt my_leaf a y vdd vss\nR1 a y 1k\n.ends\n",
            {},
        ),
        (
            "**.subckt my_block a y\n*+ vdd vss\nX0 a y vdd vss my_leaf\n"
            ".subckt my_leaf a y vdd vss\nR1 a y 1k\n.ends\n",
            {"top": "my_block"},
        ),
        (
            "X0 a y vdd vss my_leaf\n**.ends\n"
            ".subckt my_leaf a y vdd vss\nR1 a y 1k\n.ends\n",
            {},
        ),
        (
            "*+ a y vdd vss\nX0 a y vdd vss my_leaf\n"
            ".subckt my_leaf a y vdd vss\nR1 a y 1k\n.ends\n",
            {"top": ".TOP"},
        ),
    ],
)
def test_incomplete_wrapper_and_standalone_continuation_negative_controls(
    tmp_path, spice, ref_kw
):
    source = tmp_path / "ref.sp"
    source.write_text(spice)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    work = tmp_path / "work"
    work.mkdir()
    out = convert_reference(
        str(source),
        {"layout": {"top": "lay_block"}, "reference": dict(ref_kw)},
        str(work),
        str(deck),
    )
    nl = kdb.Netlist()
    nl.read(out, kdb.NetlistSpiceReader())
    top = nl.circuit_by_name("LAY_BLOCK")
    if ref_kw.get("top") == ".TOP":
        assert top is not None
        assert list(top.each_pin()) == []
    else:
        assert top is None
        assert nl.circuit_by_name("MY_BLOCK") is None


def test_xschem_wrapper_with_subckt_call_and_rerun_replay(tmp_path, fake_klayout):
    spice = (
        "**.subckt schem_top a b\n*+ c d\n"
        "XM1 c b a d sky130_fd_pr__nfet_01v8 W=0.4u L=0.2u\n"
        "**.ends\n.end\n"
    )
    request = json.loads(fx.request(tmp_path, 1, spice=spice))
    request["layout"]["top"] = "TOP"
    request["reference"].update(
        form="subckt-call",
        deck="sky130",
        device_map={"sky130_fd_pr__nfet_01v8": "NMOS"},
    )
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match", report
    assert report["reference_top"] == "SCHEM_TOP"
    committed = tmp_path / "report.json"
    committed.write_text(json.dumps(report))
    replay = rerun_lvs_report(str(committed))
    assert replay["status"] == "match", replay
    assert not replay.get("drift"), replay


@pytest.mark.parametrize(
    ("source_dir", "include_arg", "child"),
    [
        ("sub", '"leaf.sp"', "leaf.sp"),
        ("sub", '''"vendor's models/leaf.sp"''', "vendor's models/leaf.sp"),
        ("sub", """'vendor"s models/leaf.sp'""", 'vendor"s models/leaf.sp'),
        ("customer's reference", "'cells/leaf.sp'", "cells/leaf.sp"),
        ('ASIC 3" reference', '"cells/leaf.sp"', "cells/leaf.sp"),
        (r"""customer's 3" \ref""", r'"cells/a\"b\\c.sp"', 'cells/a"b\\c.sp'),
        ("customer's reference", "cells/leaf.sp", "cells/leaf.sp"),
    ],
)
def test_quoted_relative_include_bus_ports_and_deck_scopes(
    tmp_path, source_dir, include_arg, child
):
    sub = tmp_path / source_dir
    leaf = sub / child
    leaf.parent.mkdir(parents=True)
    leaf.write_text(".subckt my_leaf a0 a1 y\nR1 a0 y 1k\nR2 a1 y 2k\n.ends\n")
    wrapped = (
        f"; comment\n.include {include_arg}\n"
        ".control\n**.subckt ignored x\n.endc\n"
        ".lib unused_sec\n**.subckt also_ignored x\n.endl\n"
        "**.subckt my_block a[1],a[0]\n*+ y\n"
        "X0 a[0] a[1] y my_leaf\n**.ends my_block\n.end\n* trailing note\n"
    )
    source = sub / "top.sp"
    source.write_text(wrapped)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    work = tmp_path / "work"
    work.mkdir()
    out = convert_reference(
        str(source),
        {"layout": {"top": "lay_block"}, "reference": {}},
        str(work),
        str(deck),
    )
    assert source.read_bytes() == wrapped.encode()
    nl = kdb.Netlist()
    nl.read(out, kdb.NetlistSpiceReader())
    top = nl.circuit_by_name("LAY_BLOCK")
    assert [p.name() for p in top.each_pin()] == ["A[1]", "A[0]", "Y"]
    (child_instance,) = top.each_subcircuit()
    included = child_instance.circuit_ref()
    assert included.name == "MY_LEAF"
    assert [device.device_class().name for device in included.each_device()] == [
        "RES",
        "RES",
    ]


@pytest.mark.parametrize(
    "bad_spice",
    [
        "**.subckt\nX0 a b my_leaf\n**.ends\n",
        "**.subckt my_block a b\n**.subckt second a b\n**.ends\n",
        "**.subckt my_block a b\n.subckt child a b\n.ends\n**.ends\n",
        "**.subckt my_block a b\nX0 a b my_leaf\n**.ends other_name\n",
        ".subckt only_child a b\nR1 a b 1k\n.ends\n",
    ],
)
def test_malformed_wrappers_and_empty_implicit_top_leave_file_unchanged(
    tmp_path, bad_spice
):
    source = tmp_path / "ref.sp"
    source.write_text(bad_spice)
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    work = tmp_path / "work"
    work.mkdir()
    out = convert_reference(
        str(source),
        {"layout": {"top": "my_block"}, "reference": {}},
        str(work),
        str(deck),
    )
    assert out == str(source)
    if "only_child" in bad_spice:
        out_top = convert_reference(
            str(source),
            {"layout": {"top": "lay_block"}, "reference": {"top": ".TOP"}},
            str(work),
            str(deck),
        )
        nl = kdb.Netlist()
        nl.read(out_top, kdb.NetlistSpiceReader())
        assert nl.circuit_by_name("LAY_BLOCK") is None
