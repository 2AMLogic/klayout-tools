"""Shared bench builders for the `--regrade` tests (issue #6778).

A two-supply-corner request whose 1.62 V point "dips" on the first pass,
a stubbed ngspice that can recover or stay broken under the tightened
deck, and the first-pass/report plumbing both test modules use.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from helpers.subprocess_fakes import fake_completed
from klayout_tools import sim

TIGHTENED = {"tmax": "25p", "reltol": 1e-4}
TIGHTENED_ARGS = "1n 1u 0 25p"


def _request(
    tmp_path: Path,
    *,
    options: dict | None = None,
    corners: dict | None = None,
) -> Path:
    (tmp_path / "body.spice").write_text(
        "Vdd vdd 0 DC 1.8\nR1 vdd out 1k\nC1 out 0 1n\n"
    )
    request: dict[str, Any] = {
        "netlist": "body.spice",
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "corners": corners or {"supply_v": {"vdd": [1.8, 1.62]}},
        "measurements": [
            {"name": "vout", "expr": "v(out)", "unit": "V", "limits": {"min": 1.0}}
        ],
        "options": {
            "keep_artifacts": True,
            "tightened_rerun": TIGHTENED,
            **(options or {}),
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request))
    return path


def _stub_engine(
    monkeypatch,
    *,
    first_pass_dip: bool = True,
    tightened_value: float | None = 1.5,
    tightened_error: str | None = None,
    decks: list[str] | None = None,
    runs: list[tuple[float, bool]] | None = None,
) -> None:
    """Fake ngspice: the 1.62 V corner prints a dipped value on the first
    pass; a tightened deck (the tmax token) prints ``tightened_value``, an
    error log, or times out."""

    def fake_run(cmd, capture_output, text, timeout, cwd=None, env=None, **kw):
        if "--version" in cmd:  # the engine_build probe
            return fake_completed("** ngspice-99\n")
        deck = Path(cmd[2]).read_text()
        if decks is not None:
            decks.append(deck)
        log = Path(cmd[cmd.index("-o") + 1])
        match = re.search(r"alter vdd=([0-9.]+)", deck)
        vdd = float(match.group(1)) if match else 1.8
        tightened = TIGHTENED_ARGS in deck
        if runs is not None:
            runs.append((vdd, tightened))
        if tightened:
            if tightened_error == "timeout":
                raise subprocess.TimeoutExpired(cmd, timeout)
            if tightened_error is not None:
                log.write_text(f"{tightened_error}\n")
                return fake_completed("** ngspice-99\n")
            value = tightened_value
        else:
            value = 0.9 if (first_pass_dip and vdd < 1.7) else 1.5
        log.write_text(f"No. of Data Rows : 1\nvout = {value:.6e}\n")
        return fake_completed("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def _first_pass(tmp_path: Path, request: Path) -> Path:
    """Run the first pass locally and commit its report beside the request."""
    report = sim.run_sim(str(request), backend="local")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path


def _failing_id(report_path: Path) -> str:
    report = json.loads(report_path.read_text())
    return next(c["corner_id"] for c in report["corners"] if c["status"] == "fail")
