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

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed
from klayout_tools import lvs, pdk
from klayout_tools.cli import main
from klayout_tools.lvs import LvsError, run_lvs

#: Real-binary integration gate for the netgen engine tests below, mirroring
#: `tests/test_sim.py`'s own `HAVE_NGSPICE`/`_SKIP_NO_NGSPICE` pattern for
#: `ngspice`. Unlike `ngspice`, CI does not currently install `netgen` (it
#: has no simple package-manager install -- see the dated addendum in
#: `docs/design/lvs-extraction-spike.md` for the from-source build this
#: issue required), so this tier skips cleanly in CI and only runs on a
#: machine that already has a `netgen` binary on `$PATH`.
HAVE_NETGEN = shutil.which("netgen") is not None
_SKIP_NO_NETGEN = pytest.mark.skipif(
    not HAVE_NETGEN, reason="netgen is not installed on this machine"
)

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

# Issue #282's exact reproduction: the *minimal* cell -- a 5-pin inverter
# whose NMOS body and PMOS well sit on their own nets (`vsubs`, `NW1`),
# distinct from the supplies. Unlike `_INVERTER_SPICE` (bodies tied straight
# to `VPWR`/`VGND`), this leaves `NetlistComparer` nothing to anchor a device
# pairing on when a parameter is the only difference.
_MINIMAL_INVERTER_SPICE = """
.SUBCKT inv A Y VDD VSS vsubs
M1 Y A VSS vsubs nfet L=0.5U W=1U
M2 Y A VDD NW1 pfet L=0.5U W=2U
.ENDS inv
"""

_BUF2_REFERENCE_SPICE = """
.subckt buf2 A Y VPWR VGND
M1 MID A VGND VGND nfet W=0.65U L=0.15U
M2 MID A VPWR VPWR pfet W=1.0U L=0.15U
M3 Y MID VGND VGND nfet W=0.65U L=0.15U
M4 Y MID VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

# Folded/multi-finger NMOS: two parallel W=0.325U fingers sharing all four
# terminals (gate/source/drain/body) -- electrically identical to a single
# W=0.65U device, the same as `_INVERTER_SPICE`'s NMOS (issue #261).
_MULTIFINGER_LAYOUT_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.325U L=0.15U
M1B Y A VGND VGND nfet W=0.325U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

# Split/interleaved matched pair: a single logical NMOS drawn as two
# segments about a shared axis (same connectivity shape as the multi-finger
# case above, from `combine_devices()`'s point of view -- both are
# parallel-connected devices with matching gate/S/D/B nets).
_SPLIT_LAYOUT_SPICE = """
.subckt inv A Y VPWR VGND
M1A Y A VGND VGND nfet W=0.2U L=0.15U
M1B Y A VGND VGND nfet W=0.45U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

# Negative control: two genuinely-distinct NMOS devices in parallel (a
# different gate length on each finger) that `combine_devices()` must NOT
# merge -- and a matching two-device reference, so the compare is expected
# to match with or without `options.combine_devices`.
_DISTINCT_PARALLEL_LAYOUT_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.325U L=0.15U
M1B Y A VGND VGND nfet W=0.325U L=0.30U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""

# Issue #500's repro sketch: a lumped 3k resistor drawn as a series string of
# three identical 1k segments chained through two interior nodes (`n1`, `n2`)
# that carry no pins. `combine_devices()` folds the three segments into one
# resistor and empties `n1`/`n2` (0 terminals, 0 pins) -- the interior nets
# whose leftover presence used to inflate `counts.nets.layout`.
_SERIES_RESISTOR_STRING_LAYOUT_SPICE = """
.subckt rstring A Y
R1 Y n1 1k
R2 n1 n2 1k
R3 n2 A 1k
.ends
"""

_SERIES_RESISTOR_STRING_REFERENCE_SPICE = """
.subckt rstring A Y
R1 Y A 3k
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


# --------------------------------------------------------------------------- #
# `request` argument forms: path / inline JSON / stdin (issue #232)
# --------------------------------------------------------------------------- #


def test_load_request_arg_inline_json_matches_file_form(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    path = _write_request(tmp_path / "request.json", request)

    from_file = run_lvs(path)
    from_inline = run_lvs(json.dumps(request))

    assert from_inline["status"] == from_file["status"] == "match"
    assert from_inline["mismatch_count"] == from_file["mismatch_count"] == 0


def test_load_request_arg_stdin_matches_file_form(tmp_path, monkeypatch):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    path = _write_request(tmp_path / "request.json", request)
    from_file = run_lvs(path)

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    from_stdin = run_lvs("-")

    assert from_stdin["status"] == from_file["status"] == "match"
    assert from_stdin["mismatch_count"] == from_file["mismatch_count"] == 0


def test_load_request_arg_inline_relative_paths_resolve_against_cwd(
    tmp_path, monkeypatch
):
    _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    monkeypatch.chdir(tmp_path)

    request = {
        "layout": {"netlist": "layout.spice", "top": "inv"},
        "reference": {"netlist": "ref.spice", "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


def test_load_request_arg_stdin_relative_paths_resolve_against_cwd(
    tmp_path, monkeypatch
):
    _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    monkeypatch.chdir(tmp_path)

    request = {
        "layout": {"netlist": "layout.spice", "top": "inv"},
        "reference": {"netlist": "ref.spice", "top": "inv"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    report = run_lvs("-")
    assert report["status"] == "match"


def test_load_request_arg_file_relative_paths_resolve_against_request_dir_not_cwd(
    tmp_path, monkeypatch
):
    """Contrast case for the two tests above: the *file* form still
    resolves relative paths against the request file's own directory, even
    when the current working directory is somewhere else entirely -- only
    the inline/stdin forms (no request file to anchor to) fall back to the
    CWD."""
    request_dir = tmp_path / "req_dir"
    request_dir.mkdir()
    _write(request_dir / "layout.spice", _INVERTER_SPICE)
    _write(request_dir / "ref.spice", _INVERTER_SPICE)
    request_path = _write_request(
        request_dir / "request.json",
        {
            "layout": {"netlist": "layout.spice", "top": "inv"},
            "reference": {"netlist": "ref.spice", "top": "inv"},
        },
    )

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    report = run_lvs(request_path)
    assert report["status"] == "match"


def test_load_request_arg_malformed_inline_json_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(LvsError, match="valid inline JSON"):
        run_lvs("{not valid json")


def test_load_request_arg_malformed_stdin_json_raises(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not valid json"))
    with pytest.raises(LvsError, match="stdin request is not valid JSON"):
        run_lvs("-")


def test_load_request_arg_inline_non_object_raises():
    with pytest.raises(LvsError, match="inline request must contain a JSON object"):
        run_lvs("[1, 2, 3]")


def test_load_request_arg_stdin_non_object_raises(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("[1, 2, 3]"))
    with pytest.raises(LvsError, match="stdin request must contain a JSON object"):
        run_lvs("-")


def test_unsupported_engine_raises(tmp_path):
    path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "hspice",
            "layout": {"netlist": "x"},
            "reference": {"netlist": "y"},
        },
    )
    with pytest.raises(LvsError, match="unsupported engine 'hspice'"):
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

    prov = report["provenance"]
    assert set(prov.keys()) == {
        "klt_version",
        "klayout_version",
        "pdk",
        "deck",
        "input",
    }
    assert isinstance(prov["klt_version"], str)
    # LVS is topological -- no PDK resolved; and a pre-extracted `layout.netlist`
    # involves no extraction deck, so `deck` is null (mirrors device_classes).
    assert prov["pdk"] is None
    assert prov["deck"] is None
    # Issue #331: `lvs` already pins its two inputs via its own
    # `environment.layout_sha256`/`reference_sha256` above (unchanged by this
    # issue), so `provenance.input` stays null rather than duplicating that.
    assert prov["input"] is None


@pytest.mark.parametrize(
    ("deck_name", "m1_layer", "m1_label_layer"),
    [
        ("gf180mcu", (34, 0), (34, 10)),  # Metal1 / Metal1 pin
        ("sky130", (68, 20), (68, 5)),  # met1.drawing / met1.pin
    ],
)
def test_deviceless_labelled_layout_compares_instead_of_erroring(
    tmp_path, deck_name, m1_layer, m1_label_layer
):
    """Issue #539's exact repro, on both curated decks: a layout with zero
    recognised devices but two distinct, labelled metal1 nets (e.g. a bond
    pad, seal ring, or RDL segment) used to purge to an entirely empty
    netlist inside `_extract_netlist`, so `_select_circuit` (`lvs.py`) hit
    its "no top circuit" `LvsError` instead of ever reaching
    `NetlistComparer` -- see `lvs._select_circuit`. After the fix, `klt
    lvs` performs a real topology comparison against a reference netlist
    for exactly this circuit (matching nets/pins, zero devices on both
    sides) rather than erroring."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("pad_ring")
    m1 = layout.layer(*m1_layer)
    m1_label = layout.layer(*m1_label_layer)

    def box(x0, y0, x1, y1):
        return kdb.Box(
            kdb.DPoint(x0, y0).to_itype(layout.dbu),
            kdb.DPoint(x1, y1).to_itype(layout.dbu),
        )

    top.shapes(m1).insert(box(0, 0, 1, 1))
    top.shapes(m1_label).insert(kdb.DText("A", kdb.DTrans(kdb.DPoint(0.5, 0.5))))
    top.shapes(m1).insert(box(5, 0, 6, 1))
    top.shapes(m1_label).insert(kdb.DText("B", kdb.DTrans(kdb.DPoint(5.5, 0.5))))
    gds_path = str(tmp_path / "pad_ring.gds")
    layout.write(gds_path)

    reference_path = _write(
        tmp_path / "pad_ring.spice",
        ".SUBCKT pad_ring A B\n.ENDS pad_ring\n",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": gds_path, "deck": deck_name, "top": "pad_ring"},
            "reference": {"netlist": reference_path, "top": "pad_ring"},
        },
    )

    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["nets"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["pins"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["devices"] == {"layout": 0, "reference": 0, "matched": 0}
    assert not any(m["severity"] == "error" for m in report["mismatches"])
    correspondence = {
        (entry["layout"], entry["reference"]) for entry in report["net_correspondence"]
    }
    assert correspondence == {("A", "A"), ("B", "B")}


def test_net_correspondence_lists_all_matched_nets_with_pin_flag(tmp_path):
    """Issue #311: a clean match's ``net_correspondence`` names every
    matched net pair. All four of `_INVERTER_SPICE`'s nets (`A`, `Y`,
    `VPWR`, `VGND`) are declared pins of `.subckt inv`, so every entry's
    `pin` is `True` and, since layout/reference share the same names here,
    `layout` == `reference` on every entry."""
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

    assert report["status"] == "match"
    correspondence = report["net_correspondence"]
    assert len(correspondence) == 4 == report["counts"]["nets"]["matched"]
    assert {entry["reference"] for entry in correspondence} == {
        "A",
        "Y",
        "VPWR",
        "VGND",
    }
    for entry in correspondence:
        assert entry["layout"] == entry["reference"]
        assert entry["pin"] is True
    # Sorted by (reference, layout) for determinism.
    assert [entry["reference"] for entry in correspondence] == sorted(
        entry["reference"] for entry in correspondence
    )


def test_net_correspondence_partial_on_mismatch(tmp_path):
    """Issue #311: even on `status: "mismatch"`, `net_correspondence`
    reports the pairs that *did* match -- here `A`/`B` (both sides keep
    `R1`), while `C`/`D` never appear (the layout dropped `R2` entirely, so
    those two nets have no counterpart to pair with)."""
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
    correspondence = report["net_correspondence"]
    assert {(e["layout"], e["reference"]) for e in correspondence} == {
        ("A", "A"),
        ("B", "B"),
    }
    assert all(entry["pin"] is True for entry in correspondence)


def test_net_correspondence_includes_ambiguous_matches(tmp_path):
    """Issue #311: an ambiguously-resolved net pairing (see
    `test_same_nets_hint_resolves_an_otherwise_ambiguous_match`) is still a
    real match the comparer made -- it must appear in `net_correspondence`,
    not just the declared-pin `VDD` net that matched unambiguously."""
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
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "cell"},
            "reference": {"netlist": reference_path, "top": "cell"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    correspondence = report["net_correspondence"]
    assert len(correspondence) == 3 == report["counts"]["nets"]["matched"]

    by_reference = {entry["reference"]: entry for entry in correspondence}
    assert by_reference["VDD"] == {"layout": "VDD", "reference": "VDD", "pin": True}
    assert {"P1", "P2"} <= by_reference.keys()
    # The ambiguous pair's layout side is `$1`/`$2` (unlabelled, neither a
    # declared pin), backslash-escaped (issue #1162) to match the written
    # netlist's own node spelling -- which reference name each happened to
    # resolve to is not asserted, since the comparer's own tie-break is not
    # part of this module's documented contract.
    assert {by_reference["P1"]["layout"], by_reference["P2"]["layout"]} == {
        "\\$1",
        "\\$2",
    }
    assert by_reference["P1"]["pin"] is False
    assert by_reference["P2"]["pin"] is False


def test_net_correspondence_scopes_dedup_by_circuit(tmp_path):
    """Issue #311 regression: in a multi-circuit hierarchy, two distinct
    subcircuits routinely share a local net name. The dedup key must be
    scoped by circuit, not by bare net name -- otherwise those unrelated
    nets silently collapse into one entry, dropping the other's
    correspondence and reporting the wrong `pin` flag for whichever won
    the dedup race.

    Here `cellA` and `cellB` both have local nets named `IN`, `MID`, and
    `OUT`, but only `cellA` declares `MID` as a pin; in `cellB` `MID` is a
    purely internal net. A name-only dedup key collapsed all three shared
    names (11 matched nets -> 8 entries) and reported a single `MID` entry
    with a `pin` flag that was right for at most one of the two circuits.
    With a circuit-scoped key every matched net appears, the documented
    invariant `len(net_correspondence) == counts.nets.matched` holds, and
    both `MID` nets are represented with their own (differing) `pin`
    flags."""
    hierarchy_spice = """
.subckt cellA IN MID OUT
R1 IN MID 1k
R2 MID OUT 1k
.ends
.subckt cellB IN OUT
R1 IN MID 1k
R2 MID OUT 1k
.ends
.subckt top
X1 a1 a2 a3 cellA
X2 b1 b2 cellB
.ends
"""
    layout_path = _write(tmp_path / "layout.spice", hierarchy_spice)
    reference_path = _write(tmp_path / "ref.spice", hierarchy_spice)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    correspondence = report["net_correspondence"]
    # The invariant the field documents in docs/cli/lvs.md: every matched
    # net has exactly one correspondence entry, even across a hierarchy
    # with cross-circuit name collisions.
    assert len(correspondence) == report["counts"]["nets"]["matched"] == 11

    # `MID` exists in both cellA (a declared pin) and cellB (internal). A
    # name-only key would have kept just one; the scoped key keeps both,
    # each with its own `pin` flag.
    mid_entries = [e for e in correspondence if e["reference"] == "MID"]
    assert len(mid_entries) == 2
    assert {e["pin"] for e in mid_entries} == {True, False}
    # The other cross-circuit collisions (`IN`, `OUT`) are likewise kept
    # as two entries apiece rather than merged into one.
    assert len([e for e in correspondence if e["reference"] == "IN"]) == 2
    assert len([e for e in correspondence if e["reference"] == "OUT"]) == 2


def _make_single_nmos_layout(extra_drain_label: str = "Y2"):
    """One NMOS (sky130 layers, no `nwell` drawn) whose drain pad carries
    two distinct li1 labels (`Y` and ``extra_drain_label``) -- the same
    multi-label-merge shape `tests/test_extract.py`'s
    `_make_inverter_layout(extra_y_label=...)` fixture exercises, trimmed to
    a single device since a self-compare (see the test below) needs no
    second transistor. KLayout's `Net.expanded_name()` joins the two labels
    with a comma at extraction time -- the real-object case
    `spice_safe_net_name`/`_name_or_none` (issue #696) must convert, unlike
    a name typed directly into a hand-written SPICE fixture."""
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (no nwell -> nmos)
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (gate)

    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (source side)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (drain side)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (source pad)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (drain pad)

    label(66, 20, "A", 1000, 500)  # gate, directly on poly
    label(67, 5, "VGND", 200, 500)  # source pad
    label(67, 5, "Y", 1800, 500)  # drain pad, label 1
    label(67, 5, extra_drain_label, 1650, 500)  # drain pad, label 2 (merges)

    return layout


def test_net_correspondence_spells_merged_net_like_the_netlist(tmp_path):
    """Issue #696: `net_correspondence[].layout`'s spelling of a real,
    layout-extracted label-merged net (`Y`/`Y2` on one drain pad, joined by
    KLayout as `Y,Y2` internally) matches `klt extract`'s own `nets[].name`/
    the written netlist -- `Y|Y2`, not the un-escaped comma form -- so a
    caller can join `net_correspondence` straight into `klt extract`'s
    report by exact net name."""
    from klayout_tools.extract import run_extract

    layout = _make_single_nmos_layout()
    gds_path = tmp_path / "nmos.gds"
    layout.write(str(gds_path))

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(gds_path), "sky130", output=reference_path)
    assert extracted["merged_net_labels"] == [{"net": "Y|Y2", "labels": ["Y", "Y2"]}]

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(gds_path), "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    correspondence = {
        (entry["layout"], entry["reference"]) for entry in report["net_correspondence"]
    }
    assert ("Y|Y2", "Y|Y2") in correspondence
    assert not any(
        "Y,Y2" in (layout, reference) for layout, reference in correspondence
    )


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
# device.property on the minimal cell (issue #282: the degraded
# `device.unmatched` + `net.unmatched` cascade recovered into the documented
# category)
# --------------------------------------------------------------------------- #


def _minimal_inverter_report(tmp_path, reference_text: str):
    layout_path = _write(tmp_path / "layout.spice", _MINIMAL_INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", reference_text)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    return run_lvs(path)


def test_minimal_cell_width_change_reports_device_property(tmp_path):
    """Issue #282's reproduction verbatim: on a two-device inverter with its
    own substrate/well nets, changing only `M2`'s `W` used to report
    `{"device.unmatched": 2, "net.unmatched": 4}` and no `device.property`
    entry at all -- the report pointed at connectivity when the defect was a
    width."""
    report = _minimal_inverter_report(
        tmp_path, _MINIMAL_INVERTER_SPICE.replace("W=2U", "W=4U")
    )

    assert report["status"] == "mismatch"
    assert report["category_counts"]["device.property"] == 1

    (entry,) = [
        m for m in report["mismatches"] if m["category"] == lvs.CATEGORY_DEVICE_PROPERTY
    ]
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert entry["device"]["class"] == "PFET"
    assert entry["property"] == {
        "name": "w_um",
        "layout": pytest.approx(2.0),
        "reference": pytest.approx(4.0),
    }

    # The parameter entry is the only `error`; the unmatched device pair and
    # the four nets it dragged in are collateral, reported as warnings so a
    # caller filtering on severity reads the root cause first.
    errors = [m for m in report["mismatches"] if m["severity"] == "error"]
    assert errors == [entry]
    collateral = [m for m in report["mismatches"] if m["severity"] == "warning"]
    assert sorted(m["category"] for m in collateral) == [
        lvs.CATEGORY_DEVICE_UNMATCHED,
        lvs.CATEGORY_DEVICE_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
        lvs.CATEGORY_NET_UNMATCHED,
    ]


@pytest.mark.parametrize("width", ["W=2.001U", "W=2.5U", "W=4U"])
def test_minimal_cell_width_change_reports_device_property_at_any_delta(
    tmp_path, width
):
    """Not a tolerance effect -- the issue reports the same degraded cascade
    from `W=2.001U` through `W=4U`."""
    report = _minimal_inverter_report(
        tmp_path, _MINIMAL_INVERTER_SPICE.replace("W=2U", width)
    )
    assert report["status"] == "mismatch"
    assert report["category_counts"]["device.property"] == 1


def test_minimal_cell_length_change_reports_device_property(tmp_path):
    """The recovery names whichever parameter actually differs, not just `W`."""
    report = _minimal_inverter_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE.replace(
            "M2 Y A VDD NW1 pfet L=0.5U", "M2 Y A VDD NW1 pfet L=0.6U"
        ),
    )
    (entry,) = [
        m for m in report["mismatches"] if m["category"] == lvs.CATEGORY_DEVICE_PROPERTY
    ]
    assert entry["property"]["name"] == "l_um"
    assert entry["property"]["layout"] == pytest.approx(0.5)
    assert entry["property"]["reference"] == pytest.approx(0.6)


def test_minimal_cell_width_change_survives_a_renamed_well_net(tmp_path):
    """The well net is dangling (only the mismatching device touches it), so
    the recovery pairs it structurally rather than by name -- the realistic
    layout-vs-schematic case, where extraction rarely reproduces the
    schematic's own net name."""
    report = _minimal_inverter_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE.replace(
            "NW1 pfet L=0.5U W=2U", "NWELL pfet L=0.5U W=4U"
        ),
    )
    assert report["category_counts"]["device.property"] == 1


def test_minimal_cell_rewired_device_is_not_reported_as_a_parameter_defect(tmp_path):
    """Negative control for the recovery itself: a genuine connectivity
    defect (the PMOS source moved from `VDD` to `VSS`) with no parameter
    change must stay a connectivity finding."""
    report = _minimal_inverter_report(
        tmp_path, _MINIMAL_INVERTER_SPICE.replace("M2 Y A VDD NW1", "M2 Y A VSS NW1")
    )
    assert report["status"] == "mismatch"
    assert "device.property" not in report["category_counts"]
    assert all(m["severity"] == "error" for m in report["mismatches"])


def test_minimal_cell_rewired_and_resized_device_is_not_downgraded(tmp_path):
    """A width change *and* a rewire together: the terminal nets no longer
    correspond, so the recovery declines and nothing is downgraded to a
    warning -- masking the connectivity defect would be the one genuinely
    harmful outcome."""
    report = _minimal_inverter_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE.replace(
            "M2 Y A VDD NW1 pfet L=0.5U W=2U", "M2 Y A VSS NW1 pfet L=0.5U W=4U"
        ),
    )
    assert report["status"] == "mismatch"
    assert "device.property" not in report["category_counts"]
    assert all(m["severity"] == "error" for m in report["mismatches"])


def test_minimal_cell_device_class_swap_is_not_a_parameter_defect(tmp_path):
    """Two unmatched devices of *different* classes are not a degraded
    parameter pair, however small the circuit."""
    report = _minimal_inverter_report(
        tmp_path, _MINIMAL_INVERTER_SPICE.replace("NW1 pfet", "NW1 nfet")
    )
    assert report["status"] == "mismatch"
    assert "device.property" not in report["category_counts"]


def test_minimal_cell_two_wrong_widths_are_not_recovered(tmp_path):
    """The recovery is scoped to a *single* unmatched device pair -- with two
    corrupted devices the comparer's event stream no longer identifies which
    layout device belongs to which reference device, so nothing is claimed."""
    report = _minimal_inverter_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE.replace("W=1U", "W=1.5U").replace("W=2U", "W=4U"),
    )
    assert report["status"] == "mismatch"
    assert "device.property" not in report["category_counts"]


def test_minimal_cell_identical_netlists_still_match(tmp_path):
    """The recovery never manufactures a finding: an unchanged self-compare
    of the same minimal cell is still clean."""
    report = _minimal_inverter_report(tmp_path, _MINIMAL_INVERTER_SPICE)
    assert report["status"] == "match"
    assert report["mismatches"] == []


# --------------------------------------------------------------------------- #
# options.parameter_tolerance (issue #589): an opted-in *design* tolerance,
# implemented as snap-and-recompare rather than by widening the float-noise
# epsilon (`status` is always `compare()`'s own boolean)
# --------------------------------------------------------------------------- #


def _tolerance_report(
    tmp_path,
    layout_text: str,
    reference_text: str,
    tolerance=None,
    top: str = "inv",
    **request_extra,
):
    """Run a two-netlist compare, optionally with `options.parameter_tolerance`.

    `tolerance=None` omits the option entirely (the default path), so the same
    helper drives both the opted-in cases and their default-behaviour
    regression controls.
    """
    layout_path = _write(tmp_path / "layout.spice", layout_text)
    reference_path = _write(tmp_path / "ref.spice", reference_text)
    request = {
        "layout": {"netlist": layout_path, "top": top},
        "reference": {"netlist": reference_path, "top": top},
    }
    if tolerance is not None:
        request["options"] = {"parameter_tolerance": tolerance}
    request.update(request_extra)
    return run_lvs(_write_request(tmp_path / "request.json", request))


# The issue's own motivating shape: a curated deck's resistor value is a
# 5-6-significant-figure SPICE-model fit, while the schematic reference's card
# was computed from the same model rounded to four figures. The drawn geometry
# is exactly right; the two numbers differ by ~0.04%.
_RESISTOR_LAYOUT_SPICE = """
.subckt rblock A Y
R1 A Y 1234.5
.ends
"""

_RESISTOR_REFERENCE_SPICE = """
.subckt rblock A Y
R1 A Y 1235.0
.ends
"""


def test_parameter_tolerance_omitted_is_echoed_as_null(tmp_path):
    """Default: the field is always present (null-not-omitted, this contract's
    convention) and the verdict is untouched."""
    report = _tolerance_report(tmp_path, _INVERTER_SPICE, _INVERTER_SPICE)
    assert report["parameter_tolerance"] is None
    assert report["status"] == "match"


def test_parameter_tolerance_default_leaves_a_small_delta_a_mismatch(tmp_path):
    """Regression guard for "default unchanged": the same 0.09% width delta
    that the opt-in below absorbs is still a hard `device.property` mismatch
    when the option is omitted."""
    report = _tolerance_report(
        tmp_path, _INVERTER_SPICE, _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U")
    )

    assert report["parameter_tolerance"] is None
    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}
    assert lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED not in report["category_counts"]


def test_parameter_tolerance_absorbs_an_in_tolerance_delta(tmp_path):
    """The core case: a 0.09% width delta inside a 0.1% tolerance reports
    `status: "match"` -- asserted on `status` directly, not merely on the
    absence of a `device.property` entry, since only a real second
    `NetlistComparer.compare()` can move the verdict."""
    report = _tolerance_report(
        tmp_path,
        _INVERTER_SPICE,
        _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U"),
        tolerance=0.001,
    )

    assert report["status"] == "match"
    assert report["parameter_tolerance"] == 0.001
    assert "device.property" not in report["category_counts"]


def test_parameter_tolerance_discloses_every_absorbed_parameter(tmp_path):
    """No silent absorption: the tolerated difference is reported in-band as a
    `severity: "warning"` entry carrying *both* original values (the reference
    side's pre-snap number, not the snapped one)."""
    report = _tolerance_report(
        tmp_path,
        _INVERTER_SPICE,
        _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U"),
        tolerance=0.001,
    )

    assert report["category_counts"] == {lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED: 1}
    (entry,) = report["mismatches"]
    assert entry["category"] == lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED
    assert entry["severity"] == "warning"
    assert entry["side"] == "both"
    assert entry["device"]["class"] == "PFET"
    assert entry["property"] == {
        "name": "w_um",
        "layout": pytest.approx(1.0),
        "reference": pytest.approx(1.0009),
    }
    assert entry["details"]["tolerance"] == 0.001
    assert entry["details"]["relative_delta"] == pytest.approx(0.0008992, rel=1e-3)
    # A warning never changes the verdict, but it is still counted.
    assert report["status"] == "match"
    assert report["mismatch_count"] == 1


def test_parameter_tolerance_does_not_absorb_an_out_of_tolerance_delta(tmp_path):
    """Negative control -- the tolerance must not be a rubber stamp. An 8%
    delta with the same 0.1% tolerance set reports exactly what it reports
    today."""
    report = _tolerance_report(
        tmp_path,
        _INVERTER_SPICE,
        _INVERTER_SPICE.replace("W=1.0U", "W=1.08U"),
        tolerance=0.001,
    )

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}
    (entry,) = report["mismatches"]
    assert entry["severity"] == "error"
    assert entry["property"]["reference"] == pytest.approx(1.08)


def test_parameter_tolerance_absorbs_the_minimal_cell_degraded_pairing(tmp_path):
    """Issue #282's degraded path participates too: on the minimal cell the
    comparer never emits a parameter event at all (it reports one unmatched
    device per side plus collateral net mismatches), so a tolerance wired only
    into the clean-pairing path would leave this -- the shape real extracted
    layouts hit whenever a body/well net is not shorted to a rail -- stuck at
    `mismatch` however wide the tolerance."""
    report = _tolerance_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE,
        _MINIMAL_INVERTER_SPICE.replace("W=2U", "W=2.0018U"),
        tolerance=0.001,
    )

    assert report["status"] == "match"
    assert report["category_counts"] == {lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED: 1}
    (entry,) = report["mismatches"]
    assert entry["property"]["layout"] == pytest.approx(2.0)
    assert entry["property"]["reference"] == pytest.approx(2.0018)
    # The collateral `device.unmatched`/`net.unmatched` cascade the default
    # path reports for this fixture is gone -- the second compare pairs
    # everything.
    assert "device.unmatched" not in report["category_counts"]
    assert "net.unmatched" not in report["category_counts"]


def test_parameter_tolerance_minimal_cell_out_of_tolerance_still_mismatches(tmp_path):
    """Negative control for the degraded path: a 100% delta is untouched, and
    the issue #282 recovery still reports it the way it does today."""
    report = _tolerance_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE,
        _MINIMAL_INVERTER_SPICE.replace("W=2U", "W=4U"),
        tolerance=0.001,
    )

    assert report["status"] == "mismatch"
    assert report["category_counts"]["device.property"] == 1
    assert lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED not in report["category_counts"]


def test_parameter_tolerance_never_masks_a_connectivity_defect(tmp_path):
    """The one genuinely harmful outcome: a rewired device (no parameter
    change at all) must stay a hard connectivity finding with the tolerance
    set."""
    report = _tolerance_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE,
        _MINIMAL_INVERTER_SPICE.replace("M2 Y A VDD NW1", "M2 Y A VSS NW1"),
        tolerance=0.5,
    )

    assert report["status"] == "mismatch"
    assert lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED not in report["category_counts"]
    assert all(m["severity"] == "error" for m in report["mismatches"])


def test_parameter_tolerance_leaves_a_pair_alone_when_one_parameter_is_too_far(
    tmp_path,
):
    """All-or-nothing per device pair: a pair whose `W` is in tolerance but
    whose `L` is not is left completely alone, so it reports both parameters
    exactly as it does today rather than dropping the in-tolerance one from a
    report that still says `mismatch`."""
    report = _tolerance_report(
        tmp_path,
        _INVERTER_SPICE,
        _INVERTER_SPICE.replace("W=1.0U L=0.15U", "W=1.0009U L=0.30U"),
        tolerance=0.001,
    )

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 2}
    names = sorted(m["property"]["name"] for m in report["mismatches"])
    assert names == ["l_um", "w_um"]


def test_parameter_tolerance_absorbs_a_rounded_resistor_value(tmp_path):
    """The issue's own motivating case, end to end: a resistor whose extracted
    value is a model fit (`1234.5`) against a reference card rounded to four
    figures (`1235.0`) -- ~0.04%, far inside any manufacturing tolerance."""
    report = _tolerance_report(
        tmp_path,
        _RESISTOR_LAYOUT_SPICE,
        _RESISTOR_REFERENCE_SPICE,
        tolerance=0.001,
        top="rblock",
    )

    assert report["status"] == "match"
    assert report["category_counts"] == {lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED: 1}
    (entry,) = report["mismatches"]
    assert entry["property"]["name"] == "r"
    assert entry["property"]["layout"] == pytest.approx(1234.5)
    assert entry["property"]["reference"] == pytest.approx(1235.0)


def test_parameter_tolerance_inherits_the_single_degraded_pair_limit(tmp_path):
    """Known limitation, pinned so it is explicit rather than surprising: the
    tolerance rides on `_degraded_param_pair`'s recovery for the minimal-cell
    shape, and that recovery is deliberately scoped to a *single* unmatched
    device pair (issue #282). Two devices whose values are both in tolerance on
    a circuit small enough to degrade therefore still report `mismatch` -- the
    same verdict the default path reports for that input today. Circuits big
    enough for the comparer to pair devices structurally (the ordinary case)
    go through the clean-pairing path, which handles any number of pairs."""
    layout = """
.subckt rdiv A MID Y
R1 A MID 1234.5
R2 MID Y 2469.0
.ends
"""
    reference = """
.subckt rdiv A MID Y
R1 A MID 1235.0
R2 MID Y 2470.0
.ends
"""
    report = _tolerance_report(tmp_path, layout, reference, tolerance=0.001, top="rdiv")

    assert report["status"] == "mismatch"
    assert lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED not in report["category_counts"]


def test_parameter_tolerance_of_zero_changes_nothing(tmp_path):
    """An explicit `0` is a valid (if pointless) tolerance: nothing is
    absorbed, and the verdict matches the omitted-option path exactly."""
    report = _tolerance_report(
        tmp_path,
        _INVERTER_SPICE,
        _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U"),
        tolerance=0,
    )

    assert report["parameter_tolerance"] == 0.0
    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}


def test_parameter_tolerance_on_a_clean_compare_adds_nothing(tmp_path):
    """The option never manufactures a disclosure: an identical self-compare
    with a tolerance set is still a bare, empty `match`."""
    report = _tolerance_report(
        tmp_path, _INVERTER_SPICE, _INVERTER_SPICE, tolerance=0.01
    )

    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["parameter_tolerance"] == 0.01


def test_parameter_tolerance_composes_with_hints_same_nets(tmp_path):
    """The second `compare()` runs on a fresh `NetlistComparer`, so the
    caller's hints have to be re-declared on it -- otherwise a tolerance-
    assisted compare would silently drop them."""
    reference = _MINIMAL_INVERTER_SPICE.replace("W=2U", "W=2.0018U").replace(
        "VSS vsubs", "VSS SUBSTRATE"
    )
    report = _tolerance_report(
        tmp_path,
        _MINIMAL_INVERTER_SPICE,
        reference.replace(".SUBCKT inv A Y VDD VSS vsubs", ".SUBCKT inv A Y VDD VSS"),
        tolerance=0.001,
        hints={"same_nets": [["VSUBS", "SUBSTRATE"]]},
    )

    assert report["status"] == "match"
    assert "hints.rejected" not in report["category_counts"]
    assert report["category_counts"] == {lvs.CATEGORY_DEVICE_PARAMETER_TOLERATED: 1}


@pytest.mark.parametrize("value", ["0.001", True, [0.001], {"W": 0.001}])
def test_parameter_tolerance_non_numeric_raises(tmp_path, value):
    """Malformed request, not a silent fallback to the default -- a tolerance
    the caller believes is in force but is not is the worst failure mode this
    option has."""
    with pytest.raises(LvsError, match="options.parameter_tolerance must be a number"):
        _tolerance_report(tmp_path, _INVERTER_SPICE, _INVERTER_SPICE, tolerance=value)


def test_parameter_tolerance_negative_raises(tmp_path):
    with pytest.raises(LvsError, match="must not be negative"):
        _tolerance_report(tmp_path, _INVERTER_SPICE, _INVERTER_SPICE, tolerance=-0.1)


def test_parameter_tolerance_at_or_above_one_raises(tmp_path):
    """A relative tolerance of 1.0 would call almost any two values equal --
    rejected rather than honoured as a rubber stamp."""
    with pytest.raises(LvsError, match="must be below 1.0"):
        _tolerance_report(tmp_path, _INVERTER_SPICE, _INVERTER_SPICE, tolerance=1.0)


def test_parameter_tolerance_explicit_null_is_the_default(tmp_path):
    """`"parameter_tolerance": null` is indistinguishable from omitting it."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice", _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U")
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"parameter_tolerance": None},
        },
    )
    report = run_lvs(path)

    assert report["parameter_tolerance"] is None
    assert report["status"] == "mismatch"


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


def test_same_nets_hint_rejected_pairing_is_reported(tmp_path):
    """Issue #499: a `hints.same_nets` pair the comparer refuses (the two
    nets are not topologically equivalent, unlike the symmetric-swap case
    above) must not be silently dropped -- it is reported as one
    `hints.rejected`/`error` `mismatches[]` entry naming both sides exactly
    as declared."""
    reference_spice = """
.subckt cell VDD GND
R1 VDD P1 1k
R2 GND P2 2k
.ends
"""
    layout_spice = """
.subckt cell VDD GND
R1 VDD $1 1k
R2 GND $2 2k
.ends
"""
    reference_path = _write(tmp_path / "ref.spice", reference_spice)
    layout_path = _write(tmp_path / "layout.spice", layout_spice)

    # $1 sits on the VDD/1k leg; forcing it equal to P2 (the GND/2k leg) is
    # not a valid isomorphism -- the comparer must refuse this assertion.
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "cell"},
            "reference": {"netlist": reference_path, "top": "cell"},
            "hints": {"same_nets": [["$1", "P2"]]},
        },
    )
    response = run_lvs(request_path)

    rejected = [m for m in response["mismatches"] if m["category"] == "hints.rejected"]
    assert len(rejected) == 1
    (entry,) = rejected
    assert entry["severity"] == "error"
    assert entry["net"] == {"layout": "$1", "reference": "P2"}
    assert response["category_counts"]["hints.rejected"] == 1
    assert response["status"] == "mismatch"


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
# options.flatten_reference / options.flatten_layout (issue #1085): `klt
# extract` always extracts a *flat* layout netlist, so a hierarchical
# reference (one leaf `.subckt` plus N instance calls of it -- the shape a
# macro built by tiling one verified leaf cell naturally takes) can never
# structurally match it without one side being flattened first.
# --------------------------------------------------------------------------- #

#: Two chained instances of a one-resistor leaf, both declared -- the
#: hierarchical form. `top`'s three pins (P1/P2/P3) chain X1's B pin to X2's
#: A pin through the shared net P2.
_HIERARCHICAL_DIVIDER_SPICE = """
.subckt leaf A B
R1 A B 1k
.ends
.subckt top P1 P2 P3
X1 P1 P2 leaf
X2 P2 P3 leaf
.ends
"""

#: The same two-resistor chain, already flattened -- exactly what `klt
#: extract`'s always-flat extraction (or a hand-written flat netlist) would
#: produce for the layout above.
_FLAT_DIVIDER_SPICE = """
.subckt top P1 P2 P3
R1 P1 P2 1k
R2 P2 P3 1k
.ends
"""


def test_flat_layout_against_hierarchical_reference_is_a_topology_mismatch(tmp_path):
    """Bug-reproducing baseline (issue #1085): without any flatten option, a
    flat layout-side netlist compared against a hierarchical reference of
    the same topology is a hard, undiagnosable `topology` mismatch on both
    sides -- `NetlistComparer` has no subcircuit-call circuit on the layout
    side to pair against the reference's, so it declines to even attempt a
    per-net/per-device compare. This is the exact failure mode the issue
    reports; the tests below show `options.flatten_reference`/
    `options.flatten_layout` turning it into a real verdict."""
    layout_path = _write(tmp_path / "layout.spice", _FLAT_DIVIDER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _HIERARCHICAL_DIVIDER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"topology": 2}
    assert report["counts"]["devices"] == {"layout": 2, "reference": 0, "matched": 0}
    assert all(m["category"] == "topology" for m in report["mismatches"])
    assert "topology.flattened" not in report["category_counts"]

    # Issue #1132: `category_error_counts`/`error_count` split `error`
    # entries out of `category_counts`'s combined error+warning tally -- on
    # this all-error report they agree with `category_counts` exactly.
    assert report["category_error_counts"] == {"topology": 2}
    assert report["error_count"] == 2
    # Each unmatched-*circuit* (as opposed to unmatched subcircuit
    # *instance*, covered by
    # `test_subcircuit_instance_mismatch_names_the_instance_and_its_type`
    # below) entry names the circuit(s) that failed to pair, so this
    # `topology` finding is attributable without a side-channel netlist
    # diff -- `instance`/`subcircuit` stay `null` (this is a whole-circuit
    # mismatch, not a subcircuit-instance one).
    assert [m["circuit"] for m in report["mismatches"]] == [
        {"layout": "TOP", "reference": "TOP"},
        {"layout": None, "reference": "LEAF"},
    ]
    assert all(
        m["instance"] is None and m["subcircuit"] is None for m in report["mismatches"]
    )


def test_subcircuit_instance_mismatch_names_the_instance_and_its_type(tmp_path):
    """Issue #1132: an unmatched subcircuit *instance* -- as opposed to a
    whole unmatched circuit, covered by
    `test_flat_layout_against_hierarchical_reference_is_a_topology_mismatch`
    above -- names the containing circuit, the instance's own name, and the
    circuit it instantiates, not just an anonymous `topology` entry. Both
    sides are hierarchical and otherwise identical; the reference adds one
    extra `leaf` instance (`X3`) on two brand-new nets, so `NetlistComparer`
    can still structurally pair `top`'s other two instances and reports the
    extra one as a genuine one-sided `subcircuit_mismatch` event (not a
    whole-`top`-circuit `circuit_mismatch`, the scenario above)."""
    layout_path = _write(tmp_path / "layout.spice", _HIERARCHICAL_DIVIDER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice",
        _HIERARCHICAL_DIVIDER_SPICE.replace(
            "X2 P2 P3 leaf\n.ends",
            "X2 P2 P3 leaf\nX3 N4 N5 leaf\n.ends",
        ),
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    (entry,) = [m for m in report["mismatches"] if m["category"] == "topology"]
    assert entry["description"] == (
        "subcircuit instance could not be matched to a counterpart"
    )
    assert entry["severity"] == "error"
    assert entry["side"] == "reference"
    assert entry["circuit"] == {"layout": None, "reference": "TOP"}
    assert entry["instance"] == {"layout": None, "reference": "3"}
    assert entry["subcircuit"] == {"layout": None, "reference": "LEAF"}
    # Collateral `net.unmatched` findings for X3's two dangling nets --
    # not this issue's concern, but pinned so a future change to how those
    # collaterals are classified doesn't silently change this test's shape.
    assert report["category_counts"]["net.unmatched"] == 2
    assert report["category_error_counts"] == {"net.unmatched": 2, "topology": 1}
    assert report["error_count"] == 3


def test_unclassified_mismatch_safety_net_entry_carries_every_field(
    tmp_path, monkeypatch
):
    """The safety-net entry `run_lvs` synthesises when `compare()` reports a
    mismatch but this module's own event classification produced nothing (see
    docs/cli/lvs.md -> "A seventh entry") must carry the *same* key set as
    every classification site's entry -- `circuit`/`instance`/`subcircuit`
    included, `null` rather than absent.

    It was hand-rolled as a dict literal before issue #1132's review, so it
    silently went missing the three keys that issue added and a consumer
    subscripting `mismatch["circuit"]` hit a `KeyError` on this one path. It
    now goes through `_mismatch()` like every other site; this test pins that
    (and would fail again on the next field added out of band).

    Forcing the branch takes a monkeypatch: by construction it only fires on a
    gap in the module's own event coverage, which no real fixture can produce
    on demand. `_build_mismatches` is stubbed to the empty classification it
    would return in such a gap, over a genuinely mismatching compare so
    `compare()` itself still returns the "mismatch" verdict the safety net
    exists to honour.
    """
    monkeypatch.setattr(lvs, "_build_mismatches", lambda *a, **k: [])
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

    # The verdict is still the engine's, never re-derived from the (now
    # empty) classification -- the invariant the safety net protects.
    assert report["status"] == "mismatch"
    (entry,) = report["mismatches"]
    assert entry["category"] == "topology"
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert "no further detail available" in entry["description"]

    # Key set identical to a `_mismatch()`-built entry's, with every optional
    # object present-and-null rather than omitted.
    assert set(entry) == set(lvs._mismatch("topology", "error", "d", "both"))
    for field in ("net", "device", "property", "details"):
        assert entry[field] is None
    for field in ("circuit", "instance", "subcircuit"):
        assert field in entry, f"{field} omitted, not null"
        assert entry[field] is None

    assert report["error_count"] == 1
    assert report["category_error_counts"] == {"topology": 1}


def test_flatten_reference_turns_the_topology_mismatch_into_a_match(tmp_path):
    """`options.flatten_reference: true` collapses the reference's two
    subcircuit-call circuits into its top circuit before comparing, so it
    now structurally matches the already-flat layout side."""
    layout_path = _write(tmp_path / "layout.spice", _FLAT_DIVIDER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _HIERARCHICAL_DIVIDER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
            "options": {"flatten_reference": True},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["nets"]["matched"] == 3
    assert len(report["net_correspondence"]) == 3

    # Disclosed, not silent: a `"match"` reached after an opted-in flatten is
    # never indistinguishable from one reached against the original
    # hierarchy (mirrors `device.parameter_tolerated`/`device.bulk_reconciled`'s
    # own transparency precedent).
    assert report["category_counts"] == {"topology.flattened": 1}
    [flattened] = report["mismatches"]
    assert flattened["category"] == "topology.flattened"
    assert flattened["severity"] == "warning"
    assert flattened["side"] == "reference"
    assert "2 circuit(s)" in flattened["description"]
    assert "1 top-level circuit(s)" in flattened["description"]


def test_flatten_layout_turns_a_hierarchical_layout_netlist_into_a_match(tmp_path):
    """Symmetric counterpart (issue #1085's item 2): a pre-extracted
    `layout.netlist` can itself be hierarchical -- `options.flatten_layout`
    flattens that side instead, matching against an already-flat
    reference."""
    layout_path = _write(tmp_path / "layout.spice", _HIERARCHICAL_DIVIDER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _FLAT_DIVIDER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
            "options": {"flatten_layout": True},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["category_counts"] == {"topology.flattened": 1}
    [flattened] = report["mismatches"]
    assert flattened["side"] == "layout"
    assert "options.flatten_layout" in flattened["description"]


def test_flatten_options_default_false_leaves_status_unchanged(tmp_path):
    """Omitting both flags entirely reproduces today's unconditional
    behaviour byte-for-byte -- same baseline as
    `test_flat_layout_against_hierarchical_reference_is_a_topology_mismatch`,
    just asserted via the explicit `{}` `options` form some callers use."""
    layout_path = _write(tmp_path / "layout.spice", _FLAT_DIVIDER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _HIERARCHICAL_DIVIDER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
            "options": {"flatten_reference": False, "flatten_layout": False},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"topology": 2}


def test_flatten_reference_is_a_silent_noop_on_an_already_flat_netlist(tmp_path):
    """`options.flatten_reference`/`options.flatten_layout` on a netlist that
    already has only its top circuit (no subcircuit calls to substitute) is a
    true no-op: `Netlist.flatten()` leaves the circuit count unchanged, so no
    `topology.flattened` disclosure is added and the verdict is identical to
    the option being omitted -- a caller who always opts in (e.g. as a
    blanket default) never pays a spurious-warning cost on an
    already-flat pair."""
    layout_path = _write(tmp_path / "layout.spice", _FLAT_DIVIDER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _FLAT_DIVIDER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
            "options": {"flatten_reference": True, "flatten_layout": True},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["mismatch_count"] == 0


def _make_leaf_nmos_cell(layout, name: str = "LEAF"):
    """One NMOS (sky130 layers), gate/source/drain labelled `A`/`VGND`/`Y` --
    the minimal single-device leaf cell used by the `CellInstArray` test
    below. Mirrors `_make_single_nmos_layout` above, but builds a standalone
    leaf *cell* (not a whole `Layout`) so it can be instanced twice via one
    `CellInstArray`."""
    import klayout.db as kdb

    cell = layout.create_cell(name)

    def draw(layer, datatype, box):
        cell.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        cell.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff.drawing (no nwell -> nmos)
    draw(66, 20, kdb.Box(800, -200, 1200, 1200))  # poly.drawing (gate)
    draw(66, 44, kdb.Box(100, 300, 300, 700))  # licon1 (source side)
    draw(66, 44, kdb.Box(1700, 300, 1900, 700))  # licon1 (drain side)
    draw(67, 20, kdb.Box(0, 200, 400, 800))  # li1 (source pad)
    draw(67, 20, kdb.Box(1600, 200, 2000, 800))  # li1 (drain pad)

    label(66, 20, "A", 1000, 500)  # gate
    label(67, 5, "VGND", 200, 500)  # source pad
    label(67, 5, "Y", 1800, 500)  # drain pad

    return cell


def test_flatten_reference_matches_a_real_cell_inst_array_layout(tmp_path):
    """End-to-end reproduction of the issue's own repro shape: a real GDS
    layout with one leaf cell instanced twice via a single `CellInstArray`
    (the issue's "N instances of one cell" macro pattern, trimmed to N=2),
    compared -- via inline extraction (`layout.file` + `layout.deck`) --
    against a hand-written hierarchical reference netlist declaring exactly
    that shape (one `leaf` `.subckt` plus two `X` instance calls).
    `klt extract` always extracts flat (see `docs/cli/extract.md`'s
    "flat, not hierarchical" note), so without `options.flatten_reference`
    this is the issue's undiagnosable `topology` mismatch; with it, a real
    `"match"` verdict."""
    import klayout.db as kdb

    layout = kdb.Layout()
    leaf = _make_leaf_nmos_cell(layout)
    top = layout.create_cell("TOP")
    top.insert(
        kdb.CellInstArray(
            leaf.cell_index(),
            kdb.Trans(kdb.Vector(0, 0)),
            kdb.Vector(5000, 0),
            kdb.Vector(0, 5000),
            2,
            1,
        )
    )
    gds_path = str(tmp_path / "array.gds")
    layout.write(gds_path)

    # Hand-written hierarchical reference: one leaf subcircuit (gate/drain/
    # source/bulk terminals, matching `klt extract`'s own D-G-S-B M-card
    # convention), instanced twice by `top`. Device parameters copied
    # verbatim from what `klt extract`'s sky130 deck derives for this exact
    # leaf geometry (2um x 1um diff, 0.4um x 1um poly gate) -- an exact
    # parameter match isn't this test's point (`options.parameter_tolerance`
    # covers that), so the reference is written to already agree.
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.subckt leaf A Y VGND SUB
M1 Y A VGND SUB nfet L=0.4U W=1U AS=0.8P AD=0.8P PS=3.6U PD=3.6U
.ends
.subckt top VGND VGND1 Y Y1 SUB
X1 g1 Y VGND SUB leaf
X2 g2 Y1 VGND1 SUB leaf
.ends
""",
    )

    baseline_path = _write_request(
        tmp_path / "baseline_request.json",
        {
            "layout": {"file": gds_path, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    baseline_report = run_lvs(baseline_path)
    assert baseline_report["status"] == "mismatch"
    assert baseline_report["category_counts"]["topology"] >= 2

    flattened_path = _write_request(
        tmp_path / "flattened_request.json",
        {
            "layout": {"file": gds_path, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "top"},
            "options": {"flatten_reference": True},
        },
    )
    flattened_report = run_lvs(flattened_path)

    assert flattened_report["status"] == "match"
    assert flattened_report["counts"]["devices"] == {
        "layout": 2,
        "reference": 2,
        "matched": 2,
    }
    assert "topology.flattened" in flattened_report["category_counts"]
    assert flattened_report["category_counts"]["topology.flattened"] == 1


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
    # draws none, so the comparer contributes one mismatch, the pre-existing
    # "class declared but zero instances on both sides" warning (#204's own
    # downgrade-to-warning precedent) -- not a real topology defect. sky130's
    # two MiM-capacitor entries (issue #225) do *not* add further mismatches
    # here: unlike a bipolar entry, `extract.py` never even registers a
    # capacitor device class on a layout that draws no matching marker at
    # all (see `CapacitorDevice`'s "empty region -> skipped entirely" note),
    # so this cap-free cell's extracted netlist carries no such class for
    # the comparer to report as unmatched. Issue #281 adds a second,
    # unrelated warning: sky130 draws no distinct NMOS substrate/tap layer,
    # so every NMOS body terminal lands on the deck's synthetic substrate
    # net (`device.body_unverified`) -- sky130's own PMOS `tap` layer gives
    # PMOS bodies a real net, so no PMOS warning fires here.
    assert report["mismatch_count"] == 2
    assert all(m["severity"] == "warning" for m in report["mismatches"])
    body_unverified = [
        m for m in report["mismatches"] if m["category"] == "device.body_unverified"
    ]
    assert len(body_unverified) == 1
    assert body_unverified[0]["device"]["class"] == "nfet"
    # `layout.file` + `layout.deck` (inline extraction) was given -- echoes
    # the sky130 deck's device-class coverage (issue #221, extended by
    # #223/#225/#222) -- what the deck can *recognise*, independent of what
    # this particular cap-free cell's netlist actually registered above.
    assert report["device_classes"] == [
        "nfet",
        "pfet",
        "pnp",
        "sky130_fd_pr__model__cap_mim",
        "sky130_fd_pr__model__cap_mim_m4",
        "resistor",
    ]
    assert (
        report["environment"]["extracted_netlist"] is None
    )  # keep_extracted defaults False

    # Inline extraction => the layout-side extraction deck is pinned in the
    # shared provenance block, hashed for reproducibility.
    assert report["provenance"]["deck"]["name"] == "sky130"
    assert report["provenance"]["deck"]["content_hash"].startswith("sha256:")


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
# device.body_unverified warning (issue #281): MOS body terminals extracted
# onto deck-synthesized nets, not real schematic ones -- see docs/cli/lvs.md
# and extract.py's `nfet_body`/`connect_global` handling.
# --------------------------------------------------------------------------- #


def test_body_unverified_warns_nmos_only_on_sky130(tmp_path):
    """sky130 draws no distinct NMOS substrate/tap layer, so every NMOS body
    lands on the deck's synthetic substrate net -- but sky130's own PMOS
    `tap` layer (65/44) gives PMOS bodies a real, named net, so no PMOS
    warning fires here."""
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

    body_entries = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
    ]
    assert len(body_entries) == 1
    entry = body_entries[0]
    assert entry["severity"] == "warning"
    assert entry["side"] == "layout"
    assert entry["device"]["class"] == "nfet"


def _write_hier_inverter_gds(path: Path) -> str:
    """A minimal sky130 inverter whose gate ``A`` label lives inside an
    instanced sub-cell (issue #291), so flat extraction names the gate net
    ``A`` from below an instance boundary. Written to ``path`` and returned."""
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff (nmos active)
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff (pmos active)
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly (shared gate bar)
    for y0 in (0, 2000):
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon (S)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon (D)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D)
    draw(66, 44, kdb.Box(900, 1400, 1100, 1600))  # gate licon
    draw(67, 20, kdb.Box(850, 1350, 1150, 1650))  # gate li1

    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)
    label(67, 5, "VPWR", 200, 2500)
    label(67, 5, "Y", 1800, 2500)

    draw(65, 44, kdb.Box(-400, 2400, -200, 2600))  # tap (nwell tap)
    draw(66, 44, kdb.Box(-380, 2450, -220, 2550))  # licon over tap
    draw(67, 20, kdb.Box(-450, 2400, -150, 2600))  # li1 over tap
    label(64, 5, "VPB", -300, 2500)

    # Gate `A` label inside an instanced sub-cell (below the top cell).
    sub = layout.create_cell("A_LABEL")
    sub.shapes(layout.layer(67, 5)).insert(kdb.Text("A", kdb.Trans(1000, 1500)))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(0, 0)))

    layout.write(str(path))
    return str(path)


def test_top_cell_pins_request_field_keeps_subcell_label_internal(tmp_path):
    """Issue #291: `layout.top_cell_pins` threads through inline extraction so
    a net named only by a sub-cell label is not promoted to a layout-side pin
    the reference then has to declare. The layout-side pin count drops by
    exactly the one demoted gate pin (`A`)."""
    gds = _write_hier_inverter_gds(tmp_path / "hier.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)

    def _run(top_cell_pins: bool) -> dict:
        request = {
            "layout": {"file": gds, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "inv"},
        }
        if top_cell_pins:
            request["layout"]["top_cell_pins"] = True
        return run_lvs(_write_request(tmp_path / "request.json", request))

    default = _run(False)
    scoped = _run(True)

    assert scoped["counts"]["pins"]["layout"] == default["counts"]["pins"]["layout"] - 1


def _write_flat_inverter_gds(path: Path) -> str:
    """Same layout as ``_write_hier_inverter_gds`` but with the gate ``A``
    label drawn directly in the top cell (not inside an instanced
    sub-cell) -- exercises the per-*net* declared-pin-set mechanism (issue
    #514), which is orthogonal to ``top_cell_pins``'s per-*cell* one and
    applies even when interface and internal labels all live at the same
    hierarchy level (the natural shape of a hand-drawn overlay)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("TOP")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    draw(65, 20, kdb.Box(0, 0, 2000, 1000))  # diff (nmos active)
    draw(65, 20, kdb.Box(0, 2000, 2000, 3000))  # diff (pmos active)
    draw(64, 20, kdb.Box(-500, 1500, 2500, 3500))  # nwell
    draw(66, 20, kdb.Box(800, -200, 1200, 3200))  # poly (shared gate bar)
    for y0 in (0, 2000):
        draw(66, 44, kdb.Box(100, y0 + 300, 300, y0 + 700))  # licon (S)
        draw(66, 44, kdb.Box(1700, y0 + 300, 1900, y0 + 700))  # licon (D)
        draw(67, 20, kdb.Box(0, y0 + 200, 400, y0 + 800))  # li1 (S)
        draw(67, 20, kdb.Box(1600, y0 + 200, 2000, y0 + 800))  # li1 (D)
    draw(66, 44, kdb.Box(900, 1400, 1100, 1600))  # gate licon
    draw(67, 20, kdb.Box(850, 1350, 1150, 1650))  # gate li1

    label(67, 5, "VGND", 200, 500)
    label(67, 5, "Y", 1800, 500)
    label(67, 5, "VPWR", 200, 2500)
    label(67, 5, "Y", 1800, 2500)
    label(67, 5, "A", 1000, 1500)  # drawn directly in the top cell

    draw(65, 44, kdb.Box(-400, 2400, -200, 2600))  # tap (nwell tap)
    draw(66, 44, kdb.Box(-380, 2450, -220, 2550))  # licon over tap
    draw(67, 20, kdb.Box(-450, 2400, -150, 2600))  # li1 over tap
    label(64, 5, "VPB", -300, 2500)

    layout.write(str(path))
    return str(path)


def test_declared_pins_request_field_keeps_undeclared_label_internal(tmp_path):
    """Issue #514: `layout.declared_pins` threads through inline extraction so
    a net named by a label drawn directly in the top cell -- not below any
    instance boundary, so `top_cell_pins` (issue #291) cannot help -- stays
    internal when it is not in the declared set. The layout-side pin count
    drops by exactly the one demoted gate pin (`A`).

    (Declaring the pin set also demotes the synthesized substrate global
    (`vsubs`) if it is omitted -- unlike `top_cell_pins`, which never
    touches a `connect_global` net, `declared_pins` treats every promoted
    pin name uniformly, so a caller must include supply/global net names
    too if they are meant to stay interface pins. Both fixtures below
    include `vsubs` for that reason.)"""
    gds = _write_flat_inverter_gds(tmp_path / "flat.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)

    def _run(declared_pins: list[str] | None) -> dict:
        request = {
            "layout": {"file": gds, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "inv"},
        }
        if declared_pins is not None:
            request["layout"]["declared_pins"] = declared_pins
        return run_lvs(_write_request(tmp_path / "request.json", request))

    default = _run(None)
    scoped = _run(["VGND", "VPWR", "VPB", "Y", "vsubs"])  # omit "A"

    assert scoped["counts"]["pins"]["layout"] == default["counts"]["pins"]["layout"] - 1


def test_declared_pins_empty_list_raises(tmp_path):
    gds = _write_flat_inverter_gds(tmp_path / "flat.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": gds, "deck": "sky130", "declared_pins": []},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    with pytest.raises(LvsError, match="must not be empty"):
        run_lvs(path)


def test_declared_pins_wrong_type_raises(tmp_path):
    gds = _write_flat_inverter_gds(tmp_path / "flat.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "file": gds,
                "deck": "sky130",
                "declared_pins": "VGND,VPWR",  # must be a list, not a string
            },
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    with pytest.raises(LvsError, match="must be a list of net name strings"):
        run_lvs(path)


def _write_series_resistor_gds(path: Path) -> str:
    """Two sky130 drawn poly resistors (`res_generic_po`) wired in series
    through one interior tap, labelled purely for documentation -- issue
    #514's motivating scenario in miniature: a schematic that models this as
    a *single* lumped resistor, drawn as a chain with a human-meaningful
    interior node (the tap ladder repro's shape, minimised to two segments).
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("RSTRING")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    poly, marker, contact, metal, metal_label = (
        (66, 20),  # poly.drawing
        (66, 13),  # poly.res
        (66, 44),  # licon1.drawing
        (67, 20),  # li1.drawing
        (67, 5),  # li1.pin
    )

    # Segment 1: RA -- (marked 6um resistor body) -- interior tap.
    draw(*poly, kdb.Box(0, 0, 12000, 1000))
    draw(*marker, kdb.Box(3000, 0, 9000, 1000))
    draw(*contact, kdb.Box(1400, 400, 1600, 600))
    draw(*metal, kdb.Box(1100, 200, 1900, 800))
    label(*metal_label, "RA", 1500, 500)
    draw(*contact, kdb.Box(10400, 400, 10600, 600))
    # One continuous li1 strip spans both segments' inner contacts, merging
    # them into a single interior net -- labelled "TAP" for documentation.
    draw(*metal, kdb.Box(10100, 200, 14900, 800))
    label(*metal_label, "TAP", 12500, 500)

    # Segment 2: interior tap -- (marked 6um resistor body) -- RB.
    draw(*poly, kdb.Box(13000, 0, 25000, 1000))
    draw(*marker, kdb.Box(16000, 0, 22000, 1000))
    draw(*contact, kdb.Box(14400, 400, 14600, 600))
    draw(*contact, kdb.Box(23400, 400, 23600, 600))
    draw(*metal, kdb.Box(23100, 200, 23900, 800))
    label(*metal_label, "RB", 23500, 500)

    layout.write(str(path))
    return str(path)


#: Two 289.2-ohm (6 squares * 48.2 ohm/sq) `res_generic_po` segments folded
#: into the single 578.4-ohm lumped resistor the reference schematic models.
_SERIES_RESISTOR_GDS_REFERENCE_SPICE = """
.subckt rstring RA RB
R1 RA RB 578.4
.ends
"""


def test_declared_pins_unblocks_combine_devices_for_labelled_internal_tap(tmp_path):
    """End-to-end reproduction of issue #514's motivating scenario. Without
    `declared_pins`, the interior tap's documentation label promotes it to a
    top-level pin `options.combine_devices` will not fold through, so the
    two drawn resistor segments stay two separate layout-side devices.
    Declaring only the real interface (`RA`, `RB`) keeps the tap's *name*
    (still in `nets[]`/the written netlist) but demotes it to an internal
    net -- which is exactly what lets `combine_devices` fold the chain into
    a single device, without deleting the tap label. (The reference here is
    a plain hand-written SPICE `R` card, which `NetlistSpiceReader` always
    classes as generic `RES` -- a device-class-naming mismatch against the
    deck's own `res_generic_po` class that is orthogonal to this issue, so
    this asserts the fold itself via `counts.devices.layout`, not full
    `status: "match"`; see `test_declared_pins_request_field_keeps_
    undeclared_label_internal` above for a `status`-level assertion on the
    pin-demotion mechanism itself.)"""
    gds = _write_series_resistor_gds(tmp_path / "rstring.gds")
    reference_path = _write(
        tmp_path / "ref.spice", _SERIES_RESISTOR_GDS_REFERENCE_SPICE
    )

    def _run(declared_pins: list[str] | None) -> dict:
        request = {
            "layout": {"file": gds, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": "rstring"},
            "options": {"combine_devices": True},
        }
        if declared_pins is not None:
            request["layout"]["declared_pins"] = declared_pins
        return run_lvs(_write_request(tmp_path / "request.json", request))

    without_declared_pins = _run(None)
    assert without_declared_pins["counts"]["devices"]["layout"] == 2

    with_declared_pins = _run(["RA", "RB"])
    assert with_declared_pins["counts"]["devices"]["layout"] == 1
    assert with_declared_pins["counts"]["devices"]["reference"] == 1


def _write_res_high_po_gds(path: Path, l_um: float = 10.0) -> str:
    """A `w=1um` sky130 `res_high_po`-marked poly bar with an `l_um`-long
    marked (resistive) segment and a contacted, labelled 3um head at each
    end -- `tests/test_extract.py`'s `_make_sky130_res_high_po_layout`
    reproduced here so the LVS tier does not import another test module.

    `res_high_po` is the one sky130 flavour that declares a nonzero
    `ResistorDevice.fixed_offset_ohm` (issue #518), which is what makes it
    the fixture for issue #521's LVS half.
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("RES")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    head_um = 3.0
    l_nm = int(round(l_um * 1000))
    head_nm = int(round(head_um * 1000))
    total_nm = l_nm + 2 * head_nm

    draw(66, 20, kdb.Box(0, 0, total_nm, 1000))  # poly.drawing
    marked = kdb.Box(head_nm, 0, head_nm + l_nm, 1000)
    draw(66, 13, marked)  # poly.res
    draw(86, 20, marked.enlarged(200, 200))  # rpm
    draw(94, 20, marked.enlarged(200, 200))  # psdm

    for x, name in ((head_nm // 2, "RA"), (total_nm - head_nm // 2, "RB")):
        draw(66, 44, kdb.Box(x - 100, 400, x + 100, 600))  # licon1.drawing
        draw(67, 20, kdb.Box(x - 400, 200, x + 400, 800))  # li1.drawing
        label(67, 5, name, x, 500)  # li1.pin

    layout.write(str(path))
    return str(path)


#: The reference schematic for `_write_res_high_po_gds` at `l=10um`, written
#: from the *real* two-term PDK model: ngspice's own measurement of
#: `sky130_fd_pr__res_high_po` (`rhead` + `rbody`) at `w=1um`, the value
#: `decks/sky130.py`'s `sheet_rho_ohm_sq`/`fixed_offset_ohm` pair was fitted
#: to. Formatted the way `NetlistSpiceReader` parses a deck-flavoured
#: resistor (two terminals + bulk node + model name).
_RES_HIGH_PO_PDK_MODEL_SPICE = """
.subckt RES RA RB vsubs
R1 RA RB vsubs 3627.97760 res_high_po
.ends
"""

#: The same schematic written from the *pre-correction* single-term value
#: (`L / W * sheet_rho_ohm_sq` alone, no `fixed_offset_ohm`) -- the negative
#: control: whichever of the two references matches, the other must not.
_RES_HIGH_PO_BODY_ONLY_SPICE = """
.subckt RES RA RB vsubs
R1 RA RB vsubs 3248.27244 res_high_po
.ends
"""


@pytest.mark.parametrize(
    ("reference_spice", "expected_status"),
    [
        (_RES_HIGH_PO_PDK_MODEL_SPICE, "match"),
        (_RES_HIGH_PO_BODY_ONLY_SPICE, "mismatch"),
    ],
)
def test_inline_extraction_compares_fixed_offset_corrected_resistance(
    tmp_path, reference_spice, expected_status
):
    """Issue #521: `klt lvs`'s inline-extraction path feeds the *corrected*
    device parameter to `NetlistComparer`.

    `NetlistComparer` reads `device.parameter(...)` straight off the
    `kdb.Device` object, never through `klt extract`'s JSON `devices[]`
    view -- so while the deck's two-term corrections (issue #512's
    `perim_cap_f_um`, issue #518's `fixed_offset_ohm`) lived only in
    `_describe_devices`, a reference netlist carrying the PDK's real
    two-term value compared against the raw single-term extracted value and
    reported a spurious parameter mismatch.

    Both directions are asserted, so the test cannot pass by the comparer
    simply ignoring `R`: the real PDK-model reference matches (it did not
    before this fix), and the pre-correction body-only reference -- what the
    extractor used to hand the comparer -- now does not.
    """
    gds = _write_res_high_po_gds(tmp_path / "res_high_po.gds")
    reference_path = _write(tmp_path / "ref.spice", reference_spice)

    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {"file": gds, "deck": "sky130"},
                "reference": {"netlist": reference_path, "top": "RES"},
            },
        )
    )

    assert report["counts"]["devices"]["layout"] == 1
    assert report["counts"]["devices"]["reference"] == 1
    assert report["status"] == expected_status


def _write_series_res_high_po_gds(
    path: Path, segments: int = 3, l_um: float = 10.0
) -> str:
    """``segments`` sky130 ``res_high_po`` unit primitives (see
    ``_write_res_high_po_gds``), each individually contacted and marked, wired
    in series through *unlabelled* interior ``li1`` straps -- issue #559's
    motivating shape: one logical resistor drawn as N separately-drawn,
    individually-contacted primitives wired by interconnect, rather than one
    continuous body with intermediate taps.

    Only the leftmost and rightmost contacts are labelled (``RA``/``RB``), so
    every interior net stays unlabelled -- ``make_top_level_pins()`` never
    promotes it to a pin, and ``options.combine_devices`` can fold the whole
    chain into one device with no ``declared_pins`` reconciliation needed
    (contrast ``_write_series_resistor_gds``/`test_declared_pins_unblocks_
    combine_devices_for_labelled_internal_tap`, whose interior tap *is*
    labelled for documentation and therefore needs ``declared_pins`` to
    unblock the fold).
    """
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("RES")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    head_um = 3.0
    l_nm = int(round(l_um * 1000))
    head_nm = int(round(head_um * 1000))
    seg_span_nm = l_nm + 2 * head_nm  # one segment's own poly span (head-body-head)
    gap_nm = 1000  # 1um gap between segments, bridged by a continuous li1 strap

    left_x_by_segment = []
    right_x_by_segment = []
    x0 = 0
    for _ in range(segments):
        draw(66, 20, kdb.Box(x0, 0, x0 + seg_span_nm, 1000))  # poly.drawing
        marked = kdb.Box(x0 + head_nm, 0, x0 + head_nm + l_nm, 1000)
        draw(66, 13, marked)  # poly.res
        draw(86, 20, marked.enlarged(200, 200))  # rpm
        draw(94, 20, marked.enlarged(200, 200))  # psdm

        left_x = x0 + head_nm // 2
        right_x = x0 + seg_span_nm - head_nm // 2
        for x in (left_x, right_x):
            draw(66, 44, kdb.Box(x - 100, 400, x + 100, 600))  # licon1.drawing
            draw(67, 20, kdb.Box(x - 400, 200, x + 400, 800))  # li1.drawing
        left_x_by_segment.append(left_x)
        right_x_by_segment.append(right_x)

        x0 += seg_span_nm + gap_nm

    label(67, 5, "RA", left_x_by_segment[0], 500)
    label(67, 5, "RB", right_x_by_segment[-1], 500)

    # Bridge each segment's right contact to the next segment's left contact
    # with a continuous li1 strap -- one interior net per join, left
    # unlabelled (never promoted to a pin).
    for i in range(segments - 1):
        draw(
            67,
            20,
            kdb.Box(
                right_x_by_segment[i] - 400, 200, left_x_by_segment[i + 1] + 400, 800
            ),
        )

    layout.write(str(path))
    return str(path)


#: `res_high_po`'s deck constants (`sheet_rho_ohm_sq`, `fixed_offset_ohm`),
#: transcribed from `decks/sky130.py` for the reference-SPICE values below --
#: kept as literals (not imported) so this test fails loudly on a deck-value
#: drift instead of silently tracking it.
_RES_HIGH_PO_SHEET_RHO_OHM_SQ = 324.827244
_RES_HIGH_PO_FIXED_OFFSET_OHM = 379.705147


def _series_res_high_po_expected_r_ohm(
    segments: int, l_um: float, *, offset_count: int
) -> float:
    """``total_body_R + offset_count * fixed_offset_ohm`` for ``segments``
    `res_high_po` unit primitives (`w_um=1.0` each, per
    `_write_series_res_high_po_gds`) wired in series.

    ``offset_count=1`` is the *correct* folded-device value this issue's fix
    restores (the fixed offset applied once for the logical device);
    ``offset_count=segments`` is the *buggy* pre-fix value (the fixed offset
    summed once per drawn primitive) -- both are asserted as a negative
    control below, mirroring
    `test_inline_extraction_compares_fixed_offset_corrected_resistance`'s
    two-reference-netlist idiom.
    """
    body_r_ohm = segments * (l_um / 1.0) * _RES_HIGH_PO_SHEET_RHO_OHM_SQ
    return body_r_ohm + offset_count * _RES_HIGH_PO_FIXED_OFFSET_OHM


def test_combine_devices_applies_fixed_offset_once_per_folded_device(tmp_path):
    """Issue #559: `options.combine_devices` folding N series-connected
    `res_high_po` primitives (each carrying a nonzero `fixed_offset_ohm`)
    into one logical device must add the fixed offset exactly once, not once
    per drawn primitive.

    Without the fix, KLayout's native series `combine_devices()` sums each
    primitive's *already-corrected* `R` -- `total_body_R + N *
    fixed_offset_ohm` -- so the buggy reference (`offset_count=segments`)
    would incorrectly report `status: "match"` and the correct one
    (`offset_count=1`) would incorrectly `"mismatch"`. Both directions are
    asserted, mirroring
    `test_inline_extraction_compares_fixed_offset_corrected_resistance`, so
    the test cannot pass by the comparer merely ignoring `R`.
    """
    segments = 3
    l_um = 10.0
    gds = _write_series_res_high_po_gds(
        tmp_path / "res_high_po_series.gds", segments=segments, l_um=l_um
    )

    correct_r_ohm = _series_res_high_po_expected_r_ohm(segments, l_um, offset_count=1)
    buggy_r_ohm = _series_res_high_po_expected_r_ohm(
        segments, l_um, offset_count=segments
    )
    assert correct_r_ohm != pytest.approx(
        buggy_r_ohm
    )  # sanity: fixture is discriminating

    def _run(reference_r_ohm: float) -> dict:
        reference_spice = f"""
.subckt RES RA RB vsubs
R1 RA RB vsubs {reference_r_ohm:.5f} res_high_po
.ends
"""
        reference_path = _write(tmp_path / "ref.spice", reference_spice)
        return run_lvs(
            _write_request(
                tmp_path / "request.json",
                {
                    "layout": {"file": gds, "deck": "sky130"},
                    "reference": {"netlist": reference_path, "top": "RES"},
                    "options": {"combine_devices": True},
                },
            )
        )

    correct_report = _run(correct_r_ohm)
    assert correct_report["counts"]["devices"]["layout"] == 1
    assert correct_report["counts"]["devices"]["reference"] == 1
    assert correct_report["status"] == "match"

    buggy_report = _run(buggy_r_ohm)
    assert buggy_report["counts"]["devices"]["layout"] == 1
    assert buggy_report["status"] == "mismatch"


def test_combine_devices_false_still_applies_fixed_offset_per_primitive(tmp_path):
    """Regression guard for issue #559's fix: when `options.combine_devices`
    is `false` (or simply omitted -- the default), each of N series-wired
    `res_high_po` primitives keeps receiving its own `fixed_offset_ohm`
    exactly as before this issue -- the fix must not change any
    non-combined behavior. Uses the same series fixture as
    `test_combine_devices_applies_fixed_offset_once_per_folded_device`, but
    without `combine_devices`, so the interior nets stay real, unfolded pins
    -- N separate layout-side devices, each individually corrected."""
    segments = 3
    l_um = 10.0
    gds = _write_series_res_high_po_gds(
        tmp_path / "res_high_po_series_uncombined.gds", segments=segments, l_um=l_um
    )

    # No reference needed to assert the per-primitive correction directly --
    # inspect the extracted (uncombined) netlist's own `devices[]` view.
    from klayout_tools.extract import run_extract

    extracted = run_extract(
        gds, "sky130", output=str(tmp_path / "res_high_po_series_uncombined.spice")
    )
    assert extracted["device_counts"].get("res_high_po") == segments
    per_primitive_r_ohm = (l_um / 1.0) * _RES_HIGH_PO_SHEET_RHO_OHM_SQ + (
        _RES_HIGH_PO_FIXED_OFFSET_OHM
    )
    resistor_devices = [d for d in extracted["devices"] if d["class"] == "res_high_po"]
    assert len(resistor_devices) == segments
    for device in resistor_devices:
        assert device["params"]["r_ohm"] == pytest.approx(per_primitive_r_ohm)


def test_deferred_extract_offset_lookup_is_case_insensitive(tmp_path):
    """Issue #585: `apply_resistor_fixed_offset_corrections` must match a
    device class read back from SPICE via `kdb.NetlistSpiceReader` -- which
    uppercases class names to `RES_HIGH_PO` -- against the sky130 deck's own
    lowercase `res_high_po` name.

    Extract a single `res_high_po` primitive with the fixed-offset correction
    *deferred*, write it to SPICE, read it back (uppercased class name),
    confirm the correction is missing, then apply it via the public helper
    and confirm it lands exactly once. Before the fix the case-sensitive
    lookup silently missed `RES_HIGH_PO` and added nothing.
    """
    import klayout.db as kdb

    from klayout_tools.decks import get_extraction_deck
    from klayout_tools.extract import (
        apply_resistor_fixed_offset_corrections,
        run_extract,
    )

    segments = 1
    l_um = 10.0
    gds = _write_series_res_high_po_gds(
        tmp_path / "res_high_po_one.gds", segments=segments, l_um=l_um
    )
    spice_path = str(tmp_path / "res_high_po_one.spice")
    extracted = run_extract(
        gds, "sky130", output=spice_path, apply_resistor_fixed_offset=False
    )
    # Deferred: the written primitive carries only its raw body R (no offset).
    raw_body_r_ohm = (l_um / 1.0) * _RES_HIGH_PO_SHEET_RHO_OHM_SQ
    resistor_devices = [d for d in extracted["devices"] if d["class"] == "res_high_po"]
    assert len(resistor_devices) == segments
    assert resistor_devices[0]["params"]["r_ohm"] == pytest.approx(raw_body_r_ohm)

    # Read the SPICE back: NetlistSpiceReader uppercases the class name.
    netlist = kdb.Netlist()
    netlist.read(spice_path, kdb.NetlistSpiceReader())
    class_names = {
        device.device_class().name
        for circuit in netlist.each_circuit()
        for device in circuit.each_device()
    }
    assert "RES_HIGH_PO" in class_names  # the uppercased name the bug missed

    def _only_resistor_r() -> float:
        rs = [
            device.parameter("R")
            for circuit in netlist.each_circuit()
            for device in circuit.each_device()
            if device.device_class().name.lower() == "res_high_po"
        ]
        assert len(rs) == segments
        return rs[0]

    assert _only_resistor_r() == pytest.approx(raw_body_r_ohm)

    # The public helper must fire despite the case mismatch, exactly once.
    apply_resistor_fixed_offset_corrections(netlist, get_extraction_deck("sky130"))
    assert _only_resistor_r() == pytest.approx(
        raw_body_r_ohm + _RES_HIGH_PO_FIXED_OFFSET_OHM
    )


def test_pre_extracted_netlist_with_deck_applies_fixed_offset_once(tmp_path):
    """Issue #585: the `layout.netlist` + `layout.deck` +
    `options.combine_devices: true` request shape must apply the resistor
    `fixed_offset_ohm` correction exactly once per post-combine device --
    the pre-extracted sibling of
    `test_combine_devices_applies_fixed_offset_once_per_folded_device`.

    A pre-extracted netlist produced with the correction *deferred*
    (`run_extract(..., apply_resistor_fixed_offset=False)`) carries only raw
    per-primitive body `R`. Reading it back through `run_lvs` with a
    `layout.deck` and `combine_devices` must fold the series chain into one
    device and add the fixed offset once (`offset_count=1`), not once per
    drawn primitive (`offset_count=segments`). Both reference directions are
    asserted so the test cannot pass by the comparer merely ignoring `R`.
    """
    from klayout_tools.extract import run_extract

    segments = 3
    l_um = 10.0
    gds = _write_series_res_high_po_gds(
        tmp_path / "res_high_po_series_pre.gds", segments=segments, l_um=l_um
    )
    spice_path = str(tmp_path / "res_high_po_series_pre.spice")
    extracted = run_extract(
        gds, "sky130", output=spice_path, apply_resistor_fixed_offset=False
    )
    top = extracted["top"]
    # Deferred: each primitive carries only its raw body R (no offset baked in).
    raw_body_r_ohm = (l_um / 1.0) * _RES_HIGH_PO_SHEET_RHO_OHM_SQ
    resistor_devices = [d for d in extracted["devices"] if d["class"] == "res_high_po"]
    assert len(resistor_devices) == segments
    for device in resistor_devices:
        assert device["params"]["r_ohm"] == pytest.approx(raw_body_r_ohm)

    correct_r_ohm = _series_res_high_po_expected_r_ohm(segments, l_um, offset_count=1)
    buggy_r_ohm = _series_res_high_po_expected_r_ohm(
        segments, l_um, offset_count=segments
    )
    assert correct_r_ohm != pytest.approx(buggy_r_ohm)  # fixture is discriminating

    def _run(reference_r_ohm: float) -> dict:
        reference_spice = f"""
.subckt {top} RA RB vsubs
R1 RA RB vsubs {reference_r_ohm:.5f} res_high_po
.ends
"""
        reference_path = _write(tmp_path / "ref.spice", reference_spice)
        return run_lvs(
            _write_request(
                tmp_path / "request.json",
                {
                    "layout": {
                        "netlist": spice_path,
                        "deck": "sky130",
                        "top": top,
                    },
                    "reference": {"netlist": reference_path, "top": top},
                    "options": {"combine_devices": True},
                },
            )
        )

    correct_report = _run(correct_r_ohm)
    assert correct_report["counts"]["devices"]["layout"] == 1
    assert correct_report["counts"]["devices"]["reference"] == 1
    assert correct_report["status"] == "match"

    buggy_report = _run(buggy_r_ohm)
    assert buggy_report["counts"]["devices"]["layout"] == 1
    assert buggy_report["status"] == "mismatch"


# --------------------------------------------------------------------------- #
# `layout.deck_options` (issue #600): the request-document counterpart of
# `klt extract --deck-option`, threading gf180mcu's caller-selectable
# `poly_res` sheet-rho flavour (issue #595, `tests/test_extract.py`'s
# `test_gf180mcu_poly_res_deck_option_*` tests) into `klt lvs`'s layout-side
# extraction and its own `get_extraction_deck` call (the latter also drives
# `device_classes`/the deferred resistor `fixed_offset_ohm` correction for
# the pre-extracted `layout.netlist` + `layout.deck` shape).
# --------------------------------------------------------------------------- #

#: `L=6um / W=1um` -> exactly 6 squares, the same nominal geometry
#: `test_extract.py`'s `_make_poly_resistor_layout` uses.
_GF180_POLY_RES_SQUARES = 6.0


def _write_gf180mcu_poly_res_gds(path: Path) -> str:
    """A single gf180mcu `Resistor` (62/0)-marked poly bar -- the
    shared-geometry family whose `1k`/`2k`/`3k` sheet-rho siblings are only
    selectable via `deck_options={"poly_res": ...}` (issue #595), mirroring
    `test_extract.py`'s `_make_poly_resistor_layout(..., extra=((62, 0,
    ...),))` fixture (kept self-contained here rather than imported, per this
    test module's existing convention of not cross-importing test fixtures --
    see `_write_series_res_high_po_gds` above for the sky130 analogue)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    top = layout.create_cell("RES")

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    bar = kdb.Box(0, 0, 12000, 1000)
    marked = kdb.Box(3000, 0, 9000, 1000)

    draw(30, 0, bar)  # Poly2
    draw(110, 5, marked)  # RES_MK
    draw(31, 0, marked.enlarged(500, 500))  # Pplus
    draw(49, 0, marked.enlarged(500, 500))  # SAB
    draw(62, 0, marked.enlarged(500, 500))  # Resistor -> selects a poly_res flavour

    for x, name in ((1500, "RA"), (10500, "RB")):
        draw(33, 0, kdb.Box(x - 100, 400, x + 100, 600))  # Contact
        draw(34, 0, kdb.Box(x - 400, 200, x + 400, 800))  # Metal1
        label(34, 10, name, x, 500)  # Metal1 pin

    layout.write(str(path))
    return str(path)


def _gf180mcu_poly_res_reference_spice(
    top: str, r_ohm: float, substrate_net: str
) -> str:
    """A plain-element reference netlist for one `_write_gf180mcu_poly_res_gds`
    resistor -- terminal order `(a, b, w)` matches gf180mcu's
    `DeviceExtractorResistorWithBulk`-emitted `R$1 RA RB <substrate> ...`
    (verified against a real extraction's own written SPICE)."""
    return f"""
.subckt {top} RA RB {substrate_net}
R1 RA RB {substrate_net} {r_ohm:.5f} ppolyf_u_2k
.ends
"""


def test_deck_options_selects_gf180mcu_poly_res_flavour_for_inline_extraction(
    tmp_path,
):
    """`layout.deck_options={"poly_res": "2k"}` on an inline `layout.file` +
    `layout.deck: "gf180mcu"` request resolves the layout side's `Resistor`
    -marked poly segment as `ppolyf_u_2k` (2000 ohm/sq), matching a reference
    netlist sized for that flavour -- and echoes the resolved mapping in
    `provenance.deck.options` (issue #600, mirroring `test_extract.py`'s
    `test_gf180mcu_poly_res_deck_option_selects_flavour`)."""
    from klayout_tools.decks import get_extraction_deck

    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    substrate_net = get_extraction_deck("gf180mcu").substrate_net
    r_ohm = _GF180_POLY_RES_SQUARES * 2000.0
    reference_path = _write(
        tmp_path / "ref.spice",
        _gf180mcu_poly_res_reference_spice("RES", r_ohm, substrate_net),
    )

    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {
                    "file": gds,
                    "deck": "gf180mcu",
                    "deck_options": {"poly_res": "2k"},
                },
                "reference": {"netlist": reference_path, "top": "RES"},
            },
        )
    )

    assert report["status"] == "match"
    assert report["counts"]["devices"]["layout"] == 1
    assert report["provenance"]["deck"]["options"] == {"poly_res": "2k"}


def test_deck_options_omitted_falls_back_to_1k_default_and_mismatches_2k_reference(
    tmp_path,
):
    """The bug this issue fixes: without `layout.deck_options`, the same 2k
    -flavour-drawn layout used above extracts at the deck's `1k` default
    (1000 ohm/sq) instead -- a spurious resistance mismatch against a
    reference sized for the actual (2k) flavour, and `provenance.deck` omits
    `options` entirely (issue #600)."""
    from klayout_tools.decks import get_extraction_deck

    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    substrate_net = get_extraction_deck("gf180mcu").substrate_net
    r_ohm = _GF180_POLY_RES_SQUARES * 2000.0
    reference_path = _write(
        tmp_path / "ref.spice",
        _gf180mcu_poly_res_reference_spice("RES", r_ohm, substrate_net),
    )

    report = run_lvs(
        _write_request(
            tmp_path / "request.json",
            {
                "layout": {"file": gds, "deck": "gf180mcu"},
                "reference": {"netlist": reference_path, "top": "RES"},
            },
        )
    )

    assert report["status"] == "mismatch"
    assert "options" not in report["provenance"]["deck"]


def test_deck_options_unrecognised_value_raises_lvs_error(tmp_path):
    """An unrecognised `poly_res` value is a clean `LvsError` wrapping
    `InvalidDeckOptionError`, not a traceback and not a silently-kept default
    (issue #600, mirroring `test_extract.py`'s
    `test_gf180mcu_poly_res_deck_option_invalid_value_is_extract_error`)."""
    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "file": gds,
                "deck": "gf180mcu",
                "deck_options": {"poly_res": "4k"},
            },
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="poly_res=4k"):
        run_lvs(path)


def test_deck_options_unrecognised_key_raises_lvs_error(tmp_path):
    """A `deck_options` key no `ResistorDevice.flavour_option`/
    `CapacitorDevice.flavour_option` matches is a clean `LvsError` (issue
    #600; `mim_cap` -- this test's own unrecognised-key example before
    issue #1151 made it a real, recognised gf180mcu key -- can no longer be
    used here)."""
    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "file": gds,
                "deck": "gf180mcu",
                "deck_options": {"not_a_real_deck_option": "x"},
            },
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="no selectable option"):
        run_lvs(path)


def test_deck_options_wrong_type_raises(tmp_path):
    """`layout.deck_options` must be a JSON object of string pairs -- a
    string (or any other non-object) is a clean request error, mirroring
    `test_declared_pins_wrong_type_raises`'s pattern for a sibling field
    (issue #600)."""
    gds = _write_flat_inverter_gds(tmp_path / "flat.gds")
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "file": gds,
                "deck": "sky130",
                "deck_options": "poly_res=2k",  # must be an object, not a string
            },
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    with pytest.raises(LvsError, match="must be a JSON object"):
        run_lvs(path)


def test_deck_options_without_deck_raises(tmp_path):
    """`layout.deck_options` given without `layout.deck` -- meaningless, since
    there is no deck to apply it to -- is a clean request error, not silently
    ignored (issue #600), whether or not extraction would even be attempted
    (exercised here via the pre-extracted `layout.netlist` shape, which never
    resolves a deck at all unless one is given)."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "netlist": layout_path,
                "deck_options": {"poly_res": "2k"},
            },
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="requires request.layout.deck"):
        run_lvs(path)


def test_deck_options_invalid_value_on_pre_extracted_netlist_shape_raises(tmp_path):
    """The pre-extracted `layout.netlist` + `layout.deck` shape (issue #585)
    never triggers extraction, so `run_lvs`'s own `get_extraction_deck` call
    is the *only* place `deck_options` is resolved for it -- must raise a
    clean `LvsError`, not an unhandled `InvalidDeckOptionError` traceback
    (issue #600)."""
    from klayout_tools.extract import run_extract

    gds = _write_gf180mcu_poly_res_gds(tmp_path / "poly_res.gds")
    spice_path = str(tmp_path / "poly_res.spice")
    extracted = run_extract(
        gds, "gf180mcu", output=spice_path, deck_options={"poly_res": "2k"}
    )
    top = extracted["top"]
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "netlist": spice_path,
                "deck": "gf180mcu",
                "top": top,
                "deck_options": {"poly_res": "4k"},
            },
            "reference": {"netlist": reference_path},
        },
    )
    with pytest.raises(LvsError, match="poly_res=4k"):
        run_lvs(path)


def test_body_unverified_warns_nmos_only_on_gf180mcu(tmp_path):
    """gf180mcu draws no distinct NMOS substrate/tap layer -- `Comp` is
    shared with ordinary active, `ExtractionDeck.tap is None` -- so the NMOS
    body always warns. The PMOS side no longer warns unconditionally (issue
    #1084): this deck now declares `tap_nplus`/`tap_pplus`, a *derivable*
    well-tap mechanism, mirroring sky130's own deck-structural (not
    per-device) treatment of a deck that has *some* tap mechanism -- even
    though `GF180_CLKINV` itself draws no well tie, so its one PMOS body
    still resolves to an anonymous net (see
    `test_gf180mcu_clkinv_1_spot_check` in `tests/test_extract.py`)."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(GF180_CLKINV), "gf180mcu", output=reference_path)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(GF180_CLKINV), "deck": "gf180mcu"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)
    assert report["status"] == "match"

    body_entries = {
        m["device"]["class"]: m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
    }
    assert set(body_entries) == {"nfet"}
    assert all(entry["severity"] == "warning" for entry in body_entries.values())
    assert all(entry["side"] == "layout" for entry in body_entries.values())


# --------------------------------------------------------------------------- #
# gf180mcu derived tap (issue #1084): a layout that draws an Nplus/Pplus-
# over-Comp well/substrate tie reaches `status: "match"` against a reference
# netlist whose bulk terminals name the real supply rails -- with **no**
# caller-side reference-netlist rewriting, this issue's own core acceptance
# criterion -- mirroring #490's sky130 substrate-tap match test above.
# --------------------------------------------------------------------------- #


def _write_gf180mcu_clkinv_with_ties(path: Path) -> str:
    """The real `gf180mcu_fd_sc_mcu9t5v0__clkinv_1` corpus cell, plus *added*
    Nplus/Pplus-over-Comp tie geometry: an n+ well tie inside `Nwell`
    routed up through `Metal1` into the cell's own `VDD` rail shape, and a
    p+ substrate tie outside every `Nwell` routed down into the cell's own
    `VSS` rail shape -- exactly the issue's own "Nplus-over-COMP strip
    inside Nwell tied to the positive rail... Pplus-over-COMP strip on
    substrate tied to the ground rail" repro. Both ties' `Comp`/`Nplus`/
    `Pplus` diffusion sits at x 1700..1900 -- clear of the cell's own
    `Comp` geometry (PMOS `Comp` bbox x180..1560, NMOS `Comp` bbox
    x180..1660) -- and each `Metal1` landing pad is extended up/down into
    the real `VDD`/`VSS` rail polygons' own solid area (verified against
    the corpus GDS directly: the rail shapes are not solid rectangles --
    the `VDD` rail's fill starts at y=4590, the `VSS` rail's fill runs
    through y=800 at this x -- rather than actually *touching* real rail
    geometry, a same-named-but-disconnected label (as sky130's tap tests
    use) would extract as a second, separate net of the same name, not a
    real merge, since KLayout does not merge nets by matching label text)."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(CORPUS_DIR / "gf180mcu" / "gf180mcu_fd_sc_mcu9t5v0__clkinv_1.gds"))
    top = layout.top_cell()

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    # Well tie: Nplus-covered Comp inside Nwell (Nwell y >= 2265), Metal1
    # routed up into the VDD rail's solid top band (y >= 4590).
    draw(22, 0, kdb.Box(1700, 3600, 1900, 3800))  # Comp
    draw(32, 0, kdb.Box(1700, 3600, 1900, 3800))  # Nplus (well-tie doping)
    draw(33, 0, kdb.Box(1720, 3620, 1880, 3780))  # Contact
    draw(34, 0, kdb.Box(1700, 3600, 1900, 4700))  # Metal1 -- reaches VDD rail

    # Substrate tie: Pplus-covered Comp outside every Nwell, Metal1 routed
    # down into the VSS rail's solid area.
    draw(22, 0, kdb.Box(1700, -300, 1900, -100))  # Comp
    draw(31, 0, kdb.Box(1700, -300, 1900, -100))  # Pplus (substrate-tie doping)
    draw(33, 0, kdb.Box(1720, -280, 1880, -120))  # Contact
    draw(34, 0, kdb.Box(1700, -300, 1900, 800))  # Metal1 -- reaches VSS rail

    layout.write(str(path))
    return str(path)


#: Reference schematic for `gf180mcu_fd_sc_mcu9t5v0__clkinv_1` naming both
#: devices' bulk terminals on the real supply rails -- exactly what a real
#: schematic-derived reference netlist does (the issue's own framing), not
#: the deck's synthesized `vsubs`/an anonymous net a caller would otherwise
#: have to rewrite around.
_GF180MCU_CLKINV_SPICE_NAMED_RAILS = """
.subckt gf180mcu_fd_sc_mcu9t5v0__clkinv_1 I VDD VSS ZN vsubs
M1 ZN I VSS VSS nfet L=0.6U W=0.73U
M2 ZN I VDD VDD pfet L=0.5U W=1.83U
.ends
"""


def test_gf180mcu_lvs_matches_reference_named_rails_without_netlist_rewriting(
    tmp_path,
):
    """Issue #1084's own core acceptance criterion: a gf180mcu layout that
    draws a real Nplus/Pplus-over-Comp well/substrate tie reaches
    `status: "match"` against a reference netlist that names both devices'
    bulk terminals on the real supply rails -- with **no** caller-side
    reference-netlist rewriting, and neither `device.body_unverified` entry
    fires (both body terminals resolved to real, named nets)."""
    gds = _write_gf180mcu_clkinv_with_ties(tmp_path / "clkinv.gds")
    reference_path = _write(tmp_path / "ref.spice", _GF180MCU_CLKINV_SPICE_NAMED_RAILS)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": gds, "deck": "gf180mcu"},
            "reference": {
                "netlist": reference_path,
                "top": "gf180mcu_fd_sc_mcu9t5v0__clkinv_1",
            },
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert not any(
        m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
        for m in report["mismatches"]
    )


def test_body_unverified_absent_for_pre_extracted_layout_netlist(tmp_path):
    """No `layout.deck` (the pre-extracted `layout.netlist` form) means no
    known deck body-net behaviour to warn about -- the warning never fires
    in this shape, even though the fixture inverter has both an NMOS and a
    PMOS device."""
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
    assert report["status"] == "match"
    assert not any(
        m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
        for m in report["mismatches"]
    )
    assert report["device_classes"] is None


# --------------------------------------------------------------------------- #
# NMOS body resolves to a real, drawn substrate-tap net (issue #490): a
# reference schematic naming that same net matches cleanly with no
# `hints.same_nets` workaround, and `device.body_unverified` no longer fires
# for the NMOS side.
# --------------------------------------------------------------------------- #


def _write_sky130_inv_with_substrate_tap(
    path: Path, tap_label: str = "VSUBRING"
) -> str:
    """The real `sky130_fd_sc_hd__inv_1` corpus cell (guaranteed LVS-legitimate
    wiring -- unlike a hand-drawn approximation, its drain pads are genuinely
    tied into one `Y` net), plus an *added* `tap.drawing` ring drawn well
    outside the cell's own bbox/`nwell` (never touching either), contacted up
    through licon1/li1 and labelled -- a genuine, drawable P-substrate tie
    (#490), exactly the issue's own "guard ring around it" repro sketch."""
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.read(str(SKY130_INV))
    top = layout.top_cell()

    def draw(layer, datatype, box):
        top.shapes(layout.layer(layer, datatype)).insert(box)

    def label(layer, datatype, text, x, y):
        top.shapes(layout.layer(layer, datatype)).insert(
            kdb.Text(text, kdb.Trans(x, y))
        )

    # The real cell's bbox is roughly x -190..1570, y -240..2960 (nwell/PMOS
    # in the upper half, y >= 1305) -- draw the ring well clear of all of it,
    # off to the left and below.
    draw(65, 44, kdb.Box(-1200, -800, -1000, -200))  # tap.drawing (substrate tie)
    draw(66, 44, kdb.Box(-1180, -650, -1020, -550))  # licon1 over the tap
    draw(67, 20, kdb.Box(-1250, -700, -950, -500))  # li1 over the tap
    label(67, 5, tap_label, -1100, -600)

    layout.write(str(path))
    return str(path)


#: Reference schematic naming the NMOS body on the *same* net the layout's
#: substrate-tap ring resolves to (`VSUBRING`) -- a genuine schematic pin,
#: not the deck's synthesized `vsubs`. Mirrors `_INVERTER_SPICE`, with the
#: body pin split out as its own net instead of tied straight to `VGND`.
_SKY130_INV_SPICE_NAMED_SUBSTRATE = """
.subckt sky130_fd_sc_hd__inv_1 A VGND VPWR Y
M1 Y A VGND VSUBRING nfet W=0.65U L=0.15U
M2 Y A VPWR VPB pfet W=1.0U L=0.15U
.ends
"""


def test_nmos_body_matches_reference_named_substrate_net_without_same_nets_hint(
    tmp_path,
):
    """Issue #490's core acceptance criterion: a layout that draws a real
    substrate-tap ring reaches `status: "match"` against a reference netlist
    that names the NMOS body on that same real net -- with **no**
    `hints.same_nets` workaround required, and the NMOS `device.body_unverified`
    warning no longer fires (the body terminal resolved to a real net)."""
    from klayout_tools.extract import run_extract

    gds = _write_sky130_inv_with_substrate_tap(tmp_path / "inv.gds")
    # The real cell's own pin order/subckt name (needed for `reference.top` to
    # resolve and the pin count to line up) -- read once via inline extraction
    # of the *unmodified* corpus cell, matching `test_inline_extraction_
    # composes_extract_and_compare`'s own pattern.
    extracted = run_extract(str(SKY130_INV), "sky130", output=str(tmp_path / "x.spice"))
    reference_path = _write(
        tmp_path / "ref.spice",
        _SKY130_INV_SPICE_NAMED_SUBSTRATE.replace(
            "sky130_fd_sc_hd__inv_1", extracted["top"]
        ),
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": gds, "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert not any(
        m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
        and m["device"]["class"] == "nfet"
        for m in report["mismatches"]
    )


def test_nmos_body_still_warns_when_no_substrate_tap_is_drawn(tmp_path):
    """Contrast case: the same reference/comparison shape, but the layout
    draws no substrate-tap ring at all -- the NMOS body still falls back to
    the synthesized `vsubs` global, so `device.body_unverified` still fires
    for the NMOS side (no regression from #490 on the common, no-ring
    case)."""
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

    nmos_body_unverified = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
        and m["device"]["class"] == "nfet"
    ]
    assert len(nmos_body_unverified) == 1


# --------------------------------------------------------------------------- #
# options.combine_devices (issue #261): folded/multi-finger and split/
# interleaved matched devices vs. a lumped schematic device
# --------------------------------------------------------------------------- #


def test_multifinger_device_mismatches_without_combine_devices(tmp_path):
    """Default behaviour (flag absent) is unchanged: two parallel fingers
    sharing all four terminals still report as an unmatched device against
    a lumped schematic device."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"].get("device.unmatched", 0) >= 1
    assert report["counts"]["devices"]["layout"] == 3
    assert report["counts"]["devices"]["reference"] == 2


def test_multifinger_device_matches_with_combine_devices(tmp_path):
    """`options.combine_devices: true` merges the two parallel W=0.325U
    fingers into a single W=0.65U device, matching the lumped reference."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["mismatch_count"] == 0
    assert report["counts"]["devices"]["layout"] == 2
    assert report["counts"]["devices"]["reference"] == 2
    # Parallel fingers share all four terminals, so folding them frees no
    # interior net -- both sides carry the inverter's four nets (issue #500:
    # the post-combine purge must not over-remove when nothing was emptied).
    assert report["counts"]["nets"]["layout"] == 4
    assert report["counts"]["nets"]["reference"] == 4


def test_split_interleaved_device_matches_with_combine_devices(tmp_path):
    """Split/interleaved matched-pair segments (common-centroid style: two
    unevenly-sized fingers about a shared axis) combine the same way as the
    multi-finger case -- `combine_devices()` only cares about connectivity,
    not finger symmetry."""
    layout_path = _write(tmp_path / "layout.spice", _SPLIT_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)

    path_without = _write_request(
        tmp_path / "request_without.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )
    report_without = run_lvs(path_without)
    assert report_without["status"] == "mismatch"

    path_with = _write_request(
        tmp_path / "request_with.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )
    report_with = run_lvs(path_with)
    assert report_with["status"] == "match"
    assert report_with["mismatch_count"] == 0
    # As with the multi-finger case, the split segments share all terminals,
    # so no interior net is freed -- the four inverter nets survive the purge
    # on both sides (issue #500).
    assert report_with["counts"]["nets"]["layout"] == 4
    assert report_with["counts"]["nets"]["reference"] == 4


def test_combine_devices_purges_emptied_series_string_nets(tmp_path):
    """Issue #500: with `options.combine_devices: true`, the interior nodes a
    series string collapses into a single device (0 terminals, 0 pins after
    the combine) are purged, so `counts.nets.layout` reports the honest
    post-combine topology (2 nets: `A`, `Y`) instead of the pre-purge 4 (the
    two interior nodes still counted), and they produce no `net.unmatched`
    findings in `mismatches[]`."""
    layout_path = _write(
        tmp_path / "layout.spice", _SERIES_RESISTOR_STRING_LAYOUT_SPICE
    )
    reference_path = _write(
        tmp_path / "ref.spice", _SERIES_RESISTOR_STRING_REFERENCE_SPICE
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "rstring"},
            "reference": {"netlist": reference_path, "top": "rstring"},
            "options": {"combine_devices": True},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    # Three 1k segments folded into a single 3k device on the layout side.
    assert report["counts"]["devices"]["layout"] == 1
    assert report["counts"]["devices"]["reference"] == 1
    # The two interior nodes the combine emptied are purged: the honest net
    # count is 2 (`A`, `Y`) on both sides, not the pre-purge 4 on the layout.
    assert report["counts"]["nets"]["layout"] == 2
    assert report["counts"]["nets"]["reference"] == 2
    # No spurious net.unmatched findings for the emptied interior nodes.
    assert report["category_counts"].get("net.unmatched", 0) == 0
    assert report["mismatch_count"] == 0


def test_series_string_nets_not_purged_without_combine_devices(tmp_path):
    """`options.combine_devices: false` (the default) is unchanged: with no
    combine, nothing is folded and nothing is purged -- the series string is
    compared as three real devices with two real interior nodes, so
    `counts.nets.layout` is the full 4 (issue #500: the purge runs only when
    combine actually ran)."""
    layout_path = _write(
        tmp_path / "layout.spice", _SERIES_RESISTOR_STRING_LAYOUT_SPICE
    )
    reference_path = _write(
        tmp_path / "ref.spice", _SERIES_RESISTOR_STRING_REFERENCE_SPICE
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "rstring"},
            "reference": {"netlist": reference_path, "top": "rstring"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    # No combine ran, so nothing is purged: the interior nodes are still real,
    # counted topology (A, Y, n1, n2 = 4 nets; three resistors).
    assert report["counts"]["nets"]["layout"] == 4
    assert report["counts"]["devices"]["layout"] == 3


def test_purge_emptied_nets_keeps_unused_top_level_pin(tmp_path):
    """The purge is scoped to genuinely-empty nets: a net with zero terminals
    but a real pin (a genuinely-unused top-level pin) is preserved, so
    `counts.pins.*` is unaffected -- only interior nodes with nothing attached
    are dropped (issue #500's edge case, the reason the fix is narrower than
    KLayout's own `purge_nets()`)."""
    import klayout.db as kdb

    spice = _write(
        tmp_path / "n.spice",
        """
.subckt rstring A Y EN
R1 Y n1 1k
R2 n1 A 1k
.ends
""",
    )
    netlist = kdb.Netlist()
    netlist.read(spice, kdb.NetlistSpiceReader())
    netlist.combine_devices()
    circuit = next(iter(netlist.each_circuit()))
    pins_before = circuit.pin_count()

    lvs._purge_emptied_nets(netlist)

    names = {n.expanded_name() for n in circuit.each_net()}
    # Interior node n1 (0 terminals, 0 pins) is dropped...
    assert "N1" not in names
    # ...but the unused top-level pin's net EN (0 terminals, 1 pin) is kept,
    # and the pin count is unchanged.
    assert "EN" in names
    assert circuit.pin_count() == pins_before


def test_combine_devices_does_not_merge_genuinely_distinct_parallel_devices(tmp_path):
    """Edge case from the acceptance criteria: two parallel devices that
    differ in a device parameter (gate length here) must NOT be merged by
    `combine_devices()` -- verified against a two-device reference that
    matches with or without the flag, since nothing should combine either
    way."""
    layout_path = _write(tmp_path / "layout.spice", _DISTINCT_PARALLEL_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _DISTINCT_PARALLEL_LAYOUT_SPICE)

    for combine_devices in (False, True):
        path = _write_request(
            tmp_path / f"request_{combine_devices}.json",
            {
                "layout": {"netlist": layout_path, "top": "inv"},
                "reference": {"netlist": reference_path, "top": "inv"},
                "options": {"combine_devices": combine_devices},
            },
        )
        report = run_lvs(path)
        assert report["status"] == "match"
        # Three devices on each side throughout -- confirms the two
        # differently-sized (L) parallel fingers were never collapsed into
        # one, with the flag either on or off.
        assert report["counts"]["devices"]["layout"] == 3
        assert report["counts"]["devices"]["reference"] == 3


def test_combine_devices_defaults_false(tmp_path):
    """`options.combine_devices` absent from the request behaves exactly
    like `false` -- the multi-finger layout still mismatches."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {},
        },
    )
    report = run_lvs(path)
    assert report["status"] == "mismatch"


# --------------------------------------------------------------------------- #
# combine_devices() partial-match RuntimeError (issue #466)
# --------------------------------------------------------------------------- #

# KLayout's own `Netlist.combine_devices()` raises this exact internal-
# consistency `RuntimeError` on a partial-match device group: N real
# (matching-relevant) instances plus M dummy instances that all share two of
# three terminals, but only the N real instances also share the third (see
# `_combine_devices_safely`'s docstring in `lvs.py`). Reproducing this from
# the outside proved impractical for a fast unit test: extensive attempts
# against the pinned `klayout==0.30.10` -- hand-built `klayout.db.Netlist`
# objects using both `DeviceClassBJT3Transistor` and `DeviceClassMOS4Transistor`
# (varying real/dummy counts, device ordering, parameters, and pin exposure),
# SPICE-read netlists using the same partial-match shape, and the real `klt
# gen bjt_array` -> `klt extract` pipeline with the real units' emitters
# bused together via drawn metal (mirroring this issue's own reproduction
# recipe) -- never triggered it; it is, per the issue itself, a `klayout.db`
# C++-internal invariant violation, not something `klt`'s own Python code can
# deterministically force to happen. These tests instead exercise the actual
# code under test -- `_combine_devices_safely`'s handling of this exact
# `RuntimeError` shape -- by making `Netlist.combine_devices()` raise it
# directly, the standard way to test a wrapper's handling of a third-party
# exception whose internal trigger conditions cannot be independently forced.
_COMBINE_DEVICES_PARTIAL_MATCH_ERROR = (
    "Internal error: Terminal still connected after removing device in "
    "device combination: name=, circuit=<top>, terminal=E in "
    "Netlist.combine_devices"
)


def test_combine_devices_safely_returns_warning_on_partial_match_runtimeerror(
    monkeypatch,
):
    """`_combine_devices_safely` catches KLayout's own partial-match
    `RuntimeError` and returns a `device.combine_incomplete` warning entry
    instead of letting it propagate."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    warning = lvs._combine_devices_safely(kdb.Netlist(), "layout")

    assert warning is not None
    assert warning["category"] == lvs.CATEGORY_DEVICE_COMBINE_INCOMPLETE
    assert warning["severity"] == "warning"
    assert warning["side"] == "layout"
    assert "combine_devices" in warning["description"]
    assert warning["net"] is None
    assert warning["device"] is None


def test_combine_devices_safely_reraises_unrelated_runtimeerror(monkeypatch):
    """A `RuntimeError` that does not carry the KLayout-specific
    ``Netlist.combine_devices`` marker text is not this issue's error shape
    -- `_combine_devices_safely` must not silently swallow it, so an
    unrelated failure is never masked as a benign combine-degradation."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError("some unrelated klayout.db internal error")

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    with pytest.raises(RuntimeError, match="unrelated"):
        lvs._combine_devices_safely(kdb.Netlist(), "layout")


def test_combine_devices_safely_returns_none_on_clean_combine():
    """No warning when `combine_devices()` completes without raising --
    the common case, matching every other `options.combine_devices` test in
    this file."""
    import klayout.db as kdb

    assert lvs._combine_devices_safely(kdb.Netlist(), "layout") is None


def test_combine_devices_safely_retries_and_recovers_from_intermittent_failure(
    tmp_path, monkeypatch
):
    """Issue #1185: a `combine_devices()` that fails on its first call(s) but
    succeeds on a later one -- the shape of the reported run-to-run
    nondeterminism -- is retried against fresh `Netlist.dup()` copies rather
    than degrading on the very first failure. The real (non-monkeypatched)
    `Netlist.combine_devices` runs underneath; only its *outcome* is forced
    to fail twice before "succeeding" (via `call_count`), so this exercises
    the real merge logic, not a fully-faked return value."""
    import klayout.db as kdb

    real_combine_devices = kdb.Netlist.combine_devices
    call_count = 0

    def _flaky(self):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)
        return real_combine_devices(self)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _flaky)

    netlist = kdb.Netlist()
    netlist.read(
        _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE),
        kdb.NetlistSpiceReader(),
    )
    circuit = next(iter(netlist.each_circuit()))
    devices_before = sum(1 for _ in circuit.each_device())

    warning = lvs._combine_devices_safely(netlist, "layout")

    # Recovered on the third attempt -- no warning, and the *original*
    # `netlist` object (not a throwaway copy) now carries the combined
    # result, adopted via `Netlist.assign()`.
    assert warning is None
    assert call_count == 3
    combined_circuit = next(iter(netlist.each_circuit()))
    devices_after = sum(1 for _ in combined_circuit.each_device())
    assert devices_after < devices_before


def test_combine_devices_safely_exhausts_bounded_retry_budget(monkeypatch):
    """When every attempt hits the partial-match error, `_combine_devices_
    safely` retries up to (and no more than) `max_attempts` times -- exactly
    `_COMBINE_DEVICES_MAX_ATTEMPTS` calls to `combine_devices()`, not an
    unbounded loop -- before degrading gracefully, and the resulting warning
    discloses the attempt count (issue #1185)."""
    import klayout.db as kdb

    call_count = 0

    def _always_raise(self):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _always_raise)

    warning = lvs._combine_devices_safely(kdb.Netlist(), "layout")

    assert warning is not None
    assert call_count == lvs._COMBINE_DEVICES_MAX_ATTEMPTS
    assert warning["category"] == lvs.CATEGORY_DEVICE_COMBINE_INCOMPLETE
    assert warning["severity"] == "warning"
    assert str(lvs._COMBINE_DEVICES_MAX_ATTEMPTS) in warning["description"]
    assert "attempts" in warning["description"]


def test_combine_devices_safely_max_attempts_is_configurable(monkeypatch):
    """`max_attempts` is a real parameter, not a hardcoded loop bound --
    passing `1` reproduces the pre-#1185 single-shot behaviour exactly (no
    retry at all), the smallest possible regression check that the retry
    budget is honored precisely rather than approximately."""
    import klayout.db as kdb

    call_count = 0

    def _always_raise(self):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _always_raise)

    warning = lvs._combine_devices_safely(kdb.Netlist(), "layout", max_attempts=1)

    assert call_count == 1
    assert warning is not None
    assert warning["category"] == lvs.CATEGORY_DEVICE_COMBINE_INCOMPLETE


def test_combine_devices_safely_never_retries_unrelated_runtimeerror(monkeypatch):
    """An unrelated `RuntimeError` (not this issue's marked shape) must not
    be retried either -- it propagates on the very first attempt, exactly as
    `test_combine_devices_safely_reraises_unrelated_runtimeerror` already
    checks for the un-retried code path, confirmed here by call count so a
    future change cannot silently start retrying (and thus swallowing) an
    unrelated failure."""
    import klayout.db as kdb

    call_count = 0

    def _raise(self):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("some unrelated klayout.db internal error")

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    with pytest.raises(RuntimeError, match="unrelated"):
        lvs._combine_devices_safely(kdb.Netlist(), "layout")

    assert call_count == 1


def test_lvs_end_to_end_survives_combine_devices_circuit_reference_invalidation(
    tmp_path,
):
    """Issue #1185: a successful (non-monkeypatched) retry-path combine
    replaces the netlist's circuits in place via `Netlist.assign()`, which
    invalidates the `kdb.Circuit` object `run_lvs` selected *before* calling
    `_combine_devices_safely`. This end-to-end run exercises that exact path
    with the real multi-finger layout (three fingers combine into one on the
    very first attempt, the common case) -- if `run_lvs` used a stale
    circuit reference afterwards this would raise a SWIG "already destroyed"
    error instead of completing normally."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    report = run_lvs(path)  # must not raise "Object has been destroyed already"

    assert report["status"] == "match"
    assert report["counts"]["devices"]["layout"] == 2
    assert report["counts"]["nets"]["layout"] == 4


def test_lvs_partial_match_combine_devices_runtimeerror_degrades_gracefully(
    tmp_path, monkeypatch
):
    """End-to-end: `klt lvs` with `options.combine_devices: true` does not
    raise an unhandled traceback when KLayout's own `combine_devices()` hits
    the partial-match internal error -- it completes through the normal
    JSON-envelope return, with a `device.combine_incomplete` warning entry
    (one per side) recording that combine did not fully apply, instead of
    the whole run aborting (issue #466).

    Issue #1370 changed the *verdict* this run reports (and only that): the
    engine still reports "the netlists differ", but because the compare the
    caller asked for (a combined one) never ran on either side, that
    difference is no longer reported as a design `"mismatch"` -- it is
    `"inconclusive"`. Everything else here is unchanged from #466."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    report = run_lvs(path)  # must not raise

    assert report["schema_version"] == lvs.SCHEMA_VERSION
    combine_warnings = [
        m for m in report["mismatches"] if m["category"] == "device.combine_incomplete"
    ]
    # Both netlists hit the (monkeypatched) error -- one warning per side.
    assert {m["side"] for m in combine_warnings} == {"layout", "reference"}
    assert all(m["severity"] == "warning" for m in combine_warnings)
    # Neither device count changed -- `combine_devices()` never actually ran
    # (same counts as `test_multifinger_device_mismatches_without_combine_devices`).
    assert report["counts"]["devices"]["layout"] == 3
    assert report["counts"]["devices"]["reference"] == 2
    assert report["status"] == lvs.STATUS_INCONCLUSIVE


# --------------------------------------------------------------------------- #
# options.combine_devices_max_attempts (issue #1412): the caller-configurable
# retry budget behind `_combine_devices_safely`'s bounded-retry mitigation
# (issue #1185), threaded through `run_lvs` end-to-end.
# --------------------------------------------------------------------------- #


def test_parse_combine_devices_max_attempts_defaults_to_module_constant():
    """Omitting `options.combine_devices_max_attempts` resolves to the
    unchanged pre-#1412 default (`_COMBINE_DEVICES_MAX_ATTEMPTS`, `5`)."""
    assert (
        lvs._parse_combine_devices_max_attempts({}) == lvs._COMBINE_DEVICES_MAX_ATTEMPTS
    )
    assert lvs._parse_combine_devices_max_attempts({"combine_devices": True}) == 5


def test_parse_combine_devices_max_attempts_honors_explicit_value():
    """An explicit `options.combine_devices_max_attempts` overrides the
    default, both above and below it."""
    assert (
        lvs._parse_combine_devices_max_attempts({"combine_devices_max_attempts": 12})
        == 12
    )
    assert (
        lvs._parse_combine_devices_max_attempts({"combine_devices_max_attempts": 1})
        == 1
    )


@pytest.mark.parametrize(
    "value",
    [0, -1, 1.5, "5", True, False, None],
)
def test_parse_combine_devices_max_attempts_rejects_malformed_values(value):
    """Anything that is not a positive `int` is a clean `LvsError` -- never a
    silent fallback to the default, matching this module's other
    request-side options (issue #1412). `bool` is explicitly rejected even
    though `isinstance(True, int)` is true in Python -- a caller passing
    `true`/`false` almost certainly meant `options.combine_devices` itself,
    not this budget. `None` is likewise rejected (unlike
    `options.parameter_tolerance`, where `null` means "omitted") because the
    key must be *absent*, not explicitly nulled, to get the default."""
    with pytest.raises(LvsError, match="combine_devices_max_attempts"):
        lvs._parse_combine_devices_max_attempts({"combine_devices_max_attempts": value})


def test_run_lvs_honors_combine_devices_max_attempts_option(tmp_path, monkeypatch):
    """End-to-end: a request-level `options.combine_devices_max_attempts`
    actually bounds the number of `combine_devices()` calls `run_lvs` makes
    per side, not just the module-level default (issue #1412). Forcing every
    attempt to fail and setting the budget to `2` must cost exactly 2 calls
    per side (4 total across layout+reference) -- not the default budget's 5
    per side (10 total) -- and the resulting `device.combine_incomplete`
    warning must disclose that smaller attempt count."""
    import klayout.db as kdb

    call_count = 0

    def _always_raise(self):
        nonlocal call_count
        call_count += 1
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _always_raise)

    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True, "combine_devices_max_attempts": 2},
        },
    )

    report = run_lvs(path)  # must not raise

    assert call_count == 4  # 2 attempts * 2 sides
    combine_warnings = [
        m for m in report["mismatches"] if m["category"] == "device.combine_incomplete"
    ]
    assert {m["side"] for m in combine_warnings} == {"layout", "reference"}
    assert all("after 2 attempts" in m["description"] for m in combine_warnings)
    assert report["status"] == lvs.STATUS_INCONCLUSIVE


def test_run_lvs_combine_devices_max_attempts_rejects_malformed_value(tmp_path):
    """A malformed `options.combine_devices_max_attempts` is a clean
    application error (exit 1 via `LvsError`), surfaced even when
    `options.combine_devices` itself is left at its default `false` -- the
    option is validated unconditionally, the same discipline
    `options.flatten_layout`/`options.flatten_reference` already follow
    (issue #1412)."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices_max_attempts": 0},
        },
    )

    with pytest.raises(LvsError, match="combine_devices_max_attempts"):
        run_lvs(path)


# --------------------------------------------------------------------------- #
# options.combine_devices escape hatches (issue #1370): a device-class
# restriction, a symmetric degrade + `status: "inconclusive"` on retry
# exhaustion, and identifier-carrying `device.combine_incomplete` entries
# --------------------------------------------------------------------------- #

# One inverter drawn with the NMOS folded into two parallel fingers *and* the
# pull-up path drawn as two series resistors, against a reference that lumps
# both. Combining everything matches; combining only `NMOS` folds the fingers
# but leaves the resistor string as two devices.
_MIXED_CLASS_LAYOUT_SPICE = """
.subckt inv A Y VDD VSS
M0 Y A VSS VSS NMOS L=0.15U W=0.325U
M1 Y A VSS VSS NMOS L=0.15U W=0.325U
R0 VDD mid 500
R1 mid Y 500
.ends
"""

_MIXED_CLASS_REFERENCE_SPICE = """
.subckt inv A Y VDD VSS
M0 Y A VSS VSS NMOS L=0.15U W=0.65U
R0 VDD Y 1000
.ends
"""


def test_combine_devices_list_restricts_to_named_device_classes(tmp_path):
    """Issue #1370: a list-valued `options.combine_devices` combines *only*
    the named device classes.

    The same layout is compared three ways against the same fully-lumped
    reference: `true` folds both the MOS fingers and the series resistor
    string (2 devices, match); `["NMOS"]` folds only the fingers, leaving the
    two resistors uncombined (3 devices, no match); `["RES"]` folds only the
    resistors (3 devices, no match). Asserting the device *counts*, not just
    the verdict, is what proves the restriction actually took effect rather
    than combining being skipped or applied wholesale."""
    layout_path = _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _MIXED_CLASS_REFERENCE_SPICE)

    def _run(option, name):
        path = _write_request(
            tmp_path / f"request_{name}.json",
            {
                "layout": {"netlist": layout_path, "top": "inv"},
                "reference": {"netlist": reference_path, "top": "inv"},
                "options": {"combine_devices": option},
            },
        )
        return run_lvs(path)

    everything = _run(True, "all")
    assert everything["counts"]["devices"]["layout"] == 2
    assert everything["status"] == "match"

    # Only the MOS fingers fold: 1 combined NMOS + 2 uncombined resistors.
    nmos_only = _run(["NMOS"], "nmos")
    assert nmos_only["counts"]["devices"]["layout"] == 3
    assert nmos_only["status"] == "mismatch"
    assert nmos_only["options"]["combine_devices"] == ["NMOS"]

    # Only the resistor string folds: 2 uncombined NMOS + 1 combined resistor.
    res_only = _run(["RES"], "res")
    assert res_only["counts"]["devices"]["layout"] == 3
    assert res_only["status"] == "mismatch"
    assert res_only["options"]["combine_devices"] == ["RES"]


def test_combine_devices_list_is_case_insensitive(tmp_path):
    """`NetlistSpiceReader` upper-cases class names read back from SPICE
    (`NMOS`) while an in-process extraction deck writes them as declared
    (`nfet_01v8`) -- so the restriction matches case-insensitively, the same
    convention `apply_resistor_fixed_offset_corrections` already uses (issue
    #585)."""
    layout_path = _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _MIXED_CLASS_REFERENCE_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": ["nmos"]},
        },
    )
    report = run_lvs(path)
    # Same outcome as the upper-cased `["NMOS"]` above: the fingers folded.
    assert report["counts"]["devices"]["layout"] == 3


def test_combine_devices_list_unknown_class_is_a_request_error(tmp_path):
    """Issue #1370 edge case (a): a device class named in the list but
    present in *neither* netlist must not silently no-op (which would look
    exactly like a real `device.unmatched` cascade) -- it is a clean
    `LvsError` naming the unknown class and the ones that do exist."""
    layout_path = _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _MIXED_CLASS_REFERENCE_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": ["NMOS", "NOT_A_CLASS"]},
        },
    )
    with pytest.raises(LvsError) as excinfo:
        run_lvs(path)
    message = str(excinfo.value)
    assert "NOT_A_CLASS" in message
    # The unknown class is named, the valid one is not reported as unknown,
    # and the available classes are listed so the caller can fix the typo.
    assert "NMOS" in message
    assert "RES" in message


def test_combine_devices_list_accepts_class_present_on_only_one_side(tmp_path):
    """A class registered on just one side is legitimate (a layout-only
    parasitic flavour, a reference-only lumped model) -- only "present on
    neither side" is an error, so this must run rather than raise."""
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.subckt inv A Y VDD VSS
M0 Y A VSS VSS NMOS L=0.15U W=0.65U
R0 VDD Y 1000
.ends
""",
    )
    layout_only = _write(
        tmp_path / "layout_only.spice",
        _MIXED_CLASS_LAYOUT_SPICE.replace("R1 mid Y 500", "C1 mid Y 1p"),
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_only, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            # `CAP` exists only on the layout side.
            "options": {"combine_devices": ["CAP"]},
        },
    )
    report = run_lvs(path)  # must not raise
    assert report["options"]["combine_devices"] == ["CAP"]


@pytest.mark.parametrize(
    "value",
    [[], ["NMOS", ""], ["NMOS", 3], "NMOS", 1, {}],
)
def test_combine_devices_rejects_wrong_shaped_values(tmp_path, value):
    """`bool(...)` would happily coerce `"NMOS"`, `1`, `{}`, or an empty list
    into a verdict-shaping flag the caller never meant -- each is a clean
    request error instead (issue #1370), mirroring how `declared_pins` and
    `deck_options` are validated."""
    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": value},
        },
    )
    with pytest.raises(LvsError, match="options.combine_devices"):
        run_lvs(path)


def test_combine_devices_safely_honors_device_class_restriction(tmp_path):
    """Unit-level counterpart of the end-to-end restriction test: with
    `device_classes=["NMOS"]`, `_combine_devices_safely` folds the two MOS
    fingers and leaves the two series resistors alone."""
    import klayout.db as kdb

    netlist = kdb.Netlist()
    netlist.read(
        _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE),
        kdb.NetlistSpiceReader(),
    )

    assert (
        lvs._combine_devices_safely(netlist, "layout", device_classes=["NMOS"]) is None
    )

    circuit = next(iter(netlist.each_circuit()))
    classes = sorted(d.device_class().name for d in circuit.each_device())
    # One folded NMOS plus the two still-separate resistors.
    assert classes == ["NMOS", "RES", "RES"]


# KLayout's own error text, but with a device name filled in -- the shape that
# lets every identifier field resolve. `NetlistSpiceReader` strips the SPICE
# element letter, so `_MULTIFINGER_LAYOUT_SPICE`'s `M1` is device `1` of
# circuit `INV`. The empty-`name=` variant KLayout emits most often is
# `_COMBINE_DEVICES_PARTIAL_MATCH_ERROR` above.
_COMBINE_DEVICES_NAMED_DEVICE_ERROR = (
    "Internal error: Terminal still connected after removing device in "
    "device combination: name=1, circuit=INV, terminal=G in "
    "Netlist.combine_devices"
)


def test_combine_incomplete_entry_carries_circuit_device_and_net(tmp_path, monkeypatch):
    """Issue #1370 criterion 3: the `device.combine_incomplete` entry names
    the failing circuit, device (and its class), and the net wired to the
    reported terminal -- resolved against the netlist the error was raised
    on, not left `null` with only the raw exception string in `description`.
    """
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_NAMED_DEVICE_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    netlist = kdb.Netlist()
    netlist.read(
        _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE),
        kdb.NetlistSpiceReader(),
    )

    warning = lvs._combine_devices_safely(netlist, "layout")

    assert warning is not None
    assert warning["circuit"] == {"layout": "INV", "reference": None}
    # Device `1` of circuit INV is one of the NFET fingers...
    assert warning["device"]["layout"] == "1"
    assert warning["device"]["reference"] is None
    assert warning["device"]["class"] == "NFET"
    # ...and its gate terminal G is wired to net A.
    assert warning["net"] == {"layout": "A", "reference": None}
    # The raw KLayout message is still available verbatim.
    assert warning["details"]["terminal"] == "G"
    assert warning["details"]["klayout_error"] == _COMBINE_DEVICES_NAMED_DEVICE_ERROR


def test_combine_incomplete_falls_back_to_device_class_when_name_is_empty(
    tmp_path, monkeypatch
):
    """KLayout usually reports an *empty* `name=` (the device is mid-removal
    when the invariant trips). The device's own name is then unrecoverable,
    but the class is not: when exactly one device class in the named circuit
    declares the reported terminal, that class is reported under
    `device.class` with both name keys `null` -- never guessed beyond that.

    `_MIXED_CLASS_LAYOUT_SPICE` has exactly the two classes that make this
    testable: `NMOS` (S/G/D/B) and `RES` (A/B), so terminal `G` narrows to
    one class while terminal `B` -- shared by the MOS bulk and the
    resistor's second node -- would not."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(
            "Internal error: Terminal still connected after removing device "
            "in device combination: name=, circuit=INV, terminal=G in "
            "Netlist.combine_devices"
        )

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    netlist = kdb.Netlist()
    netlist.read(
        _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE),
        kdb.NetlistSpiceReader(),
    )

    warning = lvs._combine_devices_safely(netlist, "reference")

    assert warning["circuit"] == {"reference": "INV", "layout": None}
    assert warning["device"] == {"reference": None, "layout": None, "class": "NMOS"}
    # No device instance to hang a net off, so `net` stays honestly null.
    assert warning["net"] is None
    assert warning["details"]["terminal"] == "G"


def test_combine_incomplete_leaves_ambiguous_device_class_null(tmp_path, monkeypatch):
    """The empty-`name=` fallback never guesses: terminal `B` is declared by
    both classes in `_MIXED_CLASS_LAYOUT_SPICE` (the MOS bulk and the
    resistor's second node), so `device` stays `null` rather than picking
    one. Only `circuit` -- which KLayout stated outright -- is populated."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(
            "Internal error: Terminal still connected after removing device "
            "in device combination: name=, circuit=INV, terminal=B in "
            "Netlist.combine_devices"
        )

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    netlist = kdb.Netlist()
    netlist.read(
        _write(tmp_path / "layout.spice", _MIXED_CLASS_LAYOUT_SPICE),
        kdb.NetlistSpiceReader(),
    )

    warning = lvs._combine_devices_safely(netlist, "layout")

    assert warning["circuit"] == {"layout": "INV", "reference": None}
    assert warning["device"] is None
    assert warning["net"] is None


def test_combine_devices_symmetric_degrade_on_asymmetric_failure(tmp_path, monkeypatch):
    """Issue #1370 edge case (b): the retry budget is exhausted on the
    *reference* side only (the layout side combines cleanly on its first
    attempt).

    Without the symmetric degrade, this compares a fully-folded layout
    against an unfolded reference and reports the difference as design
    errors. With it, the layout's successful fold is rolled back too, so both
    sides are compared uncombined -- and because the requested compare never
    ran, the verdict is `"inconclusive"`, not `"mismatch"`."""
    import klayout.db as kdb

    real_combine_devices = kdb.Netlist.combine_devices
    # The layout netlist is combined first (see `run_lvs`); every call after
    # that belongs to the reference side, which always fails.
    state = {"layout_done": False}

    def _layout_ok_reference_always_fails(self):
        if not state["layout_done"]:
            state["layout_done"] = True
            return real_combine_devices(self)
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(
        kdb.Netlist, "combine_devices", _layout_ok_reference_always_fails
    )

    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    report = run_lvs(path)

    assert report["status"] == lvs.STATUS_INCONCLUSIVE
    warnings = [
        m for m in report["mismatches"] if m["category"] == "device.combine_incomplete"
    ]
    # Only the reference side failed -- exactly one warning, on that side.
    assert [m["side"] for m in warnings] == ["reference"]
    # ...but BOTH sides were rolled back: the layout's three drawn devices are
    # back (not the two it had briefly folded into), matching what
    # `options.combine_devices: false` reports for the same inputs.
    assert report["counts"]["devices"]["layout"] == 3
    assert report["counts"]["devices"]["reference"] == 2


def test_combine_devices_degrade_keeps_a_match_verdict(tmp_path, monkeypatch):
    """The downgrade is deliberately one-directional: only a `"mismatch"`
    becomes `"inconclusive"`. An uncombined compare that still *matched* is
    strictly stronger evidence than a combined one would have been (the fold
    turned out to be unnecessary), and this module never re-derives a verdict
    the engine did not reach -- so `"match"` survives the degrade, with the
    `device.combine_incomplete` warning still disclosing that combining did
    not apply."""
    import klayout.db as kdb

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    # Identical netlists: they match with or without any combining.
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["category_counts"]["device.combine_incomplete"] == 2


def test_lvs_cli_exits_4_on_inconclusive(tmp_path, monkeypatch, capsys):
    """Issue #1370: `status: "inconclusive"` gets its own exit code, `4` --
    the same value `klt equiv` already uses for its own unreachable-verdict
    outcome (`cli/equiv_cmd.py`'s `EXIT_INCONCLUSIVE`). Folding it into `3`
    would leave an automation gate unable to tell "the design differs" from
    "the comparison could not be performed"."""
    import klayout.db as kdb

    from klayout_tools.cli import equiv_cmd, lvs_cmd

    # The two verbs agree on the numeric value, by construction.
    assert lvs_cmd.EXIT_INCONCLUSIVE == equiv_cmd.EXIT_INCONCLUSIVE == 4

    def _raise(self):
        raise RuntimeError(_COMBINE_DEVICES_PARTIAL_MATCH_ERROR)

    monkeypatch.setattr(kdb.Netlist, "combine_devices", _raise)

    layout_path = _write(tmp_path / "layout.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"combine_devices": True},
        },
    )

    import argparse

    args = argparse.Namespace(request=path, format="json", check=None, rerun=False)
    assert lvs_cmd.run(args) == lvs_cmd.EXIT_INCONCLUSIVE
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "inconclusive"


def test_rerun_reconstructs_a_device_class_restricted_combine(tmp_path):
    """A committed report whose `options.combine_devices` is a *list* must
    round-trip back into the same restricted compare -- re-asserting a bare
    `true` would re-run an unrestricted combine and then report the
    difference as drift (issue #1370, layered on #1205's echo contract)."""
    committed = {
        "options": {"combine_devices": ["NMOS", "RES"]},
        "layout": "layout.spice",
        "reference": "ref.spice",
        "top": "inv",
    }
    request = lvs._reconstruct_lvs_request(committed)
    assert request["options"]["combine_devices"] == ["NMOS", "RES"]

    # The bool shape still reconstructs as a bare `true`, unchanged.
    committed["options"]["combine_devices"] = True
    assert lvs._reconstruct_lvs_request(committed)["options"]["combine_devices"] is True


# --------------------------------------------------------------------------- #
# Direct classification unit tests (fake logger, no real NetlistComparer run)
# --------------------------------------------------------------------------- #


class _FakeNamed:
    """Stands in for a `klayout.db.Net`/`Device` object: `.expanded_name()`."""

    def __init__(self, name: str) -> None:
        self._name = name

    def expanded_name(self) -> str:
        return self._name


class _FakeSubCircuit:
    """Stands in for a `klayout.db.SubCircuit` object (issue #1132):
    `.expanded_name()` (the instance's own name), `.circuit()` (the
    containing circuit), and `.circuit_ref()` (the circuit it
    instantiates) -- the three attributes
    `_subcircuit_parent_name`/`_subcircuit_ref_name`/`_name_or_none` read
    to populate an unmatched-subcircuit `mismatches[]` entry's
    `circuit`/`instance`/`subcircuit` fields."""

    def __init__(self, name: str, parent: str, ref: str) -> None:
        self._name = name
        self._parent = _FakeNamed(parent)
        self._ref = _FakeNamed(ref)

    def expanded_name(self) -> str:
        return self._name

    def circuit(self) -> _FakeNamed:
        return self._parent

    def circuit_ref(self) -> _FakeNamed:
        return self._ref


class _FakeParam:
    def __init__(self, name: str, param_id: int) -> None:
        self.name = name
        self._id = param_id

    def id(self) -> int:
        return self._id


class _FakeTerminal:
    """Stands in for a `klayout.db.DeviceTerminalDefinition`: `.name` only
    (the attribute `_terminal_names`/`_device_class_arity_mismatch` read)."""

    def __init__(self, name: str) -> None:
        self.name = name


class _FakeDeviceClass:
    def __init__(
        self,
        name: str,
        params: list[_FakeParam] | None = None,
        terminals: list[str] | None = None,
    ) -> None:
        self.name = name
        self._params = params or []
        self._terminals = [_FakeTerminal(t) for t in (terminals or [])]

    def parameter_definitions(self) -> list[_FakeParam]:
        return self._params

    def terminal_definitions(self) -> list[_FakeTerminal]:
        return self._terminals


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
    from the real compare logger (see `lvs._make_compare_logger`).

    Deliberately omits the real logger's net-key/scope bookkeeping
    (`matched_net_keys`, `net_mismatch_keys`, `device_mismatch_scopes`),
    which only the issue #282 minimal-cell recovery reads and which
    `_build_mismatches` treats as optional -- these tests pin the per-category
    classification, not that recovery (which is covered end-to-end against the
    real engine above)."""

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


def test_build_mismatches_device_class_arity_mismatch():
    """Issue #504: a `device_mismatch` event carrying *both* device
    instances, whose classes share a name but declare a different terminal
    list, reports the dedicated `device.class_arity` category -- not the
    generic one-sided `device.unmatched` wording -- naming both terminal
    lists in `details`."""
    a = _FakeDevice("1", _FakeDeviceClass("RES_X", terminals=["A", "B", "W"]), {})
    b = _FakeDevice("R1", _FakeDeviceClass("RES_X", terminals=["A", "B"]), {})
    logger = _FakeLogger()
    logger.device_mismatches.append((a, b))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_DEVICE_CLASS_ARITY
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert entry["device"] == {"layout": "1", "reference": "R1", "class": "RES_X"}
    assert entry["details"] == {
        "layout_terminals": ["A", "B", "W"],
        "reference_terminals": ["A", "B"],
    }


def test_build_mismatches_device_unmatched_same_arity_is_unaffected():
    """A both-sided `device_mismatch` whose classes agree on name *and*
    terminal list (not this issue's case) falls through to the ordinary
    `device.unmatched` classification unchanged."""
    device_class = _FakeDeviceClass("nfet", terminals=["S", "G", "D", "B"])
    a = _FakeDevice("M1", device_class, {})
    b = _FakeDevice("M1", device_class, {})
    logger = _FakeLogger()
    logger.device_mismatches.append((a, b))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_DEVICE_UNMATCHED
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

    # Issue #1132: the circuit-mismatch entry names the circuit that failed
    # to pair (`_FakeNamed` stands in for a bare `kdb.Circuit`, so only
    # `circuit` -- never `instance`/`subcircuit`, which are subcircuit-
    # instance-only fields -- is populated).
    [circuit_entry] = [e for e in entries if e["side"] == "layout" and e["circuit"]]
    assert circuit_entry["circuit"] == {"layout": "SUBA", "reference": None}
    assert circuit_entry["instance"] is None
    assert circuit_entry["subcircuit"] is None

    # The subcircuit-mismatch entry's `instance` names the instance itself
    # from whatever the comparer handed it (`_FakeNamed` only implements
    # `expanded_name()`, not the real `kdb.SubCircuit`'s `circuit()`/
    # `circuit_ref()` -- those degrade to `None` rather than raising, per
    # `_subcircuit_parent_name`/`_subcircuit_ref_name`'s `getattr` guards).
    [subcircuit_entry] = [e for e in entries if e["side"] == "reference"]
    assert subcircuit_entry["instance"] == {"layout": None, "reference": "X1"}
    assert subcircuit_entry["circuit"] == {"layout": None, "reference": None}
    assert subcircuit_entry["subcircuit"] == {"layout": None, "reference": None}


def test_build_mismatches_subcircuit_mismatch_names_circuit_instance_and_type():
    """Issue #1132: given a real `kdb.SubCircuit`-shaped object (stood in by
    `_FakeSubCircuit`, exposing `circuit()`/`circuit_ref()` in addition to
    `expanded_name()`), the unmatched-subcircuit entry names the containing
    circuit, the instance itself, and the circuit it instantiates."""
    logger = _FakeLogger()
    logger.subcircuit_mismatches.append(
        (_FakeSubCircuit("Xfill_1_0", "trng_top", "lib__fill_1"), None)
    )

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_TOPOLOGY
    assert entry["severity"] == "error"
    assert entry["side"] == "layout"
    assert entry["circuit"] == {"layout": "trng_top", "reference": None}
    assert entry["instance"] == {"layout": "Xfill_1_0", "reference": None}
    assert entry["subcircuit"] == {"layout": "lib__fill_1", "reference": None}


def test_build_mismatches_ambiguous_net_is_warning_topology():
    logger = _FakeLogger()
    logger.ambiguous_net_matches.append((_FakeNamed("N1"), _FakeNamed("N2")))

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_TOPOLOGY
    assert entry["severity"] == "warning"
    assert entry["net"] == {"layout": "N1", "reference": "N2"}
    # A `_FakeNamed` stand-in has no `terminal_count()`/`pin_count()` --
    # the single-terminal-both-sides check (issue #596, below) must not
    # error against an object that only implements `expanded_name()`, and
    # falls back to the generic ambiguous-pairing wording.
    assert entry["description"] == (
        "nets were paired ambiguously; the comparer resolved it "
        "structurally (consider a hints.same_nets entry to pin this down)"
    )


class _FakeNetWithCounts(_FakeNamed):
    """Extends `_FakeNamed` with `terminal_count()`/`pin_count()` -- the
    two `kdb.Net` methods issue #596's single-terminal-both-sides check
    reads (`lvs._build_mismatches`'s `logger.ambiguous_net_matches` loop)."""

    def __init__(self, name: str, terminal_count: int = 1, pin_count: int = 0) -> None:
        super().__init__(name)
        self._terminal_count = terminal_count
        self._pin_count = pin_count

    def terminal_count(self) -> int:
        return self._terminal_count

    def pin_count(self) -> int:
        return self._pin_count


def test_build_mismatches_ambiguous_net_both_single_terminal_is_distinct_finding():
    """Issue #596: when both paired nets have exactly one device terminal
    and no declared pin, the `topology`/`"warning"` entry's `description`
    reads as a real connectivity finding, not the routine "resolved it
    structurally" ambiguous-pairing wording -- so a caller checking
    `mismatches[].description` (or a human reading it) does not mistake it
    for a naming nit."""
    logger = _FakeLogger()
    logger.ambiguous_net_matches.append(
        (_FakeNetWithCounts("N1"), _FakeNetWithCounts("N2"))
    )

    (entry,) = lvs._build_mismatches(logger)
    assert entry["category"] == lvs.CATEGORY_TOPOLOGY
    assert entry["severity"] == "warning"
    assert entry["net"] == {"layout": "N1", "reference": "N2"}
    assert "not a routine ambiguous-pairing" in entry["description"]
    assert "single_terminal_nets" in entry["description"]
    assert "resolved it structurally" not in entry["description"]


def test_build_mismatches_ambiguous_net_multi_terminal_stays_generic():
    """A net with 2+ device terminals on either side is a genuinely
    ambiguous structural pairing -- the generic wording is unchanged."""
    logger = _FakeLogger()
    logger.ambiguous_net_matches.append(
        (
            _FakeNetWithCounts("N1", terminal_count=2),
            _FakeNetWithCounts("N2", terminal_count=2),
        )
    )

    (entry,) = lvs._build_mismatches(logger)
    assert entry["description"] == (
        "nets were paired ambiguously; the comparer resolved it "
        "structurally (consider a hints.same_nets entry to pin this down)"
    )


def test_build_mismatches_ambiguous_net_declared_pin_stays_generic():
    """A single-terminal net that is *also* a declared pin is not this
    finding -- a top-level pin with one device terminal is an ordinary,
    expected interface node, not a floating internal net."""
    logger = _FakeLogger()
    logger.ambiguous_net_matches.append(
        (
            _FakeNetWithCounts("N1", pin_count=1),
            _FakeNetWithCounts("N2", pin_count=1),
        )
    )

    (entry,) = lvs._build_mismatches(logger)
    assert entry["description"] == (
        "nets were paired ambiguously; the comparer resolved it "
        "structurally (consider a hints.same_nets entry to pin this down)"
    )


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
    assert all(m["severity"] == "warning" for m in report["mismatches"])

    # Issue #281: every NMOS body terminal is tied to the deck's synthetic
    # substrate net (neither curated deck draws a distinct NMOS tap layer),
    # so the `device.body_unverified` warning always fires for `nfet`; the
    # `pfet` counterpart additionally fires only when the deck has **no** tap
    # mechanism at all -- neither a distinct drawn `tap` layer nor a
    # derivable one (issue #1084: gf180mcu now declares `tap_nplus`/
    # `tap_pplus`, so it no longer qualifies, exactly like sky130's drawn
    # `tap` -- even though neither corpus cell here draws an actual tie).
    from klayout_tools.decks import get_extraction_deck

    deck_config = get_extraction_deck(deck)
    expected_body_classes = {deck_config.nfet_class}
    if (
        deck_config.tap is None
        and deck_config.tap_nplus is None
        and deck_config.tap_pplus is None
    ):
        expected_body_classes.add(deck_config.pfet_class)
    body_unverified_classes = {
        m["device"]["class"]
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BODY_UNVERIFIED
    }
    assert body_unverified_classes == expected_body_classes

    # Both decks also declare a bipolar entry (issue #223: sky130's `pnp`,
    # gf180mcu's `bjt`) that these MOS-only corpus cells draw none of -- one
    # more mismatch, the pre-existing "class declared but zero instances on
    # both sides" warning (#204's downgrade-to-warning precedent), not a
    # real topology defect.
    assert report["mismatch_count"] == 1 + len(expected_body_classes)


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
            # Issue #281: deck-structural, so still present alongside a real
            # defect -- not itself part of what this test corrupted.
            lvs.CATEGORY_DEVICE_BODY_UNVERIFIED,
        }
        for m in report["mismatches"]
    )


# --------------------------------------------------------------------------- #
# Corpus round-trip: machine-generated macro-scale fixture (issue #389)
# --------------------------------------------------------------------------- #

# The same real sky130_fd_sc_hd GCD macro `tests/test_drc.py`/
# `tests/test_precheck.py`/`tests/test_layout_metrics.py` already exercise --
# produced end to end by `klt synthesize` + `klt place-and-route` (real Yosys +
# real OpenROAD). Issue #389 extends the corpus round-trip tier above (which
# runs a *single* hand-drawn standard-cell view) to this macro-scale target:
# thousands of extracted devices, real routing-layer usage, one level of real
# hierarchy -- the piece of the original "DRC/LVS-clean oracle" ask PR #443
# (drc/precheck/layout-metrics) left uncovered. See
# tests/corpus/place_and_route/README.md (in tests/corpus/README.md's
# "Machine-generated macro-scale fixture" section) for full provenance and
# docs/cli/lvs.md's "Machine-generated macro-scale fixture" section for the
# methodology this mirrors from #456.
PLACE_AND_ROUTE_GDS = CORPUS_DIR / "place_and_route" / "gcd.gds.gz"

_skip_no_pnr_fixture = pytest.mark.skipif(
    not PLACE_AND_ROUTE_GDS.is_file(),
    reason="no OpenROAD-produced place-and-route corpus fixture checked in",
)


@_skip_no_pnr_fixture
def test_pnr_gcd_fixture_self_compare_matches_cleanly(tmp_path):
    """The corpus round-trip tier's known-good self-compare, at macro scale:
    the GCD macro's own extracted netlist matches the layout it came from.

    This is the LVS half of the "#391/#443 GCD fixture is a DRC/LVS-clean
    oracle" claim (issue #389). The load-bearing assertion is that extraction
    at this scale (thousands of instances) does not silently drop devices --
    every extracted device is matched on both sides -- and that the compare
    reports `status: "match"` with only the deck-structural warnings the
    hand-drawn corpus round-trip above already documents (the synthetic-
    substrate `device.body_unverified` note plus ambiguous-net/unused-class
    `topology` warnings), never an `error`-severity mismatch."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "gcd.spice")
    extracted = run_extract(str(PLACE_AND_ROUTE_GDS), "sky130", output=reference_path)
    assert extracted["top"] == "gcd"

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(PLACE_AND_ROUTE_GDS), "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"

    # No silently-dropped devices at macro scale: every extracted device is
    # accounted for on both sides. This is the "extraction at thousands of
    # instances completes without dropping devices" edge case from #389's
    # test plan -- the count is large (thousands) rather than a handful.
    devices = report["counts"]["devices"]
    assert devices["layout"] == devices["reference"] == devices["matched"]
    assert devices["matched"] > 1000

    # Nets/pins are a clean `layout == reference == matched` here (issue
    # #619). Before #619 extended `EXTRACTION_DECK.metals`/`.vias` to the
    # full li1-through-met5 stack, this real macro-scale fixture carried 78
    # nets/pins fewer than it should have: this design's real
    # place-and-route routing uses met3-met5 (`_ROUTING_LAYER_RANGE`'s own
    # `"met1-met5"` promise), so with `metals` stopped at met2, 78 top-level
    # I/O pad nets that are only joined to their design's real internal
    # routing through a met3-or-higher segment (e.g. `a_in[2]`, `clk`,
    # `done`) extracted as isolated, device-less orphan nets instead of
    # merging into the connected net they physically are -- each one
    # contributing its own spurious extra `nets`/`pins` entry, and one of
    # them (`clk`, lower-case) case-insensitively colliding with two
    # *already-connected* nets both literally named `CLK` (upper-case)
    # elsewhere in the same design, which is what produced the pre-#619
    # `nets["reference"] == nets["layout"] - 1` off-by-one this test used to
    # pin (see issue #539, which made that orphan `clk` net visible instead
    # of silently purging it). #619's `metals`/`vias` extension merges all
    # 78 into their real internal nets on both sides identically, so the
    # collision -- and the asymmetry it caused -- no longer arises.
    #
    # Re-pinned for #1443: this fixture was regenerated with #1442's
    # row-rail fix applied (filler cells now close every standard-cell row
    # gap, which also resolves #1430's nwell DRC violations as a side
    # effect). The design's connectivity is unchanged, but the routed
    # layout's own net/pin counts shifted -- the new row-rail obstruction
    # and the filler cells it un-gates change downstream
    # placement/CTS/repair, which changes how many synthesis-numbered
    # internal nets survive into the routed layout (see the analogous
    # `tests/test_extract.py::test_def_net_names_recovers_...` docstring).
    nets = report["counts"]["nets"]
    assert nets["layout"] == nets["reference"] == nets["matched"] == 2048
    pins = report["counts"]["pins"]
    assert pins["layout"] == pins["reference"] == pins["matched"] == 541

    # A clean self-compare carries only warnings -- the same deck-structural
    # signal the hand-drawn corpus round-trip tier above documents, never an
    # `error` that would break equivalence.
    assert all(m["severity"] == "warning" for m in report["mismatches"])
    assert "device.property" not in report["category_counts"]
    assert lvs.CATEGORY_NET_UNMATCHED not in report["category_counts"]
    assert lvs.CATEGORY_DEVICE_UNMATCHED not in report["category_counts"]

    # Inline extraction pins the sky130 deck in the shared provenance block.
    assert report["provenance"]["deck"]["name"] == "sky130"


@_skip_no_pnr_fixture
def test_pnr_gcd_fixture_device_property_corruption_is_caught(tmp_path):
    """The corpus round-trip tier's deliberately-broken variant, at macro
    scale and in the standard-cell region: change exactly one transistor's
    drawn width in the reference netlist (a `device.property` corruption, the
    same negative control #456 used for the mixed layout) and confirm `klt
    lvs` catches it -- `status: "mismatch"`, a single `device.property` entry
    naming that exact instance, correctly attributed rather than smeared
    across the ~4000 other, unchanged standard-cell devices."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "gcd.spice")
    extracted = run_extract(str(PLACE_AND_ROUTE_GDS), "sky130", output=reference_path)

    # Corrupt exactly one standard-cell-region transistor's width: rewrite the
    # first device line's `W=` value, leaving every other line (and all
    # connectivity) untouched, so the injected defect is a pure parameter
    # change on a single instance.
    lines = Path(reference_path).read_text().splitlines(keepends=True)
    corrupted = 0
    for i, line in enumerate(lines):
        if line.startswith("M") and "W=0.42U" in line:
            lines[i] = line.replace("W=0.42U", "W=1.0U", 1)
            corrupted += 1
            break
    assert corrupted == 1, "expected to corrupt exactly one device line"
    broken_path = tmp_path / "gcd_broken.spice"
    broken_path.write_text("".join(lines))

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(PLACE_AND_ROUTE_GDS), "deck": "sky130"},
            "reference": {"netlist": str(broken_path), "top": extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"].get("device.property") == 1

    device_property = [
        m for m in report["mismatches"] if m["category"] == "device.property"
    ]
    assert len(device_property) == 1
    entry = device_property[0]
    assert entry["severity"] == "error"
    assert entry["device"]["class"] == "nfet"
    # Correctly attributed to the one corrupted instance (its layout/reference
    # device name), not misattributed or smeared across the fabric.
    assert entry["device"]["layout"] == entry["device"]["reference"]
    assert "w_um" in entry["description"]


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
# Issue #491: array dummy-device suppression reaches `klt lvs` on sky130
# --------------------------------------------------------------------------- #


def test_lvs_res_array_dummy_suppression_no_unmatched_device(tmp_path, monkeypatch):
    """A reference schematic that (correctly) omits an array's dummy units
    must not see them reported as `device.unmatched` against the physical
    layout, now that sky130's curated deck declares a `dummy` marker layer
    and `res_array` draws it (issue #491) -- the deck+generator half of a fix
    whose extractor-side dummy-suppression guard (issue #295/#462) already
    shipped. `dummy: 0` output (the pre-#491 workaround) stands in for the
    reference schematic's own real-cells-only topology; `dummy: 2` output is
    the physical layout with edge fill."""
    from klayout_tools import pdk
    from klayout_tools.extract import run_extract
    from klayout_tools.gen import generate

    monkeypatch.delenv("PDK_ROOT", raising=False)
    monkeypatch.delenv("PDK", raising=False)
    monkeypatch.setattr(pdk, "STORE_DIRS", [])
    monkeypatch.setattr(pdk, "CONVENTIONAL_PREFIXES", [])

    pdk_root = tmp_path / "pdk_install"
    (pdk_root / "sky130A" / "libs.tech").mkdir(parents=True)

    reference_layout_path = tmp_path / "res_array_ref.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 4, "dummy": 0},
            "options": {"output": str(reference_layout_path)},
        }
    )
    reference_path = tmp_path / "ref.spice"
    ref_extracted = run_extract(
        str(reference_layout_path), "sky130", output=str(reference_path)
    )

    layout_path = tmp_path / "res_array_dummy.gds"
    generate(
        {
            "generator": "res_array",
            "pdk": {"variant": "sky130A", "root": str(pdk_root)},
            "params": {"num": 4, "dummy": 2},
            "options": {"output": str(layout_path)},
        }
    )
    layout_extracted_path = tmp_path / "lay.spice"
    layout_extracted = run_extract(
        str(layout_path), "sky130", output=str(layout_extracted_path)
    )

    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {
                "netlist": str(layout_extracted_path),
                "top": layout_extracted["top"],
            },
            "reference": {"netlist": str(reference_path), "top": ref_extracted["top"]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 4, "reference": 4, "matched": 4}
    assert not any(m["category"] == "device.unmatched" for m in report["mismatches"])


# --------------------------------------------------------------------------- #
# Issue #504: bulk-terminal device class vs. plain-element reference (arity)
# --------------------------------------------------------------------------- #

# The issue's exact minimal reproduction: a `bulk_to_substrate` resistor
# flavour's layout-side `R` card carries a third (bulk) node, while a
# schematic-derived reference's plain-element `R` card for the same model
# name carries only the two ordinary terminals.
_RES_BULK_LAYOUT_SPICE = """
.SUBCKT top A B BULK
R1 A B BULK 1000 res_x
.ENDS top
"""

_RES_PLAIN_REFERENCE_SPICE = """
.SUBCKT top A B
RR1 A B 1000 res_x
.ENDS top
"""


def test_lvs_bulk_resistor_vs_plain_reference_reports_class_arity(tmp_path):
    """Issue #504's own reproduction: previously this collapsed into an
    unattributable `device.unmatched` + `net.unmatched` cascade with no
    entry naming the actual cause. Now a dedicated `device.class_arity`
    entry names both classes' terminal lists."""
    layout_path = _write(tmp_path / "layout.spice", _RES_BULK_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _RES_PLAIN_REFERENCE_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    arity_entries = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_CLASS_ARITY
    ]
    assert len(arity_entries) == 1
    (entry,) = arity_entries
    assert entry["severity"] == "error"
    assert entry["side"] == "both"
    assert entry["device"] == {"layout": "1", "reference": "R1", "class": "RES_X"}
    assert entry["details"] == {
        "layout_terminals": ["A", "B", "W"],
        "reference_terminals": ["A", "B"],
    }
    # Not just a generic device.unmatched entry for this device pair -- the
    # dedicated category replaces it (see this module's docstring).
    assert not any(
        m["category"] == lvs.CATEGORY_DEVICE_UNMATCHED
        and m["device"] == {"layout": "1", "reference": "R1", "class": "RES_X"}
        for m in report["mismatches"]
    )


def test_lvs_bulk_resistor_arity_mismatch_does_not_collapse_other_matches(tmp_path):
    """The arity mismatch on one resistor must not collapse the whole
    compare to `0/0` matched devices/nets -- other, correctly-matched
    devices elsewhere in the same circuit still report as matched (the
    issue's acceptance criterion distinguishing this from an unattributable
    total failure)."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B BULK VDD VSS
R1 A B BULK 1000 res_x
M1 A B VSS VSS nfet W=0.65U L=0.15U
M2 A B VDD VDD pfet W=1.0U L=0.15U
.ENDS top
""",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B VDD VSS
RR1 A B 1000 res_x
M1 A B VSS VSS nfet W=0.65U L=0.15U
M2 A B VDD VDD pfet W=1.0U L=0.15U
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {"netlist": reference_path, "top": "top"},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["counts"]["devices"]["matched"] == 2
    assert report["counts"]["nets"]["matched"] >= 2
    assert any(
        m["category"] == lvs.CATEGORY_DEVICE_CLASS_ARITY for m in report["mismatches"]
    )


# --------------------------------------------------------------------------- #
# Issue #506: reference.device_bulk reconciles the arity #504/#505 only
# diagnosed
# --------------------------------------------------------------------------- #


def _device_bulk_request(tmp_path, device_bulk, **extra):
    """The #504 minimal repro (bulk-terminal resistor layout side vs. plain
    two-node reference), with `reference.device_bulk` supplied."""
    layout_path = _write(tmp_path / "layout.spice", _RES_BULK_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _RES_PLAIN_REFERENCE_SPICE)
    reference = {"netlist": reference_path, "top": "top"}
    if device_bulk is not None:
        reference["device_bulk"] = device_bulk
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": reference,
    }
    request.update(extra)
    return _write_request(tmp_path / "request.json", request)


def test_reference_device_bulk_reconciles_arity_into_a_match(tmp_path):
    """Issue #506 (issue #504's option 1): declaring which reference net the
    layout-side class's implicit bulk terminal carries normalizes the
    reference class to the deck's own arity before the compare runs, so the
    repro #505 could only diagnose now reports `status: "match"`."""
    path = _device_bulk_request(tmp_path, {"res_x": "BULK"})
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 1, "reference": 1, "matched": 1}
    assert report["counts"]["nets"]["matched"] == 3
    # The reconciliation replaces the diagnostic: the class the hook covers
    # is no longer of differing arity by the time `compare()` sees it.
    assert not any(
        m["category"] == lvs.CATEGORY_DEVICE_CLASS_ARITY for m in report["mismatches"]
    )


def test_reference_device_bulk_discloses_the_reconciliation(tmp_path):
    """A match reached through the hook is never silently indistinguishable
    from a fully independent one -- the same disclosure discipline
    `device.body_unverified` applies to an unverified MOS body."""
    path = _device_bulk_request(tmp_path, {"res_x": "BULK"})
    report = run_lvs(path)

    entries = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BULK_RECONCILED
    ]
    assert len(entries) == 1
    (entry,) = entries
    assert entry["severity"] == "warning"
    assert entry["side"] == "reference"
    assert entry["device"] == {"layout": None, "reference": None, "class": "RES_X"}
    assert entry["details"] == {
        "terminal": "W",
        "reference_net": "BULK",
        "reference_net_created": True,
        "devices": 1,
        "layout_terminals": ["A", "B", "W"],
        "reference_terminals": ["A", "B"],
    }
    assert "not independently verified" in entry["description"]
    # Disclosed in-band without changing the verdict (`warning`), and
    # counted like any other category.
    assert report["category_counts"][lvs.CATEGORY_DEVICE_BULK_RECONCILED] == 1
    assert report["mismatch_count"] == 1


def test_reference_device_bulk_matches_the_model_name_case_insensitively(tmp_path):
    """`NetlistSpiceReader` upper-cases a model name (`res_x` -> `RES_X`), so
    a request written in the netlist's own lower-case spelling resolves."""
    (tmp_path / "lower").mkdir()
    (tmp_path / "upper").mkdir()
    lower = run_lvs(_device_bulk_request(tmp_path / "lower", {"res_x": "BULK"}))
    upper = run_lvs(_device_bulk_request(tmp_path / "upper", {"RES_X": "BULK"}))

    assert lower["status"] == upper["status"] == "match"


def test_reference_device_bulk_binds_an_existing_reference_net(tmp_path):
    """The synthesized-substrate case: the layout's bulk terminal lands on
    the deck's own `vsubs` net while the reference models a real `VSS`.
    `reference.device_bulk` binds the added terminal to that *existing*
    reference net (`reference_net_created: false`) and composes with a
    `hints.same_nets` pairing for the differing net names."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B VDD vsubs
R1 A B vsubs 1000 res_x
M1 A B vsubs vsubs nfet W=0.65U L=0.15U
.ENDS top
""",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B VDD VSS
RR1 A B 1000 res_x
M1 A B VSS VSS nfet W=0.65U L=0.15U
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"res_x": "VSS"},
            },
            "hints": {"same_nets": [["vsubs", "VSS"]]},
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    (entry,) = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_BULK_RECONCILED
    ]
    assert entry["details"]["reference_net"] == "VSS"
    assert entry["details"]["reference_net_created"] is False
    assert not any(
        m["category"] == lvs.CATEGORY_HINTS_REJECTED for m in report["mismatches"]
    )


def test_reference_device_bulk_keeps_other_devices_matched(tmp_path):
    """A circuit that mixes the reconciled class with ordinary, independently
    matching devices still matches all of them -- the hook must not collapse
    `counts.devices.matched`/`counts.nets.matched` (#505's own regression
    criterion, restated for the reconciling path)."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B BULK VDD VSS
R1 A B BULK 1000 res_x
M1 A B VSS VSS nfet W=0.65U L=0.15U
M2 A B VDD VDD pfet W=1.0U L=0.15U
.ENDS top
""",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B VDD VSS
RR1 A B 1000 res_x
M1 A B VSS VSS nfet W=0.65U L=0.15U
M2 A B VDD VDD pfet W=1.0U L=0.15U
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"res_x": "BULK"},
            },
        },
    )
    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["counts"]["devices"] == {"layout": 3, "reference": 3, "matched": 3}
    assert report["counts"]["nets"]["matched"] == 5
    assert report["category_counts"] == {lvs.CATEGORY_DEVICE_BULK_RECONCILED: 1}


def test_reference_device_bulk_leaves_an_uncovered_class_diagnosed(tmp_path):
    """A second bulk-terminal class the request does *not* name still reports
    `device.class_arity` -- the hook reconciles only what it declares, and
    never turns a remaining arity gap into a silent pass."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B BULK
R1 A B BULK 1000 res_x
R2 B A BULK 2000 res_y
.ENDS top
""",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B
RR1 A B 1000 res_x
RR2 B A 2000 res_y
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"res_x": "BULK"},
            },
        },
    )
    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"][lvs.CATEGORY_DEVICE_BULK_RECONCILED] == 1
    arity = [
        m
        for m in report["mismatches"]
        if m["category"] == lvs.CATEGORY_DEVICE_CLASS_ARITY
    ]
    assert len(arity) == 1
    assert arity[0]["device"]["class"] == "RES_Y"


def test_reference_device_bulk_unknown_reference_class_raises(tmp_path):
    """A model name that resolves on neither side is a malformed request
    (exit 1), never a silent no-op -- `hints.same_nets`'s own convention."""
    path = _device_bulk_request(tmp_path, {"res_nope": "BULK"})

    with pytest.raises(LvsError, match="not found in the reference netlist"):
        run_lvs(path)


def test_reference_device_bulk_unknown_layout_class_raises(tmp_path):
    """The reference class exists but the layout side has no same-named class
    to take a terminal list from."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B
R1 A B 1000 res_other
.ENDS top
""",
    )
    reference_path = _write(tmp_path / "ref.spice", _RES_PLAIN_REFERENCE_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"res_x": "BULK"},
            },
        },
    )

    with pytest.raises(LvsError, match="not found in the layout netlist"):
        run_lvs(path)


def test_reference_device_bulk_on_an_already_matching_class_raises(tmp_path):
    """Nothing to reconcile (both sides already declare the same terminal
    list) is an error, not a no-op that silently rewrites real reference
    connectivity."""
    layout_path = _write(tmp_path / "layout.spice", _RES_BULK_LAYOUT_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B BULK
RR1 A B BULK 1000 res_x
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"res_x": "BULK"},
            },
        },
    )

    with pytest.raises(LvsError, match="already declares every terminal"):
        run_lvs(path)


def test_reference_device_bulk_multiple_missing_terminals_raises(tmp_path):
    """The hook reconciles exactly one extra terminal per class (the entry
    names exactly one net); a class two or more terminals apart is an error
    rather than an arbitrary guess about which net the rest carry."""
    layout_path = _write(
        tmp_path / "layout.spice",
        """
.SUBCKT top A B C S
Q1 A B C S qnpn
.ENDS top
""",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        """
.SUBCKT top A B
RR1 A B 1000 qnpn
.ENDS top
""",
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "top"},
            "reference": {
                "netlist": reference_path,
                "top": "top",
                "device_bulk": {"qnpn": "S"},
            },
        },
    )

    with pytest.raises(LvsError, match="exactly one extra"):
        run_lvs(path)


def test_reference_device_bulk_must_be_an_object(tmp_path):
    path = _device_bulk_request(tmp_path, ["res_x", "BULK"])

    with pytest.raises(LvsError, match="device_bulk must be a JSON object"):
        run_lvs(path)


def test_reference_device_bulk_net_must_be_a_string(tmp_path):
    path = _device_bulk_request(tmp_path, {"res_x": 3})

    with pytest.raises(LvsError, match="must be a non-empty reference net name"):
        run_lvs(path)


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
    # Issue #589: the tolerance line is only printed when opted in, so the
    # default human output is unchanged.
    assert "parameter_tolerance" not in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_text_reports_an_opted_in_parameter_tolerance(tmp_path, capsys):
    """Issue #589: a verdict reached under a caller-supplied design tolerance
    says so in the human output too, right next to `status`, and exits `0`
    like any other match."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice", _INVERTER_SPICE.replace("W=1.0U", "W=1.0009U")
    )
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"parameter_tolerance": 0.001},
        },
    )
    exit_code = main(["lvs", request_path])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "parameter_tolerance: 0.001" in out
    assert "status: match" in out
    assert "[warning] device.parameter_tolerated" in out


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


def test_cli_inline_json_request(tmp_path, capsys):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    exit_code = main(["lvs", json.dumps(request), "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "match"


def test_cli_stdin_request(tmp_path, capsys, monkeypatch):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request)))
    exit_code = main(["lvs", "-", "--format", "json"])
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "match"


# --------------------------------------------------------------------------- #
# Reference netlist form: subckt-call -> plain-element conversion + detection
# (issue #280)
# --------------------------------------------------------------------------- #

from klayout_tools.netlist_normalize import (  # noqa: E402
    NormalizeError,
    detect_subckt_call_devices,
    normalize_reference_netlist,
)

# sky130 inverter in the *simulation* (subckt-call) form a real xschem/ngspice
# flow emits: `X`-card instances of the PDK's primitive MOS `.subckt`, carrying
# parasitic params `klt lvs`'s plain-element form does not model. Converts to
# exactly `_INVERTER_SPICE`'s topology/parameters.
_INVERTER_SUBCKT_CALL_SKY130 = """
.subckt inv A Y VPWR VGND
XM1 Y A VGND VGND sky130_fd_pr__nfet_01v8 L=0.15u W=0.65u nf=1 ad=1e-12 as=1e-12
+ pd=2u ps=2u
XM2 Y A VPWR VPWR sky130_fd_pr__pfet_01v8 L=0.15u W=1.0u
.ends
"""

# The same inverter using gf180mcu device names and gf180mcu's real in-the-wild
# SI-metre geometry convention (`w=6.5e-7` == 0.65 um), plus a `+` continuation.
_INVERTER_SUBCKT_CALL_GF180 = """
.subckt inv A Y VPWR VGND
XM1 Y A VGND VGND nfet_03v3 L=1.5e-7 W=6.5e-7 m=1
+ ad='int((nf+1)/2) * w/nf * 0.18u' nrd=0.1
XM2 Y A VPWR VPWR pfet_03v3 L=1.5e-7 W=1e-6
.ends
"""


def test_normalize_sky130_device_names_resolve_auto():
    out = normalize_reference_netlist(_INVERTER_SUBCKT_CALL_SKY130)
    assert "M1 Y A VGND VGND nfet L=0.15U W=0.65U" in out
    assert "M2 Y A VPWR VPWR pfet L=0.15U W=1U" in out
    # Parasitic-only params are dropped, not carried onto the plain-element card.
    for dropped in ("ad=", "as=", "pd=", "ps=", "nf="):
        assert dropped not in out
    assert "sky130_fd_pr__" not in out


def test_normalize_gf180_device_names_resolve_with_deck():
    out = normalize_reference_netlist(_INVERTER_SUBCKT_CALL_GF180, deck="gf180mcu")
    # SI-metre geometry converts to explicit micrometre-suffixed literals.
    assert "M1 Y A VGND VGND nfet L=0.15U W=0.65U" in out
    assert "M2 Y A VPWR VPWR pfet L=0.15U W=1U" in out
    assert "nfet_03v3" not in out and "pfet_03v3" not in out


def test_normalize_unit_suffix_conversion():
    out = normalize_reference_netlist(
        "XM1 d g s b nfet_03v3 L=0.5u W=1u\n", deck="gf180mcu"
    )
    assert "L=0.5U" in out
    assert "W=1U" in out


def test_normalize_si_and_eng_suffixes_convert_to_um():
    # 500n == 0.5 um; 1.5e-6 m == 1.5 um.
    out = normalize_reference_netlist(
        "XM1 d g s b nfet_03v3 L=500n W=1.5e-6\n", deck="gf180mcu"
    )
    assert "L=0.5U" in out
    assert "W=1.5U" in out


def test_normalize_unknown_subckt_name_fails_clearly():
    with pytest.raises(NormalizeError, match="not a known"):
        normalize_reference_netlist(
            "XM1 d g s b totally_made_up L=0.5u W=1u\n", deck="sky130"
        )


def test_normalize_wrong_deck_device_name_fails():
    # A gf180mcu device name against the sky130 deck's map is not resolvable.
    with pytest.raises(NormalizeError, match="not a known device"):
        normalize_reference_netlist(
            "XM1 d g s b nfet_03v3 L=0.5u W=1u\n", deck="sky130"
        )


def test_normalize_device_map_override():
    out = normalize_reference_netlist(
        "XM1 d g s b my_custom_nfet L=0.5u W=1u\n",
        device_map={"my_custom_nfet": "nfet"},
    )
    assert "M1 d g s b nfet L=0.5U W=1U" in out


def test_normalize_device_map_non_mos_terminal_count_gets_clear_error():
    # issue #1163: a device_map entry naming a 2-terminal (or otherwise
    # non-MOS-shaped) subcircuit must not surface the opaque generic
    # "expected 4 terminals" mismatch -- device_map only supports
    # MOS-shaped overrides today, so the error must say so explicitly.
    with pytest.raises(NormalizeError, match="device_map only supports MOS-shaped"):
        normalize_reference_netlist(
            "XR1 a b my_custom_res L=1u W=1u\n",
            device_map={"my_custom_res": "res_generic_po"},
        )


def test_normalize_deck_mos_terminal_count_keeps_generic_error():
    # The same wrong-terminal-count case, but resolved from `deck` (a
    # genuinely malformed MOS call), keeps the original generic message --
    # the device_map-specific error is only for device_map-sourced bindings.
    with pytest.raises(NormalizeError, match="expected 4 terminals") as excinfo:
        normalize_reference_netlist(
            "XM1 d g s nfet_03v3 L=0.5u W=1u\n", deck="gf180mcu"
        )
    assert "device_map" not in str(excinfo.value)


# --- issue #1271: object-valued device_map (non-MOS overrides) ------------- #


def test_normalize_device_map_object_resistor_override():
    out = normalize_reference_netlist(
        "XR1 r0 r1 my_custom_res l=48.2u w=1u\n",
        device_map={"my_custom_res": {"kind": "resistor", "class": "res_generic_po"}},
    )
    assert out.strip() == "R1 r0 r1 0 res_generic_po L=48.2U W=1U"


def test_normalize_device_map_object_capacitor_override_custom_params():
    # A custom call-site length/width spelling (mirrors gf180mcu's
    # c_length/c_width) is honored, not just the "l"/"w" default.
    out = normalize_reference_netlist(
        "XC1 c0 c1 my_custom_cap my_l=2u my_w=2u\n",
        device_map={
            "my_custom_cap": {
                "kind": "capacitor",
                "class": "cap_x",
                "length_param": "my_l",
                "width_param": "my_w",
            }
        },
    )
    assert out.strip() == "C1 c0 c1 0 cap_x A=4P P=8U"


def test_normalize_device_map_object_bipolar_override():
    # Bipolar carries no geometry call-site parameter at all.
    out = normalize_reference_netlist(
        "XQ1 c b e my_custom_pnp mult=4\n",
        device_map={"my_custom_pnp": {"kind": "bipolar", "class": "pnp_custom"}},
    )
    assert out.strip() == "Q1 c b e pnp_custom NE=4"


def test_normalize_device_map_bare_string_still_means_mos():
    # Regression guard (issue #1271 acceptance criteria): the original
    # bare-string shape must continue to mean a MOS l/w binding, unchanged.
    out = normalize_reference_netlist(
        "XM1 d g s b my_custom_nfet L=0.5u W=1u\n",
        device_map={"my_custom_nfet": "nfet"},
    )
    assert out.strip() == "M1 d g s b nfet L=0.5U W=1U"


def test_normalize_device_map_object_non_mos_malformed_terminal_count_still_errors():
    # A correctly-kind-resolved but genuinely malformed non-MOS entry (wrong
    # terminal count) must still fail with a clear, named error -- the
    # #1163 clear-error path is for the previously-unsupported case (a
    # bare-string entry naming a non-MOS device), not a blanket suppression
    # of terminal-count checking once a kind is given.
    with pytest.raises(NormalizeError, match="expected 2 terminals"):
        normalize_reference_netlist(
            "XC1 c0 c1 c2 my_custom_cap l=1u w=1u\n",
            device_map={"my_custom_cap": {"kind": "capacitor", "class": "cap_x"}},
        )


@pytest.mark.parametrize(
    "value,match",
    [
        (123, "value must be a device-class string or an object"),
        ({"class": "res_generic_po"}, "'kind' must be one of"),
        ({"kind": "diode", "class": "x"}, "'kind' must be one of"),
        ({"kind": "resistor"}, "requires a non-empty 'class'"),
        ({"kind": "resistor", "class": ""}, "requires a non-empty 'class'"),
        (
            {"kind": "resistor", "class": "res_x", "length_param": ""},
            "'length_param' must be a non-empty string",
        ),
        (
            {"kind": "resistor", "class": "res_x", "width_param": 5},
            "'width_param' must be a non-empty string",
        ),
    ],
)
def test_normalize_device_map_object_malformed_entry_rejected(value, match):
    with pytest.raises(NormalizeError, match=match):
        normalize_reference_netlist(
            "XR1 r0 r1 my_res l=1u w=1u\n", device_map={"my_res": value}
        )


def test_run_lvs_reference_device_map_object_entry_accepted(tmp_path):
    # request.reference.device_map's top-level shape check (a JSON object)
    # already accepts object-valued entries -- per-entry validation happens
    # in netlist_normalize, surfaced as an LvsError.
    layout_path = _write(
        tmp_path / "layout.spice",
        ".subckt res_block A B\nR1 A B 0 res_generic_po L=48.2U W=1U\n.ends\n",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        ".subckt res_block A B\nXR1 A B my_custom_res l=48.2u w=1u\n.ends\n",
    )
    request = {
        "layout": {"netlist": layout_path, "top": "res_block"},
        "reference": {
            "netlist": reference_path,
            "top": "res_block",
            "form": "subckt-call",
            "device_map": {
                "my_custom_res": {"kind": "resistor", "class": "res_generic_po"}
            },
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


def test_run_lvs_reference_device_map_malformed_object_entry_is_lvs_error(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SUBCKT_CALL_SKY130)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {
            "netlist": reference_path,
            "top": "inv",
            "form": "subckt-call",
            "device_map": {"nfet": {"kind": "not-a-real-kind", "class": "x"}},
        },
    }
    with pytest.raises(LvsError, match="'kind' must be one of"):
        run_lvs(json.dumps(request))


@pytest.mark.parametrize("param,value", [("nf", "2"), ("m", "4"), ("mult", "2")])
def test_normalize_multiplicity_gt_one_rejected(param, value):
    with pytest.raises(NormalizeError, match="multi-finger/multiplied"):
        normalize_reference_netlist(
            f"XM1 d g s b nfet_03v3 L=0.5u W=1u {param}={value}\n", deck="gf180mcu"
        )


@pytest.mark.parametrize("param", ["nf", "m", "mult"])
def test_normalize_multiplicity_of_one_is_dropped_not_rejected(param):
    out = normalize_reference_netlist(
        f"XM1 d g s b nfet_03v3 L=0.5u W=1u {param}=1\n", deck="gf180mcu"
    )
    assert "M1 d g s b nfet L=0.5U W=1U" in out
    assert f"{param}=" not in out


def test_normalize_wrong_terminal_count_fails():
    # A device-like X card (has l/w) with the wrong number of nodes is an error.
    with pytest.raises(NormalizeError, match="expected 4 terminals"):
        normalize_reference_netlist(
            "XM1 d g s nfet_03v3 L=0.5u W=1u\n", deck="gf180mcu"
        )


def test_normalize_expression_valued_geometry_fails():
    with pytest.raises(NormalizeError, match="not a plain numeric literal"):
        normalize_reference_netlist(
            "XM1 d g s b nfet_03v3 L='w/2' W=1u\n", deck="gf180mcu"
        )


def test_normalize_non_device_subckt_passes_through():
    # An X card with no l/w is a genuine hierarchical subcircuit instance and
    # must pass through untouched (not be mistaken for a device).
    text = "X1 A Y VPWR VGND some_hierarchical_block\n"
    assert normalize_reference_netlist(text).strip() == text.strip()


def test_normalize_mixed_plain_and_subckt_call_lines():
    mixed = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
XM2 Y A VPWR VPWR sky130_fd_pr__pfet_01v8 L=0.15u W=1.0u
.ends
"""
    out = normalize_reference_netlist(mixed)
    # The plain-element M card passes through verbatim; the X card converts.
    assert "M1 Y A VGND VGND nfet W=0.65U L=0.15U" in out
    assert "M2 Y A VPWR VPWR pfet L=0.15U W=1U" in out


def test_detect_reports_undefined_known_device():
    assert detect_subckt_call_devices(_INVERTER_SUBCKT_CALL_SKY130) == [
        "sky130_fd_pr__nfet_01v8",
        "sky130_fd_pr__pfet_01v8",
    ]


def test_detect_ignores_defined_subckt():
    # A device subcircuit that is actually defined in the file is not reported
    # (the reader can read it as a subcircuit -- a different, out-of-scope case).
    text = ".subckt nfet_03v3 d g s b\n.ends\nXM1 d g s b nfet_03v3 L=0.5u W=1u\n"
    assert detect_subckt_call_devices(text) == []


def test_detect_empty_for_plain_element_netlist():
    assert detect_subckt_call_devices(_INVERTER_SPICE) == []


# --- integration through run_lvs -------------------------------------------- #


def test_run_lvs_subckt_call_reference_converts_and_matches(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SUBCKT_CALL_SKY130)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv", "form": "subckt-call"},
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    assert report["mismatch_count"] == 0


def test_run_lvs_subckt_call_reference_gf180_converts_and_matches(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SUBCKT_CALL_GF180)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {
            "netlist": reference_path,
            "top": "inv",
            "form": "subckt-call",
            "deck": "gf180mcu",
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


def test_run_lvs_default_form_detects_subckt_call_and_errors(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SUBCKT_CALL_SKY130)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    with pytest.raises(LvsError, match="simulation .subcircuit-call. form"):
        run_lvs(json.dumps(request))


def test_run_lvs_invalid_reference_form_raises(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv", "form": "bogus"},
    }
    with pytest.raises(LvsError, match="reference.form must be one of"):
        run_lvs(json.dumps(request))


def test_run_lvs_reference_device_map_must_be_object(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {
            "netlist": reference_path,
            "top": "inv",
            "device_map": ["not", "an", "object"],
        },
    }
    with pytest.raises(LvsError, match="device_map must be a JSON object"):
        run_lvs(json.dumps(request))


def test_run_lvs_subckt_call_unknown_device_errors(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice",
        ".subckt inv A Y VPWR VGND\n"
        "XM1 Y A VGND VGND not_a_real_device L=0.15u W=0.65u\n"
        ".ends\n",
    )
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv", "form": "subckt-call"},
    }
    with pytest.raises(LvsError, match="could not convert subckt-call"):
        run_lvs(json.dumps(request))


# --------------------------------------------------------------------------- #
# netlist_normalize.py device-family extension: resistor/capacitor/bipolar
# (issue #1130)
# --------------------------------------------------------------------------- #


def test_normalize_sky130_resistor_carries_lw():
    out = normalize_reference_netlist(
        "XR1 r0 r1 sky130_fd_pr__res_generic_po l=48.2u w=1u\n", deck="sky130"
    )
    assert out.strip() == "R1 r0 r1 0 res_generic_po L=48.2U W=1U"


def test_normalize_gf180_resistor_carries_r_length_r_width():
    # gf180mcu's resistor call-site params are spelled r_length/r_width (not
    # l/w), in SI metres (a real in-the-wild convention, see pdk_models.py).
    out = normalize_reference_netlist(
        "XR1 1 2 3 ppolyf_u r_width=800e-9 r_length=1.6e-6 m=1.0 par=1\n",
        deck="gf180mcu",
    )
    assert out.strip() == "R1 1 2 3 0 ppolyf_u L=1.6U W=0.8U"


def test_normalize_resistor_without_geometry_omits_lw():
    # No l/w on the call at all -- the subcircuit's own default geometry
    # applies; the converted card carries no L=/W= (not an error).
    out = normalize_reference_netlist(
        "XR1 r0 r1 sky130_fd_pr__res_generic_po\n", deck="sky130"
    )
    assert out.strip() == "R1 r0 r1 0 res_generic_po"


def test_normalize_resistor_multiplicity_gt_one_rejected():
    with pytest.raises(NormalizeError, match="multi-finger/multiplied"):
        normalize_reference_netlist(
            "XR1 r0 r1 sky130_fd_pr__res_generic_po l=1u w=1u m=2\n", deck="sky130"
        )


def test_normalize_sky130_capacitor_derives_area_perimeter():
    # A 5x5 um plate: area = 25 um^2, perimeter = 2*(5+5) = 20 um.
    out = normalize_reference_netlist(
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u mf=1\n", deck="sky130"
    )
    assert out.strip() == "C1 c0 c1 0 sky130_fd_pr__model__cap_mim A=25P P=20U"


def test_normalize_gf180_capacitor_derives_area_perimeter_from_si_metres():
    out = normalize_reference_netlist(
        "XC1 1 2 cap_mim_2f0_m4m5_noshield c_length=2e-6 c_width=2e-6\n",
        deck="gf180mcu",
    )
    assert out.strip() == "C1 1 2 0 cap_mim_2f0_m4m5_noshield A=4P P=8U"


def test_normalize_capacitor_wrong_terminal_count_fails():
    with pytest.raises(NormalizeError, match="expected 2 terminals"):
        normalize_reference_netlist(
            "XC1 c0 c1 c2 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u\n", deck="sky130"
        )


def test_normalize_capacitor_multiplicity_gt_one_rejected():
    with pytest.raises(NormalizeError, match="multi-finger/multiplied"):
        normalize_reference_netlist(
            "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u mf=2\n", deck="sky130"
        )


def test_normalize_sky130_bipolar_resolves_by_name_no_lw_needed():
    # sky130's pnp_05v5 cells carry no length/width-style call-site
    # parameter at all -- resolved by subcircuit name alone.
    out = normalize_reference_netlist(
        "XQ1 c b e sky130_fd_pr__pnp_05v5_W0p68L0p68\n", deck="sky130"
    )
    assert out.strip() == "Q1 c b e pnp"


def test_normalize_bipolar_mult_carried_onto_ne():
    out = normalize_reference_netlist(
        "XQ1 c b e sky130_fd_pr__pnp_05v5_W3p40L3p40 mult=4\n", deck="sky130"
    )
    assert out.strip() == "Q1 c b e pnp NE=4"


def test_normalize_bipolar_wrong_terminal_count_fails():
    with pytest.raises(NormalizeError, match="expected 3 terminals"):
        normalize_reference_netlist(
            "XQ1 c b sky130_fd_pr__pnp_05v5_W0p68L0p68\n", deck="sky130"
        )


def test_normalize_device_families_auto_resolve_without_deck():
    # No `deck`/`device_map` given: every family resolves against the whole
    # curated cross-deck table, same as MOS's existing auto-resolution.
    out = normalize_reference_netlist(
        "XR1 r0 r1 sky130_fd_pr__res_generic_po l=1u w=1u\n"
        "XQ1 c b e sky130_fd_pr__pnp_05v5_W0p68L0p68\n"
    )
    assert "R1 r0 r1 0 res_generic_po L=1U W=1U" in out
    assert "Q1 c b e pnp" in out


def test_normalize_unresolvable_resistor_shaped_name_still_raises():
    # A resistor-shaped call (r_length/r_width present) whose name is not a
    # curated device is a hard error, not a silent passthrough -- the
    # device-family extension must not silently degrade #280's original
    # loud-failure discipline (issue #1130's edge case).
    with pytest.raises(NormalizeError, match="not a known device"):
        normalize_reference_netlist(
            "XR1 1 2 3 not_a_real_resistor r_length=1e-6 r_width=1e-6\n",
            deck="gf180mcu",
        )


def test_normalize_unnamed_bipolar_like_subckt_passes_through():
    # Bipolar has no reliable carried-parameter signal of device-hood (see
    # the module docstring): an unrecognised subcircuit name with no
    # geometry-style parameter either is a genuine hierarchical instance,
    # not a device call gone wrong -- passes through untouched, the same
    # discipline #280 already applied to an unrecognised MOS-shaped name
    # with no l/w (`test_normalize_non_device_subckt_passes_through`).
    text = "XQ1 c b e totally_made_up_bipolar\n"
    assert normalize_reference_netlist(text, deck="sky130").strip() == text.strip()


# --------------------------------------------------------------------------- #
# `reference.deck`'s map is derived from the deck's own declared device
# classes, not a hand-maintained MOS-only list (issue #1464)
# --------------------------------------------------------------------------- #

from klayout_tools.decks import (  # noqa: E402
    get_extraction_deck,
    known_extraction_deck_names,
)
from klayout_tools.pdk_models import build_device_binding_map  # noqa: E402

#: Device classes a registered deck declares that deliberately have **no**
#: subcircuit to bind to, so `build_device_binding_map` must not invent one
#: (see `pdk_models._RESISTOR_MODEL_TABLE`'s own comment): IHP ships
#: `res_metal1`/`res_metal2` as a bare model-reference `R` card, not a
#: `.subckt`. Kept as an explicit allowlist so a *new* undeclarable class has
#: to be added here consciously rather than silently skipping the coverage
#: assertion below.
_SUBCKT_LESS_DEVICE_CLASSES = frozenset({"res_metal1", "res_metal2"})


@pytest.mark.parametrize(
    ("device_class", "instance"),
    [("rsil", "XR1"), ("rppd", "XR2"), ("rhigh", "XR3")],
)
def test_normalize_sg13cmos5l_resistor_resolves_from_deck_alone(device_class, instance):
    # Issue #1464's headline repro: sg13cmos5l's `EXTRACTION_DECK.resistors`
    # has declared rsil/rppd/rhigh since #1415, but `_RESISTOR_MODEL_TABLE`
    # has no ("sg13cmos5l", "sg13cmos5l") entry -- so `reference.deck`
    # narrowed the map to that deck's four MOS names and the resistor call
    # needed a hand-written `device_map`. The derived fallback closes it.
    out = normalize_reference_netlist(
        f"{instance} r0 r1 sub {device_class} l=10u w=0.5u\n", deck="sg13cmos5l"
    )
    assert out.strip() == (f"{instance[1:]} r0 r1 sub 0 {device_class} L=10U W=0.5U")


def test_normalize_sg13g2_capacitor_resolves_from_deck_alone():
    # The second, independent instance of the same gap found during
    # curation: `_CAPACITOR_MODEL_TABLE` has no ("sg13g2", "sg13g2") entry
    # even though sg13g2 has declared cap_cmim/rfcmim since #1456. A 5x5 um
    # plate: area = 25 um^2, perimeter = 2*(5+5) = 20 um.
    out = normalize_reference_netlist(
        "XC1 PLUS MINUS cap_cmim w=5u l=5u\n", deck="sg13g2"
    )
    assert out.strip() == "C1 PLUS MINUS 0 cap_cmim A=25P P=20U"


def test_normalize_sg13g2_resistors_still_resolve_from_curated_table():
    # sg13g2 *does* have a curated resistor entry (#1457), so the derived
    # fallback must not fire for that family at all -- the curated names
    # keep working unchanged.
    out = normalize_reference_netlist(
        "XR1 r0 r1 sub rppd l=10u w=0.5u\n", deck="sg13g2"
    )
    assert out.strip() == "R1 r0 r1 sub 0 rppd L=10U W=0.5U"


def test_normalize_explicit_device_map_still_overrides_derived_binding():
    # Acceptance criterion 3: an explicit `device_map` entry takes precedence
    # over the derived binding for the same subcircuit name (the deck map is
    # merged first, then `device_map` on top).
    out = normalize_reference_netlist(
        "XR1 r0 r1 rppd res_len=10u res_wid=0.5u\n",
        deck="sg13cmos5l",
        device_map={
            "rppd": {
                "kind": "resistor",
                "class": "my_custom_res",
                "length_param": "res_len",
                "width_param": "res_wid",
            }
        },
    )
    assert out.strip() == "R1 r0 r1 0 my_custom_res L=10U W=0.5U"
    assert "rppd" not in out


def test_normalize_sky130_non_identity_capacitor_mapping_unaffected():
    # Edge case from the issue's test plan: sky130's capacitor family *is*
    # curated, and its mapping is genuinely non-identity (declared class
    # `sky130_fd_pr__model__cap_mim` -> real subcircuit
    # `sky130_fd_pr__cap_mim_m3_1`). The derived fallback must neither
    # override it nor add an assumed-identity alias under the class name.
    out = normalize_reference_netlist(
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u\n", deck="sky130"
    )
    assert out.strip() == "C1 c0 c1 0 sky130_fd_pr__model__cap_mim A=25P P=20U"

    binding_map = build_device_binding_map("sky130")
    assert binding_map["sky130_fd_pr__cap_mim_m3_1"].device_class == (
        "sky130_fd_pr__model__cap_mim"
    )
    assert "sky130_fd_pr__model__cap_mim" not in binding_map


def test_normalize_sg13g2_metal_resistor_carveout_is_not_fabricated():
    # sg13g2 declares res_metal1/res_metal2 but its curated resistor entry
    # deliberately omits them: IHP ships no `.subckt res_metal1` at all (a
    # verified carve-out, see `_RESISTOR_MODEL_TABLE`). Because the fallback
    # is gated on the whole (deck, family) pair being uncurated, those two
    # are not invented -- the conversion still fails loudly.
    with pytest.raises(NormalizeError, match="not a known device"):
        normalize_reference_netlist("XR1 r0 r1 res_metal1 l=10u w=1u\n", deck="sg13g2")


def test_normalize_gf180_bipolar_class_is_never_derived():
    # gf180mcu declares a `bjt` class with no curated binding -- a *verified*
    # "no such device cell" carve-out. Bipolar is recognised by subcircuit
    # name alone (no geometry parameter to cross-check), so an assumed
    # identity would silently rewrite a genuine hierarchical `X ... bjt`
    # instance into a Q card. It must stay unbound: the instance passes
    # through untouched.
    text = "XQ1 c b e bjt\n"
    assert normalize_reference_netlist(text, deck="gf180mcu").strip() == text.strip()
    assert "bjt" not in build_device_binding_map("gf180mcu")


def test_normalize_unknown_deck_device_error_lists_derived_classes():
    # Acceptance criterion 4: when the "not a known device for the requested
    # deck" error does fire, it must not misrepresent what the deck knows --
    # the derived non-MOS classes are in the resolved map, so they are
    # enumerated alongside the MOS names.
    with pytest.raises(NormalizeError) as excinfo:
        normalize_reference_netlist(
            "XR1 r0 r1 not_a_real_resistor l=10u w=1u\n", deck="sg13cmos5l"
        )
    message = str(excinfo.value)
    assert "not a known device for the requested deck" in message
    for expected in ("rsil", "rppd", "rhigh", "sg13_lv_nmos"):
        assert expected in message


def test_build_device_binding_map_derived_fallback_shape():
    # Unit-level assertion of the whole fallback contract, per deck.
    assert set(build_device_binding_map("sg13cmos5l")) == {
        "sg13_lv_nmos",
        "sg13_lv_pmos",
        "sg13_hv_nmos",
        "sg13_hv_pmos",
        "rsil",
        "rppd",
        "rhigh",
    }
    # sg13g2 gains only its capacitor family (resistors are curated, so
    # res_metal1/res_metal2 stay out).
    assert set(build_device_binding_map("sg13g2")) == {
        "sg13_lv_nmos",
        "sg13_lv_pmos",
        "sg13_hv_nmos",
        "sg13_hv_pmos",
        "rsil",
        "rppd",
        "rhigh",
        "cap_cmim",
        "rfcmim",
    }
    # Both fully-curated decks are byte-for-byte unchanged: no derived entry
    # is added to either.
    assert set(build_device_binding_map("gf180mcu")) == {
        "nfet_03v3",
        "pfet_03v3",
        "nfet_06v0",
        "pfet_06v0",
        "ppolyf_u",
        "ppolyf_u_1k",
        "ppolyf_u_2k",
        "ppolyf_u_3k",
        "cap_mim_2f0_m4m5_noshield",
    }
    assert set(build_device_binding_map("sky130")) == {
        "sky130_fd_pr__nfet_01v8",
        "sky130_fd_pr__pfet_01v8",
        "sky130_fd_pr__nfet_g5v0d10v5",
        "sky130_fd_pr__pfet_g5v0d10v5",
        "sky130_fd_pr__res_generic_po",
        "sky130_fd_pr__res_high_po",
        "sky130_fd_pr__res_xhigh_po",
        "sky130_fd_pr__cap_mim_m3_1",
        "sky130_fd_pr__cap_mim_m3_2",
        "sky130_fd_pr__pnp_05v5_W0p68L0p68",
        "sky130_fd_pr__pnp_05v5_W3p40L3p40",
    }


def test_build_device_binding_map_derived_entries_match_deck_declarations():
    # The "cannot drift from the deck again" property the issue asked for:
    # every resistor/capacitor class a deck declares is resolvable through
    # `reference.deck`, with no per-class table edit. Asserted against the
    # deck objects themselves so a newly declared class fails here rather
    # than silently regressing `klt lvs`'s reference side.
    for deck_name in known_extraction_deck_names():
        binding_map = build_device_binding_map(deck_name)
        deck = get_extraction_deck(deck_name)
        resolvable_classes = {lookup.device_class for lookup in binding_map.values()}
        for resistor in deck.resistors:
            if resistor.name in _SUBCKT_LESS_DEVICE_CLASSES:
                continue
            assert resistor.name in resolvable_classes, deck_name
        for capacitor in deck.capacitors:
            assert capacitor.name in resolvable_classes, deck_name


def test_normalize_mos_missing_lw_after_name_match_raises_cleanly():
    # A recognised MOS subcircuit name with neither l nor w supplied used to
    # be unreachable (l/w presence was the *only* detection gate); the
    # device-family extension's name-first resolution can now reach this
    # path, so it must fail with a clear NormalizeError, not a bare KeyError.
    with pytest.raises(NormalizeError, match="missing required parameter"):
        normalize_reference_netlist(
            "XM1 d g s b sky130_fd_pr__nfet_01v8\n", deck="sky130"
        )


def test_normalize_mos_lw_handling_unchanged_by_device_family_extension():
    # Regression lock (acceptance criterion): the device-family extension
    # must not alter existing MOS l/w handling byte-for-byte.
    out = normalize_reference_netlist(_INVERTER_SUBCKT_CALL_SKY130)
    assert "M1 Y A VGND VGND nfet L=0.15U W=0.65U" in out
    assert "M2 Y A VPWR VPWR pfet L=0.15U W=1U" in out


def test_detect_reports_resistor_and_capacitor_and_bipolar():
    text = (
        "XR1 r0 r1 sky130_fd_pr__res_generic_po l=1u w=1u\n"
        "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 w=5u l=5u\n"
        "XQ1 c b e sky130_fd_pr__pnp_05v5_W0p68L0p68\n"
    )
    assert detect_subckt_call_devices(text) == [
        "sky130_fd_pr__res_generic_po",
        "sky130_fd_pr__cap_mim_m3_1",
        "sky130_fd_pr__pnp_05v5_W0p68L0p68",
    ]


def test_run_lvs_subckt_call_reference_resistor_family_converts_and_reads(tmp_path):
    # A resistor-family reference converts and reads through `klt lvs`
    # end-to-end without erroring (not a full "match" -- this module has no
    # PDK sheet-resistance data, so the reference's `R` value is a `0`
    # placeholder, see `netlist_normalize.py`'s module docstring -- a real
    # ohms compare is unaffected by this issue's scope, only that the
    # subckt-call conversion + parse succeeds and produces a comparable
    # device).
    layout_path = _write(
        tmp_path / "layout.spice",
        ".subckt res_block A B\nR1 A B 0 res_generic_po L=48.2U W=1U\n.ends\n",
    )
    reference_path = _write(
        tmp_path / "ref.spice",
        ".subckt res_block A B\n"
        "XR1 A B sky130_fd_pr__res_generic_po l=48.2u w=1u\n"
        ".ends\n",
    )
    request = {
        "layout": {"netlist": layout_path, "top": "res_block"},
        "reference": {
            "netlist": reference_path,
            "top": "res_block",
            "form": "subckt-call",
            "deck": "sky130",
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


# --------------------------------------------------------------------------- #
# netgen engine (issue #343): stubbed-subprocess tests
#
# Follows `tests/test_sim.py`'s `_stub_subprocess_run` pattern for the
# ngspice wrap -- no real `netgen` binary is required to run this suite. The
# captured report text below is not invented: it is the verbatim `comp.out`
# output of a from-source netgen 1.5.323 build (`RTimothyEdwards/netgen`,
# built for this issue -- see the dated addendum in
# docs/design/lvs-extraction-spike.md) run against small hand-written
# SPICE fixtures, so these tests exercise the exact text this module's
# parser has to handle, not a guessed format.
# --------------------------------------------------------------------------- #

_NETGEN_STDOUT_BANNER = "Netgen 1.5.323 compiled on Sun Aug  2 18:51:46 PDT 2026\n"

# Issue #1192: at least one still-current *packaged* netgen build
# (Debian/Ubuntu's `netgen-lvs` 1.5.133, built 2022-12-01) never writes its
# verdict line into the log file at all -- only to the process's own stdout
# -- and uses "Result:" rather than "Final result:" for that line. This log
# text mirrors `_NETGEN_MATCH_LOG` but with the trailing "Final result:"
# section (and its blank-line separator) stripped, matching the reporter's
# own reproduction transcript for this issue.
_NETGEN_MATCH_LOG_NO_VERDICT_IN_FILE = """
Subcircuit summary:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
pmos (1)                                   |pmos (1)
nmos (1)                                   |nmos (1)
Number of devices: 2                       |Number of devices: 2
Number of nets: 4                          |Number of nets: 4
---------------------------------------------------------------------------------------
Netlists match uniquely.

Subcircuit pins:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
Y                                          |Y
A                                          |A
VDD                                        |VDD
VSS                                        |VSS
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes inv and inv are equivalent.
"""

#: The older packaged build's own stdout for this same run: the startup
#: banner plus a trailing "Result: ..." verdict line that never made it into
#: the log file above.
_NETGEN_OLD_BUILD_STDOUT = (
    "Netgen 1.5.133 compiled on Thu Dec  1 12:00:00 UTC 2022\n"
    "...\n"
    "Result: Circuits match uniquely.\n"
)

_NETGEN_MATCH_LOG = """
Subcircuit summary:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
pmos (1)                                   |pmos (1)
nmos (1)                                   |nmos (1)
Number of devices: 2                       |Number of devices: 2
Number of nets: 4                          |Number of nets: 4
---------------------------------------------------------------------------------------
Netlists match uniquely.

Subcircuit pins:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
Y                                          |Y
A                                          |A
VDD                                        |VDD
VSS                                        |VSS
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes inv and inv are equivalent.

Final result: Circuits match uniquely.
.
"""

_NETGEN_PROPERTY_LOG = """
Subcircuit summary:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
pmos (1)                                   |pmos (1)
nmos (1)                                   |nmos (1)
Number of devices: 2                       |Number of devices: 2
Number of nets: 4                          |Number of nets: 4
---------------------------------------------------------------------------------------
Netlists match uniquely with property errors.
pmos:1 vs. pmos:1:
 W circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=1%)

Subcircuit pins:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
Y                                          |Y
A                                          |A
VDD                                        |VDD
VSS                                        |VSS
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes inv and inv are equivalent.

Final result: Circuits match uniquely.
Property errors were found.

The following cells had property errors:
 inv
"""

# Verbatim `comp.out` of the same from-source netgen 1.5.323 build, run over
# two one-subcircuit SPICE files whose only difference is a *string-valued*
# instance parameter (`model=fast` vs `model=slow`):
#
#     netgen -batch lvs "c.spice top" "d.spice top" "" out2.log
#
# netgen compares a non-numeric property exactly, so it emits the
# `(exact match req'd)` qualifier instead of the `(delta=..., cutoff=...)`
# form of `_NETGEN_PROPERTY_LOG`. Regression fixture for the false-`"match"`
# defect found in review of issue #343: the property line failed to parse,
# the property-error block yielded zero entries, and the clean-unique-match
# early return reported `status: "match"` with an empty `mismatches[]` even
# though netgen itself printed "Property errors were found."
# (Trailing whitespace on netgen's column-padded table rows is stripped, as
# in the other fixtures here; the property lines are verbatim.)
#
# NOTE: the `sub:1 vs. sub:1:` header below is *device*-shaped (a numeric
# index), not subcircuit-instance-shaped -- real netgen only uses a numeric
# index for primitive devices; a subcircuit instance is named by its
# instance name instead (e.g. `sub:i1 vs. sub:i1:`, exercised separately by
# `_NETGEN_SUBCKT_INSTANCE_PROPERTY_LOG` below, issue #363). This fixture
# remains a valid regression for the string-valued exact-match line path;
# only the header's construct attribution was previously mislabeled.
_NETGEN_STRING_PROPERTY_LOG = """
Subcircuit summary:
Circuit 1: sub                             |Circuit 2: sub
-------------------------------------------|-------------------------------------------
r (1)                                      |r (1)
Number of devices: 1                       |Number of devices: 1
Number of nets: 2                          |Number of nets: 2
---------------------------------------------------------------------------------------
Resolving symmetries by property value.
Resolving symmetries by pin name.
Netlists match uniquely.

Subcircuit pins:
Circuit 1: sub                             |Circuit 2: sub
-------------------------------------------|-------------------------------------------
a                                          |a
b                                          |b
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes sub and sub are equivalent.

Subcircuit summary:
Circuit 1: top                             |Circuit 2: top
-------------------------------------------|-------------------------------------------
sub (1)                                    |sub (1)
Number of devices: 1                       |Number of devices: 1
Number of nets: 2                          |Number of nets: 2
---------------------------------------------------------------------------------------
Netlists match uniquely with property errors.
sub:1 vs. sub:1:
 model circuit1: "fast"   circuit2: "slow"   (exact match req'd)

Subcircuit pins:
Circuit 1: top                             |Circuit 2: top
-------------------------------------------|-------------------------------------------
a                                          |a
b                                          |b
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes top and top are equivalent.

Final result: Circuits match uniquely.
Property errors were found.

The following cells had property errors:
 top
"""

# Verbatim `comp.out` of a from-source netgen 1.5.323 build, run over two
# top-level SPICE files whose only difference is a numeric instance
# parameter (`w=1u` vs `w=2u`) on a *subcircuit instance* `xi1`:
#
#     netgen -batch lvs "c.spice top" "d.spice top" "" out3.log
#
# Unlike a primitive device (`pmos:1 vs. pmos:1:`), netgen names a
# subcircuit-instance property-error block by the *instance name*, not a
# numeric index -- `sub:i1 vs. sub:i1:`. Regression fixture for issue #363:
# `_NETGEN_PROPERTY_BLOCK_RE`'s block header previously required a numeric
# index in both positions, so this exact header failed to match, the
# property-error block yielded zero entries, and the report fell through to
# the marker-based backstop with `device: null, property: null` instead of
# a structured `device.property` entry.
_NETGEN_SUBCKT_INSTANCE_PROPERTY_LOG = """
Subcircuit summary:
Circuit 1: sub                             |Circuit 2: sub
-------------------------------------------|-------------------------------------------
r (1)                                      |r (1)
Number of devices: 1                       |Number of devices: 1
Number of nets: 2                          |Number of nets: 2
---------------------------------------------------------------------------------------
Resolving symmetries by property value.
Resolving symmetries by pin name.
Netlists match uniquely.

Subcircuit pins:
Circuit 1: sub                             |Circuit 2: sub
-------------------------------------------|-------------------------------------------
a                                          |a
b                                          |b
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes sub and sub are equivalent.

Subcircuit summary:
Circuit 1: top                             |Circuit 2: top
-------------------------------------------|-------------------------------------------
sub (1)                                    |sub (1)
Number of devices: 1                       |Number of devices: 1
Number of nets: 2                          |Number of nets: 2
---------------------------------------------------------------------------------------
Netlists match uniquely with property errors.
sub:i1 vs. sub:i1:
 w circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=0%)

Subcircuit pins:
Circuit 1: top                             |Circuit 2: top
-------------------------------------------|-------------------------------------------
a                                          |a
b                                          |b
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes top and top are equivalent.

Final result: Circuits match uniquely.
Property errors were found.

The following cells had property errors:
 top
"""

# Same declared property error, but with the per-parameter evidence in a
# shape this module does not structure (a hypothetical future/unknown netgen
# qualifier wording). netgen's own "Property errors were found." marker must
# still force `status: "mismatch"` -- the downgrade may never depend on the
# per-line regex succeeding.
_NETGEN_UNSTRUCTURED_PROPERTY_LOG = """
Subcircuit summary:
Circuit 1: top                             |Circuit 2: top
-------------------------------------------|-------------------------------------------
sub (1)                                    |sub (1)
Number of devices: 1                       |Number of devices: 1
Number of nets: 2                          |Number of nets: 2
---------------------------------------------------------------------------------------
Netlists match uniquely with property errors.

Final result: Circuits match uniquely.
Property errors were found.

The following cells had property errors:
 top
"""

_NETGEN_TOPOLOGY_LOG = """
Subcircuit summary:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
pmos (1)                                   |pmos (1)
nmos (1)                                   |nmos (1)
Number of devices: 2                       |Number of devices: 2
Number of nets: 4                          |Number of nets: 4
---------------------------------------------------------------------------------------
NET mismatches: Class fragments follow (with fanout counts):
Circuit 1: inv                             |Circuit 2: inv

---------------------------------------------------------------------------------------
Net: A                                     |Net: A
  pmos/gate = 1                            |  pmos/gate = 1
  nmos/gate = 1                            |
                                           |
Net: VSS                                   |Net: VSS
  nmos/(drain|source) = 1                  |  nmos/(drain|source) = 1
  nmos/bulk = 1                            |  nmos/bulk = 1
                                           |  nmos/gate = 1
---------------------------------------------------------------------------------------
Netlists do not match.
Port matching may fail to disambiguate symmetries.

Subcircuit pins:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
A                                          |A
VSS                                        |VSS
VDD                                        |VDD
Y                                          |Y
---------------------------------------------------------------------------------------
Cell pin lists are equivalent.
Device classes inv and inv are equivalent.

Final result: Netlists do not match.
Port matching may fail to disambiguate symmetries.
"""

_NETGEN_PIN_MISMATCH_LOG = """
Subcircuit summary:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
pmos (1)                                   |pmos (1)
nmos (1)                                   |nmos (1)
(no matching element)                      |r (1)
Number of devices: 2 **Mismatch**          |Number of devices: 3 **Mismatch**
Number of nets: 4 **Mismatch**             |Number of nets: 5 **Mismatch**
---------------------------------------------------------------------------------------
NET mismatches: Class fragments follow (with fanout counts):
Circuit 1: inv                             |Circuit 2: inv

---------------------------------------------------------------------------------------
Net: VSS                                   |Net: VSS
  nmos/(drain|source) = 1                  |  nmos/(drain|source) = 1
  nmos/bulk = 1                            |  nmos/bulk = 1
                                           |  r/(end_a|end_b) = 1
                                           |
(no matching net)                          |Net: EXTRA
                                           |  r/(end_a|end_b) = 1
---------------------------------------------------------------------------------------
DEVICE mismatches: Class fragments follow (with node fanout counts):
Circuit 1: inv                             |Circuit 2: inv

---------------------------------------------------------------------------------------
(no matching instance)                     |Instance: r:1
                                           |  (end_a,end_b) = (3,1)
                                           |
---------------------------------------------------------------------------------------
Netlists do not match.
Port matching may fail to disambiguate symmetries.

Subcircuit pins:
Circuit 1: inv                             |Circuit 2: inv
-------------------------------------------|-------------------------------------------
VSS                                        |VSS
VDD                                        |VDD
A                                          |A
Y                                          |Y
(no pin, node is VSS)                      |EXTRA
---------------------------------------------------------------------------------------
Cell pin lists for inv and inv altered to match.
Device classes inv and inv are equivalent.

Final result: Top level cell failed pin matching.
"""


def _stub_netgen_subprocess(
    monkeypatch,
    *,
    log_text: str | None = _NETGEN_MATCH_LOG,
    stdout: str = _NETGEN_STDOUT_BANNER,
    side_effect: BaseException | None = None,
    captured_cmds: list | None = None,
):
    """Stub `lvs.subprocess.run` for the netgen engine path -- mirrors
    `tests/test_sim.py`'s `_stub_subprocess_run` for the ngspice wrap.

    The real `_run_netgen_lvs` derives its log path itself (a temp dir it
    owns), so the fake reads it back off `cmd[-1]` (the last positional
    argument `_run_netgen_lvs` passes to `netgen -batch lvs`) rather than
    an `-o`-style flag (ngspice's convention, not netgen's).
    """

    def fake_run(cmd, capture_output, text, timeout):
        if captured_cmds is not None:
            captured_cmds.append(cmd)
        if side_effect is not None:
            raise side_effect
        if log_text is not None:
            log_path = cmd[-1]
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(log_text)
        return fake_completed(stdout)

    monkeypatch.setattr(lvs.subprocess, "run", fake_run)


def _netgen_request(tmp_path: Path, **extra) -> str:
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request = {
        "engine": "netgen",
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    request.update(extra)
    return _write_request(tmp_path / "request.json", request)


def test_netgen_engine_clean_match(tmp_path, monkeypatch):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["engine"] == "netgen"
    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["mismatch_count"] == 0
    assert report["counts"]["nets"] == {"layout": 4, "reference": 4, "matched": 4}
    assert report["counts"]["devices"] == {"layout": 2, "reference": 2, "matched": 2}
    assert report["counts"]["pins"] == {"layout": 4, "reference": 4, "matched": 4}
    assert report["net_correspondence"] == []
    assert report["environment"]["engine"] == "netgen"
    assert report["environment"]["engine_version"] == "1.5.323"


def test_netgen_engine_property_mismatch_reports_device_property(tmp_path, monkeypatch):
    """A netgen 'match uniquely' verdict that also reports a property error
    is downgraded to `status: "mismatch"` -- a real parameter defect must
    never read as a clean match, mirroring the `klayout` engine's own
    `device.property` semantics."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_PROPERTY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}
    (mismatch,) = report["mismatches"]
    assert mismatch["category"] == "device.property"
    assert mismatch["severity"] == "error"
    assert mismatch["property"] == {
        "name": "W",
        "layout": "1e-06",
        "reference": "2e-06",
    }
    assert mismatch["device"] == {
        "layout": "pmos:1",
        "reference": "pmos:1",
        "class": "pmos",
    }
    assert mismatch["details"] is None


def test_netgen_engine_string_property_error_is_not_a_false_match(
    tmp_path, monkeypatch
):
    """Regression (issue #343 review): netgen compares a *string-valued*
    property exactly and reports `(exact match req'd)` instead of
    `(delta=..., cutoff=...)`. That line previously failed to parse, the
    property block yielded no entries, and the clean-unique-match early
    return produced `status: "match"` with an empty `mismatches[]` -- while
    netgen's own report said "Property errors were found."

    The verdict must be `"mismatch"` with a non-empty `mismatches[]`, and the
    string-valued difference must still be structured into a `device.property`
    entry rather than only tripping the marker guard."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_STRING_PROPERTY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["mismatches"], "netgen declared property errors -- must not be empty"
    assert report["mismatch_count"] == 1
    assert report["category_counts"] == {"device.property": 1}
    (mismatch,) = report["mismatches"]
    assert mismatch["category"] == "device.property"
    assert mismatch["severity"] == "error"
    assert mismatch["property"] == {
        "name": "model",
        "layout": '"fast"',
        "reference": '"slow"',
    }
    assert mismatch["device"] == {
        "layout": "sub:1",
        "reference": "sub:1",
        "class": "sub",
    }
    assert "exact match req'd" in mismatch["description"]


def test_netgen_engine_subcircuit_instance_property_error_is_structured(
    tmp_path, monkeypatch
):
    """Regression (issue #363): a property-error block whose header names a
    *subcircuit instance* (`sub:i1 vs. sub:i1:`) rather than a numerically
    indexed device (`pmos:1 vs. pmos:1:`) must still be parsed into a
    structured `device.property` entry -- not fall through to the
    marker-based backstop with `device`/`property` left `null`."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_SUBCKT_INSTANCE_PROPERTY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["mismatches"], "netgen declared property errors -- must not be empty"
    assert report["mismatch_count"] == 1
    assert report["category_counts"] == {"device.property": 1}
    (mismatch,) = report["mismatches"]
    assert mismatch["category"] == "device.property"
    assert mismatch["severity"] == "error"
    assert mismatch["property"] == {
        "name": "w",
        "layout": "1e-06",
        "reference": "2e-06",
    }
    assert mismatch["device"] == {
        "layout": "sub:i1",
        "reference": "sub:i1",
        "class": "sub",
    }
    assert mismatch["details"] is None


def test_netgen_engine_declared_property_errors_without_parsable_detail(
    tmp_path, monkeypatch
):
    """The marker guard is independent of the per-line regex: when netgen
    declares "Property errors were found." but no per-parameter line can be
    structured at all, the verdict is still `"mismatch"` and a best-effort
    entry carries netgen's own text in `details.raw` -- never a clean match
    with an empty `mismatches[]`."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_UNSTRUCTURED_PROPERTY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}
    (mismatch,) = report["mismatches"]
    assert mismatch["category"] == "device.property"
    assert "property errors" in mismatch["description"]
    assert "property errors" in mismatch["details"]["raw"]


def test_netgen_engine_topology_mismatch_buckets_net_details(tmp_path, monkeypatch):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_TOPOLOGY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"net.unmatched": 1}
    (mismatch,) = report["mismatches"]
    assert mismatch["category"] == "net.unmatched"
    assert mismatch["severity"] == "error"
    assert mismatch["net"] is None
    assert mismatch["details"] is not None
    assert "NET mismatches:" in mismatch["details"]["raw"]
    assert "Net: A" in mismatch["details"]["raw"]
    # The block stops before the next report section -- it must not swallow
    # the "Subcircuit pins:"/"Final result:" report tail.
    assert "Subcircuit pins:" not in mismatch["details"]["raw"]
    assert "Final result:" not in mismatch["details"]["raw"]


def test_netgen_engine_pin_mismatch_reports_pin_unmatched_and_device_bucket(
    tmp_path, monkeypatch
):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_PIN_MISMATCH_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    categories = sorted(m["category"] for m in report["mismatches"])
    assert categories == ["device.unmatched", "net.unmatched", "pin.unmatched"]
    pin_entry = next(
        m for m in report["mismatches"] if m["category"] == "pin.unmatched"
    )
    assert pin_entry["severity"] == "error"
    assert "pin matching" in pin_entry["description"]


def test_netgen_engine_populates_counts_matched_zero_on_mismatch(tmp_path, monkeypatch):
    """Known limitation (see docs/cli/lvs.md): the netgen engine does not
    reconstruct a per-net/per-device correspondence, so `counts.*.matched`
    stays at the conservative floor (never a fabricated estimate) on a
    mismatch verdict, and `net_correspondence` stays empty -- keeping the
    `len(net_correspondence) == counts.nets.matched` invariant intact."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_TOPOLOGY_LOG)
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["counts"]["nets"]["matched"] == 0
    assert report["counts"]["devices"]["matched"] == 0
    assert report["counts"]["pins"]["matched"] == 0
    assert report["net_correspondence"] == []
    assert len(report["net_correspondence"]) == report["counts"]["nets"]["matched"]
    # The real (layout/reference) counts are still reported honestly.
    assert report["counts"]["nets"]["layout"] == 4
    assert report["counts"]["devices"]["layout"] == 2


def test_netgen_engine_missing_binary_raises_actionable_error(tmp_path, monkeypatch):
    _stub_netgen_subprocess(
        monkeypatch, side_effect=FileNotFoundError("no such file: netgen")
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(LvsError, match="binary not found on PATH"):
        run_lvs(path)


def test_netgen_engine_timeout_raises(tmp_path, monkeypatch):
    _stub_netgen_subprocess(
        monkeypatch,
        side_effect=subprocess.TimeoutExpired(cmd=["netgen"], timeout=300.0),
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(LvsError, match="did not complete within"):
        run_lvs(path)


def test_netgen_engine_no_report_file_raises(tmp_path, monkeypatch):
    """netgen exits 0 even when it fails before producing a report (e.g. a
    malformed netlist) -- verified empirically for this issue. No report
    file at all must not be silently treated as a match."""
    _stub_netgen_subprocess(
        monkeypatch, log_text=None, stdout="Error in SPICE file read: No file\n"
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(LvsError, match="did not produce a report file"):
        run_lvs(path)


def test_netgen_engine_unparseable_report_raises_not_silently_match(
    tmp_path, monkeypatch
):
    """The exact failure mode this issue exists to catch: malformed/
    unexpected netgen report text must never silently produce
    `status: "match"` -- the parser fails loud instead."""
    _stub_netgen_subprocess(
        monkeypatch, log_text="some garbage netgen never actually writes\n"
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(LvsError, match="could not parse netgen's LVS report"):
        run_lvs(path)


def test_netgen_engine_unrecognised_verdict_text_raises(tmp_path, monkeypatch):
    """A 'Final result:' section is present, but its text matches none of
    this module's known netgen verdict strings, and no other structured
    evidence (a property-error block) was found either -- still fails loud
    rather than defaulting to a match."""
    _stub_netgen_subprocess(
        monkeypatch,
        log_text="Final result: Something netgen never actually prints.\n",
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(LvsError, match="could not classify netgen's LVS verdict"):
        run_lvs(path)


def test_netgen_engine_falls_back_to_stdout_for_result_verdict(tmp_path, monkeypatch):
    """Issue #1192: some still-current packaged netgen builds (e.g. Debian's
    `netgen-lvs` 1.5.133) never write their verdict line into the log file
    at all, only to the process's own stdout, and word it "Result:" rather
    than "Final result:". `run_lvs` must recover the verdict from `stdout`
    rather than unconditionally raising when the log file has no recognised
    marker."""
    _stub_netgen_subprocess(
        monkeypatch,
        log_text=_NETGEN_MATCH_LOG_NO_VERDICT_IN_FILE,
        stdout=_NETGEN_OLD_BUILD_STDOUT,
    )
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["environment"]["engine_version"] == "1.5.133"


def test_netgen_engine_log_verdict_takes_precedence_over_stdout(tmp_path, monkeypatch):
    """When the log file *does* have a recognised verdict marker, it is used
    even if `stdout` also has a (possibly differently-worded) one -- the log
    is the artifact a caller can inspect after the run, so it stays the
    trusted source whenever it is available."""
    _stub_netgen_subprocess(
        monkeypatch,
        log_text=_NETGEN_TOPOLOGY_LOG,
        stdout=_NETGEN_STDOUT_BANNER + "Result: Circuits match uniquely.\n",
    )
    path = _netgen_request(tmp_path)

    report = run_lvs(path)

    assert report["status"] == "mismatch"


def test_netgen_engine_no_verdict_in_log_or_stdout_still_raises(tmp_path, monkeypatch):
    """Neither source has a recognised verdict marker at all -- still fails
    loud, and the raised message includes the detected netgen version to
    distinguish a version-skew report-format gap from a genuinely malformed
    netlist (issue #1192)."""
    _stub_netgen_subprocess(
        monkeypatch,
        log_text="some garbage netgen never actually writes\n",
        stdout=_NETGEN_STDOUT_BANNER,
    )
    path = _netgen_request(tmp_path)

    with pytest.raises(
        LvsError, match="could not parse netgen's LVS report"
    ) as excinfo:
        run_lvs(path)
    assert "1.5.323" in str(excinfo.value)


def test_netgen_engine_hints_unsupported_raises(tmp_path, monkeypatch):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(tmp_path, hints={"same_nets": [["VPWR", "VPWR"]]})

    with pytest.raises(LvsError, match="only supported for engine 'klayout'"):
        run_lvs(path)


def test_netgen_engine_device_bulk_unsupported_raises(tmp_path, monkeypatch):
    """Issue #506: `reference.device_bulk` is a `klayout.db`-side device-class
    normalisation applied to the in-memory reference netlist -- the netgen
    engine reads its own SPICE files through its own device model, so the
    request is rejected rather than silently ignored (same boundary
    `request.hints` already draws)."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _netgen_request(
        tmp_path,
        reference={
            "netlist": reference_path,
            "top": "inv",
            "device_bulk": {"nfet": "VGND"},
        },
    )

    with pytest.raises(LvsError, match="only supported for engine 'klayout'"):
        run_lvs(path)


def test_netgen_engine_parameter_tolerance_unsupported_raises(tmp_path, monkeypatch):
    """Issue #589: netgen's own per-property tolerances are declared in its
    setup file as *absolute* per-device-class values, so a single relative
    tolerance has no faithful translation into one. Rejected (pointing at
    `options.netgen_setup`) rather than silently ignored -- an opted-in
    tolerance the caller believes is in force but is not would be worse than
    not supporting it at all."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(tmp_path, options={"parameter_tolerance": 0.001})

    with pytest.raises(LvsError, match="only supported for engine 'klayout'"):
        run_lvs(path)


def test_netgen_engine_parameter_tolerance_null_is_accepted(tmp_path, monkeypatch):
    """An explicitly-null tolerance is the default, not an opt-in -- so it
    must not trip the engine boundary above."""
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(tmp_path, options={"parameter_tolerance": None})

    report = run_lvs(path)
    assert report["status"] == "match"
    assert report["parameter_tolerance"] is None


def test_netgen_engine_passes_setup_file_argument(tmp_path, monkeypatch):
    setup_path = _write(tmp_path / "sky130A_setup.tcl", "# netgen setup\n")
    captured: list = []
    _stub_netgen_subprocess(
        monkeypatch, log_text=_NETGEN_MATCH_LOG, captured_cmds=captured
    )
    path = _netgen_request(tmp_path, options={"netgen_setup": str(setup_path)})

    run_lvs(path)

    (cmd,) = captured
    assert cmd[0] == "netgen"
    assert cmd[1:3] == ["-batch", "lvs"]
    assert cmd[5] == str(setup_path)


def test_netgen_engine_missing_setup_file_raises(tmp_path, monkeypatch):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(
        tmp_path, options={"netgen_setup": str(tmp_path / "nope_setup.tcl")}
    )

    with pytest.raises(LvsError, match="options.netgen_setup not found"):
        run_lvs(path)


def test_netgen_engine_omitted_setup_passes_empty_string(tmp_path, monkeypatch):
    """No `options.netgen_setup` given -- netgen's own documented "trivial
    default setup" behaviour, passed as an empty-string positional arg
    (verified against the real `netgen::lvs` Tcl proc for this issue)."""
    captured: list = []
    _stub_netgen_subprocess(
        monkeypatch, log_text=_NETGEN_MATCH_LOG, captured_cmds=captured
    )
    path = _netgen_request(tmp_path)

    run_lvs(path)

    (cmd,) = captured
    assert cmd[5] == ""


def test_netgen_engine_default_engine_still_klayout(tmp_path, monkeypatch):
    """Omitting `request.engine` entirely still defaults to `"klayout"` --
    adding `netgen` as a second `SUPPORTED_ENGINES` entry must not change
    the documented default."""
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

    assert report["engine"] == "klayout"


# --------------------------------------------------------------------------- #
# netgen engine: real-binary integration tests (skip if netgen is absent)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NETGEN
def test_netgen_engine_real_binary_clean_self_compare(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "netgen",
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )

    report = run_lvs(path)

    assert report["status"] == "match"
    assert report["mismatches"] == []
    assert report["environment"]["engine_version"]


@_SKIP_NO_NETGEN
def test_netgen_engine_real_binary_reports_device_property(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(
        tmp_path / "ref.spice",
        _INVERTER_SPICE.replace("W=0.65U", "W=0.70U"),
    )
    path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "netgen",
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        },
    )

    report = run_lvs(path)

    assert report["status"] == "mismatch"
    assert report["category_counts"] == {"device.property": 1}


@_SKIP_NO_NETGEN
def test_netgen_engine_real_binary_agrees_with_klayout_on_sky130_corpus(tmp_path):
    """Issue #343's own end-to-end validation, automated: both engines run
    against the same real sky130 corpus cell (inline extraction + a
    self-compare, then the corpus round-trip tier's own NMOS-body-short
    negative control) report the same `status` -- comparator/contract
    independence, per this issue's acceptance criteria."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(SKY130_INV), "sky130", output=reference_path)

    def _report(engine: str, reference: str) -> dict:
        path = _write_request(
            tmp_path / f"request-{engine}.json",
            {
                "engine": engine,
                "layout": {"file": str(SKY130_INV), "deck": "sky130"},
                "reference": {"netlist": reference, "top": extracted["top"]},
            },
        )
        return run_lvs(path)

    clean_klayout = _report("klayout", reference_path)
    clean_netgen = _report("netgen", reference_path)
    assert clean_klayout["status"] == clean_netgen["status"] == "match"

    with open(reference_path, encoding="utf-8") as handle:
        text = handle.read()
    assert " VGND vsubs" in text
    broken_path = str(tmp_path / "ref_shorted.spice")
    with open(broken_path, "w", encoding="utf-8") as handle:
        handle.write(text.replace(" VGND vsubs", " VGND VGND"))

    broken_klayout = _report("klayout", broken_path)
    broken_netgen = _report("netgen", broken_path)
    assert broken_klayout["status"] == broken_netgen["status"] == "mismatch"


# --------------------------------------------------------------------------- #
# netgen engine against an SG13G2-shaped install (issue #522)
#
# `klt drc` and the default `"klayout"` `klt lvs` engine both need a curated,
# PDK-specific device-recognition deck (`klayout_tools.decks`) that does not
# exist for SG13G2 yet -- building one is a large, separate follow-up (a
# from-scratch DRC/LVS deck port, not a resolver change), tracked as #524,
# out of scope here.
# The `"netgen"` engine is the one `klt lvs` path that does *not* need a
# curated deck: it compares two already-built SPICE netlists, so it can be
# exercised against SG13G2 today via `pdk.netgen_setup_file` -- the resolved
# PDK's *own* `ihp-sg13g2_setup.tcl`, staged exactly like open_pdks stages
# `<variant>_setup.tcl` (confirmed against a real fetched
# `scripts/fetch-ihp-sg13g2.sh` install; see
# `test_flat_layout_netgen_setup_file_matches_ihp_naming` in
# `tests/test_pdk.py`). This test fabricates a minimal, flat SG13G2-shaped
# install (same shape as a real one, but hermetic -- no multi-hundred-MB
# fetch required) with a trivial, syntactically-valid setup script standing
# in for the real one, and self-compares a trivial cell through the full
# `klt pdk find` -> `netgen_setup_file` -> `klt lvs --engine netgen` path.
# Real-binary-gated like every other test in this section: skips cleanly
# without a local `netgen` (this dev environment has none -- see the PR this
# issue shipped in for the documented limitation), runs for real wherever
# `netgen` is installed.
# --------------------------------------------------------------------------- #


@_SKIP_NO_NETGEN
def test_netgen_engine_real_binary_against_sg13g2_shaped_install(tmp_path):
    pdk_root = tmp_path / "ihp-sg13g2"
    (pdk_root / "libs.tech" / "netgen").mkdir(parents=True)
    # A trivial, syntactically-valid Tcl script standing in for the real
    # `ihp-sg13g2_setup.tcl` -- netgen sources it and falls back to its own
    # built-in device recognition for anything the script does not declare,
    # so a comment-only script is a legitimate (if minimal) netgen setup,
    # not a fake/stubbed input.
    (pdk_root / "libs.tech" / "netgen" / "ihp-sg13g2_setup.tcl").write_text(
        "# minimal SG13G2 setup stand-in (test fixture, issue #522)\n"
    )

    setup_path = pdk.netgen_setup_file(root=str(pdk_root))
    assert setup_path == str(pdk_root / "libs.tech" / "netgen" / "ihp-sg13g2_setup.tcl")

    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "netgen",
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"netgen_setup": setup_path},
        },
    )

    report = run_lvs(request_path)

    assert report["status"] == "match"
    assert report["mismatches"] == []


# --------------------------------------------------------------------------- #
# netgen engine against an SG13CMOS5L-shaped install (issue #1399)
#
# Mirrors `test_netgen_engine_real_binary_against_sg13g2_shaped_install`
# directly above, one PDK variant over. SG13CMOS5L is installed the
# identical flat, single-PDK way SG13G2 is (verified against a real
# fleet-host install, `~/share/pdk/ihp-sg13cmos5l`, commit `607e18d`,
# 2026-08-25) and its own `netgen_setup_file` resolution needs no change --
# `libs.tech/netgen/ihp-sg13cmos5l_setup.tcl` matches the same
# `<variant>_setup.tcl` convention SG13G2 uses (confirmed against a real
# fetched install; see `test_flat_layout_netgen_setup_file_matches_ihp_
# sg13cmos5l_naming` in `tests/test_pdk.py`). Real-binary-gated like the
# SG13G2 test above: skips cleanly without a local `netgen`, runs for real
# wherever `netgen` is installed.
# --------------------------------------------------------------------------- #


@_SKIP_NO_NETGEN
def test_netgen_engine_real_binary_against_sg13cmos5l_shaped_install(tmp_path):
    pdk_root = tmp_path / "ihp-sg13cmos5l"
    (pdk_root / "libs.tech" / "netgen").mkdir(parents=True)
    # A trivial, syntactically-valid Tcl script standing in for the real
    # `ihp-sg13cmos5l_setup.tcl` -- see the SG13G2 test above for why a
    # comment-only script is a legitimate netgen setup, not a stub.
    (pdk_root / "libs.tech" / "netgen" / "ihp-sg13cmos5l_setup.tcl").write_text(
        "# minimal SG13CMOS5L setup stand-in (test fixture, issue #1399)\n"
    )

    setup_path = pdk.netgen_setup_file(root=str(pdk_root))
    assert setup_path == str(
        pdk_root / "libs.tech" / "netgen" / "ihp-sg13cmos5l_setup.tcl"
    )

    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    request_path = _write_request(
        tmp_path / "request.json",
        {
            "engine": "netgen",
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
            "options": {"netgen_setup": setup_path},
        },
    )

    report = run_lvs(request_path)

    assert report["status"] == "match"
    assert report["mismatches"] == []


# --------------------------------------------------------------------------- #
# `klt lvs --check` / `--rerun` (issue #1106): verify a committed report
# --------------------------------------------------------------------------- #

from klayout_tools.lvs import check_lvs_report, rerun_lvs_report  # noqa: E402


def _write_report(path: Path, report: dict) -> str:
    path.write_text(json.dumps(report))
    return str(path)


def _matching_netlist_request(tmp_path: Path) -> tuple[str, str]:
    """A minimal pre-extracted-netlist (no deck) `klt lvs` request whose
    layout/reference are byte-identical -- `status: "match"`, no deck
    involved (mirrors `test_load_request_arg_inline_json_matches_file_form`).
    Returns ``(layout_path, reference_path)``."""
    layout_path = _write(tmp_path / "layout.spice", _INVERTER_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    return layout_path, reference_path


def test_check_lvs_report_clean_pass(tmp_path):
    """Cheap mode, pre-extracted-netlist form (no deck): unmutated
    layout/reference re-hash to the recorded `environment.*_sha256` values
    -- `status: "match"`, both checks `match: True`."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    result = check_lvs_report(str(report_path))

    assert result["schema_version"] == 1
    assert result["mode"] == "check"
    assert result["status"] == "match"
    fields = {check["field"] for check in result["checks"]}
    assert fields == {"environment.layout_sha256", "environment.reference_sha256"}
    assert all(check["match"] for check in result["checks"])


def test_check_lvs_report_detects_moved_reference_hash(tmp_path):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    _write(Path(reference_path), _INVERTER_SPICE + "* mutated\n")

    result = check_lvs_report(str(report_path))

    assert result["status"] == "drifted"
    by_field = {check["field"]: check for check in result["checks"]}
    assert by_field["environment.reference_sha256"]["match"] is False
    assert by_field["environment.layout_sha256"]["match"] is True


def test_check_lvs_report_detects_moved_layout_hash(tmp_path):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    _write(Path(layout_path), _INVERTER_SPICE + "* mutated\n")

    result = check_lvs_report(str(report_path))

    assert result["status"] == "drifted"
    by_field = {check["field"]: check for check in result["checks"]}
    assert by_field["environment.layout_sha256"]["match"] is False


def test_check_lvs_report_with_deck_checks_deck_hash(tmp_path):
    """Inline extraction (`layout.file` + `layout.deck`) also reconciles
    `provenance.deck.content_hash` -- the third hash issue #1106's design
    question #2 calls out, beyond the two `environment.*_sha256` fields the
    pre-extracted-netlist form alone exercises."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(SKY130_INV), "sky130", output=reference_path)
    request = {
        "layout": {"file": str(SKY130_INV), "deck": "sky130"},
        "reference": {"netlist": reference_path, "top": extracted["top"]},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    result = check_lvs_report(str(report_path))

    fields = {check["field"] for check in result["checks"]}
    assert "provenance.deck.content_hash" in fields
    by_field = {check["field"]: check for check in result["checks"]}
    assert by_field["provenance.deck.content_hash"]["match"] is True


def test_check_lvs_report_missing_file_raises(tmp_path):
    with pytest.raises(LvsError, match="not found"):
        check_lvs_report(str(tmp_path / "nope.json"))


def test_check_lvs_report_malformed_json_raises(tmp_path):
    report_path = tmp_path / "bad.json"
    report_path.write_text("not json")
    with pytest.raises(LvsError, match="not valid JSON"):
        check_lvs_report(str(report_path))


def test_check_lvs_report_matching_versions_no_advisory(tmp_path):
    """A freshly-committed report's `provenance.klt_version`/
    `klayout_version` match the engine currently running `--check` (the
    ordinary case) -- `advisories` is present but empty (issue #1373)."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    result = check_lvs_report(str(report_path))

    assert result["advisories"] == []
    assert result["status"] == "match"


def test_check_lvs_report_klt_version_drift_advisory(tmp_path):
    """A committed report whose `provenance.klt_version` differs from the
    engine currently running `--check` surfaces a non-fatal advisory naming
    both versions -- `status`/`checks[]` (hash-integrity) are unaffected."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    report["provenance"]["klt_version"] = "0.0.1-forged"
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)

    result = check_lvs_report(str(report_path))

    # Non-fatal: the hash-integrity verdict is unaffected by version drift.
    assert result["status"] == "match"
    assert all(check["match"] for check in result["checks"])

    advisories = {entry["field"]: entry for entry in result["advisories"]}
    assert advisories.keys() == {"provenance.klt_version"}
    assert advisories["provenance.klt_version"]["report"] == "0.0.1-forged"
    assert advisories["provenance.klt_version"]["current"] == lvs._klt_version()


def test_check_lvs_report_klayout_version_drift_advisory(tmp_path):
    """Same as above, for `provenance.klayout_version`."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    report["provenance"]["klayout_version"] = "0.0.1-forged"
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)

    result = check_lvs_report(str(report_path))

    assert result["status"] == "match"
    advisories = {entry["field"]: entry for entry in result["advisories"]}
    assert advisories.keys() == {"provenance.klayout_version"}
    assert advisories["provenance.klayout_version"]["report"] == "0.0.1-forged"
    assert advisories["provenance.klayout_version"]["current"] == lvs._klayout_version()


def test_check_lvs_report_missing_version_fields_no_crash(tmp_path):
    """A report predating `provenance.klt_version`/`klayout_version` (older
    report format) is "unknown", not "drift" -- no advisory, and `--check`
    does not raise."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    del report["provenance"]["klt_version"]
    del report["provenance"]["klayout_version"]
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)

    result = check_lvs_report(str(report_path))

    assert result["advisories"] == []
    assert result["status"] == "match"


def test_rerun_lvs_report_clean_pass(tmp_path):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    result = rerun_lvs_report(str(report_path))

    assert result["mode"] == "rerun"
    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["status"] == "match"


def test_rerun_lvs_report_detects_mismatch(tmp_path):
    """A reference netlist mutated (a real parameter change, not a no-op
    comment) after the report was committed drifts under full-mode
    `--rerun`, naming `status`/`mismatch_count`/`mismatches` among the
    drifted fields."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    _write(
        Path(reference_path),
        _INVERTER_SPICE.replace("W=0.65U", "W=0.70U"),
    )

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "drifted"
    drifted_fields = {entry["field"] for entry in result["drift"]}
    assert "status" in drifted_fields
    assert "mismatch_count" in drifted_fields
    assert result["fresh"]["status"] == "mismatch"


def test_rerun_lvs_report_excludes_volatile_provenance_fields(tmp_path):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    report["provenance"]["klt_version"] = "0.0.1-forged"
    report["provenance"]["klayout_version"] = "0.0.1-forged"
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []


def test_rerun_lvs_report_excludes_extracted_netlist_path(tmp_path):
    """A report committed from an `options.keep_extracted: true` request
    (issue #1223) records `environment.extracted_netlist` as a path anchored
    to the *original* request document's directory. `_reconstruct_lvs_request`
    never re-asserts `keep_extracted`, so a fresh `--rerun` always reports
    that field as `null` -- a false drift, not a real one, when nothing else
    about the compare changed."""
    from klayout_tools.extract import run_extract

    reference_path = str(tmp_path / "ref.spice")
    extracted = run_extract(str(SKY130_INV), "sky130", output=reference_path)

    request_path = _write_request(
        tmp_path / "request.json",
        {
            "layout": {"file": str(SKY130_INV), "deck": "sky130"},
            "reference": {"netlist": reference_path, "top": extracted["top"]},
            "options": {"keep_extracted": True},
        },
    )
    report = run_lvs(request_path)
    assert report["environment"]["extracted_netlist"] is not None

    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["environment"]["extracted_netlist"] is None


def test_rerun_lvs_report_missing_layout_reference_raises(tmp_path):
    report_path = tmp_path / "bad.json"
    _write_report(report_path, {"engine": "klayout"})
    with pytest.raises(LvsError, match="layout.*reference"):
        rerun_lvs_report(str(report_path))


# --------------------------------------------------------------------------- #
# Issue #1205: the committed report must record enough to reconstruct the
# request `--rerun` re-runs -- the reference-side top (which differs from the
# layout-side top by construction for an LVS negative control) and the
# compare-shaping `options` block (whose loss made a `combine_devices: true`
# report re-run as an un-folded compare and call the difference "drift").
# --------------------------------------------------------------------------- #

#: A deliberately-broken copy of `_INVERTER_SPICE`'s cell -- the PMOS gate is
#: shorted to the output -- under its own `<cell>_shorted` circuit name. This
#: is the shape of an LVS negative control: the *layout* top is
#: `inv_shorted`, but it is compared against the *intact* cell's reference
#: netlist, whose top is `inv`, so the two tops differ by construction.
_SHORTED_INVERTER_LAYOUT_SPICE = """
.subckt inv_shorted A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y Y VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


def _negative_control_request(tmp_path: Path) -> dict:
    """The issue's own reproduction, in the pre-extracted-netlist form: a
    `<cell>_shorted` layout compared against the intact cell's reference
    netlist, so `layout.top` != `reference.top`."""
    layout_path = _write(tmp_path / "shorted.spice", _SHORTED_INVERTER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    return {
        "layout": {"netlist": layout_path, "top": "inv_shorted"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }


def _folded_request(tmp_path: Path) -> dict:
    """A folded/multi-finger layout compared against the single-lumped-device
    reference it is electrically identical to, which only reaches
    `status: "match"` with `options.combine_devices` set."""
    layout_path = _write(tmp_path / "folded.spice", _MULTIFINGER_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.spice", _INVERTER_SPICE)
    return {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
        "options": {"combine_devices": True},
    }


def test_run_lvs_echoes_reference_top_and_options(tmp_path):
    """The report records each side's own resolved top and the whole
    compare-shaping `options` block as used -- the fields `--rerun`
    reconstructs from (issue #1205)."""
    request = _negative_control_request(tmp_path)
    request["options"] = {"combine_devices": True}

    report = run_lvs(json.dumps(request))

    # `NetlistSpiceReader` upper-cases circuit names; the request's own
    # lower-case `top` still selects them (see `_select_circuit`).
    assert report["top"] == "INV_SHORTED"
    assert report["reference_top"] == "INV"
    assert report["options"] == {
        "combine_devices": True,
        "flatten_layout": False,
        "flatten_reference": False,
        "netgen_setup": None,
        "parameter_tolerance": None,
    }
    # The pre-#1205 fields are untouched -- this is an additive change, so no
    # `schema_version` bump (docs/json-contract.md, "additive envelope").
    assert report["schema_version"] == 1
    assert report["parameter_tolerance"] is None


def test_rerun_lvs_report_reconstructs_asymmetric_reference_top(tmp_path):
    """Issue #1205's first reproduction: a negative-control report (layout
    top `inv_shorted` vs. reference top `inv`) re-runs cleanly instead of
    exiting 1 with "top cell/subcircuit 'inv_shorted' not found in reference
    netlist", and reproduces the committed verdict without drift."""
    request = _negative_control_request(tmp_path)
    committed = run_lvs(json.dumps(request))
    assert committed["status"] == "mismatch"  # it is a negative control
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, committed)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["status"] == "mismatch"
    assert result["fresh"]["top"] == "INV_SHORTED"
    assert result["fresh"]["reference_top"] == "INV"


def test_rerun_lvs_report_preserves_combine_devices_option(tmp_path):
    """Issue #1205's second reproduction: a clean `status: "match"` report
    from a `combine_devices: true` request no longer re-runs as an un-folded
    compare and reports the resulting fresh mismatch list as drift."""
    request = _folded_request(tmp_path)
    committed = run_lvs(json.dumps(request))
    assert committed["status"] == "match"
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, committed)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["status"] == "match"
    assert result["fresh"]["options"]["combine_devices"] is True


def test_rerun_lvs_report_without_combine_devices_still_drifts_on_real_change(
    tmp_path,
):
    """The `combine_devices` round-trip must not paper over a *real* change:
    the same folded report drifts when the reference netlist actually moves
    after the report was committed."""
    request = _folded_request(tmp_path)
    committed = run_lvs(json.dumps(request))
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, committed)

    _write(tmp_path / "ref.spice", _INVERTER_SPICE.replace("W=0.65U", "W=0.70U"))

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "drifted"
    assert "status" in {entry["field"] for entry in result["drift"]}
    assert result["fresh"]["status"] == "mismatch"


def test_rerun_lvs_report_preserves_parameter_tolerance_in_options_block(tmp_path):
    """`options.parameter_tolerance` is echoed both at the top level (issue
    #589) and inside the new `options` block (#1205); the reconstruction
    reads one value, never two conflicting ones."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
        "options": {"parameter_tolerance": 0.01},
    }
    committed = run_lvs(json.dumps(request))
    assert committed["parameter_tolerance"] == 0.01
    assert committed["options"]["parameter_tolerance"] == 0.01
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, committed)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []


def test_rerun_lvs_report_legacy_report_without_request_echo_fields(tmp_path):
    """A report committed *before* issue #1205 added `reference_top`/
    `options` re-runs exactly as it used to: the reference top falls back to
    the single recorded `top`, the tolerance falls back to the top-level
    echo, and neither newly-added field is reported as drift (a field the
    committed report never carried cannot have drifted)."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
        "options": {"parameter_tolerance": 0.01},
    }
    committed = run_lvs(json.dumps(request))
    committed.pop("reference_top")
    committed.pop("options")
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, committed)

    result = rerun_lvs_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    # The fresh report still carries the new fields -- they are simply not
    # diffed against a committed report that predates them.
    assert result["fresh"]["reference_top"] == "INV"
    assert result["fresh"]["options"]["parameter_tolerance"] == 0.01


def test_check_lvs_report_cheap_mode_unaffected_by_asymmetric_top(tmp_path):
    """Cheap mode (`--check` without `--rerun`) reconstructs no request at
    all, so it keeps working unchanged on exactly the reports full mode used
    to choke on (issue #1205's "cheap mode is unaffected either way and must
    stay working")."""
    request = _negative_control_request(tmp_path)
    request["options"] = {"combine_devices": True}
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    result = check_lvs_report(str(report_path))

    assert result["mode"] == "check"
    assert result["status"] == "match"
    fields = {check["field"] for check in result["checks"]}
    assert fields == {"environment.layout_sha256", "environment.reference_sha256"}
    assert all(check["match"] for check in result["checks"])


def test_cli_lvs_check_clean_exits_zero(tmp_path, capsys):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))
    capsys.readouterr()

    assert main(["lvs", "--check", str(report_path), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "check"
    assert data["status"] == "match"


def test_cli_lvs_check_version_drift_advisory_text_exits_zero(tmp_path, capsys):
    """A version-drift advisory is non-fatal end to end through the CLI
    (issue #1373): exit code and `status` stay `0`/`"match"` (hashes
    unmutated), but the text output names the `[DRIFT]` advisory."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report = run_lvs(json.dumps(request))
    report["provenance"]["klt_version"] = "0.0.1-forged"
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, report)
    capsys.readouterr()

    assert main(["lvs", "--check", str(report_path)]) == 0
    out = capsys.readouterr().out
    assert "status: match" in out
    assert "[DRIFT] provenance.klt_version: 0.0.1-forged (report) vs" in out


def test_cli_lvs_check_drifted_exits_three(tmp_path, capsys):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))
    _write(Path(reference_path), _INVERTER_SPICE + "* mutated\n")
    capsys.readouterr()

    assert main(["lvs", "--check", str(report_path), "--format", "json"]) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "drifted"


def test_cli_lvs_check_rerun_exits_three_on_drift(tmp_path, capsys):
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))
    _write(Path(reference_path), _INVERTER_SPICE.replace("W=0.65U", "W=0.70U"))
    capsys.readouterr()

    assert (
        main(["lvs", "--check", str(report_path), "--rerun", "--format", "json"]) == 3
    )
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "rerun"
    assert data["status"] == "drifted"


def test_cli_lvs_check_with_request_argument_is_a_usage_error(tmp_path):
    """`request` and `--check` are a required, mutually exclusive argparse
    group (issue #1106) -- combining them is a usage error (exit 2), the
    same as any other mutually exclusive argparse pair, not an
    application-level (`--format json` error envelope) one."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    report_path = tmp_path / "lvs.json"
    _write_report(report_path, run_lvs(json.dumps(request)))

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "lvs",
                json.dumps(request),
                "--check",
                str(report_path),
                "--format",
                "json",
            ]
        )
    assert exc_info.value.code == 2


def test_cli_lvs_rerun_without_check_is_rejected(tmp_path, capsys):
    """`--rerun` alongside `request` (no `--check`) passes argparse's
    mutually-exclusive-group check but is still a semantic error at the
    application level (exit 1) -- `--rerun` only means something for
    `--check`."""
    layout_path, reference_path = _matching_netlist_request(tmp_path)
    request = {
        "layout": {"netlist": layout_path, "top": "inv"},
        "reference": {"netlist": reference_path, "top": "inv"},
    }
    capsys.readouterr()

    assert main(["lvs", json.dumps(request), "--rerun", "--format", "json"]) == 1
    err = json.loads(capsys.readouterr().err)
    assert "requires --check" in err["error"]["message"]


def test_cli_lvs_no_request_no_check_is_a_usage_error():
    """Omitting both `request` and `--check` is a usage error (exit 2) --
    unchanged from the pre-#1106 "`request` is a required positional"
    contract (`test_cli_missing_request_arg_is_usage_error` above), now
    enforced via a required mutually exclusive group instead of a plain
    required positional (see `parser.py`'s `lvs_input_group`)."""
    with pytest.raises(SystemExit) as exc_info:
        main(["lvs", "--format", "json"])
    assert exc_info.value.code == 2


# --------------------------------------------------------------------------- #
# Reference netlist form: gate-level-verilog -> plain-element conversion
# (issue #1336)
# --------------------------------------------------------------------------- #

from klayout_tools.verilog_netlist import (  # noqa: E402
    VerilogNetlistError,
    convert_gate_level_verilog,
    parse_gate_level_verilog,
    parse_subckt_pin_orders,
)

#: A small, structurally-representative gate-level netlist -- the shape
#: OpenROAD's own `write_verilog` produces for `klt place-and-route`'s
#: `verilog_path` (module instantiations, named-port-connected, a multi-bit
#: bus port expanded to per-bit connections at each instance, no
#: behavioral statements).
_GATE_LEVEL_VERILOG = """
// as-built gate-level netlist
module top(clk, rst_n, a_in, y_out);
  input clk;
  input rst_n;
  input [1:0] a_in;
  output [1:0] y_out;
  wire n1;

  mylib__inv_1 u1 (.A(a_in[0]), .Y(n1));
  mylib__buf_1 u2 (.A(n1), .Y(y_out[0]));
  mylib__inv_1 u3 (.A(a_in[1]), .Y(y_out[1]));
endmodule
"""

_MYLIB_SPICE = """
.subckt mylib__inv_1 A VGND VNB VPB VPWR Y
M1 Y A VGND VGND nfet L=0.15U W=0.5U
.ends
.subckt mylib__buf_1 A VGND VNB VPB VPWR Y
.ends
"""


def test_parse_gate_level_verilog_expands_bus_ports_and_connections():
    modules = parse_gate_level_verilog(_GATE_LEVEL_VERILOG)
    assert len(modules) == 1
    top = modules[0]
    assert top["name"] == "top"
    assert top["ports"] == [
        "clk",
        "rst_n",
        "a_in[1]",
        "a_in[0]",
        "y_out[1]",
        "y_out[0]",
    ]
    assert [inst["cell"] for inst in top["instances"]] == [
        "mylib__inv_1",
        "mylib__buf_1",
        "mylib__inv_1",
    ]
    assert top["instances"][0]["connections"] == {"A": "a_in[0]", "Y": "n1"}
    assert top["instances"][1]["connections"] == {"A": "n1", "Y": "y_out[0]"}


def test_parse_gate_level_verilog_strips_comments():
    text = """
    /* block
       comment */
    module m(a, y); // trailing
      input a;
      output y;
      cellx u0 (.A(a), .Y(y));
    endmodule
    """
    modules = parse_gate_level_verilog(text)
    assert modules[0]["instances"][0]["cell"] == "cellx"


def test_parse_gate_level_verilog_resolves_escaped_identifiers():
    text = r"""
    module m(a, y);
      input a;
      output y;
      \my.cell u0 (.A(a), .Y(y));
    endmodule
    """
    modules = parse_gate_level_verilog(text)
    assert modules[0]["instances"][0]["cell"] == "my.cell"


def test_parse_gate_level_verilog_escaped_identifier_connection_hierarchy_path():
    r"""A backend-emitted escaped identifier that embeds a flattened
    `generate`/`genvar` hierarchy path -- `[`, `]`, `.` and `/` all inside
    one Verilog escaped identifier -- is one atomic net name, never a bus
    bit-select (issue #1371)."""
    text = "\n".join(
        [
            r"module m(a, y);",
            r"  input a;",
            r"  output y;",
            r"  wire \my_array[0].u_inst/_01_ ;",
            r"  sky130_fd_sc_hd__inv_2 \my_array[0].u_inst/_13_  "
            r"(.A(a), .Y(\my_array[0].u_inst/_01_ ));",
            r"  sky130_fd_sc_hd__buf_2 \my_array[0].u_inst/_14_  "
            r"(.A(\my_array[0].u_inst/_01_ ), .X(y));",
            r"endmodule",
        ]
    )
    instances = parse_gate_level_verilog(text)[0]["instances"]
    assert [inst["name"] for inst in instances] == [
        "my_array[0].u_inst/_13_",
        "my_array[0].u_inst/_14_",
    ]
    assert instances[0]["connections"] == {"A": "a", "Y": "my_array[0].u_inst/_01_"}
    assert instances[1]["connections"] == {"A": "my_array[0].u_inst/_01_", "X": "y"}


def test_parse_gate_level_verilog_escaped_identifier_bit_select_needs_whitespace():
    r"""Verilog's escape rule is whitespace-terminated: `\a.b[3]` is one
    literal name, while `\c.d [3]` is a real single-index bit-select of the
    escaped net `c.d` (issue #1371)."""
    text = "\n".join(
        [
            r"module m(y);",
            r"  output y;",
            r"  cellx u0 (.A(\a.b[3] ), .B(\c.d [3]), .Y(y));",
            r"endmodule",
        ]
    )
    assert parse_gate_level_verilog(text)[0]["instances"][0]["connections"] == {
        "A": "a.b[3]",
        "B": "c.d[3]",
        "Y": "y",
    }


def test_parse_gate_level_verilog_escaped_identifier_range_slice_rejected():
    """An escaped base name does not buy a multi-bit range slice past this
    module's deliberately narrow grammar (issue #1371)."""
    text = "\n".join(
        [
            r"module m(y);",
            r"  output y;",
            r"  cellx u0 (.A(\c.d [3:0]), .Y(y));",
            r"endmodule",
        ]
    )
    with pytest.raises(VerilogNetlistError, match="range slice"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_escaped_identifier_expression_rejected():
    """Trailing content after a whitespace-terminated escaped identifier is
    still an unsupported expression, and still fails loudly (issue #1371)."""
    text = "\n".join(
        [
            r"module m(y);",
            r"  output y;",
            r"  cellx u0 (.A(\a.b + \c.d ), .Y(y));",
            r"endmodule",
        ]
    )
    with pytest.raises(VerilogNetlistError, match="not a plain net reference"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_lone_backslash_connection_rejected():
    """A backslash whose escaped name is empty fails as a conversion error,
    not as an unhandled crash (issue #1371)."""
    text = "\n".join(
        [
            r"module m(y);",
            r"  output y;",
            r"  cellx u0 (.A(\ ), .Y(y));",
            r"endmodule",
        ]
    )
    with pytest.raises(VerilogNetlistError, match="names nothing"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_escaped_identifier_assign_alias():
    r"""The same atomic-escaped-identifier rule applies to an `assign`
    alias's two operands (issue #1371)."""
    text = "\n".join(
        [
            r"module m(y);",
            r"  output y;",
            r"  assign \n[0].a = \n[1].b ;",
            r"  cellx u0 (.A(\n[0].a ), .Y(y));",
            r"endmodule",
        ]
    )
    connections = parse_gate_level_verilog(text)[0]["instances"][0]["connections"]
    assert connections["A"] == "n[1].b"


def test_convert_gate_level_verilog_writes_escaped_hierarchy_path_nets():
    r"""End-to-end: an escaped-identifier net survives conversion as the
    literal name, so the SPICE reference `klt lvs` compares against carries
    the same net the layout side does (issue #1371)."""
    text = "\n".join(
        [
            r"module m(a, y);",
            r"  input a;",
            r"  output y;",
            r"  wire \my_array[0].u_inst/_01_ ;",
            r"  mylib__inv_1 \my_array[0].u_inst/_13_  "
            r"(.A(a), .Y(\my_array[0].u_inst/_01_ ));",
            r"  mylib__buf_1 u2 (.A(\my_array[0].u_inst/_01_ ), .Y(y));",
            r"endmodule",
        ]
    )
    orders = parse_subckt_pin_orders(_MYLIB_SPICE)
    out = convert_gate_level_verilog(text, pin_order_lookup=orders.get)
    assert "Xmy_array_0__u_inst__13_ a my_array[0].u_inst/_01_ mylib__inv_1" in out
    assert "Xu2 my_array[0].u_inst/_01_ y mylib__buf_1" in out


def test_parse_gate_level_verilog_resolves_assign_alias():
    text = """
    module m(a, y);
      input a;
      output y;
      assign n1 = a;
      cellx u0 (.A(n1), .Y(y));
    endmodule
    """
    modules = parse_gate_level_verilog(text)
    assert modules[0]["instances"][0]["connections"]["A"] == "a"


def test_parse_gate_level_verilog_resolves_bit_constants():
    text = """
    module m(y);
      output y;
      cellx u0 (.A(1'b0), .Y(y));
    endmodule
    """
    modules = parse_gate_level_verilog(text)
    assert modules[0]["instances"][0]["connections"]["A"] == "__CONST0__"


def test_parse_gate_level_verilog_no_module_fails():
    with pytest.raises(VerilogNetlistError, match="no 'module'"):
        parse_gate_level_verilog("// nothing here\n")


def test_parse_gate_level_verilog_ansi_style_port_list_rejected():
    text = "module m(input a, output y);\n  cellx u0 (.A(a), .Y(y));\nendmodule\n"
    with pytest.raises(VerilogNetlistError, match="ANSI-style"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_positional_connection_rejected():
    text = "module m(a, y);\n  input a;\n  output y;\n  cellx u0 (a, y);\nendmodule\n"
    with pytest.raises(VerilogNetlistError, match="positional"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_range_slice_connection_rejected():
    text = (
        "module m(a, y);\n  input [3:0] a;\n  output [3:0] y;\n"
        "  cellx u0 (.A(a[3:0]), .Y(y));\nendmodule\n"
    )
    with pytest.raises(VerilogNetlistError, match="range slice"):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_concatenation_rejected():
    text = (
        "module m(a, b, y);\n  input a;\n  input b;\n  output y;\n"
        "  cellx u0 (.A({a, b}), .Y(y));\nendmodule\n"
    )
    with pytest.raises(VerilogNetlistError):
        parse_gate_level_verilog(text)


def test_parse_gate_level_verilog_unconnected_pin_is_dropped():
    text = (
        "module m(a, y);\n  input a;\n  output y;\n"
        "  cellx u0 (.A(a), .Y());\nendmodule\n"
    )
    modules = parse_gate_level_verilog(text)
    assert modules[0]["instances"][0]["connections"] == {"A": "a"}


def test_parse_subckt_pin_orders_basic():
    orders = parse_subckt_pin_orders(_MYLIB_SPICE)
    assert orders == {
        "mylib__inv_1": ["A", "VGND", "VNB", "VPB", "VPWR", "Y"],
        "mylib__buf_1": ["A", "VGND", "VNB", "VPB", "VPWR", "Y"],
    }


def test_parse_subckt_pin_orders_continuation_line():
    text = ".subckt wide_cell A B C\n+ D E\n.ends\n"
    assert parse_subckt_pin_orders(text) == {"wide_cell": ["A", "B", "C", "D", "E"]}


def test_parse_subckt_pin_orders_uppercase_directive():
    text = ".SUBCKT foo A B\n.ENDS\n"
    assert parse_subckt_pin_orders(text) == {"foo": ["A", "B"]}


def test_parse_subckt_pin_orders_real_sky130_header():
    # Copied verbatim from a real, volare-fetched `sky130A` install's own
    # `libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice`, line 8426
    # (verified 2026-08-23) -- confirms real-PDK pin-order resolution without
    # this test itself depending on a real PDK install being present.
    real_header = ".subckt sky130_fd_sc_hd__inv_1 A VGND VNB VPB VPWR Y\n.ends\n"
    orders = parse_subckt_pin_orders(real_header)
    assert orders["sky130_fd_sc_hd__inv_1"] == ["A", "VGND", "VNB", "VPB", "VPWR", "Y"]


def test_parse_subckt_pin_orders_real_gf180mcu_header():
    # Copied verbatim from a real, volare-fetched `gf180mcuC` install's own
    # `libs.ref/gf180mcu_fd_sc_mcu9t5v0/{spice,cdl}/gf180mcu_fd_sc_mcu9t5v0.
    # {spice,cdl}` (byte-identical headers, verified 2026-08-23) -- a
    # different pin-naming convention (`I`/`ZN`, not `A`/`Y`) than sky130's,
    # confirming pin order/names are read from the file, never hardcoded.
    real_header = ".SUBCKT gf180mcu_fd_sc_mcu9t5v0__inv_1 I ZN VDD VNW VPW VSS\n.ENDS\n"
    orders = parse_subckt_pin_orders(real_header)
    assert orders["gf180mcu_fd_sc_mcu9t5v0__inv_1"] == [
        "I",
        "ZN",
        "VDD",
        "VNW",
        "VPW",
        "VSS",
    ]


def test_convert_gate_level_verilog_writes_expected_spice():
    orders = parse_subckt_pin_orders(_MYLIB_SPICE)
    out = convert_gate_level_verilog(_GATE_LEVEL_VERILOG, pin_order_lookup=orders.get)
    # Power/ground pins never appear -- `verilog_path` never carries them
    # (see the module docstring's "No power/ground pins carried" section).
    assert "VGND" not in out and "VPWR" not in out
    assert ".SUBCKT mylib__inv_1 A Y" in out
    assert ".SUBCKT mylib__buf_1 A Y" in out
    assert "Xu1 a_in[0] n1 mylib__inv_1" in out
    assert "Xu2 n1 y_out[0] mylib__buf_1" in out
    assert "Xu3 a_in[1] y_out[1] mylib__inv_1" in out


def test_convert_gate_level_verilog_unresolvable_cell_errors():
    with pytest.raises(VerilogNetlistError, match="no resolvable pin order"):
        convert_gate_level_verilog(
            _GATE_LEVEL_VERILOG, pin_order_lookup=lambda _c: None
        )


def test_convert_gate_level_verilog_unknown_pin_connection_errors():
    text = (
        "module m(a, y);\n  input a;\n  output y;\n"
        "  cellx u0 (.NOPE(a), .Y(y));\nendmodule\n"
    )
    with pytest.raises(VerilogNetlistError, match="not in its resolved PDK pin list"):
        convert_gate_level_verilog(text, pin_order_lookup=lambda _c: ["A", "Y"])


def test_convert_gate_level_verilog_hierarchical_submodule_reference():
    text = """
    module leaf(a, y);
      input a;
      output y;
      cellx u0 (.A(a), .Y(y));
    endmodule
    module top(a, y);
      input a;
      output y;
      leaf u1 (.a(a), .y(y));
    endmodule
    """
    out = convert_gate_level_verilog(text, pin_order_lookup=lambda _c: ["A", "Y"])
    assert ".SUBCKT leaf a y" in out
    assert "Xu1 a y leaf" in out


def test_convert_gate_level_verilog_unconnected_pin_synthesizes_fresh_net():
    # `u0` leaves `Y` unconnected, but `u1` (same cell type) does connect
    # `Y` -- so the black-box stub's declared pin list includes `Y` (the
    # union across every instance of that cell type), and `u0`'s own call
    # gets a synthesized, unique disconnected net for it.
    text = (
        "module m(a, b);\n  input a;\n  output b;\n"
        "  cellx u0 (.A(a), .Y());\n"
        "  cellx u1 (.A(b), .Y(b));\n"
        "endmodule\n"
    )
    out = convert_gate_level_verilog(text, pin_order_lookup=lambda _c: ["A", "Y"])
    assert "Xu0 a __NC_u0_Y__ cellx" in out
    assert "Xu1 b b cellx" in out


# --- integration through run_lvs -------------------------------------------- #


def _make_fake_pdk_library(
    tmp_path: Path,
    variant: str,
    library: str,
    subckt_text: str,
    *,
    asset: str = "spice",
) -> str:
    """A minimal, hermetic fake PDK install (no dependency on a real PDK
    being installed) shaped exactly like a real open_pdks/volare/ciel
    install -- `<root>/<variant>/{libs.tech,libs.ref/<library>/<asset>/
    <library>.<asset>}` -- so `find_pdk`'s real resolver (never a shortcut)
    is exercised end to end. Mirrors `test_extract.py`'s own
    `_make_pdk_install` helper, extended with the `libs.ref` asset this
    issue's pin-order resolution needs."""
    root = tmp_path / "pdk_install"
    (root / variant / "libs.tech").mkdir(parents=True, exist_ok=True)
    lib_dir = root / variant / "libs.ref" / library / asset
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / f"{library}.{asset}").write_text(subckt_text)
    return str(root)


#: Layout side, hand-written to mirror exactly what `klt extract
#: --abstract-cells` would write for this design (issue #620's own black-box
#: `.SUBCKT ... .ENDS` shape): two pin-only standard-cell stubs plus a top
#: circuit calling them.
_GATE_LEVEL_LAYOUT_SPICE = """
.subckt top in mid out
X1 in mid mylib__inv_1
X2 mid out mylib__buf_1
.ends
.subckt mylib__inv_1 A Y
.ends
.subckt mylib__buf_1 A Y
.ends
"""

#: The same design's connectivity with one real defect: instance `X2`'s
#: connections are swapped (`out mid` instead of `mid out`), matching this
#: doc/test suite's "negative control" convention elsewhere in this file --
#: a real, catchable connectivity defect, not merely a parameter change.
_GATE_LEVEL_LAYOUT_SPICE_BROKEN = _GATE_LEVEL_LAYOUT_SPICE.replace(
    "X2 mid out mylib__buf_1", "X2 out mid mylib__buf_1"
)

_GATE_LEVEL_REFERENCE_VERILOG = """
module top(in, mid, out);
  input in;
  output mid;
  output out;
  mylib__inv_1 u1 (.A(in), .Y(mid));
  mylib__buf_1 u2 (.A(mid), .Y(out));
endmodule
"""

_GATE_LEVEL_LIBRARY_SPICE = (
    ".subckt mylib__inv_1 A Y\n.ends\n.subckt mylib__buf_1 A Y\n.ends\n"
)


def test_run_lvs_gate_level_verilog_reference_converts_and_matches(tmp_path):
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    assert report["mismatch_count"] == 0


#: The same design whose internal net and one instance name are Verilog
#: escaped identifiers embedding a place-and-route-flattened
#: `generate`/`genvar` hierarchy path -- `[`, `]`, `.` and `/` all inside a
#: single escaped name, which is exactly what a backend tool emits for a
#: module-instance array (issue #1371).
_GATE_LEVEL_REFERENCE_VERILOG_ESCAPED = "\n".join(
    [
        r"module top(in, out);",
        r"  input in;",
        r"  output out;",
        r"  wire \my_array[0].u_inst/_01_ ;",
        r"  mylib__inv_1 \my_array[0].u_inst/_13_  "
        r"(.A(in), .Y(\my_array[0].u_inst/_01_ ));",
        r"  mylib__buf_1 u2 (.A(\my_array[0].u_inst/_01_ ), .Y(out));",
        r"endmodule",
        r"",
    ]
)

_GATE_LEVEL_LAYOUT_SPICE_ESCAPED = """
.subckt top in out
X1 in my_array[0].u_inst/_01_ mylib__inv_1
X2 my_array[0].u_inst/_01_ out mylib__buf_1
.ends
.subckt mylib__inv_1 A Y
.ends
.subckt mylib__buf_1 A Y
.ends
"""


def test_run_lvs_gate_level_verilog_reference_escaped_hierarchy_names(tmp_path):
    """End to end: a reference netlist whose escaped identifiers carry a
    flattened hierarchy path converts and compares, instead of failing the
    whole run with "could not convert gate-level-verilog reference netlist"
    (issue #1371)."""
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE_ESCAPED)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG_ESCAPED)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    assert report["mismatch_count"] == 0


#: The same design with power pins on the LAYOUT side only -- the real
#: shape `klt extract --abstract-cells` produces against sky130/gf180mcu
#: standard cells, whose own definitions draw in-cell `VPWR`/`VGND`/well-tie
#: labels. The Verilog reference cannot carry them (`verilog_path` is
#: written without `-include_pwr_gnd`), so this asymmetry is the normal
#: case for this form, not an edge case.
_GATE_LEVEL_LAYOUT_SPICE_WITH_POWER = """
.subckt top in mid out VPWR VGND
X1 in mid VGND VPWR mylib__inv_1
X2 mid out VGND VPWR mylib__buf_1
.ends
.subckt mylib__inv_1 A Y VGND VPWR
.ends
.subckt mylib__buf_1 A Y VGND VPWR
.ends
"""

_GATE_LEVEL_LIBRARY_SPICE_WITH_POWER = (
    ".subckt mylib__inv_1 A Y VGND VPWR\n.ends\n"
    ".subckt mylib__buf_1 A Y VGND VPWR\n.ends\n"
)


def test_run_lvs_gate_level_verilog_tolerates_layout_side_power_pins(tmp_path):
    """The layout side carries `VPWR`/`VGND` pins and nets the Verilog
    reference structurally cannot; the compare is still `"match"`.

    This is the documented behaviour of `docs/cli/lvs.md`'s "No power/ground
    pins" note (issue #1336) and the reason this form is usable at all
    against a real `klt extract --abstract-cells` layout netlist: KLayout's
    comparer matches the black-box cell circuits on their common,
    name-matched signal pins and tolerates the layout's extra pins/nets,
    rather than reporting a `pin.unmatched` per standard-cell instance.
    """
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE_WITH_POWER
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE_WITH_POWER)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"
    assert report["mismatch_count"] == 0
    assert "pin.unmatched" not in report["category_counts"]


def test_run_lvs_gate_level_verilog_power_miswire_is_not_detectable(tmp_path):
    """The other side of the coin above, pinned deliberately: a layout whose
    `VGND` pin is wired to the power rail still reports `"match"`.

    A gate-level-Verilog reference has no power connectivity to contradict
    the layout's, so this compare proves *signal* connectivity only. This
    test exists so the limitation is a measured, enforced fact rather than a
    prose caveat -- if a future change made power connectivity comparable,
    this test fails and the claim in `docs/cli/lvs.md` must be updated with
    it.
    """
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE_WITH_POWER
    )
    miswired = _GATE_LEVEL_LAYOUT_SPICE_WITH_POWER.replace(
        "X1 in mid VGND VPWR mylib__inv_1", "X1 in mid VPWR VPWR mylib__inv_1"
    )
    layout_path = _write(tmp_path / "layout.spice", miswired)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


def test_run_lvs_gate_level_verilog_reference_via_cdl_fallback(tmp_path):
    # `.cdl` (no `spice/` asset at all) -- exercises the fallback resolution
    # path, and gf180mcu's own real pin-naming convention (`I`/`ZN`).
    root = _make_fake_pdk_library(
        tmp_path,
        "gf180mcuC",
        "gf180mcu_fd_sc_mcu9t5v0",
        ".SUBCKT gf180mcu_fd_sc_mcu9t5v0__inv_1 I ZN\n.ENDS\n",
        asset="cdl",
    )
    layout_path = _write(
        tmp_path / "layout.spice",
        ".subckt top a y\nX1 a y gf180mcu_fd_sc_mcu9t5v0__inv_1\n.ends\n"
        ".subckt gf180mcu_fd_sc_mcu9t5v0__inv_1 I ZN\n.ends\n",
    )
    reference_path = _write(
        tmp_path / "ref.v",
        "module top(a, y);\n  input a;\n  output y;\n"
        "  gf180mcu_fd_sc_mcu9t5v0__inv_1 u1 (.I(a), .ZN(y));\nendmodule\n",
    )
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "gf180mcu_fd_sc_mcu9t5v0",
            "pdk": "gf180mcuC",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "match"


def test_run_lvs_gate_level_verilog_reference_detects_defect(tmp_path):
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE_BROKEN)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    report = run_lvs(json.dumps(request))
    assert report["status"] == "mismatch"
    assert report["mismatch_count"] >= 1


def test_run_lvs_gate_level_verilog_missing_library_errors(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
        },
    }
    with pytest.raises(LvsError, match="reference.library is required"):
        run_lvs(json.dumps(request))


def test_run_lvs_gate_level_verilog_unknown_cell_errors(tmp_path):
    root = _make_fake_pdk_library(
        tmp_path, "myvariant", "mylib", _GATE_LEVEL_LIBRARY_SPICE
    )
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    reference_path = _write(
        tmp_path / "ref.v",
        "module top(in, out);\n  input in;\n  output out;\n"
        "  mylib__nope_1 u1 (.A(in), .Y(out));\nendmodule\n",
    )
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": root,
        },
    }
    with pytest.raises(LvsError, match="could not convert gate-level-verilog"):
        run_lvs(json.dumps(request))


def test_run_lvs_gate_level_verilog_pdk_not_found_errors(tmp_path):
    layout_path = _write(tmp_path / "layout.spice", _GATE_LEVEL_LAYOUT_SPICE)
    reference_path = _write(tmp_path / "ref.v", _GATE_LEVEL_REFERENCE_VERILOG)
    request = {
        "layout": {"netlist": layout_path, "top": "top"},
        "reference": {
            "netlist": reference_path,
            "top": "top",
            "form": "gate-level-verilog",
            "library": "mylib",
            "pdk": "myvariant",
            "pdk_root": str(tmp_path / "does-not-exist"),
        },
    }
    with pytest.raises(LvsError, match="no supported-layout PDK install"):
        run_lvs(json.dumps(request))


# --- real-PDK pin-order resolution ------------------------------------------ #
#
# The tests above pin pin-order *parsing* against `.subckt` header lines
# copied verbatim out of real installs, which keeps them hermetic. These two
# additionally exercise the real resolution path end to end --
# `lvs._resolve_gate_level_pin_order_lookup` -> `pdk.find_pdk` ->
# `libs.ref/<library>/{spice,cdl}/<library>.{spice,cdl}` -> the real file's
# own bytes -- on any machine with a real volare/ciel install, and skip
# cleanly where none is present (the same `list_pdks()`-search convention
# `tests/test_synthesize.py`'s `_find_real_sky130_variant` established for
# its own real-PDK integration tier). Issue #1336's acceptance criterion is
# that pin order comes from the library, never a hardcoded table, for BOTH
# supported standard-cell libraries -- and the two libraries disagree on
# both pin names and pin order (`A ... Y` vs `I ZN ...`), so a hardcoded
# table could not satisfy both.


def _find_real_library_pin_order_variant(library: str) -> tuple[str, str] | None:
    """Search every install/variant `list_pdks()` discovers for one shipping
    a real `libs.ref/<library>/{spice,cdl}/<library>.{spice,cdl}` file.
    Returns ``(root, variant)`` or ``None``."""
    try:
        result = pdk.list_pdks()
    except Exception:
        return None
    for install in result["installs"]:
        for variant in install["variants"]:
            for asset, extension in (("spice", "spice"), ("cdl", "cdl")):
                candidate = os.path.join(
                    install["root"],
                    variant["name"],
                    "libs.ref",
                    library,
                    asset,
                    f"{library}.{extension}",
                )
                if os.path.isfile(candidate):
                    return install["root"], variant["name"]
    return None


_REAL_SKY130_HD_VARIANT = _find_real_library_pin_order_variant("sky130_fd_sc_hd")
_REAL_GF180MCU_9T5V0_VARIANT = _find_real_library_pin_order_variant(
    "gf180mcu_fd_sc_mcu9t5v0"
)


@pytest.mark.skipif(
    _REAL_SKY130_HD_VARIANT is None,
    reason="no real sky130_fd_sc_hd .spice/.cdl library resolves via list_pdks()",
)
def test_real_sky130_library_resolves_pin_order_from_the_installed_file():
    root, variant = _REAL_SKY130_HD_VARIANT
    lookup = lvs._resolve_gate_level_pin_order_lookup("sky130_fd_sc_hd", variant, root)
    # sky130's own declared order for a 1x inverter: signal `A`, the four
    # supply/well pins, then the output `Y` -- alphabetical, with the
    # supplies interleaved between the signal pins rather than grouped at
    # either end, which is exactly why it cannot be guessed.
    assert lookup("sky130_fd_sc_hd__inv_1") == ["A", "VGND", "VNB", "VPB", "VPWR", "Y"]
    # A sequential cell too, so the assertion is not resting on one
    # trivially-small cell -- and it shows the same alphabetical
    # interleaving (`Q` lands *after* all four supplies, not next to `D`).
    assert lookup("sky130_fd_sc_hd__dfxtp_1") == [
        "CLK",
        "D",
        "VGND",
        "VNB",
        "VPB",
        "VPWR",
        "Q",
    ]
    # An unresolvable cell type returns None -- the signal
    # `convert_gate_level_verilog` turns into its own hard error.
    assert lookup("sky130_fd_sc_hd__not_a_real_cell") is None


@pytest.mark.skipif(
    _REAL_GF180MCU_9T5V0_VARIANT is None,
    reason=(
        "no real gf180mcu_fd_sc_mcu9t5v0 .spice/.cdl library resolves via list_pdks()"
    ),
)
def test_real_gf180mcu_library_resolves_pin_order_from_the_installed_file():
    root, variant = _REAL_GF180MCU_9T5V0_VARIANT
    lookup = lvs._resolve_gate_level_pin_order_lookup(
        "gf180mcu_fd_sc_mcu9t5v0", variant, root
    )
    # gf180mcu's own convention differs from sky130's on BOTH axes: pin
    # names (`I`/`ZN`, not `A`/`Y`) and order (signals first, supplies
    # last). Resolving both libraries correctly from one code path is the
    # "never hardcoded" acceptance criterion.
    assert lookup("gf180mcu_fd_sc_mcu9t5v0__inv_1") == [
        "I",
        "ZN",
        "VDD",
        "VNW",
        "VPW",
        "VSS",
    ]
    assert lookup("gf180mcu_fd_sc_mcu9t5v0__not_a_real_cell") is None
