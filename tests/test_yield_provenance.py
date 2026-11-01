"""Tests for the run-level ``provenance`` block and per-measurement
``spec_row`` echoed by ``klt yield`` / ``klt yield-samples`` (issue #2840).

Split out of ``test_yield.py`` to keep that file under its size ceiling.
"""

from __future__ import annotations

import importlib.util
import json
import math

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import YieldError, derive_sample_set, run_yield

requires_native = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason="klt_yield_native is not built (see docs/cli/yield.md)",
)


def _norm_ppf(p: float) -> float:
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def _normal_grid(n, mu, sigma):
    return [mu + sigma * _norm_ppf((i - 0.5) / n) for i in range(1, n + 1)]


def _sim_report(tmp_path, values, limits=None):
    corners = [
        {
            "corner_id": f"tt/1.800V/27C/mc{i}",
            "status": "pass",
            "measurements": [{"name": "vref", "value": v, "unit": "V"}],
            "monte_carlo": {"sample_index": i, "seed": 1000 + i},
        }
        for i, v in enumerate(values)
    ]
    report = {
        "schema_version": 1,
        "netlist": "tb.spice",
        "status": "pass",
        "environment": {"monte_carlo": {"n": len(values), "seed": 1000}},
        "measurements": [{"name": "vref", "unit": "V", "limits": limits or {}}],
        "corners": corners,
    }
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(report))
    return str(path)


def _derivation_inputs(tmp_path):
    """Nominal + control `klt sim` MC reports with a seed, derivable as-is."""

    def doc(offset, seed):
        draws = [
            {
                "corner_id": f"tt/1.800V/27C/mc{i}",
                "status": "pass",
                "measurements": [
                    {"name": "iq", "value": offset + 0.01 * i, "unit": "uA"}
                ],
                "monte_carlo": {"sample_index": i, "seed": seed + i},
            }
            for i in range(200)
        ]
        return {
            "schema_version": 1,
            "netlist": "tb.spice",
            "status": "pass",
            "environment": {"monte_carlo": {"n": 200, "seed": seed}},
            "measurements": [{"name": "iq", "unit": "uA", "limits": {"max": 1000.0}}],
            "corners": draws,
        }

    nominal = tmp_path / "nominal.json"
    control = tmp_path / "control.json"
    nominal.write_text(json.dumps(doc(5.0, 1000)))
    control.write_text(json.dumps(doc(15.0, 2000)))
    return str(nominal), str(control)


# --------------------------------------------------------------------------- #
# Run-level `provenance` block and per-measurement `spec_row` (issue #2840)
# --------------------------------------------------------------------------- #

_PROVENANCE = {
    "seed": 0,
    "rng": "ngspice-sobol",
    "draws_per_population": 300,
    "common_random_numbers": True,
    "corner_matrix": ["tt", "ss", "ff"],
}


def _prov_doc(tmp_path, provenance=None, spec_row=None, name="doc.json"):
    entry = {
        "name": "m",
        "unit": "V",
        "samples": _normal_grid(300, 1.2, 0.01),
        "limits": {"min": 1.1, "max": 1.3},
    }
    if spec_row is not None:
        entry["spec_row"] = spec_row
    doc = {"measurements": [entry]}
    if provenance is not None:
        doc["provenance"] = provenance
    path = tmp_path / name
    path.write_text(json.dumps(doc))
    return str(path)


@requires_native
def test_provenance_block_and_spec_row_are_echoed_verbatim(tmp_path):
    report = run_yield(_prov_doc(tmp_path, _PROVENANCE, spec_row="VREF-3sigma-tol"))
    assert report["source"]["provenance"] == _PROVENANCE
    assert report["measurements"][0]["spec_row"] == "VREF-3sigma-tol"


@requires_native
def test_absent_provenance_and_spec_row_read_as_null(tmp_path):
    report = run_yield(_prov_doc(tmp_path))
    assert report["source"]["provenance"] is None
    assert report["measurements"][0]["spec_row"] is None


@requires_native
def test_empty_corner_matrix_is_accepted(tmp_path):
    prov = {"seed": 0, "corner_matrix": []}
    assert run_yield(_prov_doc(tmp_path, prov))["source"]["provenance"] == prov


@requires_native
def test_spec_row_in_limits_file_wins_and_is_echoed(tmp_path):
    limits = tmp_path / "limits.json"
    limits.write_text(
        json.dumps({"measurements": {"m": {"min": 1.0, "spec_row": "ROW-B"}}})
    )
    report = run_yield(_prov_doc(tmp_path, spec_row="ROW-A"), limits_path=str(limits))
    assert report["measurements"][0]["spec_row"] == "ROW-B"


@requires_native
def test_spec_row_on_a_skipped_measurement_is_simply_not_reported(tmp_path):
    # A measurement with no resolvable limits is skipped with a warning and
    # never reaches the report -- its spec_row goes nowhere, and the run
    # still succeeds for the measurements that do have limits (issue #2840).
    doc = {
        "measurements": [
            {
                "name": "unlimited",
                "unit": "V",
                "samples": _normal_grid(300, 1.2, 0.01),
                "spec_row": "ROW-UNREACHABLE",
            },
            {
                "name": "m",
                "unit": "V",
                "samples": _normal_grid(300, 1.2, 0.01),
                "limits": {"min": 1.1, "max": 1.3},
                "spec_row": "ROW-M",
            },
        ]
    }
    path = tmp_path / "doc.json"
    path.write_text(json.dumps(doc))
    report = run_yield(str(path))
    assert [m["name"] for m in report["measurements"]] == ["m"]
    assert report["measurements"][0]["spec_row"] == "ROW-M"
    assert any("unlimited" in w for w in report["warnings"])


@requires_native
def test_sim_report_monte_carlo_is_not_double_written(tmp_path):
    report = run_yield(
        _sim_report(
            tmp_path, _normal_grid(300, 1.2, 0.01), limits={"min": 1.1, "max": 1.3}
        )
    )
    assert report["source"]["monte_carlo"] == {"n": 300, "seed": 1000}
    assert report["source"]["provenance"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"seed": "abc"},
        {"seed": True},
        {"corner_matrix": "tt"},
        {"corner_matrix": ["tt", 1]},
        {"draws_per_population": 0},
        {"common_random_numbers": "yes"},
        {"rng": 5},
        "seed=1",
    ],
)
def test_malformed_provenance_is_a_yield_error(tmp_path, bad):
    with pytest.raises(YieldError):
        run_yield(_prov_doc(tmp_path, bad))


def test_malformed_provenance_exits_1_with_json_envelope(tmp_path, capsys):
    path = _prov_doc(tmp_path, {"seed": "abc"})
    assert main(["yield", path, "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "yield"
    assert "provenance.seed must be an integer" in error["error"]["message"]


def test_non_string_spec_row_is_a_yield_error(tmp_path):
    with pytest.raises(YieldError):
        run_yield(_prov_doc(tmp_path, spec_row=7))


def test_yield_samples_carries_the_nominal_monte_carlo_seed(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    mc = json.loads(open(nominal).read()).get("environment", {}).get("monte_carlo")
    doc = derive_sample_set(nominal, control)
    if isinstance(mc, dict) and isinstance(mc.get("seed"), int):
        assert doc["provenance"]["seed"] == mc["seed"]
    else:
        assert "provenance" not in doc


def test_yield_samples_provenance_round_trips_through_yield(tmp_path):
    nominal, control = _derivation_inputs(tmp_path)
    data = json.loads(open(nominal).read())
    data.setdefault("environment", {})["monte_carlo"] = {"n": 195, "seed": 42}
    open(nominal, "w").write(json.dumps(data))
    doc = derive_sample_set(nominal, control)
    assert doc["provenance"] == {"seed": 42, "draws_per_population": 195}
    path = tmp_path / "derived.json"
    path.write_text(json.dumps(doc))
    from klayout_tools.yield_analysis import _read_samples

    assert _read_samples(str(path))[2]["provenance"] == doc["provenance"]
