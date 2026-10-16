"""Pin ``.meas dc ... FIND <expr> AT=<end point>`` behavior (issue #2964).

ngspice interpolates ``AT=`` inside the swept axis. When the sweep step is not
exactly representable in binary (``0.1``), the accumulated last sample can fall
short of the literal end point, so ``AT=<hi>`` is reported as ``out of
interval`` while ``AT=<lo>`` and interior points still resolve. A binary-exact
step (``0.25``) resolves both end points. Runs one real single-corner
``ngspice -b`` per test and skips when ngspice is not installed.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim

pytestmark = pytest.mark.usefixtures("real_build_identity_git")

_SKIP_NO_NGSPICE = pytest.mark.skipif(
    shutil.which("ngspice") is None or os.environ.get("KLT_SKIP_NGSPICE_TESTS") == "1",
    reason="ngspice is not installed on this machine",
)


def _run(tmp_path: Path, step: str) -> dict:
    (tmp_path / "body.spice").write_text("V1 in 0 0\nR1 in out 1k\nR2 out 0 1k\n")
    request = {
        "netlist": "body.spice",
        "analysis": {"kind": "dc", "args": f"V1 0 1 {step}"},
        "measurements": [
            {"name": "lo", "spice": ".meas dc lo FIND v(out) AT=0"},
            {"name": "mid", "spice": ".meas dc mid FIND v(out) AT=0.5"},
            {"name": "hi", "spice": ".meas dc hi FIND v(out) AT=1"},
        ],
        "options": {"keep_artifacts": False},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    (corner,) = sim.run_sim(str(path))["corners"]
    return corner


@_SKIP_NO_NGSPICE
def test_binary_exact_step_resolves_both_end_points(tmp_path):
    corner = _run(tmp_path, "0.25")
    values = {m["name"]: m["value"] for m in corner["measurements"]}
    assert values == pytest.approx({"lo": 0.0, "mid": 0.25, "hi": 0.5})
    assert corner["diagnostics"] == []


@_SKIP_NO_NGSPICE
def test_non_binary_step_loses_the_high_end_point(tmp_path):
    corner = _run(tmp_path, "0.1")
    by_name = {m["name"]: m for m in corner["measurements"]}
    assert by_name["lo"]["value"] == pytest.approx(0.0)
    assert by_name["mid"]["value"] == pytest.approx(0.25)
    assert by_name["hi"]["value"] is None
    assert by_name["hi"]["status"] == "error"
    (diag,) = corner["diagnostics"]
    assert diag["code"] == "measurement"
    assert "'hi'" in diag["message"]
    assert "out of interval" in diag["message"]
