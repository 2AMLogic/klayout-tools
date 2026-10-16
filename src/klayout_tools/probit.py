"""Probit threshold fit for binary-outcome decision circuits (``klt
yield-threshold``, issue #2976).

A clocked comparator, latch or sense amplifier never reveals its input-referred
offset: each Monte Carlo sample only reports "decided high" or "decided low" at
the stimulus level it was driven with. The offset distribution is recovered by
fitting ``P(high | s) = Phi((s - mu) / sigma)`` -- a probit model -- to the
per-sample outcomes, which gives the threshold mean ``mu`` and sigma ``sigma``.

Pure Python (stdlib only, like the rest of ``src/klayout_tools/``). Everything
here is numerics; reading the records document and the CLI live in
:func:`fit_document` and ``cli/yield_threshold_cmd.py``.

Numerical choices that matter (the reason this is a module and not a recipe):

* **Tail precision.** ``log Phi(z)`` comes from ``erfc`` in the body and an
  asymptotic series below ``-20`` sigma (:func:`log_ndtr`), and the score uses
  the inverse Mills ratio ``phi/Phi`` computed in log space (:func:`_mills`).
  A naive ``log(0.5 * erfc(...))`` underflows near ``-38`` sigma; this one is
  finite and accurate far beyond it.
* **Concave likelihood.** The fit is Newton's method with step halving on the
  ``(alpha, beta)`` linear predictor ``alpha + beta * s`` (log-likelihood is
  concave there), on a centred and scaled stimulus axis.
* **Profile-likelihood intervals** for ``mu`` and ``sigma`` -- not the
  chi-square interval on a sample standard deviation, which does not apply
  because the threshold is never observed directly.
* **No faked estimates.** Separable data, a single outcome, fewer than two
  stimulus levels, a non-increasing trend or non-convergence give ``None``
  estimates plus a ``status`` and ``reason``.
* **Clopper-Pearson** intervals are implemented here rather than reusing
  ``native/yield``'s: that crate exposes only whole-request JSON entry points
  (``analyze_yield_json``) to Python, not the interval, and this verb must not
  require the native extension. The two are cross-checked against the same
  reference values in ``tests/test_probit.py``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable
from statistics import NormalDist
from typing import Any

SCHEMA_VERSION = 1

DEFAULT_CONFIDENCE = 0.95

_SQRT2 = math.sqrt(2.0)
_LOG_SQRT_2PI = 0.5 * math.log(2.0 * math.pi)

#: Fit statuses. ``ok`` is the only one carrying estimates.
STATUS_OK = "ok"
STATUS_ALL_ONE_OUTCOME = "all_one_outcome"
STATUS_INSUFFICIENT_LEVELS = "insufficient_stimulus_levels"
STATUS_SEPARATED = "separated"
STATUS_NON_INCREASING = "non_increasing"
STATUS_NON_CONVERGENCE = "non_convergence"
STATUS_NO_SAMPLES = "no_samples"


class ThresholdError(Exception):
    """Raised when ``klt yield-threshold`` cannot run: a bad/missing input
    document or an invalid option. The CLI turns it into a clean stderr
    message + exit code 1, never a traceback (``docs/json-contract.md``).
    """


# --------------------------------------------------------------------------- #
# Special functions
# --------------------------------------------------------------------------- #


def log_ndtr(z: float) -> float:
    """``log Phi(z)`` for the standard normal CDF, stable on both tails."""
    if z > 5.0:
        return math.log1p(-0.5 * math.erfc(z / _SQRT2))
    if z >= -20.0:
        return math.log(0.5 * math.erfc(-z / _SQRT2))
    # Asymptotic expansion of the lower tail:
    #   Phi(z) ~ phi(z) / (-z) * (1 - 1/z^2 + 3/z^4 - 15/z^6 + 105/z^8 - 945/z^10)
    z2 = z * z
    inv = 1.0 / z2
    series = 1.0 + inv * (
        -1.0 + inv * (3.0 + inv * (-15.0 + inv * (105.0 + inv * -945.0)))
    )
    return -0.5 * z2 - _LOG_SQRT_2PI - math.log(-z) + math.log(series)


def _log_pdf(z: float) -> float:
    return -0.5 * z * z - _LOG_SQRT_2PI


def _mills(z: float) -> float:
    """Inverse Mills ratio ``phi(z) / Phi(z)``, computed in log space."""
    return math.exp(_log_pdf(z) - log_ndtr(z))


def _gammainc_upper(a: float, x: float) -> float:
    """Regularised upper incomplete gamma ``Q(a, x)`` (Numerical Recipes)."""
    if x <= 0.0:
        return 1.0
    if x < a + 1.0:
        term = 1.0 / a
        total = term
        ap = a
        for _ in range(1000):
            ap += 1.0
            term *= x / ap
            total += term
            if abs(term) < abs(total) * 1e-16:
                break
        return max(0.0, 1.0 - total * math.exp(-x + a * math.log(x) - math.lgamma(a)))
    tiny = 1e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return h * math.exp(-x + a * math.log(x) - math.lgamma(a))


def chi2_sf(x: float, dof: int) -> float:
    """Chi-square survival function ``P(X >= x)`` for ``dof`` degrees of freedom."""
    return _gammainc_upper(dof / 2.0, max(x, 0.0) / 2.0)


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta ``I_x(a, b)`` (continued fraction)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_front = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(ln_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(ln_front) * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 2000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = tiny if abs(d) < tiny else d
        c = 1.0 + aa / c
        c = tiny if abs(c) < tiny else c
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-16:
            break
    return h


def _beta_ppf(p: float, a: float, b: float) -> float:
    """Quantile of Beta(a, b) by bisection (monotone, robust in the tails)."""
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if mid <= lo or mid >= hi:
            break
        if _betainc(a, b, mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def clopper_pearson(k: int, n: int, confidence: float) -> tuple[float, float]:
    """Exact two-sided Clopper-Pearson interval for ``k`` successes in ``n``."""
    alpha = 1.0 - confidence
    low = 0.0 if k == 0 else _beta_ppf(alpha / 2.0, k, n - k + 1)
    high = 1.0 if k == n else _beta_ppf(1.0 - alpha / 2.0, k + 1, n - k)
    return low, high


def resolvable_error_rate(n: int, confidence: float) -> float:
    """The error rate ``n`` samples can resolve at ``confidence``.

    The Clopper-Pearson upper bound on a rate after observing **zero** errors
    in ``n`` samples, ``1 - ((1 - confidence) / 2) ** (1 / n)`` (about ``3/n``
    at 95%: the "rule of three"). A target error rate below this cannot be
    confirmed by ``n`` samples; a stimulus quoted for it is an extrapolation.
    """
    return 1.0 - ((1.0 - confidence) / 2.0) ** (1.0 / n)


# --------------------------------------------------------------------------- #
# Probit fit
# --------------------------------------------------------------------------- #

#: One stimulus level: ``(stimulus, n, high_count)``.
Level = tuple[float, int, int]


def group_levels(points: list[tuple[float, bool]]) -> list[Level]:
    """Group ``(stimulus, high)`` samples into sorted per-level counts."""
    counts: dict[float, list[int]] = {}
    for stimulus, high in points:
        entry = counts.setdefault(stimulus, [0, 0])
        entry[0] += 1
        entry[1] += 1 if high else 0
    return [(s, n, k) for s, (n, k) in sorted(counts.items())]


def _loglik(levels: list[Level], mu: float, beta: float) -> float:
    """Log-likelihood at ``P(high | s) = Phi(beta * (s - mu))``."""
    total = 0.0
    for s, n, k in levels:
        z = beta * (s - mu)
        total += k * log_ndtr(z) + (n - k) * log_ndtr(-z)
    return total


def _score_and_information(
    pts: list[Level], a: float, b: float
) -> tuple[float, float, float, float, float]:
    """Score ``(g0, g1)`` and information ``(h00, h01, h11)`` of the grouped
    probit log-likelihood in the ``eta = a + b * u`` parametrisation."""
    g0 = g1 = h00 = h01 = h11 = 0.0
    for u, n, k in pts:
        z = a + b * u
        r_pos = _mills(z)
        r_neg = _mills(-z)
        score = k * r_pos - (n - k) * r_neg
        weight = k * r_pos * (z + r_pos) + (n - k) * r_neg * (r_neg - z)
        g0 += score
        g1 += score * u
        h00 += weight
        h01 += weight * u
        h11 += weight * u * u
    return g0, g1, h00, h01, h11


def _backtrack(
    ll: Callable[[float, float], float],
    a: float,
    b: float,
    da: float,
    db: float,
    current: float,
) -> tuple[float, float] | None:
    """Halve the Newton step until the log-likelihood does not decrease;
    returns ``(step, new_value)`` or ``None`` if no step improves it."""
    step = 1.0
    for _ in range(60):
        candidate = ll(a + step * da, b + step * db)
        if candidate >= current - 1e-14 * abs(current):
            return step, candidate
        step *= 0.5
    return None


def _newton_2d(levels: list[Level]) -> tuple[float, float, float, int] | None:
    """Maximise the log-likelihood over ``(mu, beta)``; ``None`` if it fails.

    Works in ``eta = a + b * u`` with ``u`` the centred, scaled stimulus (the
    log-likelihood is concave in ``(a, b)``), then maps back.
    """
    total_n = sum(n for _, n, _ in levels)
    mean_s = sum(s * n for s, n, _ in levels) / total_n
    sd_s = math.sqrt(sum(n * (s - mean_s) ** 2 for s, n, _ in levels) / total_n)
    if sd_s <= 0.0:
        return None
    pts = [((s - mean_s) / sd_s, n, k) for s, n, k in levels]

    def ll(a: float, b: float) -> float:
        return sum(
            k * log_ndtr(a + b * u) + (n - k) * log_ndtr(-(a + b * u))
            for u, n, k in pts
        )

    a, b = 0.0, 1.0
    current = ll(a, b)
    iteration = 0
    while iteration < 200:
        iteration += 1
        g0, g1, h00, h01, h11 = _score_and_information(pts, a, b)
        det = h00 * h11 - h01 * h01
        if not (det > 0.0) or not math.isfinite(det):
            return None
        da = (h11 * g0 - h01 * g1) / det
        db = (-h01 * g0 + h00 * g1) / det
        searched = _backtrack(ll, a, b, da, db, current)
        if searched is None:
            return None
        step, candidate = searched
        a += step * da
        b += step * db
        improved = candidate - current
        current = candidate
        if abs(step * da) < 1e-11 * (1 + abs(a)) and abs(step * db) < 1e-11 * (
            1 + abs(b)
        ):
            break
        if improved == 0.0 and step < 1e-6:
            break
        if abs(b) > 1e8:
            return None
    else:
        return None
    if not (b > 0.0):
        return -1.0, -1.0, current, iteration  # sentinel: non-increasing
    beta = b / sd_s
    mu = mean_s - (a / b) * sd_s
    return mu, beta, _loglik(levels, mu, beta), iteration


def _maximise_1d(
    objective_derivs: Callable[[float], tuple[float, float, float]],
    start: float,
    lower: float | None = None,
) -> float:
    """Newton on a concave 1-D function; returns the maximising argument.

    ``objective_derivs(x)`` returns ``(value, gradient, curvature >= 0)``.
    With ``lower`` set the maximiser is clamped to ``x >= lower``.
    """
    x = start
    value, grad, curv = objective_derivs(x)
    for _ in range(200):
        if lower is not None and x <= lower and grad <= 0.0:
            return lower
        if curv <= 0.0 or not math.isfinite(curv):
            step = 0.1 * math.copysign(1.0, grad) * max(1.0, abs(x))
        else:
            step = grad / curv
        scale = 1.0
        for _ in range(60):
            candidate = x + scale * step
            if lower is not None and candidate < lower:
                candidate = lower
            new_value, new_grad, new_curv = objective_derivs(candidate)
            if new_value >= value - 1e-14 * abs(value):
                break
            scale *= 0.5
        else:
            return x
        moved = abs(candidate - x)
        x, value, grad, curv = candidate, new_value, new_grad, new_curv
        if moved < 1e-13 * (1.0 + abs(x)):
            break
    return x


def _profile_ll_mu(levels: list[Level], mu: float, beta_start: float) -> float:
    """Log-likelihood maximised over ``beta >= 0`` with ``mu`` held fixed."""

    def derivs(beta: float) -> tuple[float, float, float]:
        value = grad = curv = 0.0
        for s, n, k in levels:
            d = s - mu
            z = beta * d
            r_pos, r_neg = _mills(z), _mills(-z)
            value += k * log_ndtr(z) + (n - k) * log_ndtr(-z)
            grad += d * (k * r_pos - (n - k) * r_neg)
            curv += d * d * (k * r_pos * (z + r_pos) + (n - k) * r_neg * (r_neg - z))
        return value, grad, curv

    beta = _maximise_1d(derivs, beta_start, lower=0.0)
    return derivs(beta)[0]


def _profile_ll_sigma(levels: list[Level], beta: float, mu_start: float) -> float:
    """Log-likelihood maximised over ``mu`` with ``beta = 1/sigma`` held fixed."""

    def derivs(mu: float) -> tuple[float, float, float]:
        value = grad = curv = 0.0
        for s, n, k in levels:
            z = beta * (s - mu)
            r_pos, r_neg = _mills(z), _mills(-z)
            value += k * log_ndtr(z) + (n - k) * log_ndtr(-z)
            grad += -beta * (k * r_pos - (n - k) * r_neg)
            curv += (
                beta * beta * (k * r_pos * (z + r_pos) + (n - k) * r_neg * (r_neg - z))
            )
        return value, grad, curv

    mu = _maximise_1d(derivs, mu_start)
    return derivs(mu)[0]


def _find_crossing(
    deviance: Callable[[float], float],
    centre: float,
    step: float,
    direction: int,
    crit: float,
    limit: float,
) -> float | None:
    """Locate where ``deviance`` rises through ``crit`` going away from ``centre``.

    ``None`` when it has not crossed within ``limit`` of the centre (the
    interval is open on that side).
    """
    inner, width = centre, step
    outer = None
    while width <= limit:
        probe = centre + direction * width
        if deviance(probe) > crit:
            outer = probe
            break
        inner = probe
        width *= 2.0
    if outer is None:
        return None
    for _ in range(100):
        mid = 0.5 * (inner + outer)
        if mid == inner or mid == outer:
            break
        if deviance(mid) > crit:
            outer = mid
        else:
            inner = mid
    return 0.5 * (inner + outer)


def fit_probit(
    levels: list[Level], confidence: float = DEFAULT_CONFIDENCE
) -> dict[str, Any]:
    """Fit ``P(high | s) = Phi((s - mu) / sigma)`` to grouped outcomes.

    Returns a dict with ``status`` (``"ok"`` or the reason no estimate exists),
    ``reason``, ``mu``/``sigma`` and their profile-likelihood intervals
    (``None`` when absent or open on that side), ``log_likelihood`` and
    ``iterations``. Never fabricates an estimate for degenerate data.
    """
    result: dict[str, Any] = {
        "status": STATUS_OK,
        "reason": None,
        "mu": None,
        "sigma": None,
        "mu_ci": {"lower": None, "upper": None},
        "sigma_ci": {"lower": None, "upper": None},
        "log_likelihood": None,
        "iterations": None,
    }

    def degenerate(status: str, reason: str) -> dict[str, Any]:
        result["status"] = status
        result["reason"] = reason
        return result

    total = sum(n for _, n, _ in levels)
    if total == 0:
        return degenerate(STATUS_NO_SAMPLES, "no usable samples")
    highs = sum(k for _, _, k in levels)
    if highs == 0 or highs == total:
        which = "high" if highs == total else "low"
        return degenerate(
            STATUS_ALL_ONE_OUTCOME,
            f"every sample decided {which}; the threshold is not bracketed "
            "by the stimulus sweep",
        )
    if len(levels) < 2:
        return degenerate(
            STATUS_INSUFFICIENT_LEVELS,
            "fewer than 2 distinct stimulus levels; mu and sigma are not "
            "separately identifiable",
        )

    max_low = max(s for s, n, k in levels if k < n)
    min_high = min(s for s, n, k in levels if k > 0)
    max_high = max(s for s, n, k in levels if k > 0)
    min_low = min(s for s, n, k in levels if k < n)
    if max_low <= min_high or max_high <= min_low:
        lo, hi = (
            sorted((max_low, min_high))
            if max_low <= min_high
            else sorted((max_high, min_low))
        )
        result["transition_interval"] = [lo, hi]
        return degenerate(
            STATUS_SEPARATED,
            "the outcomes are separated by the stimulus (no overlapping "
            "levels): the likelihood is maximised as sigma -> 0, so only the "
            "transition interval is known",
        )

    fitted = _newton_2d(levels)
    if fitted is None:
        return degenerate(
            STATUS_NON_CONVERGENCE, "the maximum-likelihood iteration did not converge"
        )
    mu, beta, loglik, iterations = fitted
    if beta <= 0.0:
        return degenerate(
            STATUS_NON_INCREASING,
            "the fraction deciding high does not increase with the stimulus",
        )
    sigma = 1.0 / beta
    result.update(mu=mu, sigma=sigma, log_likelihood=loglik, iterations=iterations)

    crit = NormalDist().inv_cdf(0.5 + confidence / 2.0) ** 2

    def dev_mu(m: float) -> float:
        return 2.0 * (loglik - _profile_ll_mu(levels, m, beta))

    def dev_log_sigma(ls: float) -> float:
        return 2.0 * (loglik - _profile_ll_sigma(levels, math.exp(-ls), mu))

    span = max(s for s, _, _ in levels) - min(s for s, _, _ in levels)
    mu_limit = 1e6 * max(span, sigma)
    result["mu_ci"] = {
        "lower": _find_crossing(dev_mu, mu, sigma, -1, crit, mu_limit),
        "upper": _find_crossing(dev_mu, mu, sigma, +1, crit, mu_limit),
    }
    log_sigma = math.log(sigma)
    ls_lo = _find_crossing(dev_log_sigma, log_sigma, 0.1, -1, crit, 40.0)
    ls_hi = _find_crossing(dev_log_sigma, log_sigma, 0.1, +1, crit, 40.0)
    result["sigma_ci"] = {
        "lower": None if ls_lo is None else math.exp(ls_lo),
        "upper": None if ls_hi is None else math.exp(ls_hi),
    }
    return result


def goodness_of_fit(levels: list[Level], mu: float, sigma: float) -> dict[str, Any]:
    """Pearson chi-square and deviance of the fit against the grouped counts.

    ``dof = levels - 2``. ``reliable`` is ``False`` when any expected count
    (either outcome) is below 5, where the chi-square reference is poor.
    """
    pearson = deviance = 0.0
    reliable = True
    for s, n, k in levels:
        z = (s - mu) / sigma
        log_p, log_q = log_ndtr(z), log_ndtr(-z)
        p, q = math.exp(log_p), math.exp(log_q)
        expected_high, expected_low = n * p, n * q
        if expected_high < 5.0 or expected_low < 5.0:
            reliable = False
        pearson += (k - expected_high) ** 2 / max(n * p * q, 1e-300)
        if k > 0:
            deviance += 2.0 * k * (math.log(k / n) - log_p)
        if n - k > 0:
            deviance += 2.0 * (n - k) * (math.log((n - k) / n) - log_q)
    dof = len(levels) - 2
    out: dict[str, Any] = {
        "pearson_chi2": pearson,
        "deviance": max(deviance, 0.0),
        "dof": dof,
        "p_value": None,
        "reliable": reliable and dof > 0,
        "reason": None,
    }
    if dof <= 0:
        out["reason"] = "dof <= 0: need more than 2 stimulus levels to test the fit"
    else:
        out["p_value"] = chi2_sf(pearson, dof)
        if not reliable:
            out["reason"] = (
                "an expected count is below 5; the chi-square reference is approximate"
            )
    return out


def error_rate_stimulus(
    fit: dict[str, Any],
    target: float,
    n_samples: int,
    confidence: float,
    span: tuple[float, float],
) -> dict[str, Any]:
    """The stimulus magnitude at which the error rate reaches ``target``.

    The error rate at stimulus ``s`` is the probability of the wrong decision,
    ``Phi(-|s - mu| / sigma)``, so it equals ``target`` at
    ``mu +/- sigma * Phi^-1(1 - target)``. ``extrapolated`` is ``True`` when
    ``target`` is below the rate ``n_samples`` resolve at ``confidence`` (a
    lower rate has no direct evidence in the sample) or when the stimulus lies
    outside the swept range; the two are reported separately.
    """
    resolvable = resolvable_error_rate(n_samples, confidence)
    out: dict[str, Any] = {
        "error_rate": target,
        "resolvable_error_rate": resolvable,
        "rule_of_three_error_rate": 3.0 / n_samples,
        "extrapolated": target < resolvable,
        "stimulus_low": None,
        "stimulus_high": None,
        "stimulus_high_conservative": None,
        "stimulus_low_conservative": None,
        "outside_stimulus_sweep": None,
    }
    if fit["status"] != STATUS_OK:
        return out
    q = NormalDist().inv_cdf(1.0 - target)
    mu, sigma = fit["mu"], fit["sigma"]
    out["stimulus_low"] = mu - sigma * q
    out["stimulus_high"] = mu + sigma * q
    mu_ci, sg_ci = fit["mu_ci"], fit["sigma_ci"]
    if mu_ci["upper"] is not None and sg_ci["upper"] is not None:
        out["stimulus_high_conservative"] = mu_ci["upper"] + sg_ci["upper"] * q
    if mu_ci["lower"] is not None and sg_ci["upper"] is not None:
        out["stimulus_low_conservative"] = mu_ci["lower"] - sg_ci["upper"] * q
    out["outside_stimulus_sweep"] = bool(
        out["stimulus_low"] < span[0] or out["stimulus_high"] > span[1]
    )
    if out["outside_stimulus_sweep"]:
        out["extrapolated"] = True
    return out


# --------------------------------------------------------------------------- #
# Records document -> report
# --------------------------------------------------------------------------- #

_DEFAULT_CORNER = "default"
_EXCLUDED_STATUSES = {"errored": "errored", "inconclusive": "inconclusive"}


def _load(path: str) -> tuple[Any, str]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise ThresholdError(
            f"cannot read records document {path!r}: {exc.strerror or exc}"
        ) from exc
    try:
        return json.loads(raw), hashlib.sha256(raw).hexdigest()
    except ValueError as exc:
        raise ThresholdError(
            f"records document {path!r} is not valid JSON: {exc}"
        ) from exc


def _number(value: Any, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ThresholdError(f"{where} must be a finite number, got {value!r}")
    return float(value)


def _outcome(
    record: dict[str, Any], where: str, threshold: float | None, outcome_when: str
) -> bool:
    if "outcome" in record:
        raw = record["outcome"]
        if isinstance(raw, bool):
            return raw
        if raw in (0, 1) and not isinstance(raw, float):
            return bool(raw)
        raise ThresholdError(f"{where}.outcome must be a boolean or 0/1, got {raw!r}")
    if "value" in record:
        if threshold is None:
            raise ThresholdError(
                f"{where} carries a `value` but no --threshold was given to "
                "derive the outcome"
            )
        value = _number(record["value"], f"{where}.value")
        return value > threshold if outcome_when == "above" else value < threshold
    raise ThresholdError(f"{where} needs an `outcome` or a `value`")


def _nested_corners(entries: Any) -> dict[str, list[Any]]:
    if not isinstance(entries, list) or not entries:
        raise ThresholdError("`corners` must be a non-empty list")
    corners: dict[str, list[Any]] = {}
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("records"), list):
            raise ThresholdError(
                f"corners[{i}] must be an object with a `records` list"
            )
        name = entry.get("name", _DEFAULT_CORNER)
        if not isinstance(name, str) or not name:
            raise ThresholdError(f"corners[{i}].name must be a non-empty string")
        if name in corners:
            raise ThresholdError(f"duplicate corner name {name!r}")
        corners[name] = [
            (f"corners[{i}].records[{j}]", r) for j, r in enumerate(entry["records"])
        ]
    return corners


def _flat_corners(records: list[Any]) -> dict[str, list[Any]]:
    corners: dict[str, list[Any]] = {}
    for j, record in enumerate(records):
        name = (
            record.get("corner", _DEFAULT_CORNER) if isinstance(record, dict) else None
        )
        if not isinstance(name, str) or not name:
            raise ThresholdError(f"records[{j}].corner must be a non-empty string")
        corners.setdefault(name, []).append((f"records[{j}]", record))
    return corners


def _corner_records(doc: Any) -> dict[str, list[Any]]:
    if not isinstance(doc, dict):
        raise ThresholdError("records document must be a JSON object")
    if "corners" in doc:
        return _nested_corners(doc["corners"])
    if isinstance(doc.get("records"), list):
        return _flat_corners(doc["records"])
    raise ThresholdError("records document needs a `corners` list or a `records` list")


def fit_corner(
    name: str,
    records: list[Any],
    *,
    confidence: float,
    target_error_rate: float | None,
    threshold: float | None,
    outcome_when: str,
) -> dict[str, Any]:
    """Reduce one corner's records to its levels, fit, GOF and target stimulus."""
    points: list[tuple[float, bool]] = []
    excluded = {"errored": 0, "inconclusive": 0}
    for where, record in records:
        if not isinstance(record, dict):
            raise ThresholdError(f"{where} must be an object")
        status = record.get("status")
        if status is not None and status != "ok":
            if status not in _EXCLUDED_STATUSES:
                raise ThresholdError(
                    f"{where}.status must be 'ok', 'errored' or 'inconclusive', "
                    f"got {status!r}"
                )
            excluded[_EXCLUDED_STATUSES[status]] += 1
            continue
        stimulus = _number(record.get("stimulus"), f"{where}.stimulus")
        points.append((stimulus, _outcome(record, where, threshold, outcome_when)))

    levels = group_levels(points)
    fit = fit_probit(levels, confidence)
    gof = None
    if fit["status"] == STATUS_OK:
        gof = goodness_of_fit(levels, fit["mu"], fit["sigma"])
    entry: dict[str, Any] = {
        "name": name,
        "n_samples": len(points),
        "excluded": excluded,
        "levels": [
            {
                "stimulus": s,
                "n": n,
                "high": k,
                "proportion": k / n,
                "ci": list(clopper_pearson(k, n, confidence)),
            }
            for s, n, k in levels
        ],
        "fit": fit,
        "goodness_of_fit": gof,
        "target": None,
    }
    if target_error_rate is not None and points:
        span = (levels[0][0], levels[-1][0])
        entry["target"] = error_rate_stimulus(
            fit, target_error_rate, len(points), confidence, span
        )
    return entry


def fit_document(
    path: str,
    *,
    confidence: float = DEFAULT_CONFIDENCE,
    target_error_rate: float | None = None,
    threshold: float | None = None,
    outcome_when: str = "above",
) -> dict[str, Any]:
    """Read a records document and return the ``klt yield-threshold`` payload."""
    if not (0.0 < confidence < 1.0):
        raise ThresholdError(
            f"--confidence must be strictly between 0 and 1, got {confidence}"
        )
    if target_error_rate is not None and not (0.0 < target_error_rate < 0.5):
        raise ThresholdError(
            "--target-error-rate must be strictly between 0 and 0.5, "
            f"got {target_error_rate}"
        )
    if outcome_when not in ("above", "below"):
        raise ThresholdError("--outcome-when must be 'above' or 'below'")
    doc, digest = _load(path)
    corners = [
        fit_corner(
            name,
            records,
            confidence=confidence,
            target_error_rate=target_error_rate,
            threshold=threshold,
            outcome_when=outcome_when,
        )
        for name, records in _corner_records(doc).items()
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "input": {"path": os.fspath(path), "content_hash": f"sha256:{digest}"},
        "confidence": confidence,
        "target_error_rate": target_error_rate,
        "outcome": (
            {"derived_from": "value", "threshold": threshold, "high_when": outcome_when}
            if threshold is not None
            else {"derived_from": "outcome", "threshold": None, "high_when": None}
        ),
        "corners": corners,
    }
