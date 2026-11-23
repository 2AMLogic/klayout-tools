"""`klt pex` over an oscillator whose graded value is a `crossings`
reduction (issue #6913).

`klt pex` needed no code change for this -- the delta rows read each side's
measurement `value` -- so these tests pin the behaviour the issue asks for:
`schematic_value`/`extracted_value` carry the period (or frequency) both
sides' `crossings.value_from` selected, and a side with too few edges is an
error row blaming that side, distinguishable from a small value.

A new file (not `tests/test_pex.py`) because that file is ratcheted at its
current size; the fixtures and the faked-`run_sim` pattern are imported from
it, the same way `tests/test_sim_batch_runner_features.py` imports from
`tests/test_sim_batch.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from klayout_tools import pex as pex_module
from klayout_tools.pex import run_pex
from test_pex import _make_sky130_poly_resistor_layout, _write_gds, _write_testbench

_CORNER = "tt/1.800V/27C"

_PERIOD_OK = {
    "name": "f_ro1",
    "value": 2.0e-9,
    "unit": "Hz",
    "status": "pass",
    "margin": None,
    "crossings": {
        "times": [5e-10, 2.5e-9, 4.5e-9],
        "edges": ["rise", "rise", "rise"],
        "truncated": False,
        "reductions": {
            "period": {"n": 2, "mean": 2.0e-9, "std": 0.0, "min": 2.0e-9, "max": 2.0e-9}
        },
    },
}

_INSUFFICIENT = {
    "name": "f_ro1",
    "value": None,
    "unit": "Hz",
    "status": "error",
    "margin": None,
}


def _insufficient_diagnostic() -> dict:
    return {
        "severity": "error",
        "code": "measurement",
        "message": (
            "[crossings_insufficient_edges] measurement 'f_ro1': "
            "crossings.period needs at least 2 crossings (got 1) -- too few "
            "edges in the window to grade value_from 'period.mean'; widen "
            "the crossings.from/to window or drop value_from to grade the "
            "edge count"
        ),
    }


def _sim_report(measurements, diagnostics=()):
    return {
        "corners": [
            {
                "corner_id": _CORNER,
                "measurements": measurements,
                "diagnostics": list(diagnostics),
            }
        ],
        "corner_count": 1,
        "measurements": [{"name": "f_ro1"}],
    }


@pytest.fixture
def resistor_layout(tmp_path):
    return _write_gds(_make_sky130_poly_resistor_layout(), tmp_path / "res.gds")


def _write_oscillator_request(tmp_path: Path, testbench: Path) -> Path:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "netlist": str(testbench),
                "analysis": {"kind": "tran", "args": "1n 1u"},
                "measurements": [
                    {
                        "name": "f_ro1",
                        "unit": "Hz",
                        "limits": {"min": 1e-9, "max": 3e-9},
                        "crossings": {
                            "signal": "v(ro1)",
                            "level": 0.9,
                            "edge": "rise",
                            "from": "200n",
                            "value_from": "period.mean",
                        },
                    }
                ],
            }
        )
    )
    return path


def test_run_pex_deltas_an_oscillator_on_its_period(
    tmp_path, resistor_layout, monkeypatch
):
    """The recipe from the issue: a ring-oscillator testbench whose crossings
    measurement declares `value_from` with the start-up exclusion in
    `crossings.from`; the delta row reports schematic-vs-extracted periods
    (a real frequency shift), not a quantised edge count."""
    calls: list[dict] = []

    def _fake_run_sim(request_path, **_kwargs):
        document = json.loads(Path(request_path).read_text())
        calls.append(document)
        # Schematic side: 2.0 ns period; extracted side: 2.2 ns (+10 %).
        value = 2.0e-9 if len(calls) == 1 else 2.2e-9
        measurement = {**_PERIOD_OK, "value": value}
        return _sim_report([measurement])

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = tmp_path / "dut.spice"
    dut.write_text(".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_oscillator_request(tmp_path, tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    # Both legs saw the same crossings declaration, start-up window included.
    for document in calls:
        (entry,) = document["measurements"]
        assert entry["crossings"]["value_from"] == "period.mean"
        assert entry["crossings"]["from"] == "200n"

    (row,) = report["delta"]
    assert row["measurement"] == "f_ro1"
    assert row["schematic_value"] == pytest.approx(2.0e-9)
    assert row["extracted_value"] == pytest.approx(2.2e-9)
    assert row["delta_pct"] == pytest.approx(10.0)
    assert row["status"] == "pass"
    assert row["schematic_status"] == "pass"
    assert row["extracted_status"] == "pass"


def test_run_pex_reports_insufficient_edges_as_an_error_row(
    tmp_path, resistor_layout, monkeypatch
):
    """A non-oscillating extracted cell (1 edge) is an error row blaming the
    extracted side with the `[crossings_insufficient_edges]` message --
    `delta_pct: null`, never a small value a `limits.min` could pass."""
    calls = {"n": 0}

    def _fake_run_sim(*_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # schematic side: plenty of edges
            return _sim_report([_PERIOD_OK])
        return _sim_report([_INSUFFICIENT], [_insufficient_diagnostic()])

    monkeypatch.setattr(pex_module, "run_sim", _fake_run_sim)

    dut = tmp_path / "dut.spice"
    dut.write_text(".SUBCKT RES RA RB\nR1 RA RB 289.2\n.ENDS RES\n")
    tb = _write_testbench(tmp_path / "testbench.spice", dut)
    request = _write_oscillator_request(tmp_path, tb)

    report = run_pex(resistor_layout, [str(request)], "sky130")

    (row,) = report["delta"]
    assert row["status"] == "error"
    assert row["delta_pct"] is None
    assert row["extracted_value"] is None
    assert row["extracted_status"] == "error"
    assert row["schematic_status"] == "pass"
    # The blame names the side and carries the stable reason token, so the
    # row is distinguishable from a small graded value.
    assert row["error"]["side"] == "extracted"
    assert row["error"]["message"].startswith("[crossings_insufficient_edges]")
    assert row["error"]["code"] == "measurement"
    assert report["status"] == "error"
