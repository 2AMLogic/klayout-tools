"""Dry-run warnings for top-level request keys (issues #3064, #5130)."""

from __future__ import annotations

import json
from pathlib import Path

from klayout_tools import sim


def _dry_run_request(tmp_path: Path, **extra) -> Path:
    (tmp_path / "body.spice").write_text(
        ".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": "body.spice",
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "monte_carlo": {"n": 3, "seed": 7, "vary": "both"},
                **extra,
            }
        )
    )
    return path


def test_run_sim_dry_run_does_not_warn_on_stimulus_key(tmp_path):
    stimulus = {
        "signals": [
            {
                "source": "vclk",
                "node": "clk",
                "events": [{"t_s": 1e-9, "level": 1.8}],
            }
        ]
    }
    report = sim.run_sim(
        str(_dry_run_request(tmp_path, stimulus=stimulus)), dry_run=True
    )
    assert not any("stimulus" in w for w in report["warnings"])
    report = sim.run_sim(
        str(_dry_run_request(tmp_path, stimulus_=stimulus)), dry_run=True
    )
    assert any("stimulus_" in w for w in report["warnings"])
