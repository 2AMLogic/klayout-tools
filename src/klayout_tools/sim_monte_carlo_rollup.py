"""Monte Carlo *statistics*: sample statistics and per-measurement rollup.

Moved verbatim out of ``sim.py`` (issue #8020) so that module stays under
its per-file size ratchet. Holds the statistics half of #344's Monte Carlo
decomposition (phase 2, #349), which reduces the samples
``sim_monte_carlo`` expanded to a verdict: :func:`_percentile` /
:func:`_sample_statistics` (mean/sigma/quantiles),
:func:`_evaluate_sigma_window` (the optional ``mean +/- k*sigma`` window
inside a measurement's ``limits``), the ``window_scope`` validator and echo,
and :func:`_monte_carlo_rollup`, which builds one measurement's pooled and
``by_corner`` blocks over the *trusted* samples only.

A strict leaf: nothing here imports ``sim.py``, which re-exports the names
its own code or an outside caller still reaches so
``klayout_tools.sim.<name>`` keeps working.
"""

from __future__ import annotations

import statistics
from typing import Any

from .sim_errors import SimError
from .sim_measurement_grading import (
    _ABSENT_STATUS,
    _INCONCLUSIVE_STATUS,
    _evaluate_limits,
)


def _quantile_key(percentile: float) -> str:
    """Response key for a percentile: ``5 -> "p5"``, ``2.5 -> "p2.5"``."""
    return f"p{percentile:g}"


def _percentile(sorted_values: list[float], percentile: float) -> float:
    """Linearly-interpolated percentile of ``sorted_values`` (ascending).

    The "inclusive"/linear method (numpy's default, and
    :func:`statistics.quantiles`' ``method="inclusive"``): the requested
    percentile indexes ``(n - 1) * p / 100`` into the sorted sample and
    interpolates between the two neighbouring order statistics. Hand-rolled
    rather than routed through :func:`statistics.quantiles` because that API
    returns a fixed set of equally-spaced cut points, not an arbitrary
    caller-declared percentile list (``request.monte_carlo.quantiles``).
    ``p0``/``p100`` degenerate to the sample min/max.
    """
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * (percentile / 100.0)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _evaluate_sigma_window(
    mean: float,
    stddev: float,
    k_sigma: float,
    limits: dict[str, float] | None,
) -> dict[str, Any]:
    """Evaluate whether the ``mean +/- k_sigma * stddev`` window stays inside
    ``limits``, returning ``{k, low, high, status, margin}``.

    Both window endpoints are scored with :func:`_evaluate_limits` -- the
    same function (and therefore the same ``min``/``max`` handling and
    ``margin`` sign convention) a single deterministic value goes through --
    rather than a parallel comparison path. The window is inside the limits
    iff *both* endpoints are, so the endpoint statuses combine as
    ``fail`` > ``pass`` and ``margin`` is the worse (smaller) of the two: the
    nearest binding limit when passing, the worst violation when failing. No
    ``limits`` -> ``pass`` with a ``null`` margin, exactly as for a single
    value.

    This function deliberately has no ``inconclusive`` branch (issues #2492
    + #2493): it scores a window, and by the time it is called the
    ``mean``/``stddev`` it is handed were already computed over the
    *trusted* samples only -- distrusted samples are excluded upstream, in
    :func:`_monte_carlo_rollup`/
    :func:`_sample_statistics`, and counted there. Grading here would
    double-count the same fact and leave the window's own endpoints
    describing a population the verdict then disowned.
    """
    low = mean - k_sigma * stddev
    high = mean + k_sigma * stddev
    low_status, low_margin = _evaluate_limits(low, limits)
    high_status, high_margin = _evaluate_limits(high, limits)
    margins = [m for m in (low_margin, high_margin) if m is not None]
    return {
        "k": k_sigma,
        "low": low,
        "high": high,
        "status": "fail" if "fail" in (low_status, high_status) else "pass",
        "margin": min(margins) if margins else None,
    }


def _sample_statistics(
    values: list[float],
    *,
    errored: int,
    inconclusive: int = 0,
    absent: int | None = None,
    quantiles: tuple[float, ...],
    k_sigma: float | None,
    limits: dict[str, float] | None,
) -> dict[str, Any]:
    """Reduce one measurement's Monte Carlo sample ``values`` to the reported
    statistics block: ``{n, errored, inconclusive, mean, stddev, min, max,
    quantiles, sigma_window}``.

    ``n`` counts only samples that produced a number *and* were trusted
    enough to grade; ``errored`` counts the samples whose value was
    unextractable (``null``) and were therefore excluded from every
    statistic -- a sample set is never silently reduced without saying so.

    ``inconclusive`` (issues #2492 + #2493) counts the samples that *did*
    produce a number the run declines to trust -- either the sample's corner
    emitted a diagnostic code the request listed in
    ``options.fail_on_diagnostic``, or the sampled value fell outside a
    declared ``options.node_voltage_bounds``/``measurements[].plausible_range``.
    Those samples are excluded from
    ``mean``/``stddev``/``min``/``max``/``quantiles`` and therefore from the
    ``sigma_window`` derived from them, and counted here instead: keeping
    them would let a sample the run has explicitly disowned move the very
    mean and sigma the window verdict is computed from, which is strictly
    worse than reporting the window over the trusted subset and saying how
    many samples were set aside. ``n + errored + inconclusive`` always
    equals the number of samples drawn for this measurement.

    ``absent`` (issue #2925) counts the samples of an ``optional`` measurement
    that legitimately produced no value; the key is present only for such a
    measurement (``absent is None`` omits it, keeping every other request's
    block unchanged) and the identity becomes ``n + errored + inconclusive +
    absent``. Absent samples are excluded from every statistic and are *not*
    ``errored``, which keeps meaning "the simulation failed".

    ``stddev`` is the **sample** standard deviation (Bessel-corrected,
    ``n - 1``), the estimator appropriate for a finite Monte Carlo draw from
    a population, and is ``null`` for ``n < 2`` (undefined, never faked as
    ``0.0`` -- a fabricated zero sigma would read as "no variation" and
    silently pass any window check). ``sigma_window`` is ``null`` unless a
    sigma multiple was declared *and* ``mean``/``stddev`` both exist.
    """
    ordered = sorted(values)
    n = len(ordered)
    mean = statistics.fmean(ordered) if n else None
    stddev = statistics.stdev(ordered) if n >= 2 else None

    sigma_window: dict[str, Any] | None = None
    if k_sigma is not None and mean is not None and stddev is not None:
        sigma_window = _evaluate_sigma_window(mean, stddev, k_sigma, limits)

    absent_field = {} if absent is None else {"absent": absent}
    return {
        "n": n,
        "errored": errored,
        "inconclusive": inconclusive,
        **absent_field,
        "mean": mean,
        "stddev": stddev,
        "min": ordered[0] if n else None,
        "max": ordered[-1] if n else None,
        "quantiles": {
            _quantile_key(q): (_percentile(ordered, q) if n else None)
            for q in quantiles
        },
        "sigma_window": sigma_window,
    }


def _base_corner_id(corner: dict[str, Any]) -> str:
    """The corner a Monte Carlo sample was drawn from: its ``corner_id`` with
    the ``/mc<sample_index>`` suffix removed (see :attr:`CornerPoint.corner_id`).

    Derived from the sample's own ``monte_carlo.sample_index`` rather than by
    pattern-matching the id, so a process/supply label that happens to
    contain ``/mc`` can never be mis-split.
    """
    corner_id = corner["corner_id"]
    suffix = f"/mc{corner['monte_carlo']['sample_index']}"
    if corner_id.endswith(suffix):
        return corner_id[: -len(suffix)]
    return corner_id


MC_WINDOW_SCOPES = ("pooled", "per_corner")


def _validate_mc_window_scope(mc_spec: dict[str, Any]) -> str:
    """Validate ``request.monte_carlo.window_scope`` (issue #2960): which
    population's ``mean +/- k*sigma`` window gates a measurement's status.
    ``"pooled"`` (the default; an explicit ``null`` reads as not declared)
    grades the union across corners, ``"per_corner"`` grades every
    ``by_corner[]`` population on its own."""
    raw = mc_spec.get("window_scope")
    if raw is None:
        return "pooled"
    if not isinstance(raw, str) or raw not in MC_WINDOW_SCOPES:
        raise SimError(
            "request.monte_carlo.window_scope must be one of "
            f"{', '.join(repr(v) for v in MC_WINDOW_SCOPES)} (got {raw!r})"
        )
    return raw


def _mc_window_scope_echo(mc_spec: dict[str, Any], window_scope: str) -> dict[str, str]:
    """``environment.monte_carlo`` echo for ``window_scope``: only when the
    request declared it, so a sampling-only request keeps its shape."""
    if mc_spec.get("window_scope") is None:
        return {}
    return {"window_scope": window_scope}


def _monte_carlo_rollup(
    entries: list[tuple[dict[str, Any], dict[str, Any]]],
    spec: dict[str, Any],
    quantiles: tuple[float, ...],
    default_k_sigma: float | None,
) -> dict[str, Any] | None:
    """Monte Carlo statistics for one measurement, or ``None`` when no
    sampled corner contributed to it.

    Top-level statistics pool **every** sample of every corner; ``by_corner``
    breaks the same samples down per originating (pre-sampling) corner, in
    corner order. For the common single-corner Monte Carlo request the two
    describe the same draw; for a request that combines a PVT matrix *with*
    ``monte_carlo``, ``by_corner`` is the statistically meaningful view (each
    corner is its own population) and the pooled block is the union across
    corners -- see docs/cli/sim.md.
    """
    sampled = [(c, m) for c, m in entries if c.get("monte_carlo") is not None]
    if not sampled:
        return None

    limits = spec.get("limits")
    # An explicit `null` reads as "not declared" (the house convention for
    # optional fields), so it falls back to the run-wide default.
    override = spec.get("k_sigma")
    k_sigma = default_k_sigma if override is None else float(override)

    def _stats(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
        # Issues #2492 + #2493: a distrusted sample is set aside *before*
        # any statistic is computed (see `_sample_statistics`), not merely
        # reported alongside one -- an untrusted number must not be allowed
        # to move the mean/sigma the window verdict rests on. Both reasons
        # are screened here, and they need screening at two different
        # levels: `options.fail_on_diagnostic` disqualifies the sample's
        # whole *corner* (its measurement can still read `pass`/`fail`,
        # graded against `limits` before the corner was disqualified), while
        # a plausibility-bound violation is graded onto the *measurement*
        # itself. Neither is caught by the pre-existing `value is not None`
        # filter, since a distrusted sample has a real, non-null value --
        # that is exactly what makes it dangerous to the pooled statistics.
        absent = sum(1 for _, m in pairs if m["status"] == _ABSENT_STATUS)
        # The `absent` count is reported only for a measurement declared
        # `optional`, so every other request keeps its exact statistics shape.
        report_absent = absent if spec.get("optional") is True else None
        # Not named `trusted`: CodeQL's sensitive-data name heuristic reads a
        # bare `trusted` as a "secret" and then flags the report payload this
        # list's statistics flow into (PR #8027, py/clear-text-logging-
        # sensitive-data). These are corner/measurement samples, not secrets.
        conclusive = [
            (c, m)
            for c, m in pairs
            if c.get("status") != _INCONCLUSIVE_STATUS
            and m["status"] not in (_INCONCLUSIVE_STATUS, _ABSENT_STATUS)
        ]
        values = [m["value"] for _, m in conclusive if m["value"] is not None]
        return _sample_statistics(
            values,
            errored=len(conclusive) - len(values),
            inconclusive=len(pairs) - len(conclusive) - absent,
            absent=report_absent,
            quantiles=quantiles,
            k_sigma=k_sigma,
            limits=limits,
        )

    grouped: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for corner, m in sampled:
        grouped.setdefault(_base_corner_id(corner), []).append((corner, m))

    rollup = _stats(sampled)
    rollup["by_corner"] = [
        {"corner_id": corner_id, **_stats(pairs)}
        for corner_id, pairs in grouped.items()
    ]
    return rollup
