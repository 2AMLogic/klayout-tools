"""Tests for `klt lvs` and the `klayout_tools.lvs` library.

Three tiers:

- **Request/library unit tests** exercise `load_request`/`run_lvs`'s error
  paths (bad request, unresolvable layout/reference input, unknown deck,
  unsupported engine) directly, with no real KLayout compare involved.
- **Synthetic-netlist compare tests** run the real `NetlistComparer` engine
  against small, hand-written SPICE fixtures shaped to exercise one category
  at a time -- a `device.property` case (curator field-note negative
  control #2: change one device's width, leave connectivity alone) and
  `net.merged`/`net.split` cases (negative control #1: short/split nets) --
  plus the ambiguous-match/`hints.same_nets` interaction. These are
  synthetic (not the real corpus) because the real corpus's curated decks
  give every NMOS body a degree-1, single-owner net (`vsubs`; see
  `docs/cli/extract.md` -> "Coverage"), which collapses even a small,
  isolated device-parameter change into a much larger structural mismatch --
  a real, useful signal for the corpus round-trip tier below, but a poor
  fixture for isolating one category at a time.
- **Direct classification unit tests** call `lvs._build_mismatches` with a
  fake logger object (not a real `NetlistComparer` run) to cover every
  mismatch category precisely and deterministically, including categories
  (`device.class`, `pin.unmatched`, `topology` from a circuit/subcircuit/
  device-class mismatch, and the ambiguous-net warning) that are hard to
  provoke in isolation from the real engine's emergent matching behaviour.
- **Corpus round-trip tests** run the real sky130 corpus: a known-good
  self-compare (an extracted netlist against itself, composing inline
  extraction with compare in one request, per the epic's own "known-good
  cell matches cleanly" success criterion) and a deliberately-broken variant
  (a shorted reference net, the epic's "deliberately broken cell reports
  mismatches" criterion).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import lvs
from klayout_tools.cli import main
from klayout_tools.lvs import LvsError, run_lvs

CORPUS_DIR = Path(__file__).parent / "corpus"
SKY130_INV = CORPUS_DIR / "sky130" / "sky130_fd_sc_hd__inv_1.gds"


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    return str(path)


def _write_request(path: Path, request: dict) -> str:
    path.write_text(json.dumps(request))
    return str(path)


# --------------------------------------------------------------------------- #
# Synthetic SPICE fixtures (device-level, not layout-derived)
# --------------------------------------------------------------------------- #

_INVERTER_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

_BUF2_REFERENCE_SPICE = """
.subckt buf2 A Y VPWR VGND
M1 MID A VGND VGND nfet W=0.65U L=0.15U
M2 MID A VPWR VPWR pfet W=1.0U L=0.15U
M3 Y MID VGND VGND nfet W=0.65U L=0.15U
M4 Y MID VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


# --------------------------------------------------------------------------- #
# load_request / request-shape errors
# --------------------------------------------------------------------------- #


def test_missing_request_file_raises(tmp_path):
    with pytest.raises(LvsError, match="file not found"):
        run_lvs(str(tmp_path / "nope.json"))


def test_request_directory_raises(tmp_path):
    with pytest.raises(LvsError, match="not a file"):
        run_lvs(str(tmp_path))


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "request.json"
    path.write_text("{not json")
    with pytest.raises(LvsError, match="not valid JSON"):
        run_lvs(str(path))


def test_request_must_be_json_object(tmp_path):
    path = tmp_path / "request.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(LvsError, match="must contain a JSON object"):
        run_lvs(str(path))


def test_missing_layout_field_raises(tmp_path):
    path = _write_request(tmp_path / "request.json", {"reference": {"netlist": "x"}})
    with pytest.raises(LvsError, match="missing required field: layout"):
        run_lvs(path)


def test_missing_reference_field_raises(tmp_path):
    path = _write_request(tmp_path / "request.json", {"layout": {"netlist": "x"}})
    with pytest.raises(LvsError, match="missing required field: reference"):
        run_lvs(path)


def test_unsupported_engine_raises(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "netgen",
            "layout": {"netlist": "x"},
            "reference": {"netlist": "y"},
        },
    )
    with pytest.raises(LvsError, match="unsupported engine 'netgen'"):
        run_lvs(path)


def test_layout_requires_file_or_netlist(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {"layout": {}, "reference": {"netlist": "y"}},
    )
    with pytest.raises(LvsError, match="requires 'file' or 'netlist'"):
        run_lvs(path)


def test_layout_rejects_both_file_and_netlist(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": "a.gds", "netlist": "a.spice", "deck": "sky130"},
            "reference": {"netlist": "y"},
        },
    )
    with pytest.raises(LvsError, match="exactly one of 'file' or 'netlist'"):
        run_lvs(path)


def test_layout_file_requires_deck(tmp_path):
    gds = _write(tmp_path / "a.gds", "")  # existence is checked before deck
    path = _write_request(
        tmp_path / "request.json",
        {"layout": {"file": gds}, "reference": {"netlist": "y"}},
    )
    with pytest.raises(LvsError, match="layout.deck is required"):
        run_lvs(path)


def test_layout_file_not_found_raises(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(tmp_path / "missing.gds"), "deck": "sky130"},
            "reference": {"netlist": "y"},
        },
    )
    with pytest.raises(LvsError, match="layout file not found"):
        run_lvs(path)


def test_layout_netlist_not_found_raises(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": str(tmp_path / "missing.spice")},
            "reference": {"netlist": "y"},
        },
    )
    with pytest.raises(LvsError, match="layout netlist not found"):
        run_lvs(path)


def test_reference_netlist_not_found_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path},
            "reference": {"netlist": str(tmp_path / "missing.spice")},
        },
    )
    with pytest.raises(LvsError, match="reference netlist not found"):
        run_lvs(path)


def test_unknown_deck_raises(tmp_path):
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.create_cell("TOP")
    gds_path = str(tmp_path / "a.gds")
    layout.write(gds_path)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": gds_path, "deck": "nope"},
            "reference": {"netlist": str(tmp_path / "ref.spice")},
        },
    )
    with pytest.raises(LvsError, match="unknown deck 'nope'"):
        run_lvs(path)


def test_malformed_reference_netlist_reports_no_top_circuit(tmp_path):
    """Some garbage input is silently tolerated by `NetlistSpiceReader` --
    it prints a warning and returns an empty netlist (zero circuits), rather
    than raising (see `lvs._read_reference_netlist`'s docstring). That
    surfaces as a clean, structured `LvsError` a moment later via the "no
    top circuit" check, not a parser exception."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = tmp_path / "ref.spice"
    reference_path.write_bytes(b"\x00\x01\x02 not spice at all \xff\xfe")
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": str(reference_path)},
        },
    )
    with pytest.raises(LvsError, match="reference netlist has no top circuit"):
        run_lvs(path)


def test_unparseable_reference_netlist_raises(tmp_path):
    """Other garbage (a leading token that resolves to an unknown SPICE
    element type) *does* raise inside `NetlistSpiceReader` -- caught and
    turned into a clean `LvsError`, not a traceback."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", "not a spice file at all")
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="could not parse reference netlist"):
        run_lvs(path)


def test_named_top_not_found_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "nope"},
        },
    )
    with pytest.raises(LvsError, match="'nope' not found in reference netlist"):
        run_lvs(path)


def test_ambiguous_top_without_explicit_selection_raises(tmp_path):
    two_circuits = _INVERTER_SPICE + _BUF2_REFERENCE_SPICE
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", two_circuits)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="2 top circuits"):
        run_lvs(path)


# --------------------------------------------------------------------------- #
# Clean match (synthetic self-compare)
# --------------------------------------------------------------------------- #


def test_clean_self_compare_reports_match(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    report = run_lvs(path)

    assert report["schema_version"] == 1
    assert report["engine"] == "klayout"
    assert report["status"] == "match"
    assert report["mismatch_count"] == 0
    assert report["category_counts"] == {}
    assert report["mismatches"] == []
    assert report["counts"]["nets"] == {"layout": 4, "reference": 4, "matched": 4}
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["pins"] == {"layout": 4, "reference": 4, "matched": 4}
    # `layout.netlist` (pre-extracted) was given -- no deck is involved, so
    # there is no device-class coverage to report (issue #221).
    assert report["device_classes"] is None
    assert report["environment"]["engine"] == "klayout"
    assert report["environment"]["engine_version"]
    assert len(report["environment"]["layout_sha256"]) == 64
    assert len(report["environment"]["reference_sha256"]) == 64
    assert report["environment"]["extracted_netlist"] is None


def test_auto_selected_top_matches_explicit_top(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path},
            "reference": {"netlist": reference_path},
        },
    )
    report = run_lvs(path)
    assert report["status"] == "match"
    assert report["top"] == "INV"  # NetlistSpiceReader uppercases circuit names


def test_differently_named_tops_still_report_specific_mismatch(tmp_path):
    """Issue #231: `layout.top`/`reference.top` naming *different* circuits
    must not degrade the report to two generic ``topology`` "could not be
    matched to a counterpart" entries -- the request document already
    declares these two circuits as the pair to compare, so `run_lvs` must
    pin that pairing (`NetlistComparer.same_circuits`) instead of leaving it
    to the comparer's own by-name matching. Same deliberate defect as
    `test_device_width_change_reports_device_property_mismatch` (a width
    change, no topology change), just with differently-named tops on each
    side."""
    layout_spice = _INVERTER_SPICE.replace(".subckt inv ", ".subckt inv_layout ")
    reference_spice = _INVERTER_SPICE.replace(
        ".subckt inv ", ".subckt inv_ref "
    ).replace("W=1.0U", "W=2.0U")
    layout_path = _write(tmp_path / "layout.spice", layout_spice)
    reference_path = _write(tmp_path / "ref.spice", reference_spice)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv_layout"},
            "reference": {"netlist": reference_path, "top": "inv_ref"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["mismatch_count"] == 1
    assert report["category_counts"] == {"device.property": 1}

    (entry,) = report["mismatches"]
    assert entry["category"] == "device.property"
    for mismatch in report["mismatches"]:
        assert (
            mismatch["description"] != "circuit could not be matched to a counterpart"
        )


# --------------------------------------------------------------------------- #
# device.property (curator negative control #2: width change, no topology change)
# --------------------------------------------------------------------------- #


def test_device_width_change_reports_device_property_mismatch(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice", _INVERTER_SPICE.replace("W=1.0U", "W=2.0U")
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["mismatch_count"] == 1
    assert report["category_counts"] == {"device.property": 1}

    (entry,) = report["mismatches"]
    assert entry["category"] == "device.property"
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert entry["device"]["class"] == "PFET"
    # `W=1.0U`/`W=2.0U`'s explicit micron suffix round-trips as a plain
    # micron value (unlike a bare, unit-less literal -- see docs/cli/lvs.md
    # -> "Unit suffixes matter").
    assert entry["property"] == {
        "name": "w_um",
        "layout": pytest.approx(1.0),
        "reference": pytest.approx(2.0),
    }


# --------------------------------------------------------------------------- #
# net.merged / net.split (curator negative control #1: topology short/split)
# --------------------------------------------------------------------------- #


def test_shorted_nets_report_net_merged(tmp_path):
    """layout ties MID directly to A (a short that should not exist) --
    reference has MID as a genuinely distinct net."""
    reference_path = _write(tmp_path / "ref.spice", _BUF2_REFERENCE_SPICE)
    layout_path = _write(
        tmp_path / "layout.spice", _BUF2_REFERENCE_SPICE.replace("MID", "A")
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "buf2"},
            "reference": {"netlist": reference_path, "top": "buf2"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"]["net.merged"] == 1
    merged = next(m for m in report["mismatches"] if m["category"] == "net.merged")
    assert merged["side"] == "reference"
    assert merged["net"] == {"layout": None, "reference": "A"}


def test_split_net_reports_net_split(tmp_path):
    """layout splits MID into two distinct nets (MID1/MID2) where the
    reference uses a single shared MID."""
    reference_path = _write(tmp_path / "ref.spice", _BUF2_REFERENCE_SPICE)
    layout_spice = """
.subckt buf2 A Y VPWR VGND
M1 MID1 A VGND VGND nfet W=0.65U L=0.15U
M2 MID2 A VPWR VPWR pfet W=1.0U L=0.15U
M3 Y MID1 VGND VGND nfet W=0.65U L=0.15U
M4 Y MID2 VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""
    layout_path = _write(tmp_path / "layout.spice", layout_spice)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "buf2"},
            "reference": {"netlist": reference_path, "top": "buf2"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"]["net.split"] == 1
    split = next(m for m in report["mismatches"] if m["category"] == "net.split")
    assert split["side"] == "layout"
    assert split["net"]["reference"] is None


def test_isolated_extra_device_reports_net_unmatched_not_merged_or_split(tmp_path):
    """Removing an entire, otherwise-unconnected device is a plain
    net.unmatched -- no co-occurring renamed pairing exists to justify a
    merge/split classification (see the module's documented heuristic).
    `C`/`D` are declared pins on both sides, but a genuinely
    device-connected net (reference) and a genuinely floating net (layout)
    are structurally distinct objects, so both directions report -- one
    entry per side, per net."""
    reference_spice = """
.subckt cell A B C D
R1 A B 1k
R2 C D 1k
.ends
"""
    layout_spice = """
.subckt cell A B C D
R1 A B 1k
.ends
"""
    reference_path = _write(tmp_path / "ref.spice", reference_spice)
    layout_path = _write(tmp_path / "layout.spice", layout_spice)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "cell"},
            "reference": {"netlist": reference_path, "top": "cell"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert "net.merged" not in report["category_counts"]
    assert "net.split" not in report["category_counts"]
    assert report["category_counts"]["net.unmatched"] == 4  # C, D on each side
    assert report["category_counts"]["device.unmatched"] == 1


# --------------------------------------------------------------------------- #
# hints.same_nets / hints.equivalent_pins
# --------------------------------------------------------------------------- #


def test_same_nets_hint_resolves_an_otherwise_ambiguous_match(tmp_path):
    """Two symmetric, otherwise-indistinguishable devices dangling off two
    unconnected nets: the comparer resolves the ambiguity on its own (the
    match verdict is unaffected either way -- a swap is a valid isomorphism)
    but reports it as a `topology`/warning finding. `hints.same_nets` pins
    the correspondence down, eliminating the warning entirely."""
    reference_spice = """
.subckt cell VDD
R1 VDD P1 1k
R2 VDD P2 1k
.ends
"""
    layout_spice = """
.subckt cell VDD
R1 VDD $1 1k
R2 VDD $2 1k
.ends
"""
    reference_path = _write(tmp_path / "ref.spice", reference_spice)
    layout_path = _write(tmp_path / "layout.spice", layout_spice)

    no_hint_path = _write_request(
        tmp_path / "no_hint.json",
        {
            "layout": {"netlist": layout_path, "top": "cell"},
            "reference": {"netlist": reference_path, "top": "cell"},
        },
    )
    no_hint = run_lvs(no_hint_path)
    assert no_hint["status"] == "match"
    assert no_hint["mismatch_count"] == 2
    assert no_hint["category_counts"] == {"topology": 2}
    assert all(m["severity"] == "warning" for m in no_hint["mismatches"])

    with_hint_path = _write_request(
        tmp_path / "with_hint.json",
        {
            "layout": {"netlist": layout_path, "top": "cell"},
            "reference": {"netlist": reference_path, "top": "cell"},
            "hints": {"same_nets": [["$1", "P1"], ["$2", "P2"]]},
        },
    )
    with_hint = run_lvs(with_hint_path)
    assert with_hint["status"] == "match"
    assert with_hint["mismatch_count"] == 0
    assert with_hint["mismatches"] == []


def test_same_nets_hint_unknown_layout_net_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "hints": {"same_nets": [["NOPE", "VGND"]]},
        },
    )
    with pytest.raises(LvsError, match="layout net 'NOPE' not found"):
        run_lvs(path)


def test_same_nets_hint_unknown_reference_net_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "hints": {"same_nets": [["VGND", "NOPE"]]},
        },
    )
    with pytest.raises(LvsError, match="reference net 'NOPE' not found"):
        run_lvs(path)


def test_equivalent_pins_hint_unknown_subcircuit_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "hints": {"equivalent_pins": {"nope": [["A", "Y"]]}},
        },
    )
    with pytest.raises(LvsError, match="circuit 'nope' not found"):
        run_lvs(path)


def test_equivalent_pins_hint_unknown_pin_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "hints": {"equivalent_pins": {"INV": [["NOPE1", "NOPE2"]]}},
        },
    )
    with pytest.raises(LvsError, match="pin 'NOPE1' not found"):
        run_lvs(path)


# --------------------------------------------------------------------------- #
# options.keep_extracted / inline extraction (layout.file + layout.deck)
# --------------------------------------------------------------------------- #


def test_inline_extraction_composes_extract_and_compare(tmp_path):
    """`layout.file` + `layout.deck` runs `klt extract`'s core extraction and
    compares the in-memory result directly -- extraction and compare compose
    in one call, per the request contract."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(SKY130_INV), "sky130", output=reference_path)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(SKY130_INV), "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    # sky130 also declares a `pnp` bipolar entry (issue #223); this cell
    # draws none, so the comparer's one mismatch is the pre-existing
    # "class declared but zero instances on both sides" warning (#204's own
    # downgrade-to-warning precedent) -- not a real topology defect. sky130's
    # two MiM-capacitor entries (issue #225) do *not* add further mismatches
    # here: unlike a bipolar entry, `extract.py` never even registers a
    # capacitor device class on a layout that draws no matching marker at
    # all (see `CapacitorDevice`'s "empty region -> skipped entirely" note),
    # so this cap-free cell's extracted netlist carries no such class for
    # the comparer to report as unmatched.
    assert report["mismatch_count"] == 1
    assert report["mismatches"][0]["severity"] == "warning"
    # `layout.file` + `layout.deck` (inline extraction) was given -- echoes
    # the sky130 deck's device-class coverage (issue #221, extended by
    # #223/#225) -- what the deck can *recognise*, independent of what this
    # particular cap-free cell's netlist actually registered above.
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
    ]
    assert (
        report["environment"]["extracted_netlist"] is None
    )  # keep_extracted defaults False


def test_keep_extracted_writes_and_echoes_intermediate_netlist(tmp_path):
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(SKY130_INV), "sky130", output=reference_path)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(SKY130_INV), "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
            "options": {"keep_extracted": True},
        },
    )
    report = run_lvs(path)

    extracted_path = report["environment"]["extracted_netlist"]
    assert extracted_path is not None
    assert Path(extracted_path).is_file()
    assert (
        Path(extracted_path) == tmp_path / ".klt" / "lvs" / f"{extracted['top']}.spice"
    )


def test_keep_extracted_is_a_noop_for_pre_extracted_layout(tmp_path):
    """`options.keep_extracted` only applies when `layout.file` triggers
    inline extraction -- a pre-extracted `layout.netlist` has nothing
    intermediate to retain."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"keep_extracted": True},
        },
    )
    report = run_lvs(path)
    assert report["environment"]["extracted_netlist"] is None


# --------------------------------------------------------------------------- #
# Direct classification unit tests (fake logger, no real NetlistComparer run)
# --------------------------------------------------------------------------- #


class _FakeNamed:
    """Stands in for a `klayout.db.Net`/`Device` object: `.expanded_name()`."""

    def __init__(self, name: str) -> None:
        self._name = name

    def expanded_name(self) -> str:
        return self._name


class _FakeParam:
    def __init__(self, name: str, param_id: int) -> None:
        self.name = name
        self._id = param_id

    def id(self) -> int:
        return self._id


class _FakeDeviceClass:
    def __init__(self, name: str, params: list[_FakeParam] | None = None) -> None:
        self.name = name
        self._params = params or []

    def parameter_definitions(self) -> list[_FakeParam]:
        return self._params


class _FakeDevice:
    def __init__(self, name: str, device_class: _FakeDeviceClass, values: dict) -> None:
        self._name = name
        self._device_class = device_class
        self._values = values

    def expanded_name(self) -> str:
        return self._name

    def device_class(self) -> _FakeDeviceClass:
        return self._device_class

    def parameter(self, param_id: int) -> float:
        return self._values[param_id]


class _FakeLogger:
    """Mimics the public attribute surface `lvs._build_mismatches` reads
    from the real compare logger (see `lvs._make_compare_logger`)."""

    def __init__(self) -> None:
        self.net_mismatches: list[tuple] = []
        self.device_mismatches: list[tuple] = []
        self.param_mismatches: list[tuple] = []
        self.class_mismatches: list[tuple] = []
        self.pin_mismatches: list[tuple] = []
        self.circuit_mismatches: list[tuple] = []
        self.subcircuit_mismatches: list[tuple] = []
        self.device_class_mismatches: list[tuple] = []
        self.ambiguous_net_matches: list[tuple] = []


def test_build_mismatches_device_class_mismatch():
    a = _FakeDevice("M1", _FakeDeviceClass("nfet"), {})
    b = _FakeDevice("M1", _FakeDeviceClass("pfet"), {})
    logger = _FakeLogger()
    logger.class_mismatches.append((a, b))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_DEVICE_CLASS
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert entry["device"] == {"layout": "M1", "reference": "M1", "class": "nfet"}


def test_build_mismatches_pin_unmatched_both_sides():
    logger = _FakeLogger()
    logger.pin_mismatches.append((_FakeNamed("EXTRA"), None))
    logger.pin_mismatches.append((None, _FakeNamed("MISSING")))

    entries = lvs._build_mismatches(logger)
    assert len(entries) == 2
    sides = {e["side"] for e in entries}
    assert sides == {"layout", "reference"}
    assert all(e["category"] == lvs.CATEGORY_PIN_UNMATCHED for e in entries)


def test_build_mismatches_circuit_and_subcircuit_and_device_class_topology():
    logger = _FakeLogger()
    logger.circuit_mismatches.append((_FakeNamed("SUBA"), None, "msg"))
    logger.subcircuit_mismatches.append((None, _FakeNamed("X1")))
    logger.device_class_mismatches.append((_FakeNamed("weirdclass"), None))

    entries = lvs._build_mismatches(logger)
    assert len(entries) == 3
    assert all(e["category"] == lvs.CATEGORY_TOPOLOGY for e in entries)
    sides = sorted(e["side"] for e in entries)
    assert sides == ["layout", "layout", "reference"]


def test_build_mismatches_ambiguous_net_is_warning_topology():
    logger = _FakeLogger()
    logger.ambiguous_net_matches.append((_FakeNamed("N1"), _FakeNamed("N2")))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_TOPOLOGY
    assert entry["severity"] == "warning"
    assert entry["net"] == {"layout": "N1", "reference": "N2"}


def test_build_mismatches_param_diff_reports_only_differing_parameter():
    device_class = _FakeDeviceClass(
        "nfet", [_FakeParam("W", 0), _FakeParam("L", 1), _FakeParam("AS", 2)]
    )
    a = _FakeDevice("M1", device_class, {0: 650000.0, 1: 150000.0, 2: 169000.0})
    b = _FakeDevice("M1", device_class, {0: 780000.0, 1: 150000.0, 2: 169000.0})
    logger = _FakeLogger()
    logger.param_mismatches.append((a, b))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_DEVICE_PROPERTY
    assert entry["property"] == {
        "name": "w_um",
        "layout": 650000.0,
        "reference": 780000.0,
    }


def test_build_mismatches_empty_logger_produces_no_entries():
    assert lvs._build_mismatches(_FakeLogger()) == []


def test_mismatches_sort_order():
    device_class = _FakeDeviceClass("nfet")
    logger = _FakeLogger()
    logger.pin_mismatches.append((_FakeNamed("Z"), None))
    logger.device_mismatches.append((_FakeDevice("M2", device_class, {}), None))
    logger.net_mismatches.append((_FakeNamed("B"), None))
    logger.net_mismatches.append((_FakeNamed("A"), None))

    entries = lvs._build_mismatches(logger)
    categories = [e["category"] for e in entries]
    # Sorted by category first: device.unmatched < net.unmatched < pin.unmatched
    assert categories == [
        lvs.CATEGORY_DEVICE_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
        lvs.CATEGORY_PIN_UNMATCHED,
    ]
    # Within net.unmatched, sorted by net.layout ("A" before "B")
    net_names = [
        e["net"]["layout"]
        for e in entries
        if e["category"] == lvs.CATEGORY_NET_UNMATCHED
    ]
    assert net_names == ["A", "B"]


# --------------------------------------------------------------------------- #
# Corpus round-trip: known-good self-compare + a deliberately-broken variant
# --------------------------------------------------------------------------- #


GF180_CLKINV = CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"

#: (deck, corpus file, nfet-body short pattern) -- the nfet body is tied to
#: the deck's global substrate net (`vsubs`) via a distinct SPICE token in
#: both curated decks (see `docs/cli/extract.md` -> "Coverage"); replacing
#: " <source-net> vsubs" with " <source-net> <source-net>" shorts the body
#: into the source net -- a topology corruption (curator negative control
#: #1), not a parameter tweak.
_CORPUS_CELLS = [
    ("sky130", SKY130_INV, " VGND vsubs", " VGND VGND"),
    ("gf180mcu", GF180_CLKINV, " VSS vsubs", " VSS VSS"),
]


def test_corpus_files_are_present():
    assert SKY130_INV.is_file()
    assert GF180_CLKINV.is_file()


@pytest.mark.parametrize(
    "deck,corpus_file,_short_from,_short_to",
    _CORPUS_CELLS,
    ids=[c[0] for c in _CORPUS_CELLS],
)
def test_corpus_known_good_cell_matches_cleanly(
    tmp_path, deck, corpus_file, _short_from, _short_to
):
    """Epic #153's own success criterion, on both curated decks: a
    known-good corpus cell's extracted netlist matches its own reference
    cleanly."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(corpus_file), deck, output=reference_path)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(corpus_file), "deck": deck},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)
    assert report["status"] == "match"
    # Both decks also declare a bipolar entry (issue #223: sky130's `pnp`,
    # gf180mcu's `bjt`) that these MOS-only corpus cells draw none of -- the
    # comparer's one mismatch is the pre-existing "class declared but zero
    # instances on both sides" warning (#204's downgrade-to-warning
    # precedent), not a real topology defect.
    assert report["mismatch_count"] == 1
    assert report["mismatches"][0]["severity"] == "warning"


@pytest.mark.parametrize(
    "deck,corpus_file,short_from,short_to",
    _CORPUS_CELLS,
    ids=[c[0] for c in _CORPUS_CELLS],
)
def test_corpus_deliberately_broken_reference_reports_mismatches(
    tmp_path, deck, corpus_file, short_from, short_to
):
    """Epic #153's other success criterion, on both curated decks: a
    deliberately-broken pair reports structured mismatches."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(corpus_file), deck, output=reference_path)

    text = Path(reference_path).read_text()
    assert short_from in text  # sanity: the corruption actually applies
    broken_text = text.replace(short_from, short_to)
    broken_path = tmp_path / "broken.spice"
    broken_path.write_text(broken_text)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(corpus_file), "deck": deck},
            "reference": {"netlist": str(broken_path), "top": extracted["top"]},
        },
    )
    report = run_lvs(path)
    assert report["status"] == "mismatch"
    assert report["mismatch_count"] > 0
    assert all(
        m["category"]
        in {
            lvs.CATEGORY_NET_UNMATCHED,
            lvs.CATEGORY_NET_MERGED,
            lvs.CATEGORY_NET_SPLIT,
            lvs.CATEGORY_DEVICE_UNMATCHED,
            lvs.CATEGORY_TOPOLOGY,
        }
        for m in report["mismatches"]
    )


# --------------------------------------------------------------------------- #
# Regression: #201 -- unused device-class registration is not a real mismatch
# --------------------------------------------------------------------------- #


def test_lvs_unused_device_class_mismatch_is_warning_not_error(tmp_path, monkeypatch):
    """`klt extract` always registers both `nfet` and `pfet` device classes
    (see `extract.py`'s ``_extract_netlist`` core), even when a layout only ever
    instantiates one polarity -- the `diff_pair` generator's plain (non-
    `mirror`) output is entirely `nfet` (the same shape as the #196
    gen-compose bring-up's all-`nfet` composed circuit that discovered this).
    A round-tripped SPICE reference netlist naturally never mentions the
    unused `pfet` class (a `NetlistSpiceReader`-parsed netlist only registers
    a device class actually referenced by a device line), so the comparer's
    `device_class_mismatch` event fires for `pfet` on a otherwise-clean
    self-compare. This must report `severity: "warning"`, not `"error"` --
    the documented "`error` breaks equivalence" contract must hold in
    practice, and `status` must still be `"match"`."""
    from klayout_tools import pdk
    from klayout_tools.extract import run_extract
    from klayout_tools.gen import generate

    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])

    pdk_root = tmp_path / "pdk_install"
    (pdk_root / "sky130A" / "libs.tech").mkdir(parents=True)

    layout_path = tmp_path / "diff_pair.gds"
    generate(
        {
            "generator": "diff_pair",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "options": {"output": str(layout_path)},
        }
    )

    reference_path = tmp_path / "ref.spice"
    extracted = run_extract(str(layout_path), "sky130", output=str(reference_path))

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(layout_path), "deck": "sky130"},
            "reference": {"netlist": str(reference_path), "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    device_class_entries = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_TOPOLOGY and "device class" in m["description"]
    ]
    assert device_class_entries, (
        "expected a topology entry for the unused pfet device class"
    )
    assert all(m["severity"] == "warning" for m in device_class_entries)
    assert not any(m["severity"] == "error" for m in report["mismatches"])


# --------------------------------------------------------------------------- #
# CLI: exit codes, --format text/json
# --------------------------------------------------------------------------- #


def test_cli_match_exits_zero_json(tmp_path, capsys):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    exit_code = main(["lvs", request_path, "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "match"


def test_cli_mismatch_exits_three_json(tmp_path, capsys):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice", _INVERTER_SPICE.replace("W=1.0U", "W=2.0U")
    )
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    exit_code = main(["lvs", request_path, "--format", "json"])
    assert exit_code == 3
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "mismatch"
    assert out["mismatch_count"] == 1


def test_cli_text_default_format(tmp_path, capsys):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    exit_code = main(["lvs", request_path])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status: match" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_error_exits_one_with_json_error(tmp_path, capsys):
    request_path = _write_request(
        tmp_path / "request.json",
        {"layout": {"netlist": "missing.spice"}, "reference": {"netlist": "y"}},
    )
    exit_code = main(["lvs", request_path, "--format", "json"])
    assert exit_code == 1
    err = json.loads(capsys.readouterr().err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "lvs"
    assert "layout netlist not found" in err["error"]["message"]


def test_cli_error_exits_one_text_format(tmp_path, capsys):
    request_path = _write_request(
        tmp_path / "request.json",
        {"layout": {"netlist": "missing.spice"}, "reference": {"netlist": "y"}},
    )
    exit_code = main(["lvs", request_path])
    assert exit_code == 1
    err = capsys.readouterr().err
    assert err.startswith("klt lvs:")


def test_cli_missing_request_arg_is_usage_error():
    with pytest.raises(SystemExit) as exc_info:
        main(["lvs"])
    assert exc_info.value.code == 2
