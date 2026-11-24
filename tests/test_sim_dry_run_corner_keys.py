"""Dry-run warnings for unknown keys under ``corners`` (issue #5060)."""

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


def test_dry_run_warns_on_misplaced_exclude_under_corners(tmp_path):
    request = _dry_run_request(
        tmp_path,
        corners={
            "temperature_c": [27, 85],
            "exclude": [{"temperature_c": 85}],
            "zzz": 1,
        },
        monte_carlo={"n": 1, "seed": 7, "vary": "both"},
    )
    report = sim.run_sim(str(request), dry_run=True)
    assert report["status"] == "valid"
    assert report["unit_count"] == 2
    assert len(report["warnings"]) == 2
    assert "'exclude' under corners" in report["warnings"][0]
    assert "top level" in report["warnings"][0]
    assert "'zzz' under corners" in report["warnings"][1]


def test_dry_run_known_corner_keys_do_not_warn(tmp_path):
    request = _dry_run_request(tmp_path, corners={"temperature_c": [27]})
    assert sim.run_sim(str(request), dry_run=True)["warnings"] == []
