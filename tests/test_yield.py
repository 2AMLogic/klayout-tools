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


def _sample_set(
    tmp_path, samples, limits=None, name="m", analytic=None, filename="samples.json"
):
    entry = {"name": name, "unit": "V", "samples": samples}
    if limits is not None:
        entry["limits"] = limits
    if analytic is not None:
        entry["analytic"] = analytic
    path = tmp_path / filename
    path.write_text(json.dumps({"measurements": [entry]}))
    return str(path)


def _multi_sample_set(tmp_path, measurements, filename="samples.json"):
    """A sample-set document over several measurements at once --
    ``measurements`` is a list of already-built entry dicts (``{"name",
    "samples", ...}``)."""
    path = tmp_path / filename
    path.write_text(json.dumps({"measurements": measurements}))
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


# --------------------------------------------------------------------------- #
# Negative control (issue #817, Phase 1b of the yield epic #710)
# --------------------------------------------------------------------------- #


@requires_native
def test_negative_control_not_provided_is_flagged_with_a_warning(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    report = run_yield(path)
    assert report["negative_control"] == {
        "provided": False,
        "samples": None,
        "status": "not_provided",
    }
    assert report["measurements"][0]["negative_control"] == {"status": "not_provided"}
    assert any("no negative control was supplied" in w for w in report["warnings"])


@requires_native
def test_negative_control_detects_a_known_bad_variant(tmp_path):
    """A seeded variant with its mean shifted well outside the spec window
    must show up as a clearly worse, non-overlapping yield interval."""
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 10.0, 1.0), name="a", filename="nc.json"
    )
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["provided"] is True
    assert report["negative_control"]["samples"] == nc_path
    assert report["negative_control"]["status"] == "detected"
    m_nc = report["measurements"][0]["negative_control"]
    assert m_nc["status"] == "detected"
    assert m_nc["n"] == 50
    assert m_nc["yield"]["empirical"]["estimate"] == 0.0
    assert not any(
        "did not show the expected degradation" in w for w in report["warnings"]
    )


@requires_native
def test_negative_control_flags_a_variant_that_does_not_show_degradation(tmp_path):
    """A "negative control" drawn from the *same* distribution as the
    primary campaign has no power to demonstrate anything -- it must be
    flagged `not_detected`, not silently accepted as a passing check."""
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 0.0, 1.0), name="a", filename="nc.json"
    )
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["status"] == "not_detected"
    assert report["measurements"][0]["negative_control"]["status"] == "not_detected"
    assert any("did not show the expected degradation" in w for w in report["warnings"])


@requires_native
def test_negative_control_missing_measurement_is_flagged(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 10.0, 1.0), name="not-a", filename="nc.json"
    )
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["status"] == "missing"
    assert report["measurements"][0]["negative_control"] == {"status": "missing"}
    assert any(
        "no matching entry in the negative control samples" in w
        for w in report["warnings"]
    )


@requires_native
def test_negative_control_partial_when_only_some_measurements_are_covered(tmp_path):
    path = _multi_sample_set(
        tmp_path,
        [
            {
                "name": "a",
                "samples": _normal_grid(300, 0.0, 1.0),
                "limits": {"min": -3.0, "max": 3.0},
            },
            {
                "name": "b",
                "samples": _normal_grid(300, 0.0, 1.0),
                "limits": {"min": -3.0, "max": 3.0},
            },
        ],
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 10.0, 1.0), name="a", filename="nc.json"
    )
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["status"] == "partial"
    by_name = {
        m["name"]: m["negative_control"]["status"] for m in report["measurements"]
    }
    assert by_name == {"a": "detected", "b": "missing"}


@requires_native
def test_negative_control_too_few_samples_is_flagged_as_an_error(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    nc_path = _sample_set(tmp_path, [10.0], name="a", filename="nc.json")
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["status"] == "not_detected"
    m_nc = report["measurements"][0]["negative_control"]
    assert m_nc["status"] == "error"
    assert "detail" in m_nc
    assert any("could not be analyzed" in w for w in report["warnings"])


@requires_native
def test_negative_control_input_errors_propagate(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    with pytest.raises(YieldError, match="samples file not found"):
        run_yield(path, negative_control_path=str(tmp_path / "nope.json"))


@requires_native
def test_cli_negative_control_flag_is_wired(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 10.0, 1.0), name="a", filename="nc.json"
    )
    assert (
        main(
            [
                "yield",
                path,
                "--negative-control",
                nc_path,
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["negative_control"]["status"] == "detected"


# --------------------------------------------------------------------------- #
# Analytic cross-check (issue #817, Phase 1b of the yield epic #710)
# --------------------------------------------------------------------------- #


@requires_native
def test_analytic_cross_check_not_provided_by_default(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    report = run_yield(path)
    assert report["analytic_cross_check"] == {
        "tolerance_sigma": 0.2,
        "status": "not_provided",
    }
    assert report["measurements"][0]["analytic_cross_check"] == {
        "status": "not_provided"
    }


@requires_native
def test_analytic_cross_check_consistent_within_tolerance(tmp_path):
    """A grid drawn exactly from `N(0, 1)` must cross-check consistent
    against the same closed-form model."""
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
        analytic={"mean": 0.0, "stddev": 1.0, "model": "N(0, 1)"},
    )
    report = run_yield(path)
    assert report["analytic_cross_check"]["status"] == "consistent"
    m_ac = report["measurements"][0]["analytic_cross_check"]
    assert m_ac["status"] == "consistent"
    assert m_ac["analytic"] == {"mean": 0.0, "stddev": 1.0}
    assert m_ac["delta_mean"] == pytest.approx(0.0, abs=1e-9)
    assert m_ac["delta_mean_sigma"] == pytest.approx(0.0, abs=1e-9)


@requires_native
def test_analytic_cross_check_discrepant_beyond_tolerance(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -5.0, "max": 5.0},
        name="a",
        analytic={"mean": 2.0, "stddev": 1.0, "model": "deliberately wrong"},
    )
    report = run_yield(path)
    assert report["analytic_cross_check"]["status"] == "discrepant"
    m_ac = report["measurements"][0]["analytic_cross_check"]
    assert m_ac["status"] == "discrepant"
    assert m_ac["delta_mean_sigma"] == pytest.approx(-2.0, abs=0.05)
    assert any("diverges from its deliberately wrong" in w for w in report["warnings"])


@requires_native
def test_analytic_cross_check_spec_file_wins_over_sample_document(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        name="a",
        analytic={
            "mean": 99.0,
            "stddev": 1.0,
            "model": "sample-document (should lose)",
        },
    )
    limits_path = tmp_path / "limits.json"
    limits_path.write_text(
        json.dumps(
            {
                "measurements": {
                    "a": {
                        "min": -3.0,
                        "max": 3.0,
                        "analytic": {"mean": 0.0, "stddev": 1.0, "model": "spec file"},
                    }
                }
            }
        )
    )
    report = run_yield(path, limits_path=str(limits_path))
    m_ac = report["measurements"][0]["analytic_cross_check"]
    assert m_ac["model"] == "spec file"
    assert m_ac["status"] == "consistent"


@requires_native
def test_analytic_tolerance_sigma_must_be_positive(tmp_path):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
    )
    with pytest.raises(YieldError, match="analytic_tolerance_sigma must be > 0"):
        run_yield(path, analytic_tolerance_sigma=0.0)


@requires_native
def test_cli_analytic_tolerance_flag_is_wired(tmp_path, capsys):
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -5.0, "max": 5.0},
        name="a",
        analytic={"mean": 0.5, "stddev": 1.0, "model": "N(0.5, 1)"},
    )
    # 0.5 sigma of drift is outside a tight 0.1-sigma tolerance...
    assert main(["yield", path, "--analytic-tolerance", "0.1", "--format", "json"]) == 0
    tight = json.loads(capsys.readouterr().out)
    assert tight["analytic_cross_check"]["status"] == "discrepant"
    # ...but within a loose 1.0-sigma tolerance.
    assert main(["yield", path, "--analytic-tolerance", "1.0", "--format", "json"]) == 0
    loose = json.loads(capsys.readouterr().out)
    assert loose["analytic_cross_check"]["status"] == "consistent"


@requires_native
def test_analytic_and_negative_control_coexist(tmp_path):
    """Both checks report independently on the same run."""
    path = _sample_set(
        tmp_path,
        _normal_grid(300, 0.0, 1.0),
        limits={"min": -3.0, "max": 3.0},
        name="a",
        analytic={"mean": 0.0, "stddev": 1.0, "model": "N(0, 1)"},
    )
    nc_path = _sample_set(
        tmp_path, _normal_grid(50, 10.0, 1.0), name="a", filename="nc.json"
    )
    report = run_yield(path, negative_control_path=nc_path)
    assert report["negative_control"]["status"] == "detected"
    assert report["analytic_cross_check"]["status"] == "consistent"
