"""Validation and grading for Monte Carlo measurement dispersion bounds."""

from __future__ import annotations

import math
from typing import Any

from ._jsonnum import is_finite_number, is_number
from .sim_errors import SimError


def validate_k_sigma(value: Any, field: str) -> float:
    """Validate a sigma-multiple (``k``) field and return it as a float.

    Shared by ``request.monte_carlo.k_sigma`` (the run-wide default) and
    ``request.measurements[].k_sigma`` (the per-measurement override) so both
    reject the same shapes with the same message style. Zero is allowed --
    ``k=0`` degenerates the limit window to the mean itself, a legitimate
    "is the *typical* part inside the window?" question.
    """
    if not is_number(value):
        raise SimError(f"{field} must be a non-negative number (got {value!r})")
    if value < 0:
        raise SimError(f"{field} must be a non-negative number (got {value!r})")
    return float(value)


def _validate_non_negative_finite(value: Any, field: str) -> float:
    """``value`` as a finite non-negative ``float``, the shared shape of the
    ``population_limits`` numeric fields (issue #6967)."""
    if not is_finite_number(value) or value < 0:
        raise SimError(f"{field} must be a finite non-negative number (got {value!r})")
    return float(value)


def _validate_population_limits(
    value: Any,
    field: str,
    mc_spec: Any,
    measurement_k_sigma: Any,
) -> dict[str, Any]:
    """Validate ``request.measurements[].population_limits`` (issue #6967)
    and return it normalised as ``{"max_k_sigma": float,
    "quantization_variance": float | None}``.

    One statistic, upper bound only: each ``by_corner[]`` population's
    ``k * sigma`` dispersion (optionally net of the declared quantization
    variance) must stay at or under ``max_k_sigma`` -- see
    :func:`_evaluate_population_limits`. Declaring the bound requires
    ``request.monte_carlo`` (there is no sampled population to bound
    without sampling) and a sigma multiple the row can resolve: the
    measurement's own ``k_sigma`` override, else the run-wide
    ``monte_carlo.k_sigma``.
    """
    if not isinstance(value, dict):
        raise SimError(f"{field} must be an object (got {value!r})")
    unknown = sorted(set(value) - {"max_k_sigma", "quantization_variance"})
    if unknown:
        raise SimError(
            f"{field} has unknown key(s) {', '.join(repr(k) for k in unknown)}; "
            "expected 'max_k_sigma' and optionally 'quantization_variance'"
        )
    if "max_k_sigma" not in value:
        raise SimError(f"{field} requires 'max_k_sigma'")
    max_k_sigma = _validate_non_negative_finite(
        value["max_k_sigma"], f"{field}.max_k_sigma"
    )
    quantization_variance = value.get("quantization_variance")
    if quantization_variance is not None:
        quantization_variance = _validate_non_negative_finite(
            quantization_variance, f"{field}.quantization_variance"
        )
    if mc_spec is None:
        raise SimError(
            f"{field} requires request.monte_carlo: there is no sampled "
            "population to bound without Monte Carlo sampling"
        )
    if not isinstance(mc_spec, dict):
        raise SimError("request.monte_carlo must be an object")
    if measurement_k_sigma is None and mc_spec.get("k_sigma") is None:
        raise SimError(
            f"{field} requires a sigma multiple to resolve: declare "
            "measurements[].k_sigma or request.monte_carlo.k_sigma"
        )
    return {
        "max_k_sigma": max_k_sigma,
        "quantization_variance": quantization_variance,
    }


def validate_spec(spec: dict[str, Any], mc_spec: Any) -> None:
    """Validate/normalise ``spec["population_limits"]`` in place (issue
    #6967), so the rollup reads floats; an undeclared bound (or an explicit
    ``null``, the house convention for optional fields) is a no-op."""
    if spec.get("population_limits") is None:
        return
    spec["population_limits"] = _validate_population_limits(
        spec["population_limits"],
        f"request.measurements[{spec['name']!r}].population_limits",
        mc_spec,
        spec.get("k_sigma"),
    )


def _population_corner_score(
    corner: dict[str, Any],
    k_sigma: float | None,
    max_k_sigma: float,
    variance: float,
) -> dict[str, Any]:
    """One corner's ``k * sigma_net`` score against ``max_k_sigma`` (issue
    #6967), or an all-``null`` row when the corner has no sigma to grade
    (``stddev is None``, i.e. ``n < 2`` trusted samples) or no ``k`` was
    resolvable -- the same "never grade what was never computed" stance as
    a ``sigma_window: null`` corner."""
    stddev = corner["stddev"]
    if k_sigma is None or stddev is None:
        return {
            "corner_id": corner["corner_id"],
            "value": None,
            "margin": None,
            "status": None,
        }
    sigma_net = math.sqrt(max(stddev * stddev - variance, 0.0))
    value = k_sigma * sigma_net
    return {
        "corner_id": corner["corner_id"],
        "value": value,
        "margin": max_k_sigma - value,
        "status": "pass" if value <= max_k_sigma else "fail",
    }


def _evaluate_population_limits(
    by_corner: list[dict[str, Any]],
    k_sigma: float | None,
    population_limits: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate the per-measurement dispersion bound (issue #6967): whether
    every ``by_corner[]`` population's ``k * sigma`` stays at or under
    ``population_limits.max_k_sigma``, worst corner first.

    ``sigma`` is each corner's own Bessel-corrected sample stddev (computed
    over *trusted* samples only -- see :func:`_monte_carlo_rollup`),
    optionally net of the declared ``quantization_variance``:
    ``sqrt(max(sigma^2 - v, 0))``, a bound on the *process* dispersion
    rather than on the measurement's own quantization noise. The mean plays
    no part, and the pooled population is never graded: it is a union
    across populations, not a distribution anyone sampled (see
    docs/cli/sim.md "Pooled vs. per corner"). The worst corner is the
    graded corner with the smallest margin, first in corner order on ties;
    ``status``/``margin``/``worst_corner`` are ``null`` when no corner
    could be graded.
    """
    max_k_sigma = population_limits["max_k_sigma"]
    variance = population_limits.get("quantization_variance")
    variance_field = {} if variance is None else {"quantization_variance": variance}
    scored = [
        _population_corner_score(
            corner,
            k_sigma,
            max_k_sigma,
            variance if variance is not None else 0.0,
        )
        for corner in by_corner
    ]
    gradable = [c for c in scored if c["status"] is not None]
    block: dict[str, Any] = {
        "statistic": "k_sigma",
        "k": k_sigma,
        "max": max_k_sigma,
        **variance_field,
        "status": None,
        "margin": None,
        "worst_corner": None,
        "by_corner": scored,
    }
    if not gradable:
        return block
    worst = min(gradable, key=lambda c: c["margin"])
    block["status"] = "fail" if any(c["status"] == "fail" for c in gradable) else "pass"
    block["margin"] = worst["margin"]
    block["worst_corner"] = worst["corner_id"]
    return block


def grade(
    entry: dict[str, Any],
    mc_stats: dict[str, Any],
    spec: dict[str, Any],
    monte_carlo_stats: dict[str, Any],
) -> None:
    """Attach and enforce the measurement's declared dispersion bound (issue
    #6967) on its ``monte_carlo`` block -- a no-op when the measurement
    declared no ``population_limits``.

    The sigma multiple resolves exactly as the window's does (see
    :func:`_monte_carlo_rollup`): the measurement's own ``k_sigma``
    override, else the run-wide ``monte_carlo.k_sigma``. A population
    ``fail`` makes the entry ``fail`` with the same precedence as a window
    ``fail``: ``error`` and ``inconclusive`` still outrank it, for the same
    reason -- the per-corner sigma is computed over *trusted* samples only,
    so a measurement that also had a distrusted sample is not a clean,
    fully-trustworthy ``fail``.
    """
    population_limits = spec.get("population_limits")
    if population_limits is None:
        return
    override = spec.get("k_sigma")
    k_sigma = monte_carlo_stats["k_sigma"] if override is None else float(override)
    mc_stats["population_limits"] = _evaluate_population_limits(
        mc_stats["by_corner"], k_sigma, population_limits
    )
    if mc_stats["population_limits"]["status"] == "fail" and entry["status"] not in (
        "error",
        "inconclusive",
    ):
        entry["status"] = "fail"
