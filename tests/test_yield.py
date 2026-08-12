"""Tests for `klt yield` and the `run_yield` library function (issue #816).

Two tiers, deliberately split:

- The **input-reader** tier (`klt sim` report / sample-set document parsing,
  limits merging, every error the CLI can raise before any statistics run)
  needs no Rust and always runs.
- The **statistics** tier needs the `klt_yield_native` extension to be built
  and importable (`maturin develop --release` in `native/yield/`, or
  `uv sync --group yield`); those tests are skipped with a clear reason --
  never silently passed -- when it is not, so an environment without a Rust
  toolchain degrades gracefully. CI's `native-yield` job is what makes sure
  that skip is never the only thing the pipeline ever sees.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys

import pytest

from klayout_tools.cli import main
from klayout_tools.yield_analysis import (
    YieldError,
    _measurements_from_sim_report,
    _read_samples,
    run_yield,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(REPO_ROOT, "examples", "yield")

requires_native = pytest.mark.skipif(
    importlib.util.find_spec("klt_yield_native") is None,
    reason=(
        "klt_yield_native is not built -- run `maturin develop --release` in "
        "native/yield/ (or `uv sync --group yield`); see "
        "docs/cli/yield.md#building-the-native-extension"
    ),
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _sample_set(tmp_path, samples, limits=None, name="m"):
    entry = {"name": name, "unit": "V", "samples": samples}
    if limits is not None:
        entry["limits"] = limits
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def _sim_report(tmp_path, values, limits=None, corner="tt/1.800V/27C"):
    """A minimal `klt sim` Monte Carlo report over one measurement."""
    corners = [
        {
            "corner_id": f"{corner}/mc{i}",
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


def _normal_grid(n: int, mu: float, sigma: float) -> list[float]:
    """`n` inverse-CDF-spaced draws from `N(mu, sigma)` -- deterministic, and
    as close to the analytic distribution as a finite sample gets. Mirrors
    the Rust test helper of the same name."""
    return [mu + sigma * _norm_ppf((i - 0.5) / n) for i in range(1, n + 1)]


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF by bisection on `math.erfc` -- slow but
    exact enough, and independent of the crate's own implementation (which
    is what these tests are checking)."""
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if 0.5 * math.erfc(-mid / math.sqrt(2.0)) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------- #
# Input readers (no native extension needed)
# --------------------------------------------------------------------------- #


def test_sim_report_is_read_without_an_intermediate_format(tmp_path):
    path = _sim_report(tmp_path, [1.0, 2.0, 3.0], limits={"min": 0.5, "max": 3.5})
    kind, measurements, source = _read_samples(path)
    assert kind == "sim-report"
    assert source["netlist"] == "tb.spice"
    assert source["monte_carlo"]["n"] == 3
    assert measurements[0]["samples"] == [1.0, 2.0, 3.0]
    assert measurements[0]["limits"] == {"min": 0.5, "max": 3.5}
    # The `/mc<i>` suffix is stripped, so the originating corner is named once.
    assert measurements[0]["source_corners"] == ["tt/1.800V/27C"]


def test_sim_report_counts_null_measurements_as_errored(tmp_path):
    path = _sim_report(tmp_path, [1.0, None, 3.0], limits={"min": 0.5, "max": 3.5})
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["errored"] == 1


def test_sim_report_ignores_non_monte_carlo_corners(tmp_path):
    """A report mixing a PVT matrix with a Monte Carlo request has both; the
    deterministic corners are not draws from any distribution."""
    path = _sim_report(tmp_path, [1.0, 2.0], limits={"min": 0.5, "max": 3.5})
    report = json.loads(open(path).read())
    report["corners"].append(
        {
            "corner_id": "ss/1.620V/125C",
            "status": "pass",
            "measurements": [{"name": "vref", "value": 99.0, "unit": "V"}],
            "monte_carlo": None,
        }
    )
    (tmp_path / "sim.json").write_text(json.dumps(report))
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["samples"] == [1.0, 2.0]


def test_sim_report_without_monte_carlo_samples_is_an_error(tmp_path):
    path = tmp_path / "sim.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [{"name": "vref", "limits": {"max": 1.0}}],
                "corners": [{"corner_id": "tt", "monte_carlo": None}],
            }
        )
    )
    with pytest.raises(YieldError, match="no Monte Carlo samples"):
        _read_samples(str(path))


def test_sample_set_document_is_read(tmp_path):
    path = _sample_set(tmp_path, [1.0, None, 3.0], limits={"max": 4.0})
    kind, measurements, _source = _read_samples(path)
    assert kind == "sample-set"
    assert measurements[0]["samples"] == [1.0, 3.0]
    assert measurements[0]["errored"] == 1
    assert measurements[0]["limits"] == {"max": 4.0}


def test_unrecognised_document_is_an_error(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text(json.dumps({"hello": "world"}))
    with pytest.raises(YieldError, match="neither a `klt sim` report"):
        _read_samples(str(path))


def test_missing_file_is_a_clean_error(tmp_path):
    with pytest.raises(YieldError, match="samples file not found"):
        run_yield(str(tmp_path / "nope.json"))


def test_malformed_json_is_a_clean_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    with pytest.raises(YieldError, match="could not read samples"):
        run_yield(str(path))


@requires_native
def test_a_measurement_without_limits_is_skipped_with_a_warning(tmp_path):
    """...but only when it was not asked for by name."""
    path = tmp_path / "samples.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [
                    {"name": "a", "samples": [1.0, 2.0]},
                    {"name": "b", "samples": [1.0, 2.0], "limits": {"max": 5.0}},
                ]
            }
        )
    )
    report = run_yield(str(path))
    assert [m["name"] for m in report["measurements"]] == ["b"]
    assert any("'a' declares no spec limits" in w for w in report["warnings"])


def test_an_explicitly_named_measurement_without_limits_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], name="a")
    with pytest.raises(YieldError, match="requested explicitly but declares no"):
        run_yield(path, measurements=["a"])


def test_no_measurement_with_limits_at_all_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0])
    with pytest.raises(YieldError, match="no measurement has spec limits"):
        run_yield(path)


def test_an_unknown_measurement_name_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0}, name="a")
    with pytest.raises(YieldError, match="no such measurement"):
        run_yield(path, measurements=["nope"])


def test_limits_file_without_measurements_is_an_error(tmp_path):
    path = _sample_set(tmp_path, [1.0, 2.0], limits={"max": 5.0}, name="a")
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"confidence": 0.9}))
    with pytest.raises(YieldError, match="no 'measurements' object"):
        run_yield(path, limits_path=str(limits))


def test_sim_report_with_a_non_numeric_value_is_an_error(tmp_path):
    path = _sim_report(tmp_path, [1.0, 2.0], limits={"max": 3.0})
    report = json.loads(open(path).read())
    report["corners"][0]["measurements"][0]["value"] = "oops"
    with pytest.raises(YieldError, match="non-numeric value"):
        _measurements_from_sim_report(report)


# --------------------------------------------------------------------------- #
# negative_control / analytic_cross_check parsing (issue #817, no native
# extension needed -- these are input-reader-tier checks)
# --------------------------------------------------------------------------- #


def _sample_set_doc(tmp_path, entry):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def test_negative_control_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "negative_control": {
            "samples": [9.0, None, 9.5],
            "description": "vos forced to 10x spec",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    nc = measurements[0]["negative_control"]
    assert nc["samples"] == [9.0, 9.5]
    assert nc["errored"] == 1
    assert nc["description"] == "vos forced to 10x spec"


def test_a_measurement_without_a_negative_control_reads_as_none(tmp_path):
    path = _sample_set_doc(
        tmp_path, {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}}
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["negative_control"] is None
    assert measurements[0]["analytic_cross_check"] is None


def test_negative_control_must_be_an_object(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "negative_control": 9,
        },
    )
    with pytest.raises(YieldError, match="non-object negative_control"):
        _read_samples(path)


def test_negative_control_without_samples_is_an_error(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "negative_control": {"description": "oops, forgot the samples"},
        },
    )
    with pytest.raises(YieldError, match="negative_control has no 'samples'"):
        _read_samples(path)


def test_analytic_cross_check_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.01, "mean": 0.0},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    check = measurements[0]["analytic_cross_check"]
    assert check == {"kind": "mismatch_offset", "sigma": 0.01, "mean": 0.0}


def test_analytic_cross_check_rejects_an_unknown_kind(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "analytic_cross_check": {"kind": "made_up"},
        },
    )
    with pytest.raises(YieldError, match="kind must be one of"):
        _read_samples(path)


def test_analytic_cross_check_rejects_a_field_not_valid_for_its_kind(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "analytic_cross_check": {"kind": "mismatch_offset", "capacitance_f": 1e-12},
        },
    )
    with pytest.raises(YieldError, match="is not valid for kind"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# sampling parsing (issue #907, Phase 2b of epic #710 -- no native extension
# needed, these are input-reader-tier checks)
# --------------------------------------------------------------------------- #


def test_a_measurement_without_sampling_reads_as_none(tmp_path):
    path = _sample_set_doc(
        tmp_path, {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}}
    )
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] is None


def test_plain_random_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "plain_random"},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {"strategy": "plain_random"}


def test_latin_hypercube_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0, 3.0, 4.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 2},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {
        "strategy": "latin_hypercube",
        "replicates": 2,
    }


def test_importance_sampling_is_parsed_from_a_sample_set(tmp_path):
    entry = {
        "name": "m",
        "samples": [1.0, 2.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "importance", "weights": [1.5, 2.5]},
    }
    path = _sample_set_doc(tmp_path, entry)
    _kind, measurements, _source = _read_samples(path)
    assert measurements[0]["sampling"] == {
        "strategy": "importance",
        "weights": [1.5, 2.5],
    }


def test_sampling_must_be_an_object(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {"name": "m", "samples": [1.0, 2.0], "limits": {"max": 5.0}, "sampling": 9},
    )
    with pytest.raises(YieldError, match="non-object sampling"):
        _read_samples(path)


def test_sampling_rejects_an_unknown_strategy(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "made_up"},
        },
    )
    with pytest.raises(YieldError, match="strategy must be one of"):
        _read_samples(path)


def test_sampling_rejects_a_field_not_valid_for_its_strategy(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "plain_random", "replicates": 2},
        },
    )
    with pytest.raises(YieldError, match="is not valid for strategy"):
        _read_samples(path)


def test_latin_hypercube_sampling_requires_replicates_at_least_two(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "latin_hypercube", "replicates": 1},
        },
    )
    with pytest.raises(YieldError, match="replicates must be an integer >= 2"):
        _read_samples(path)


def test_importance_sampling_requires_a_non_empty_weights_array(tmp_path):
    path = _sample_set_doc(
        tmp_path,
        {
            "name": "m",
            "samples": [1.0, 2.0],
            "limits": {"max": 5.0},
            "sampling": {"strategy": "importance", "weights": []},
        },
    )
    with pytest.raises(YieldError, match="weights must be a non-empty array"):
        _read_samples(path)


# --------------------------------------------------------------------------- #
# Statistics (native extension required)
# --------------------------------------------------------------------------- #


@requires_native
def test_yield_matches_the_closed_form_two_sigma_case(tmp_path):
    """A standard normal against symmetric +/-2 sigma limits has an analytic
    yield of `erf(sqrt(2)) = 0.9544997...` -- the closed-form case issue
    #816's fourth acceptance criterion asks for, checked here through the
    full CLI-facing path rather than only inside the crate."""
    analytic = math.erf(math.sqrt(2.0))
    path = _sample_set(
        tmp_path, _normal_grid(4000, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    report = run_yield(path)
    m = report["measurements"][0]

    assert m["yield"]["normal"]["estimate"] == pytest.approx(analytic, abs=5e-4)
    assert m["yield"]["empirical"]["estimate"] == pytest.approx(analytic, abs=2e-3)
    ci = m["yield"]["empirical"]["confidence_interval"]
    assert ci["low"] <= analytic <= ci["high"]
    # Cpk for a symmetric +/-2 sigma window is 2/3; sigma-to-spec is 2.
    assert m["capability"]["cpk"] == pytest.approx(2.0 / 3.0, abs=5e-3)
    assert m["capability"]["sigma_to_spec"] == pytest.approx(2.0, abs=1.5e-2)
    assert m["distribution"]["normality"]["verdict"] == "consistent"


@requires_native
def test_every_yield_number_carries_an_interval_and_a_sample_count(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(64, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    for m in report["measurements"]:
        for key in ("empirical", "normal"):
            estimate = m["yield"][key]
            assert estimate is not None
            assert estimate["n"] == 64
            assert set(estimate["confidence_interval"]) == {"low", "high"}
            assert math.isfinite(estimate["confidence_interval"]["low"])
            assert math.isfinite(estimate["confidence_interval"]["high"])
            assert estimate["confidence"] == 0.95


@requires_native
def test_a_single_sample_is_an_error_not_a_bare_point_estimate(tmp_path):
    path = _sample_set(tmp_path, [1.2], limits={"min": 1.0, "max": 1.5})
    with pytest.raises(YieldError, match="cannot carry a confidence interval"):
        run_yield(path)


@requires_native
def test_a_confidence_of_one_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(50, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    with pytest.raises(YieldError, match="no finite interval"):
        run_yield(path, confidence=1.0)


@requires_native
def test_min_samples_below_the_hard_floor_is_an_error(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(50, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    with pytest.raises(YieldError, match="min_samples must be at least 2"):
        run_yield(path, min_samples=1)


@requires_native
def test_limits_file_wins_over_the_documents_own_limits(tmp_path):
    """The spec-limits file is the caller's explicit statement of the spec."""
    path = _sample_set(
        tmp_path,
        _normal_grid(200, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0},
        name="a",
    )
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"measurements": {"a": {"min": -1.0, "max": 1.0}}}))
    report = run_yield(path, limits_path=str(limits))
    m = report["measurements"][0]
    assert m["limits"] == {"min": -1.0, "max": 1.0}
    # Phi(1) - Phi(-1) = 0.6827.
    assert m["yield"]["empirical"]["estimate"] == pytest.approx(0.6827, abs=0.01)


@requires_native
def test_a_declared_target_yield_fails_when_the_lower_bound_misses_it(tmp_path):
    """The point estimate is 1.0, but 100 clean samples cannot *claim* 99% at
    95% confidence -- the whole point of the epic's discipline."""
    path = _sample_set(
        tmp_path,
        _normal_grid(100, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.99},
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert report["status"] == "fail"
    assert m["status"] == "fail"
    assert m["yield"]["empirical"]["estimate"] == 1.0
    assert m["yield"]["empirical"]["confidence_interval"]["low"] < 0.99
    # ln(0.025)/ln(0.99) = 367.03 -> 368.
    assert m["sample_size"]["required_n_for_target"] == 368
    assert m["sample_size"]["verdict"] == "insufficient"


@requires_native
def test_a_target_yield_below_the_observed_rate_passes(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(400, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.98},
    )
    report = run_yield(path)
    assert report["status"] == "pass"
    assert report["measurements"][0]["status"] == "pass"


@requires_native
def test_sim_report_and_sample_set_agree_on_the_same_draw():
    """The two input shapes over the identical draw must produce identical
    statistics -- the guarantee that consuming the canary MC record format
    directly costs nothing."""
    from_samples = run_yield(
        os.path.join(EXAMPLES, "mc-samples.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    from_sim = run_yield(
        os.path.join(EXAMPLES, "sim-report.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    assert from_samples["measurements"] == [
        {**m, "source_corners": []} for m in from_sim["measurements"]
    ]


# --------------------------------------------------------------------------- #
# negative_control / analytic_cross_check (issue #817)
# --------------------------------------------------------------------------- #


@requires_native
def test_a_seeded_known_bad_negative_control_is_detected(tmp_path):
    """The self-check issue #817 requires: a deliberately-degraded variant
    must show up as a *statistically distinguishable* worse yield, not just
    a lower point estimate."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": _normal_grid(300, 0.6, 0.05),
            "description": "vos forced to 0.6 (12x the 0.05 spec sigma)",
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    nc = m["negative_control"]
    assert nc["verdict"] == "detected"
    assert nc["description"] == "vos forced to 0.6 (12x the 0.05 spec sigma)"
    assert nc["yield"]["empirical"]["estimate"] < m["yield"]["empirical"]["estimate"]
    assert not any("not statistically" in w for w in report["warnings"])
    assert not any(
        "no measurement declared a negative_control" in w for w in report["warnings"]
    )


@requires_native
def test_a_negative_control_that_does_not_degrade_is_flagged(tmp_path):
    """A "negative control" drawn from the same distribution as the nominal
    measurement is a broken self-check (the defect was never injected) --
    `klt yield` must flag this rather than accept it silently."""
    samples = _normal_grid(300, 0.0, 0.05)
    entry = {
        "name": "vos",
        "samples": samples,
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {"samples": list(samples)},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["negative_control"]["verdict"] == "not_detected"
    assert any("not statistically distinguishable" in w for w in m["warnings"])
    assert any("did not show the expected degradation" in w for w in report["warnings"])


@requires_native
def test_a_campaign_without_any_negative_control_is_flagged(tmp_path):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    assert report["measurements"][0]["negative_control"] is None
    assert any(
        "no measurement declared a negative_control" in w for w in report["warnings"]
    )


@requires_native
def test_ktc_noise_analytic_cross_check_matches_a_synthetic_draw(tmp_path):
    """`sigma = sqrt(kB * T / C)` for a 1 pF cap at 300 K -- the closed-form
    case issue #817's second acceptance criterion asks for."""
    boltzmann = 1.380_649e-23
    capacitance_f = 1.0e-12
    temperature_k = 300.0
    sigma = math.sqrt(boltzmann * temperature_k / capacitance_f)
    entry = {
        "name": "vn",
        "samples": _normal_grid(2000, 0.0, sigma),
        "limits": {"min": -5 * sigma, "max": 5 * sigma},
        "analytic_cross_check": {
            "kind": "ktc_noise",
            "capacitance_f": capacitance_f,
            "temperature_k": temperature_k,
        },
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    check = report["measurements"][0]["analytic_cross_check"]
    assert check["kind"] == "ktc_noise"
    assert check["verdict"] == "consistent"
    assert check["analytic_stddev"] == pytest.approx(sigma, rel=1e-9)
    assert abs(check["stddev_relative_delta"]) < 0.01


@requires_native
def test_mismatch_offset_analytic_cross_check_flags_a_disagreement(tmp_path):
    """A draw whose actual spread is nowhere near the claimed sigma is the
    discrepancy an analytic cross-check exists to catch."""
    entry = {
        "name": "vos",
        "samples": _normal_grid(2000, 0.0, 0.001),
        "limits": {"min": -0.05, "max": 0.05},
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.010},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    check = m["analytic_cross_check"]
    assert check["verdict"] == "inconsistent"
    assert any("not consistent with the mismatch_offset" in w for w in m["warnings"])


# --------------------------------------------------------------------------- #
# Variance-reduction sampling strategies (issue #907, Phase 2b of epic #710)
# --------------------------------------------------------------------------- #


@requires_native
def test_omitting_sampling_reports_plain_random_with_no_variance_reduced_estimate(
    tmp_path,
):
    path = _sample_set(
        tmp_path, _normal_grid(300, 0.0, 1.0), limits={"min": -3.0, "max": 3.0}
    )
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"] == {
        "strategy": "plain_random",
        "replicates": None,
        "effective_sample_size": None,
    }
    assert m["yield"]["variance_reduced"] is None
    assert m["sample_size"]["variance_reduced"] is None


@requires_native
def test_latin_hypercube_sampling_strategy_and_verdict_are_surfaced_in_the_report(
    tmp_path,
):
    """Issue #907's JSON-contract requirement: the sampling strategy and its
    effect on the CI/sample-size verdict must be visible in the payload, not
    just the point estimate."""
    entry = {
        "name": "vref",
        # 4 replicates of an exact quantile grid -- deterministic, so this
        # test only checks the JSON shape end to end; the Rust crate's own
        # tests (native/yield/src/estimate.rs) carry the statistical
        # variance-reduction proof against a genuinely random draw.
        "samples": _normal_grid(400, 0.0, 1.0),
        "limits": {"min": -2.0, "max": 2.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 4},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"]["strategy"] == "latin_hypercube"
    assert m["sampling"]["replicates"] == 4
    assert m["yield"]["variance_reduced"]["method"] == "lhs-replicated"
    assert m["yield"]["variance_reduced"]["n"] == 400
    assert m["sample_size"]["variance_reduced"] is not None
    assert m["sample_size"]["variance_reduced"]["verdict"] in (
        "sufficient",
        "insufficient",
    )


@requires_native
def test_importance_sampling_with_equal_weights_matches_the_plain_empirical_estimate(
    tmp_path,
):
    samples = _normal_grid(300, 0.0, 1.0)
    entry = {
        "name": "vref",
        "samples": samples,
        "limits": {"min": -2.0, "max": 2.0},
        "sampling": {"strategy": "importance", "weights": [2.0] * len(samples)},
    }
    path = _sample_set_doc(tmp_path, entry)
    report = run_yield(path)
    m = report["measurements"][0]
    assert m["sampling"]["strategy"] == "importance"
    assert m["sampling"]["effective_sample_size"] == pytest.approx(300.0, rel=1e-6)
    assert m["yield"]["variance_reduced"]["method"] == "importance-weighted"
    assert m["yield"]["variance_reduced"]["estimate"] == pytest.approx(
        m["yield"]["empirical"]["estimate"], rel=1e-9
    )


@requires_native
def test_latin_hypercube_replicates_below_two_is_an_error(tmp_path):
    # Python's own input-reader rejects this before it ever reaches the
    # native core (see test_latin_hypercube_sampling_requires_replicates_at_least_two
    # above); this test is the end-to-end confirmation via `run_yield`.
    entry = {
        "name": "vref",
        "samples": [1.0, 2.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "latin_hypercube", "replicates": 1},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="replicates must be an integer >= 2"):
        run_yield(path)


@requires_native
def test_importance_weights_length_mismatch_is_an_error(tmp_path):
    entry = {
        "name": "vref",
        "samples": [1.0, 2.0, 3.0],
        "limits": {"max": 5.0},
        "sampling": {"strategy": "importance", "weights": [1.0, 1.0]},
    }
    path = _sample_set_doc(tmp_path, entry)
    with pytest.raises(YieldError, match="sampling.weights has 2 entries"):
        run_yield(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


@requires_native
def test_cli_json_envelope_and_exit_code(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(400, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.98},
    )
    assert main(["yield", path, "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    assert payload["status"] == "pass"
    assert payload["measurement_count"] == 1


@requires_native
def test_cli_exits_3_when_a_yield_claim_is_not_supported(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(100, 0.0, 1.0),
        limits={"min": -10.0, "max": 10.0, "target_yield": 0.99},
    )
    assert main(["yield", path, "--format", "json"]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "fail"


@requires_native
def test_cli_text_output_always_shows_the_interval(tmp_path, capsys):
    path = _sample_set(
        tmp_path, _normal_grid(200, 0.0, 1.0), limits={"min": -2.0, "max": 2.0}
    )
    assert main(["yield", path]) == 0
    out = capsys.readouterr().out
    assert "yield (empirical, clopper-pearson):" in out
    assert "at 95%, N=200" in out
    assert "sample size:" in out
    assert "negative control: none declared" in out
    assert "no measurement declared a negative_control" in out


@requires_native
def test_cli_text_output_shows_the_negative_control_and_analytic_cross_check(
    tmp_path, capsys
):
    entry = {
        "name": "vos",
        "samples": _normal_grid(300, 0.0, 0.05),
        "limits": {"min": -0.5, "max": 0.5},
        "negative_control": {
            "samples": _normal_grid(300, 0.6, 0.05),
            "description": "forced offset",
        },
        "analytic_cross_check": {"kind": "mismatch_offset", "sigma": 0.05},
    }
    path = _sample_set_doc(tmp_path, entry)
    assert main(["yield", path]) == 0
    out = capsys.readouterr().out
    assert "negative control (forced offset): detected" in out
    assert "analytic cross-check (mismatch_offset): consistent" in out


def test_cli_error_uses_the_json_error_envelope(tmp_path, capsys):
    assert main(["yield", str(tmp_path / "nope.json"), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["error"]["command"] == "yield"
    assert "not found" in error["error"]["message"]


@requires_native
def test_cli_measurement_filter_accepts_repeats_and_commas(tmp_path, capsys):
    path = tmp_path / "samples.json"
    path.write_text(
        json.dumps(
            {
                "measurements": [
                    {
                        "name": name,
                        "samples": _normal_grid(50, 0.0, 1.0),
                        "limits": {"min": -3.0, "max": 3.0},
                    }
                    for name in ("a", "b", "c")
                ]
            }
        )
    )
    assert main(["yield", str(path), "--measurement", "a,c", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [m["name"] for m in payload["measurements"]] == ["a", "c"]


# --------------------------------------------------------------------------- #
# Worked example
# --------------------------------------------------------------------------- #


def test_examples_regenerate_byte_identically(tmp_path):
    """`examples/yield/generate.py` is seeded, so the committed fixtures must
    round-trip exactly -- otherwise the numbers quoted in docs/cli/yield.md
    would drift silently."""
    before = {
        name: open(os.path.join(EXAMPLES, name)).read()
        for name in ("mc-samples.json", "spec-limits.json", "sim-report.json")
    }
    subprocess.run(
        [sys.executable, os.path.join(EXAMPLES, "generate.py")],
        check=True,
        capture_output=True,
    )
    after = {name: open(os.path.join(EXAMPLES, name)).read() for name in before}
    assert after == before


@requires_native
def test_worked_example_matches_the_documented_numbers():
    """The exact figures docs/cli/yield.md's worked example quotes."""
    report = run_yield(
        os.path.join(EXAMPLES, "mc-samples.json"),
        limits_path=os.path.join(EXAMPLES, "spec-limits.json"),
    )
    assert report["status"] == "fail"
    vref = next(m for m in report["measurements"] if m["name"] == "vref")
    assert vref["n"] == 300
    assert vref["yield"]["empirical"]["estimate"] == 1.0
    assert vref["yield"]["empirical"]["confidence_interval"]["low"] == pytest.approx(
        0.987779, abs=1e-6
    )
    assert vref["yield"]["normal"]["estimate"] == pytest.approx(0.998172, abs=1e-6)
    assert vref["capability"]["cpk"] == pytest.approx(1.0122, abs=1e-4)
    assert vref["capability"]["sigma_to_spec"] == pytest.approx(3.0365, abs=1e-4)
    assert vref["sample_size"]["required_n_for_target"] == 368
    assert vref["distribution"]["normality"]["verdict"] == "consistent"

    iq = next(m for m in report["measurements"] if m["name"] == "iq_ua")
    assert iq["limits"] == {"max": 10.0, "target_yield": 0.99}
    assert iq["capability"]["cp"] is None
    assert iq["capability"]["limiting_side"] == "upper"
    assert iq["sample_size"]["required_n_for_target"] == 874
