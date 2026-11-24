"""`klt place-and-route` text `fmax_mhz` opposite-edge caveat (issue #7522).

`klt sta` qualifies every numeric `fmax_mhz` in `--format text` output with
the opposite-edge caveat (#7005). `klt place-and-route` derives the same
figure from the same OpenROAD `timing__fmax` metric, so its text output
carries the identical qualification (same wording, shared
`sta_cmd.FMAX_CAVEAT`). JSON output is unchanged.

Split out of `tests/test_place_and_route.py`, which is a governed oversized
file that may not grow (`scripts/check_file_size_baseline.py`).
"""

from __future__ import annotations

import pytest

from klayout_tools.cli import main
from test_place_and_route import (
    _setup_success_env,
    _stub_merge_def_to_gds,
    _stub_openroad_success,
)

# The engine stubs patch the shared `subprocess.run`; keep the build-identity
# Git probe on the real runner (same marker `test_place_and_route.py` carries).
pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_cli_text_full_run_qualifies_numeric_fmax(tmp_path, monkeypatch, capsys):
    """Issue #7522: a numeric top-level `fmax_mhz` carries the same
    opposite-edge qualification `klt sta` prints (#7005) -- same wording, so
    the two commands read alike. JSON stays unchanged."""
    request_path = _setup_success_env(tmp_path, monkeypatch)
    _stub_openroad_success(monkeypatch)
    _stub_merge_def_to_gds(monkeypatch)

    exit_code = main(["place-and-route", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert (
        "fmax_mhz: 304.11 (clock estimate excludes opposite-edge paths"
        " and can overstate achievable frequency)" in out
    )


def test_cli_text_null_fmax_stays_unqualified(tmp_path, monkeypatch, capsys):
    """Issue #7522: an unavailable `fmax_mhz` (floorplan-only run) keeps the
    bare `fmax_mhz: None` line -- the caveat qualifies a measurement, not a
    missing one."""
    request_path = _setup_success_env(
        tmp_path, monkeypatch, target_stage="floorplan", constraints=None, io=None
    )
    _stub_openroad_success(monkeypatch, stages=("floorplan",))

    exit_code = main(["place-and-route", request_path])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "fmax_mhz: None" in out
    assert "clock estimate excludes opposite-edge paths" not in out


def test_print_text_qualifies_zero_fmax(capsys):
    """Issue #7522: `0` is a supplied measurement, not a missing one, so it
    is qualified exactly like any other numeric `fmax_mhz` (mirroring
    `_print_slack_fields`' own `is not None` gating)."""
    from klayout_tools.cli import place_and_route_cmd

    report = {
        "engine": "openroad",
        "engine_version": "v",
        "hdl_toplevel": "gcd",
        "status": "ok",
        "stage_reached": "route",
        "seed": 1,
        "die_area_um2": 1.0,
        "core_area_um2": 1.0,
        "utilization_pct": 1.0,
        "wirelength_um": 1,
        "worst_slack_ns": 0.0,
        "total_negative_slack_ns": 0.0,
        "fmax_mhz": 0,
        "setup_violation_count": 0,
        "hold_violation_count": 0,
        "estimated_power_mw": 0.0,
        "clock_skew_ns": 0.0,
        "stages": [],
        "power": {
            "placed": {
                "status": "skipped",
                "evidence": "none",
                "tapcells": 0,
                "endcaps": 0,
                "fillers": 0,
                "special_nets": None,
            }
        },
        "def_path": None,
        "gds_path": None,
        "verilog_path": None,
    }

    place_and_route_cmd._print_text(report)

    out = capsys.readouterr().out
    assert (
        "fmax_mhz: 0 (clock estimate excludes opposite-edge paths"
        " and can overstate achievable frequency)" in out
    )
