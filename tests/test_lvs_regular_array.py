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
