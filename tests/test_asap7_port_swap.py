"""Issue #8084: the 11 pinned ASAP7 RVT masters whose series-order difference
from the CDL sits across two commutative inputs.

Standalone and unanchored ``klt lvs`` absorbs that difference by swapping the
two ports and reports ``status: "match"``; anchor the pins and the same cells
mismatch like the ``series-order`` cells. These corpus tests (skipped with a
reason unless ``scripts/fetch-pdks.sh`` has fetched lambdapdk v0.2.17) pin the
partition and adjudicate the difference. They live beside
``test_asap7_finfet.py`` rather than inside it because that file is a
baselined oversized file the per-file size ratchet forbids growing.
"""

from __future__ import annotations

import json

import pytest

from test_asap7_finfet import (
    _FOLDED_STACK_CELLS,
    _NO_DEVICE_CELLS,
    _RVT_CDL,
    _RVT_CDL_SHA256,
    _RVT_GDS,
    _RVT_GDS_SHA256,
    _SERIES_ORDER_CELLS,
    _cdl_block,
    _corpus_lvs,
    _errors,
    _needs_corpus,
    _sha256,
)

#: Layout and CDL differ only in series order across two *commutative* inputs
#: (issue #8084). Unanchored and standalone the comparer absorbs the
#: difference by swapping the ports. A block can also match, even after
#: flattening, if the inputs can still exchange. Pin anchors or asymmetric
#: connections can prevent that swap.
#: Pinned in `KNOWN_UNSUPPORTED_ASAP7_CELLS` as `"series-order-port-swap"`.
_PORT_SWAP_SERIES_ORDER_CELLS = frozenset(
    f"{name}_ASAP7_75t_R"
    for name in (
        "AND4x1 AO21x1 AO21x2 AOI32xp33 FAx1 OAI31xp33 OAI31xp67 "
        "OAI322xp33 OAI32xp33 OAI33xp33 XOR2x2"
    ).split()
)
#: Every master `KNOWN_UNSUPPORTED_ASAP7_CELLS` pins.
_PINNED_CELLS = (
    _FOLDED_STACK_CELLS | _SERIES_ORDER_CELLS | _PORT_SWAP_SERIES_ORDER_CELLS
)
#: The commutative port pair(s) each `_PORT_SWAP_SERIES_ORDER_CELLS` member's
#: unanchored match rests on (layout port, reference port), as its
#: `net_correspondence` reports them.
_PORT_SWAP_PAIRS = {
    "AND4x1_ASAP7_75t_R": (("A", "D"), ("B", "C")),
    "AO21x1_ASAP7_75t_R": (("A1", "A2"),),
    "AO21x2_ASAP7_75t_R": (("A1", "A2"),),
    "AOI32xp33_ASAP7_75t_R": (("B1", "B2"),),
    "FAx1_ASAP7_75t_R": (("A", "B"),),
    "OAI31xp33_ASAP7_75t_R": (("A1", "A3"),),
    "OAI31xp67_ASAP7_75t_R": (("A1", "A3"),),
    "OAI322xp33_ASAP7_75t_R": (("A1", "A3"),),
    "OAI32xp33_ASAP7_75t_R": (("B1", "B2"),),
    "OAI33xp33_ASAP7_75t_R": (("B1", "B3"),),
    "XOR2x2_ASAP7_75t_R": (("A", "B"),),
}
_ANCHORED = {"anchor_top_level_pins": True}


def _swapped_pins(report: dict, ports: set[str]) -> dict[str, str]:
    """``{layout port: reference port}`` for the top-level pins the comparer
    paired under different names, among the cell's declared ``ports``."""
    return {
        pair["layout"]: pair["reference"]
        for pair in report["net_correspondence"]
        if pair["pin"]
        and pair["layout"] in ports
        and pair["reference"] in ports
        and pair["layout"] != pair["reference"]
    }


def _permuted_cdl_block(cell: str, swapped: dict[str, str]) -> str:
    """The cell's CDL block with every ``swapped`` port renamed to its partner
    in the body (the ``.SUBCKT`` header is left alone): the CDL wired the way
    the layout draws the stack."""
    header, *body = _cdl_block(cell).splitlines()
    permuted = []
    for line in body:
        tokens = line.split()
        permuted.append(
            " ".join([tokens[0], *(swapped.get(token, token) for token in tokens[1:])])
            if tokens
            else line
        )
    return "\n".join([header, *permuted]) + "\n"


@_needs_corpus
@pytest.mark.parametrize("cell", sorted(_PORT_SWAP_SERIES_ORDER_CELLS))
def test_corpus_port_swap_series_order_cells_match_only_when_unanchored(tmp_path, cell):
    """Adjudication: the standalone ``match`` rests on a non-identity pairing
    of commutative ports and is silent; anchoring the pins turns it into a
    mismatch that carries the named limit; and permuting exactly those ports
    in the CDL -- nothing else -- restores the match with the pins anchored,
    so the difference is series order, not an extraction defect."""
    ports = set(_cdl_block(cell).splitlines()[0].split()[2:])
    pairs = _PORT_SWAP_PAIRS[cell]
    swapped = {name: other for a, b in pairs for other, name in ((a, b), (b, a))}
    assert swapped.keys() <= ports and len(swapped) == 2 * len(pairs)

    unanchored = _corpus_lvs(cell)
    assert unanchored["status"] == "match", [
        m for m in unanchored["mismatches"] if m["severity"] == "error"
    ][:5]
    assert "topology.known_limitation" not in unanchored["category_counts"]
    assert _swapped_pins(unanchored, ports) == swapped

    anchored = _corpus_lvs(cell, options=dict(_ANCHORED))
    assert anchored["status"] == "mismatch" and _errors(anchored)
    (entry,) = [
        m
        for m in anchored["mismatches"]
        if m["category"] == "topology.known_limitation"
    ]
    assert anchored["category_counts"]["topology.known_limitation"] == 1
    assert entry["severity"] == "warning" and entry["side"] == "layout"
    assert entry["details"] == {"cell": cell, "limitation": "series-order-port-swap"}

    permuted = tmp_path / "permuted.cdl"
    permuted.write_text(_permuted_cdl_block(cell, swapped))
    restored = _corpus_lvs(cell, permuted, options=dict(_ANCHORED))
    assert restored["status"] == "match", [
        m for m in restored["mismatches"] if m["severity"] == "error"
    ][:5]
    assert restored["error_count"] == 0


@_needs_corpus
def test_unanchored_port_swap_with_an_independent_fin_count_error(tmp_path):
    cell = "AO21x1_ASAP7_75t_R"
    cdl = _cdl_block(cell)
    (device,) = [line for line in cdl.splitlines() if line.startswith("MM7 ")]
    assert "nfin=3" in device
    reference = tmp_path / "wrong-fin-count.cdl"
    reference.write_text(cdl.replace(device, device.replace("nfin=3", "nfin=4")))

    report = _corpus_lvs(cell, reference)
    assert report["status"] == "mismatch"
    assert _swapped_pins(report, {"A1", "A2"}) == {"A1": "A2", "A2": "A1"}
    assert "device.unmatched" not in report["category_counts"]
    (error,) = [m for m in _errors(report) if m["category"] == "device.property"]
    assert error["property"] == {"name": "nfin", "layout": 3, "reference": 4}
    (warning,) = [
        m for m in report["mismatches"] if m["category"] == "topology.known_limitation"
    ]
    assert warning["severity"] == "warning"
    assert warning["details"] == {"cell": cell, "limitation": "series-order-port-swap"}
    assert "can contribute to a mismatch when" in warning["description"]


_ANCHORED_SWEEP_SCRIPT = """
import json, re, sys
from klayout_tools import build_identity
from klayout_tools.lvs import LvsError, run_lvs

build_identity._checkout_identity = lambda *a, **k: {
    "git_commit": None, "git_tag": None, "dirty": None, "is_release": False}
gds, cdl, out = sys.argv[1:4]
cells = re.findall(r"^\\.SUBCKT (\\S+)", open(cdl).read(), re.M | re.I)
results = {}
for cell in cells:
    request = {
        "layout": {"file": gds, "deck": "asap7", "top": cell},
        "reference": {"netlist": cdl, "top": cell},
        "options": {
            "finfet_bulk": {"nmos": "VSS", "pmos": "VDD"},
            "anchor_top_level_pins": True,
        },
    }
    try:
        report = run_lvs(json.dumps(request))
        results[cell] = {
            "status": report["status"],
            "known_limitation": sorted(
                m["details"]["cell"] for m in report["mismatches"]
                if m["category"] == "topology.known_limitation"
            ),
        }
    except LvsError as exc:
        results[cell] = {
            "status": "error: " + str(exc), "known_limitation": []}
json.dump(results, open(out, "w"))
"""


@_needs_corpus
def test_corpus_full_rvt_library_anchored_in_a_checked_subprocess(tmp_path):
    """Every CDL cell once more with ``options.anchor_top_level_pins: true``,
    through ``run_lvs`` in one subprocess (so a native KLayout crash fails this
    test instead of killing pytest): the swap is unavailable, so exactly the 11
    port-swap cells join the 17 always-mismatching ones, each of those 28
    carries the named limit, and the remaining 180 device-drawing cells plus
    ``TAPCELL`` still match without it."""
    import subprocess
    import sys

    if (_sha256(_RVT_GDS), _sha256(_RVT_CDL)) != (_RVT_GDS_SHA256, _RVT_CDL_SHA256):
        pytest.skip("pinned RVT assets do not match the recorded sha256 hashes")
    script_dir = tmp_path / "script"
    script_dir.mkdir()
    script = script_dir / "sweep.py"
    script.write_text(_ANCHORED_SWEEP_SCRIPT)
    out = tmp_path / "results.json"
    completed = subprocess.run(
        [sys.executable, "-I", str(script), str(_RVT_GDS), str(_RVT_CDL), str(out)],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    results = json.loads(out.read_text())

    assert len(results) == 209
    mismatching = {cell for cell, r in results.items() if r["status"] == "mismatch"}
    assert mismatching == _PINNED_CELLS
    assert len(_PINNED_CELLS) == 28
    assert all(results[cell]["known_limitation"] == [cell] for cell in _PINNED_CELLS)
    matching = {cell: r for cell, r in results.items() if cell not in _PINNED_CELLS}
    assert len(matching) == 209 - len(_PINNED_CELLS) == 181
    assert {r["status"] for r in matching.values()} == {"match"}, {
        cell: r for cell, r in matching.items() if r["status"] != "match"
    }
    assert all(r["known_limitation"] == [] for r in matching.values())
    assert len(matching) - len(_NO_DEVICE_CELLS) == 180
