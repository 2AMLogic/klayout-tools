"""Hierarchical compare of a generated regular array (issue #5310).

A scaled instance of the 50k-device case measured in ``docs/cli/lvs.md``
("Large regular arrays"): one repeated cell, kept as a subcircuit on both
sides, must give ``match`` for a clean array and ``mismatch`` for a top-level
wiring fault and for a cell-internal fault. Generator:
``scripts/lvs_array_bench.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from klayout_tools.lvs import run_lvs

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "lvs_array_bench.py"
_spec = importlib.util.spec_from_file_location("lvs_array_bench", _SCRIPT)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules["lvs_array_bench"] = bench
_spec.loader.exec_module(bench)

ROWS = COLS = 12  # 864 devices: sub-second hierarchical, a few seconds flat


def _lvs(tmp_path: Path, broken: str | None) -> dict:
    layout = tmp_path / "layout.spice"
    reference = tmp_path / "ref.spice"
    layout.write_text(
        bench.anonymize(bench.build(ROWS, COLS, hier=True, broken=broken))
    )
    reference.write_text(bench.build(ROWS, COLS, hier=True, broken=None))
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(layout), "top": "ARRAY"},
                "reference": {"netlist": str(reference), "top": "ARRAY"},
            }
        )
    )
    return run_lvs(str(request))


def test_hierarchical_array_matches(tmp_path):
    assert _lvs(tmp_path, None)["status"] == "match"


@pytest.mark.parametrize("broken", ["wiring", "internal"])
def test_hierarchical_array_broken_instance_mismatches(tmp_path, broken):
    assert _lvs(tmp_path, broken)["status"] == "mismatch"


def test_bench_comparer_agrees_with_klt_lvs():
    clean = bench.run(ROWS, COLS, hier=True, broken=None)
    faulty = bench.run(ROWS, COLS, hier=True, broken="wiring")
    assert clean["match"] is True
    assert faulty["match"] is False


# Issue #5322: `options.flatten_reference` as a keep-list of circuit globs.
_LEAF = ".SUBCKT LEAF A B VSS\n.ENDS LEAF\n"
_LAYOUT = (
    _LEAF
    + ".SUBCKT TOP IN OUT VSS\n"
    + "X1 IN N1 VSS LEAF\nX2 N1 OUT VSS LEAF\n"
    + "M1 OUT IN VSS VSS nfet W=1U L=0.15U\n.ENDS TOP\n"
)
_REFERENCE = (
    _LEAF
    + ".SUBCKT MID I O VSS\nX1 I N1 VSS LEAF\nX2 N1 O VSS LEAF\n"
    + "M1 O I VSS VSS nfet W=1U L=0.15U\n.ENDS MID\n"
    + ".SUBCKT TOP IN OUT VSS\nXM IN OUT VSS MID\n.ENDS TOP\n"
)


def _flatten_lvs(tmp_path: Path, flatten, reference: str = _REFERENCE) -> dict:
    (tmp_path / "layout.spice").write_text(_LAYOUT)
    (tmp_path / "ref.spice").write_text(reference)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": str(tmp_path / "layout.spice"), "top": "TOP"},
                "reference": {"netlist": str(tmp_path / "ref.spice"), "top": "TOP"},
                "options": {"flatten_reference": flatten},
            }
        )
    )
    return run_lvs(str(request))


def test_flatten_reference_keep_list_spares_stub(tmp_path):
    report = _flatten_lvs(tmp_path, ["leaf"])  # case-insensitive glob
    assert report["status"] == "match"
    warn = [m for m in report["mismatches"] if m["category"] == "topology.flattened"]
    assert warn and "LEAF" in warn[0]["description"].upper()
    assert report["options"]["flatten_reference"] == ["leaf"]


def test_flatten_reference_bool_still_flattens_stub(tmp_path):
    assert _flatten_lvs(tmp_path, True)["status"] == "mismatch"


def test_flatten_reference_keep_list_no_match_flattens_all(tmp_path):
    assert _flatten_lvs(tmp_path, ["NOPE*"])["status"] == "mismatch"


# Issue #5322: GDS-start abstracted array -> hierarchical compare in one request.
def _gds_abstract_lvs(tmp_path: Path, x2_y_net: str) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_extract as te

    gds = te._write_gds(te._make_abstract_cells_layout(), tmp_path / "arr.gds")
    ref = (
        ".SUBCKT SC_BUF A Y\n.ENDS SC_BUF\n"
        ".SUBCKT MID I1A I1Y I2A I2Y\n"
        f"X1 I1A I1Y SC_BUF\nX2 I2A {x2_y_net} SC_BUF\n.ENDS MID\n"
        ".SUBCKT TOP INST1_A INST1_Y INST2_A INST2_Y\n"
        "XM INST1_A INST1_Y INST2_A INST2_Y MID\n.ENDS TOP\n"
    )
    (tmp_path / "ref.spice").write_text(ref)
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {
                    "file": str(gds),
                    "deck": "sky130",
                    "top": "TOP",
                    "top_cell_pins": True,
                    "abstract_cells": ["SC_BUF"],
                },
                "reference": {"netlist": str(tmp_path / "ref.spice"), "top": "TOP"},
                "options": {"flatten_reference": ["SC_BUF"]},
            }
        )
    )
    return run_lvs(str(request))


def test_gds_abstracted_array_matches_with_keep_list(tmp_path):
    assert _gds_abstract_lvs(tmp_path, "I2Y")["status"] == "match"


def test_gds_abstracted_array_broken_top_wire_mismatches(tmp_path):
    assert _gds_abstract_lvs(tmp_path, "I1Y")["status"] == "mismatch"
