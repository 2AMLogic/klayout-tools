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
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import lvs
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
    # declared pin) -- which reference name each happened to resolve to is
    # not asserted, since the comparer's own tie-break is not part of this
    # module's documented contract.
    assert {by_reference["P1"]["layout"], by_reference["P2"]["layout"]} == {
        "$1",
        "$2",
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


def test_body_unverified_warns_nmos_and_pmos_on_gf180mcu(tmp_path):
    """gf180mcu draws no distinct NMOS substrate/tap layer *and* no distinct
    PMOS well-tap layer either (`Comp` is shared with ordinary active,
    `ExtractionDeck.tap is None`) -- both polarities warn."""
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
    assert set(body_entries) == {"nfet", "pfet"}
    assert all(entry["severity"] == "warning" for entry in body_entries.values())
    assert all(entry["side"] == "layout" for entry in body_entries.values())


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


def test_lvs_partial_match_combine_devices_runtimeerror_degrades_gracefully(
    tmp_path, monkeypatch
):
    """End-to-end: `klt lvs` with `options.combine_devices: true` does not
    raise an unhandled traceback when KLayout's own `combine_devices()` hits
    the partial-match internal error -- it completes through the normal
    JSON-envelope return, with a `device.combine_incomplete` warning entry
    (one per side) recording that combine did not fully apply, instead of
    the whole run aborting (issue #466)."""
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
    assert report["status"] == "mismatch"


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
    assert all(m["severity"] == "warning" for m in report["mismatches"])

    # Issue #281: every NMOS body terminal is tied to the deck's synthetic
    # substrate net (neither curated deck draws a distinct NMOS tap layer),
    # so the `device.body_unverified` warning always fires for `nfet`; the
    # `pfet` counterpart additionally fires only when the deck has no
    # distinct well-tap layer either (gf180mcu today; sky130's `tap` layer
    # gives PMOS bodies a real net).
    from klayout_tools.decks import get_extraction_deck

    deck_config = get_extraction_deck(deck)
    expected_body_classes = {deck_config.nfet_class}
    if deck_config.tap is None:
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


class _FakeNetgenCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


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
        return _FakeNetgenCompleted(stdout)

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


def test_netgen_engine_hints_unsupported_raises(tmp_path, monkeypatch):
    _stub_netgen_subprocess(monkeypatch, log_text=_NETGEN_MATCH_LOG)
    path = _netgen_request(tmp_path, hints={"same_nets": [["VPWR", "VPWR"]]})

    with pytest.raises(LvsError, match="only supported for engine 'klayout'"):
        run_lvs(path)


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
