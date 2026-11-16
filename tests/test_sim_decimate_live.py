"""Live (real ngspice) check of ``options.waveform_decimate`` (issue #5114).

Unit tests for the stride logic live in ``test_sim_save_vectors.py``; this
runs a tiny RC ``tran`` twice (with and without the option) and asserts the
decimation thins only ``waveform.raw.json``.  Single corner, local backend.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim

pytestmark = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice is not installed"
)

_STRIDE = 10


def _run(tmp_path: Path, name: str, options: dict) -> dict:
    (tmp_path / "rc.spice").write_text(
        "Vin in 0 PULSE(0 1 0 1n 1n 1 2)\nR1 in out 1k\nC1 out 0 1n\n"
    )
    request = tmp_path / f"{name}.json"
    request.write_text(
        json.dumps(
            {
                "netlist": "rc.spice",
                "backend": "local",
                "analysis": {"kind": "tran", "args": "10n 10u"},
                "measurements": [
                    {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=5u"}
                ],
                "options": {"waveforms": True, "keep_artifacts": True, **options},
            }
        )
    )
    report = sim.run_sim(
        str(request), artifacts_dir=str(tmp_path / name), backend="local"
    )
    assert isinstance(report, dict)
    return report


def _corner(report: dict) -> dict:
    assert len(report["corners"]) == 1
    return report["corners"][0]


def test_waveform_decimate_thins_only_waveform_json(tmp_path):
    full = _corner(_run(tmp_path, "full", {}))
    dec = _corner(_run(tmp_path, "dec", {"waveform_decimate": _STRIDE}))

    n_full = len(json.loads(Path(full["artifacts"]["waveform"]).read_text())["points"])
    n_dec = len(json.loads(Path(dec["artifacts"]["waveform"]).read_text())["points"])
    assert n_full > 10 * _STRIDE
    assert n_dec <= n_full // _STRIDE + 2
    assert n_dec < n_full

    assert dec["waveform_decimate"] == _STRIDE
    assert "waveform_decimate" not in full

    assert dec["measurements"] == full["measurements"]
    assert os.path.getsize(dec["artifacts"]["raw"]) == os.path.getsize(
        full["artifacts"]["raw"]
    )
