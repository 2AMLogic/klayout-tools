"""Real, unmocked end-to-end tests for `klt techmap`'s `verify_equivalence`
acceptance gate (issue #875, Phase 2c of Epic #704) -- wires `klt equiv`
(#726) in as `native/techmap`'s (#874) own correctness gate: a mapped
netlist is not considered done until `klt equiv` reports it `"equivalent"`
to the **pre-mapping generic netlist** it was mapped from, mirroring Phase
1b's own `klt synthesize --verify-equivalence` gate one stage later in the
pipeline (`tests/test_synthesize_equiv_gate.py`'s own precedent/structure).

Two tiers:

- **Happy path**: a real, small combinational generic netlist ($and2) is
  mapped by the real, compiled `klt-techmap` binary (issue #874's actual
  Liberty-driven cell-selection algorithm, never stubbed) against a real
  (minimal, hand-authored) Liberty library, with `verify_equivalence=True`
  -- the gate runs `klt equiv` for real (real Yosys SAT proof, real
  `iverilog`/`vvp` counterexample-confirmation path) and reports
  `"equivalent"`.
- **Seeded mismatch**: mirrors `test_synthesize_equiv_gate.py`'s own
  "genuinely-broken artifact, never hand-edited gate-level Verilog"
  technique -- a second, independently mapped design (the same ports, but
  `$or2` instead of `$and2` in its own generic netlist -- "a cell
  substituted for one with different logic function", per the issue's own
  guidance) is really mapped by the same real binary against the same real
  Liberty, producing a real, structurally-valid but functionally different
  mapped netlist (an `OR2` instance instead of an `AND2` one); that
  netlist is substituted in place of the good design's own correctly-
  mapped netlist (simulating a technology-mapping stage that produced
  output diverging from the generic netlist it declares as its source),
  and `run_techmap`'s `verify_equivalence=True` gate is proven to catch
  it -- a hard `TechmapError`, not a silent warning or a passing "happy
  path" result.

Both tests build the real `klt-techmap` binary on demand (`cargo build`,
debug profile -- functional correctness only, no need for release/lto) and
skip (not fail) on a machine lacking `cargo`, `yosys`, or `iverilog`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import techmap
from klayout_tools.techmap import TechmapError, run_techmap

HAVE_CARGO = shutil.which("cargo") is not None
HAVE_YOSYS = shutil.which("yosys") is not None
HAVE_IVERILOG = shutil.which("iverilog") is not None

pytestmark = [
    pytest.mark.skipif(not HAVE_CARGO, reason="cargo is not installed on this machine"),
    pytest.mark.skipif(not HAVE_YOSYS, reason="yosys is not installed on this machine"),
    pytest.mark.skipif(
        not HAVE_IVERILOG, reason="iverilog is not installed on this machine"
    ),
]

# A tiny, hand-authored Liberty library -- exactly the grammar
# `native/techmap/src/celllib.rs`'s own unit tests already use (`MINI_LIB`),
# duplicated here rather than imported (this repo's own convention: test
# fixtures are not shared across the Python/Rust language boundary, and not
# across unrelated test modules either -- `test_synthesize_equiv_gate.py`
# duplicates its own adder fixture rather than importing it). `AND2`/`OR2`
# are both real, truth-table-classified `$and2`/`$or2` realisations -- not a
# hand-edited/hand-broken cell.
_MINI_LIB = """\
library ("test_lib") {
    time_unit : "1ns";
    capacitive_load_unit(1.0, "pf");

    cell ("AND2") {
        area : 6.0;
        pin ("A") { direction : "input"; }
        pin ("B") { direction : "input"; }
        pin ("X") { direction : "output"; function : "(A&B)"; }
    }
    cell ("OR2") {
        area : 6.0;
        pin ("A") { direction : "input"; }
        pin ("B") { direction : "input"; }
        pin ("X") { direction : "output"; function : "(A|B)"; }
    }
}
"""

_GOOD_GENERIC_NETLIST = {
    "schema": "klt.synth.generic-netlist/1",
    "top": "g",
    "ports": [
        {"name": "a", "direction": "input", "bits": ["a"]},
        {"name": "b", "direction": "input", "bits": ["b"]},
        {"name": "y", "direction": "output", "bits": ["y"]},
    ],
    "cells": [
        {"name": "g0", "type": "$and2", "connections": {"A": "a", "B": "b", "Y": "y"}}
    ],
}

# "Seeded-broken": the same ports, but `$or2` instead of `$and2` -- a real
# "wrong gate function" bug class ("a cell substituted for one with
# different logic function", per the issue's own guidance), not a
# synthetic/arbitrary corruption.
_BROKEN_GENERIC_NETLIST = {
    **_GOOD_GENERIC_NETLIST,
    "cells": [
        {"name": "g0", "type": "$or2", "connections": {"A": "a", "B": "b", "Y": "y"}}
    ],
}


def _techmap_binary() -> str:
    """Build (debug profile -- functional correctness only, no lto) and
    return the real `klt-techmap` binary path, mirroring
    `tests/corpus/techmap/compare.py`'s own `_ensure_binary` convention
    (kept independent here rather than imported -- that module is a
    standalone benchmark script, not a shared test fixture)."""
    binary = Path(techmap._TECHMAP_CRATE_DIR) / "target" / "debug" / "klt-techmap"
    if not binary.is_file():
        subprocess.run(["cargo", "build"], cwd=techmap._TECHMAP_CRATE_DIR, check=True)
    assert binary.is_file(), f"cargo build did not produce {binary}"
    return str(binary)


def _write_design(design_dir: Path, netlist: dict) -> str:
    design_dir.mkdir(parents=True, exist_ok=True)
    (design_dir / "minilib.lib").write_text(_MINI_LIB, encoding="utf-8")
    (design_dir / "g_generic.json").write_text(json.dumps(netlist), encoding="utf-8")
    request = {
        "schema": "klt.synth.techmap.request/1",
        "generic_netlist": "g_generic.json",
        "liberty": "minilib.lib",
        "constraints": {"clock_period_ns": None},
    }
    request_path = design_dir / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    return str(request_path)


def test_verify_equivalence_gate_passes_on_real_mapping(tmp_path):
    """AC #1: a technology-mapped netlist is checked against `klt equiv`
    for equivalence to its pre-mapping generic netlist before being
    accepted -- demonstrated end-to-end on a real, correctly-mapped design:
    `klt equiv` reports `"equivalent"`."""
    _techmap_binary()  # ensure built before run_techmap resolves it

    request_path = _write_design(tmp_path / "good", _GOOD_GENERIC_NETLIST)

    report = run_techmap(request_path, verify_equivalence=True)

    assert report["status"] == "ok"
    assert report["instance_counts_by_type"] == {"AND2": 1}
    assert report["equivalence"]["status"] == "equivalent"
    assert report["equivalence"]["engine"] == "yosys"
    assert report["equivalence"]["artifacts"]["script_path"]


def test_verify_equivalence_gate_fails_hard_on_seeded_mismatch(tmp_path, monkeypatch):
    """AC #2: a deliberately-broken mapping (seeded mismatch) is caught by
    the gate -- proven, not just assumed, via a real broken artifact (a
    real `klt-techmap` run against a genuinely different generic netlist,
    `$or2` instead of `$and2`) substituted for the real correct one, never
    a hand-edited/hand-broken netlist."""
    _techmap_binary()

    # Real mapping of the broken ("$or2 instead of $and2") generic netlist,
    # independent of the good design below -- a real, structurally-valid,
    # functionally different mapped netlist (a real OR2 instance).
    bad_request_path = _write_design(tmp_path / "bad", _BROKEN_GENERIC_NETLIST)
    bad_report = run_techmap(bad_request_path)
    assert bad_report["status"] == "ok"
    assert bad_report["instance_counts_by_type"] == {"OR2": 1}
    with open(bad_report["mapped_netlist_path"], encoding="utf-8") as handle:
        broken_mapped_text = handle.read()

    # Seed the mismatch: after the *good* design's own real `klt-techmap`
    # run succeeds (produces the correct AND2 mapping for the $and2 generic
    # netlist), overwrite the mapped-netlist file the gate is about to
    # check with the broken design's own real mapped gates -- `run_techmap`'s
    # `verify_equivalence` gate must catch that the file on disk no longer
    # matches the generic netlist it declares as its source.
    good_request_path = _write_design(tmp_path / "good", _GOOD_GENERIC_NETLIST)

    original_subprocess_run = subprocess.run

    def _seed_mismatch(args, **kwargs):
        completed = original_subprocess_run(args, **kwargs)
        # `subprocess` is a shared module singleton -- this monkeypatch
        # would otherwise also intercept `klt equiv`'s own `yosys -s`
        # subprocess call a few lines later in `run_techmap`'s own gate.
        # Only the `klt-techmap` invocation itself is seeded.
        if completed.returncode == 0 and str(args[0]).endswith("klt-techmap"):
            response = json.loads(completed.stdout)
            with open(response["mapped_netlist_path"], "w", encoding="utf-8") as handle:
                handle.write(broken_mapped_text)
        return completed

    monkeypatch.setattr(techmap.subprocess, "run", _seed_mismatch)

    with pytest.raises(TechmapError, match="NOT equivalent") as exc_info:
        run_techmap(good_request_path, verify_equivalence=True)
    assert "y" in str(exc_info.value)
