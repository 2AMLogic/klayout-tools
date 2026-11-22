"""``klt pex --engine klayout-runset`` on a *hierarchical* runset (issue #6559).

A runset that does not flatten (KLayout's ``deep`` directive, or a layout whose
sub-cells the runset keeps) leaves a netlist whose top calls locally defined
cells: ``TOP`` holds two ``X`` calls to ``LEAF`` and the one ``NMOS`` sits in
``LEAF``'s definition. Two readings of that netlist were wrong:

- ``extraction.device_counts`` was the *selected circuit's* count -- ``{}`` --
  for a design that instantiates two devices; and
- ``model_mismatch`` scanned the cards, so it reported ``LEAF`` as a device
  family the reference lacked and counted the ``NMOS`` once, not twice.

The runset subprocess is faked exactly as in ``tests/test_pex_runset.py``; the
extraction, the written netlist and the envelope are real.
"""

# ruff: noqa: F401
from __future__ import annotations

import klayout.db as kdb
import pytest

from klayout_tools.extract import run_extract
from klayout_tools.extract_runset import RUNSET_ENGINE
from klayout_tools.pex import run_pex
from klayout_tools.pex_netlist_text import (
    _device_model_counts,
    _instantiated_model_counts,
)
from klayout_tools.pex_runset import PexRunset, extraction_fields
from test_extract_runset import _inputs
from test_extract_runset import fake_klayout as fake_klayout
from test_pex import _write_harness


def _hier_inputs(tmp_path):
    """A layout whose ``TOP`` instantiates a ``LEAF`` cell (one NMOS) twice."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top, leaf = ly.create_cell("TOP"), ly.create_cell("LEAF")
    act, poly, bulk = ly.layer(1, 0), ly.layer(2, 0), ly.layer(9, 0)
    leaf.shapes(act).insert(kdb.Box(0, 0, 1000, 400))
    leaf.shapes(poly).insert(kdb.Box(400, -100, 600, 500))
    top.shapes(bulk).insert(kdb.Box(-1000, -1000, 100000, 2000))
    for i in range(2):
        top.insert(kdb.CellInstArray(leaf.cell_index(), kdb.Trans(i * 2000, 0)))
    gds = tmp_path / "hier.gds"
    ly.write(str(gds))
    deck = tmp_path / "pdk.lvs"
    deck.write_text("# runset\n")
    return str(gds), str(deck)


def _reference(tmp_path, *cards):
    """The *flat* reference: what the same runset writes without ``deep``."""
    lines = [".SUBCKT TOP A B", "X1 A B NMOS", "X2 A B NMOS", *cards, ".ENDS TOP"]
    path = tmp_path / "flat.spice"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _pex(tmp_path, **kwargs):
    gds, deck = _hier_inputs(tmp_path)
    return run_pex(
        gds,
        [],
        None,
        output=str(tmp_path / "out.spice"),
        artifacts_dir=str(tmp_path / "art"),
        runset=PexRunset(deck_file=deck),
        **kwargs,
    )


# `klt extract`: what is outside the selected circuit is said, not omitted
# --------------------------------------------------------------------------- #


def test_extract_counts_devices_through_the_called_cells(tmp_path, fake_klayout):
    gds, deck = _hier_inputs(tmp_path)
    report = run_extract(
        gds,
        None,
        output=str(tmp_path / "o.spice"),
        engine=RUNSET_ENGINE,
        deck_file=deck,
    )

    # The selected circuit alone has no device; the design has two.
    assert report["device_counts"] == {}
    assert report["instantiated_device_counts"] == {"NMOS": 2}
    assert report["extra_circuits"] == ["LEAF"]
    # An empty `TOP` is not an empty extraction.
    assert not any("recognised no devices" in w for w in report["warnings"])
    assert any("'TOP' alone" in w for w in report["warnings"])


def test_flat_extract_has_equal_counts_and_no_extra_circuits(tmp_path, fake_klayout):
    gds, deck = _inputs(tmp_path, 2)
    report = run_extract(
        gds,
        None,
        output=str(tmp_path / "o.spice"),
        engine=RUNSET_ENGINE,
        deck_file=deck,
    )

    assert (
        report["instantiated_device_counts"] == report["device_counts"] == {"NMOS": 2}
    )
    assert report["extra_circuits"] == []


# `klt pex`: device_counts / device_count follow the instantiated hierarchy
# --------------------------------------------------------------------------- #


def test_pex_reports_the_instantiated_device_counts(tmp_path, fake_klayout):
    report = _pex(tmp_path, extract_only=True)

    extraction = report["extraction"]
    assert extraction["device_counts"] == {"NMOS": 2}
    assert extraction["device_count"] == 2
    assert extraction["extra_circuits"] == ["LEAF"]


def test_extraction_fields_total_the_counts_they_report():
    fields = extraction_fields(
        {
            "engine": RUNSET_ENGINE,
            "device_count": 0,
            "device_counts": {},
            "instantiated_device_counts": {"NMOS": 2, "PMOS": 1},
            "extra_circuits": ["LEAF"],
        }
    )
    assert fields["device_counts"] == {"NMOS": 2, "PMOS": 1}
    assert fields["device_count"] == 3
    assert fields["extra_circuits"] == ["LEAF"]


def test_extraction_fields_fall_back_to_the_selected_circuit_counts():
    """An extract report without the hierarchy-aware field reports what it has."""
    fields = extraction_fields({"engine": RUNSET_ENGINE, "device_counts": {"NMOS": 2}})
    assert fields["device_counts"] == {"NMOS": 2}
    assert fields["device_count"] == 2
    assert fields["extra_circuits"] == []


# `model_mismatch`: the extracted side is compared by what it instantiates
# --------------------------------------------------------------------------- #


def test_flat_reference_against_a_hierarchical_extraction_agrees(
    tmp_path, fake_klayout
):
    """The reviewer's repro: same GDS, same design, flat reference vs the
    runset's hierarchical output. No mismatch, and `LEAF` is never a family."""
    report = _pex(tmp_path, extract_only=True, reference_netlist=_reference(tmp_path))

    assert report["model_mismatch"] is None


def test_flat_reference_against_a_hierarchical_extraction_in_delta_mode(
    tmp_path, fake_klayout
):
    report = _pex(
        tmp_path,
        measure_command=_write_harness(tmp_path),
        reference_netlist=_reference(tmp_path),
    )

    assert report["delta"], "delta mode produced rows"
    assert report["model_mismatch"] is None


def test_a_missing_family_is_still_named_against_a_hierarchical_extraction(
    tmp_path, fake_klayout
):
    report = _pex(
        tmp_path,
        extract_only=True,
        reference_netlist=_reference(tmp_path, "XQ1 A B pnpMPA"),
    )

    mismatch = report["model_mismatch"]
    assert mismatch["kind"] == "model_set"
    assert mismatch["reference_only"] == ["pnpMPA"]
    assert mismatch["extracted_only"] == []
    # What the extracted side instantiates, not what its text defines.
    assert mismatch["counts"]["extracted"] == {"NMOS": 2}


def test_a_wrong_instance_count_is_still_reported_against_a_hierarchical_one(
    tmp_path, fake_klayout
):
    path = tmp_path / "one.spice"
    path.write_text(".SUBCKT TOP A B\nX1 A B NMOS\n.ENDS TOP\n")
    report = _pex(tmp_path, extract_only=True, reference_netlist=str(path))

    mismatch = report["model_mismatch"]
    assert mismatch["kind"] == "count_only"
    assert mismatch["counts"] == {"reference": {"NMOS": 1}, "extracted": {"NMOS": 2}}


# `_instantiated_model_counts`
# --------------------------------------------------------------------------- #

FLAT = ".SUBCKT TOP A B\nX1 A B NMOS\nX2 A B nmos\nM3 A B A B sky_n W=1u\n.ENDS TOP\n"
DEEP = (
    ".SUBCKT LEAF 1 2\nX$1 1 2 NMOS\n.ENDS LEAF\n"
    ".SUBCKT MID 1 2\nXa 1 2 LEAF\nXb 1 2 LEAF\nXc 1 2 LEAF\nXp 1 2 PMOS\n.ENDS MID\n"
    ".SUBCKT TOP 1 2\nXm1 1 2 MID\nXm2 1 2 mid\n.ENDS TOP\n"
)


def _plain(counts):
    return {name: count for name, count in counts.values()}


def test_a_flat_netlist_is_counted_by_the_plain_card_scan():
    assert _instantiated_model_counts(FLAT, "TOP") == _device_model_counts(FLAT)
    assert _instantiated_model_counts(FLAT, None) == _device_model_counts(FLAT)


def test_calls_multiply_through_every_level():
    counts = _plain(_instantiated_model_counts(DEEP, "TOP"))

    # 2 MID calls x 3 LEAF calls x 1 NMOS, and 2 MID calls x 1 PMOS.
    assert counts == {"NMOS": 6, "PMOS": 2}


def test_the_root_may_be_named_in_any_case_or_found_as_the_only_uncalled_cell():
    expected = {"NMOS": 6, "PMOS": 2}
    assert _plain(_instantiated_model_counts(DEEP, "top")) == expected
    assert _plain(_instantiated_model_counts(DEEP, None)) == expected
    assert _plain(_instantiated_model_counts(DEEP, "NoSuchCell")) == expected


def test_a_named_root_counts_only_what_it_instantiates():
    assert _plain(_instantiated_model_counts(DEEP, "MID")) == {"NMOS": 3, "PMOS": 1}


def test_an_unidentifiable_root_still_never_reports_a_cell_name():
    two_roots = DEEP + ".SUBCKT OTHER 1 2\nXo 1 2 LEAF\n.ENDS OTHER\n"
    counts = _plain(_instantiated_model_counts(two_roots, "NoSuchCell"))

    # Each definition once, hierarchy edges dropped.
    assert counts == {"NMOS": 1, "PMOS": 1}
    assert not {"LEAF", "MID", "TOP", "OTHER"} & {k.upper() for k in counts}


def test_a_device_outside_every_subckt_is_counted_with_the_hierarchy():
    text = DEEP + "Xtop 1 2 PMOS\n"

    assert _plain(_instantiated_model_counts(text, "TOP")) == {"NMOS": 6, "PMOS": 3}


def test_a_call_cycle_does_not_recurse_forever():
    cyclic = ".SUBCKT A 1\nXb 1 B\nXm 1 NMOS\n.ENDS A\n.SUBCKT B 1\nXa 1 A\n.ENDS B\n"

    assert _plain(_instantiated_model_counts(cyclic, "A")) == {"NMOS": 1}
