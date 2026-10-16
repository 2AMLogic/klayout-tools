"""Tests for the probit threshold fit and ``klt yield-threshold`` (issue #2976)."""

from __future__ import annotations

import json
import math
import random

import pytest

from klayout_tools import probit
from klayout_tools.cli import main


def _mills_cf_log_tail(x: float) -> float:
    """Independent ``log Phi(-x)`` for large ``x``: the Laplace continued
    fraction ``phi(x) / (x + 1/(x + 2/(x + 3/(x + ...))))``."""
    tail = x
    for k in range(400, 0, -1):
        tail = x + k / tail
    return -0.5 * x * x - 0.5 * math.log(2 * math.pi) - math.log(tail)


def _sample(mu: float, sigma: float, levels, n: int, seed: int):
    rng = random.Random(seed)
    return [(float(s), rng.gauss(mu, sigma) < s) for s in levels for _ in range(n)]


# --- special functions ---------------------------------------------------- #


@pytest.mark.parametrize("x", [8.0, 20.0, 25.0, 38.0, 45.0, 100.0])
def test_log_ndtr_matches_an_independent_continued_fraction_into_the_far_tail(x):
    assert probit.log_ndtr(-x) == pytest.approx(_mills_cf_log_tail(x), rel=1e-12)


def test_log_ndtr_is_continuous_across_the_series_switch_and_finite_far_out():
    assert probit.log_ndtr(-20.0) == pytest.approx(
        math.log(0.5 * math.erfc(20 / math.sqrt(2))), rel=1e-11
    )
    assert probit.log_ndtr(-1e4) == pytest.approx(
        -0.5e8 - 0.5 * math.log(2 * math.pi) - math.log(1e4), rel=1e-12
    )
    assert probit.log_ndtr(10.0) == pytest.approx(-7.6198530241605e-24, rel=1e-9)


def test_clopper_pearson_agrees_with_the_native_crate_reference_values():
    # native/yield/src/stats.rs: 98/100 at 95%, solved from the binomial tails.
    low, high = probit.clopper_pearson(98, 100, 0.95)
    assert low == pytest.approx(0.929_616_067_528_929_8, abs=1e-12)
    assert high == pytest.approx(0.997_568_663_176_057_4, abs=1e-12)
    # Zero errors: the rule-of-three form [(alpha/2)^(1/n), 1].
    low, high = probit.clopper_pearson(100, 100, 0.95)
    assert high == 1.0
    assert low == pytest.approx(0.025 ** (1 / 100), abs=1e-12)
    assert probit.clopper_pearson(0, 50, 0.95)[0] == 0.0


def test_resolvable_error_rate_is_the_zero_failure_bound_and_near_three_over_n():
    n = 1000
    assert probit.resolvable_error_rate(n, 0.95) == pytest.approx(
        1 - 0.025 ** (1 / n), abs=1e-15
    )
    assert probit.resolvable_error_rate(n, 0.95) == pytest.approx(3.0 / n, rel=0.25)
    assert probit.resolvable_error_rate(n, 0.95) == pytest.approx(
        1 - probit.clopper_pearson(n, n, 0.95)[0], abs=1e-12
    )


def test_chi2_sf_known_values():
    assert probit.chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    assert probit.chi2_sf(9.0, 9) == pytest.approx(0.4372741889, abs=1e-9)
    assert probit.chi2_sf(0.0, 3) == 1.0


# --- the fit -------------------------------------------------------------- #


def test_known_answer_small_table_matches_a_hand_checked_maximum():
    # Symmetric table about s = 0: 20% / 50% / 80% at -1, 0, +1 puts the MLE
    # at mu = 0 and sigma = 1 / Phi^-1(0.8)-ish; verify the score vanishes.
    levels = [(-1.0, 100, 20), (0.0, 100, 50), (1.0, 100, 80)]
    fit = probit.fit_probit(levels)
    assert fit["status"] == "ok"
    assert fit["mu"] == pytest.approx(0.0, abs=1e-9)
    # Score in sigma-space must be ~0 at the optimum.
    h = 1e-5
    f = lambda sg: probit._loglik(levels, fit["mu"], 1.0 / sg)  # noqa: E731
    assert (f(fit["sigma"] + h) - f(fit["sigma"] - h)) / (2 * h) == pytest.approx(
        0.0, abs=1e-5
    )
    assert f(fit["sigma"]) >= f(fit["sigma"] * 1.01) and f(fit["sigma"]) >= f(
        fit["sigma"] * 0.99
    )
    # The profile interval brackets the estimate and is the likelihood-ratio
    # contour: deviance at the bound equals the chi2(1) critical value.
    crit = 1.959963984540054**2
    for bound in ("lower", "upper"):
        m = fit["mu_ci"][bound]
        dev = 2 * (
            fit["log_likelihood"] - probit._profile_ll_mu(levels, m, 1.0 / fit["sigma"])
        )
        assert dev == pytest.approx(crit, rel=1e-6)
    for bound in ("lower", "upper"):
        sg = fit["sigma_ci"][bound]
        dev = 2 * (
            fit["log_likelihood"]
            - probit._profile_ll_sigma(levels, 1.0 / sg, fit["mu"])
        )
        assert dev == pytest.approx(crit, rel=1e-6)
    assert fit["sigma_ci"]["lower"] < fit["sigma"] < fit["sigma_ci"]["upper"]


def test_recovers_known_mu_sigma_within_ci():
    pts = _sample(0.4, 2.0, range(-6, 7), 60, seed=11)
    fit = probit.fit_probit(probit.group_levels(pts))
    assert fit["status"] == "ok"
    assert fit["mu_ci"]["lower"] <= 0.4 <= fit["mu_ci"]["upper"]
    assert fit["sigma_ci"]["lower"] <= 2.0 <= fit["sigma_ci"]["upper"]


def test_profile_ci_coverage_is_near_nominal_over_repeated_seeds():
    hits_mu = hits_sigma = trials = 0
    for seed in range(300):
        pts = _sample(0.4, 2.0, range(-5, 6), 30, seed=1000 + seed)
        fit = probit.fit_probit(probit.group_levels(pts))
        trials += 1
        hits_mu += fit["mu_ci"]["lower"] <= 0.4 <= fit["mu_ci"]["upper"]
        hits_sigma += fit["sigma_ci"]["lower"] <= 2.0 <= fit["sigma_ci"]["upper"]
    assert 0.91 <= hits_mu / trials <= 0.985
    assert 0.90 <= hits_sigma / trials <= 0.985


def test_far_tail_threshold_and_extreme_scale_are_fitted_without_underflow():
    # Exact expected counts (no sampling noise) from a threshold at 1e4 with
    # sigma 1e-2: the standardised start is ~1e6 sigma from the optimum, so the
    # line search evaluates log Phi far past the erfc underflow point (-38).
    mu, sigma, n = 1.0e4, 1.0e-2, 10**9
    levels = []
    for z in (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0):
        k = round(n * probit.NormalDist().cdf(z))
        levels.append((mu + z * sigma, n, k))
    fit = probit.fit_probit(levels)
    assert fit["status"] == "ok"
    assert fit["mu"] == pytest.approx(mu, abs=1e-6)
    assert fit["sigma"] == pytest.approx(sigma, rel=1e-6)


def test_far_tail_proportions_keep_the_fit_finite_at_38_sigma():
    # A level 38 sigma below the threshold with zero highs contributes
    # log Phi(+38) ~ 0 and a level 38 sigma above it with all highs likewise;
    # the overlap levels pin the fit. Failing log-space math would give -inf/nan.
    levels = [
        (-38.0, 50, 0),
        (-1.0, 100, 16),
        (0.0, 100, 50),
        (1.0, 100, 84),
        (38.0, 50, 50),
    ]
    fit = probit.fit_probit(levels)
    assert fit["status"] == "ok"
    assert fit["mu"] == pytest.approx(0.0, abs=1e-6)
    assert fit["sigma"] == pytest.approx(1.0, abs=0.05)
    assert math.isfinite(fit["log_likelihood"])
    assert math.isfinite(probit._loglik(levels, 0.0, 1.0))
    assert probit._loglik([(-38.0, 5, 5)], 0.0, 1.0) == pytest.approx(
        5 * _mills_cf_log_tail(38.0), rel=1e-12
    )


@pytest.mark.parametrize(
    ("levels", "status"),
    [
        ([(0.0, 10, 0), (1.0, 10, 0)], probit.STATUS_ALL_ONE_OUTCOME),
        ([(0.0, 10, 10), (1.0, 10, 10)], probit.STATUS_ALL_ONE_OUTCOME),
        ([(0.0, 20, 9)], probit.STATUS_INSUFFICIENT_LEVELS),
        (
            [(0.0, 10, 0), (1.0, 10, 0), (2.0, 10, 10), (3.0, 10, 10)],
            probit.STATUS_SEPARATED,
        ),
        ([(0.0, 10, 3), (1.0, 10, 10), (2.0, 10, 10)], probit.STATUS_SEPARATED),
        ([(0.0, 10, 10), (1.0, 10, 6), (2.0, 10, 0)], probit.STATUS_SEPARATED),
        (
            [(0.0, 10, 8), (1.0, 10, 5), (2.0, 10, 2), (3.0, 10, 9), (4.0, 10, 1)],
            probit.STATUS_NON_INCREASING,
        ),
        ([], probit.STATUS_NO_SAMPLES),
    ],
)
def test_degenerate_data_is_reported_never_faked(levels, status):
    fit = probit.fit_probit(levels)
    assert fit["status"] == status
    assert fit["reason"]
    assert fit["mu"] is None and fit["sigma"] is None
    assert fit["mu_ci"] == {"lower": None, "upper": None}
    assert fit["sigma_ci"] == {"lower": None, "upper": None}


def test_separated_data_reports_the_transition_interval():
    fit = probit.fit_probit([(0.0, 10, 0), (1.0, 10, 0), (2.0, 10, 10)])
    assert fit["status"] == probit.STATUS_SEPARATED
    assert fit["transition_interval"] == [1.0, 2.0]


def test_goodness_of_fit_flags_a_non_probit_response():
    # A response that jumps 0.1 -> 0.9 between two adjacent levels with flat
    # shoulders is not a probit curve.
    levels = [
        (s, 400, k)
        for s, k in ((0, 40), (1, 40), (2, 40), (3, 360), (4, 360), (5, 360))
    ]
    fit = probit.fit_probit([(float(s), n, k) for s, n, k in levels])
    gof = probit.goodness_of_fit(
        [(float(s), n, k) for s, n, k in levels], fit["mu"], fit["sigma"]
    )
    assert gof["dof"] == 4
    assert gof["p_value"] < 1e-6
    good = probit.group_levels(_sample(0.0, 1.5, range(-4, 5), 400, seed=5))
    gfit = probit.fit_probit(good)
    ggof = probit.goodness_of_fit(good, gfit["mu"], gfit["sigma"])
    assert ggof["p_value"] > 0.01


def test_goodness_of_fit_dof_nonpositive_is_explicit():
    levels = [(0.0, 50, 10), (1.0, 50, 40)]
    fit = probit.fit_probit(levels)
    gof = probit.goodness_of_fit(levels, fit["mu"], fit["sigma"])
    assert gof["dof"] == 0 and gof["p_value"] is None and gof["reliable"] is False
    assert "dof" in gof["reason"]


def test_target_error_rate_stimulus_and_extrapolation_flag():
    fit = probit.fit_probit(
        probit.group_levels(_sample(0.0, 1.0, range(-3, 4), 100, seed=2))
    )
    n = 700
    resolved = probit.error_rate_stimulus(fit, 0.01, n, 0.95, (-3.0, 3.0))
    assert resolved["extrapolated"] is False
    assert resolved["stimulus_high"] == pytest.approx(
        fit["mu"] + fit["sigma"] * probit.NormalDist().inv_cdf(0.99)
    )
    assert resolved["stimulus_high_conservative"] > resolved["stimulus_high"]
    far = probit.error_rate_stimulus(fit, 1e-9, n, 0.95, (-3.0, 3.0))
    assert far["extrapolated"] is True
    assert far["resolvable_error_rate"] > 1e-9
    assert far["outside_stimulus_sweep"] is True
    assert far["stimulus_high"] == pytest.approx(
        fit["mu"] + fit["sigma"] * probit.NormalDist().inv_cdf(1 - 1e-9)
    )


def test_target_for_an_unfitted_corner_has_null_stimulus():
    fit = probit.fit_probit([(0.0, 10, 0), (1.0, 10, 0)])
    out = probit.error_rate_stimulus(fit, 1e-3, 20, 0.95, (0.0, 1.0))
    assert out["stimulus_high"] is None and out["stimulus_low"] is None


# --- records document / CLI ----------------------------------------------- #


def _write(tmp_path, doc):
    path = tmp_path / "records.json"
    path.write_text(json.dumps(doc))
    return str(path)


def _records(mu, sigma, seed, corner=None):
    out = []
    for s, high in _sample(mu, sigma, range(-6, 7, 2), 80, seed):
        record = {"stimulus": s, "outcome": high}
        if corner:
            record["corner"] = corner
        out.append(record)
    return out


def test_cli_json_reports_every_corner_separately(tmp_path, capsys):
    doc = {
        "corners": [
            {"name": "tt", "records": _records(0.5, 2.0, 1)},
            {"name": "ss", "records": _records(-1.0, 3.0, 2)},
        ]
    }
    assert (
        main(
            [
                "yield-threshold",
                _write(tmp_path, doc),
                "--target-error-rate",
                "1e-9",
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == probit.SCHEMA_VERSION
    assert [c["name"] for c in payload["corners"]] == ["tt", "ss"]
    tt, ss = payload["corners"]
    assert tt["fit"]["status"] == "ok" and ss["fit"]["status"] == "ok"
    assert tt["fit"]["mu"] > ss["fit"]["mu"]
    assert tt["target"]["extrapolated"] is True
    assert tt["levels"][0]["ci"][0] == 0.0
    assert payload["input"]["content_hash"].startswith("sha256:")


def test_flat_records_group_by_corner_and_default_to_one_corner(tmp_path, capsys):
    flat = _records(0.0, 2.0, 3, corner="tt") + _records(1.0, 2.0, 4, corner="ff")
    assert (
        main(
            ["yield-threshold", _write(tmp_path, {"records": flat}), "--format", "json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert [c["name"] for c in payload["corners"]] == ["tt", "ff"]
    assert (
        main(
            [
                "yield-threshold",
                _write(tmp_path, {"records": _records(0, 2, 5)}),
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert [c["name"] for c in json.loads(capsys.readouterr().out)["corners"]] == [
        "default"
    ]


def test_errored_and_inconclusive_samples_are_excluded_and_counted(tmp_path, capsys):
    records = _records(0.0, 2.0, 6)
    n = len(records)
    records += [{"stimulus": 0.0, "status": "errored"}] * 3
    records += [{"stimulus": 0.0, "status": "inconclusive", "outcome": True}] * 2
    assert (
        main(
            [
                "yield-threshold",
                _write(tmp_path, {"records": records}),
                "--format",
                "json",
            ]
        )
        == 0
    )
    corner = json.loads(capsys.readouterr().out)["corners"][0]
    assert corner["n_samples"] == n
    assert corner["excluded"] == {"errored": 3, "inconclusive": 2}


def test_outcome_derived_from_value_and_threshold(tmp_path, capsys):
    records = [
        {"stimulus": s, "value": 1.0 if high else -1.0}
        for s, high in _sample(0.0, 2.0, range(-5, 6), 50, 8)
    ]
    path = _write(tmp_path, {"records": records})
    assert main(["yield-threshold", path, "--threshold", "0", "--format", "json"]) == 0
    above = json.loads(capsys.readouterr().out)
    assert above["outcome"] == {
        "derived_from": "value",
        "threshold": 0.0,
        "high_when": "above",
    }
    assert above["corners"][0]["fit"]["status"] == "ok"
    assert (
        main(
            [
                "yield-threshold",
                path,
                "--threshold",
                "0",
                "--outcome-when",
                "below",
                "--format",
                "json",
            ]
        )
        == 0
    )
    below = json.loads(capsys.readouterr().out)
    assert below["corners"][0]["fit"]["status"] == probit.STATUS_NON_INCREASING
    assert main(["yield-threshold", path, "--format", "json"]) == 1


def test_degenerate_corner_is_a_clean_report_not_an_error(tmp_path, capsys):
    records = [
        {"stimulus": float(s), "outcome": False} for s in range(5) for _ in range(10)
    ]
    assert (
        main(
            [
                "yield-threshold",
                _write(tmp_path, {"records": records}),
                "--format",
                "json",
            ]
        )
        == 0
    )
    fit = json.loads(capsys.readouterr().out)["corners"][0]["fit"]
    assert fit["status"] == probit.STATUS_ALL_ONE_OUTCOME and fit["sigma"] is None


@pytest.mark.parametrize(
    "doc",
    [
        [],
        {},
        {"records": [{"stimulus": "x", "outcome": True}]},
        {"records": [{"stimulus": 1.0}]},
        {"records": [{"stimulus": 1.0, "outcome": 2}]},
        {"records": [{"stimulus": 1.0, "outcome": True, "status": "weird"}]},
        {"corners": [{"name": "a", "records": []}, {"name": "a", "records": []}]},
    ],
)
def test_malformed_documents_emit_the_error_envelope(tmp_path, capsys, doc):
    assert main(["yield-threshold", _write(tmp_path, doc), "--format", "json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["command"] == "yield-threshold"


def test_missing_file_and_bad_options_are_errors(tmp_path, capsys):
    assert (
        main(["yield-threshold", str(tmp_path / "nope.json"), "--format", "json"]) == 1
    )
    path = _write(tmp_path, {"records": _records(0, 2, 9)})
    assert (
        main(["yield-threshold", path, "--confidence", "1.0", "--format", "json"]) == 1
    )
    assert (
        main(
            ["yield-threshold", path, "--target-error-rate", "0.7", "--format", "json"]
        )
        == 1
    )
    capsys.readouterr()


def test_text_output_renders_fit_and_extrapolation(tmp_path, capsys):
    path = _write(tmp_path, {"records": _records(0.0, 2.0, 10)})
    assert main(["yield-threshold", path, "--target-error-rate", "1e-9"]) == 0
    out = capsys.readouterr().out
    assert "fit: ok" in out and "EXTRAPOLATED" in out and "sigma =" in out
