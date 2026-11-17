"""Live (real ngspice) check of ``options.save_vectors`` augmentation for the
structured measurement forms (issue #5559): the issue's Reproduce request
(``cycle_mean`` + ``energy``) must pass under ``save_vectors`` and match the
no-``save_vectors`` values.  Single corner, local backend.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim

pytestmark = pytest.mark.skipif(
    shutil.which("ngspice") is None, reason="ngspice is not installed"
)

_BODY = (
    "Vdd vdd 0 DC 1.8\n"
    "Vclk clk 0 PULSE(0 1.8 0 1n 1n 99n 200n)\n"
    "R1 vdd p 10k\n"
    "C1 p 0 2p\n"
    "R2 p out 10k\n"
    "C2 out 0 2p\n"
)

_REQUEST = {
    "netlist": "body.spice",
    "backend": "local",
    "analysis": {"kind": "tran", "args": "1n 1u"},
    "measurements": [
        {
            "name": "p_avg",
            "unit": "W",
            "cycle_mean": {
                "signal": "v(p)",
                "align": "v(clk)-0.5*v(vdd)",
                "level": 0.0,
                "edge": "rise",
                "from": 0,
                "cycles": 4,
            },
        },
        {
            "name": "e_vdd",
            "unit": "J",
            "energy": {"source": "vdd", "voltage": "v(vdd)"},
        },
    ],
}


def _run(tmp_path: Path, name: str, options: dict) -> dict:
    (tmp_path / "body.spice").write_text(_BODY)
    request = tmp_path / f"{name}.json"
    request.write_text(json.dumps({**_REQUEST, "options": options}))
    report = sim.run_sim(
        str(request), artifacts_dir=str(tmp_path / name), backend="local"
    )
    assert isinstance(report, dict)
    return report["corners"][0]


def test_reproduce_request_matches_run_without_save_vectors(tmp_path):
    plain = _run(tmp_path, "plain", {})
    limited = _run(tmp_path, "limited", {"save_vectors": ["v(out)"]})

    assert [m["status"] for m in plain["measurements"]] == ["pass", "pass"]
    assert [m["status"] for m in limited["measurements"]] == ["pass", "pass"]
    for a, b in zip(plain["measurements"], limited["measurements"], strict=True):
        assert a["value"] == b["value"]


def test_stress_under_save_vectors_needs_only_the_named_nodes(tmp_path):
    request = {
        **_REQUEST,
        "measurements": [{"name": "s", "stress": {"a": "p", "b": "out"}}],
        "options": {"save_vectors": ["v(out)"]},
    }
    (tmp_path / "body.spice").write_text(_BODY)
    path = tmp_path / "stress.json"
    path.write_text(json.dumps(request))
    corner = sim.run_sim(
        str(path), artifacts_dir=str(tmp_path / "stress"), backend="local"
    )["corners"][0]
    assert [m["status"] for m in corner["measurements"]] == ["pass"]
