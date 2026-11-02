"""Per-measurement ``analytic_cross_check`` and ``sampling`` readers for
``klt yield`` (issues #817, #907).

Split out of :mod:`klayout_tools.yield_analysis` to keep that module under
its per-file size ceiling; behaviour is unchanged.
"""

from __future__ import annotations

from typing import Any

from .yield_provenance import _yield_error

#: Which fields each `analytic_cross_check.kind` accepts, mirroring
#: `native/yield/src/contract.rs`'s `AnalyticCrossCheckRequest` variants.
_ANALYTIC_CROSS_CHECK_FIELDS = {
    "ktc_noise": {"capacitance_f", "temperature_k", "analytic_mean"},
    "mismatch_offset": {"sigma", "mean"},
}


def _analytic_cross_check_from(
    raw: Any, name: str, where: str
) -> dict[str, Any] | None:
    """Normalise one measurement's ``analytic_cross_check`` object (issue
    #817's closed-form cross-check), or ``None`` when absent.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _yield_error(
            f"{where}: measurement '{name}' has a non-object analytic_cross_check"
        )
    kind = raw.get("kind")
    if kind not in _ANALYTIC_CROSS_CHECK_FIELDS:
        raise _yield_error(
            f"{where}: measurement '{name}' analytic_cross_check.kind must be one of "
            f"{sorted(_ANALYTIC_CROSS_CHECK_FIELDS)} (got {kind!r})"
        )
    allowed = _ANALYTIC_CROSS_CHECK_FIELDS[kind]
    out: dict[str, Any] = {"kind": kind}
    for key, value in raw.items():
        if key == "kind":
            continue
        if key not in allowed:
            raise _yield_error(
                f"{where}: measurement '{name}' analytic_cross_check.{key} is not "
                f"valid for kind '{kind}' (allowed: {sorted(allowed)})"
            )
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _yield_error(
                f"{where}: measurement '{name}' analytic_cross_check.{key} must be "
                "a number"
            )
        out[key] = float(value)
    return out


#: Which fields each `sampling.strategy` accepts, mirroring
#: `native/yield/src/contract.rs`'s `SamplingRequest` variants (issue #907,
#: Phase 2b of the statistical/yield epic #710).
_SAMPLING_STRATEGY_FIELDS = {
    "plain_random": set(),
    "latin_hypercube": {"replicates"},
    "importance": {"weights"},
}


def _sampling_from(raw: Any, name: str, where: str) -> dict[str, Any] | None:
    """Normalise one measurement's ``sampling`` object (issue #907's
    variance-reduction sampling strategies), or ``None`` when absent -- in
    which case the native core treats it as Phase 1's plain random Monte
    Carlo, unaffected by any of this.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _yield_error(f"{where}: measurement '{name}' has a non-object sampling")
    strategy = raw.get("strategy")
    if strategy not in _SAMPLING_STRATEGY_FIELDS:
        raise _yield_error(
            f"{where}: measurement '{name}' sampling.strategy must be one of "
            f"{sorted(_SAMPLING_STRATEGY_FIELDS)} (got {strategy!r})"
        )
    allowed = _SAMPLING_STRATEGY_FIELDS[strategy]
    for key in raw:
        if key not in {"strategy", *allowed}:
            raise _yield_error(
                f"{where}: measurement '{name}' sampling.{key} is not valid for "
                f"strategy '{strategy}' (allowed: {sorted(allowed)})"
            )

    if strategy == "latin_hypercube":
        replicates = raw.get("replicates")
        if (
            isinstance(replicates, bool)
            or not isinstance(replicates, int)
            or replicates < 2
        ):
            raise _yield_error(
                f"{where}: measurement '{name}' sampling.replicates must be an "
                f"integer >= 2 (got {replicates!r})"
            )
        return {"strategy": strategy, "replicates": replicates}

    if strategy == "importance":
        raw_weights = raw.get("weights")
        if not isinstance(raw_weights, list) or not raw_weights:
            raise _yield_error(
                f"{where}: measurement '{name}' sampling.weights must be a "
                "non-empty array"
            )
        weights: list[float] = []
        for value in raw_weights:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _yield_error(
                    f"{where}: measurement '{name}' sampling.weights entries must "
                    "be numbers"
                )
            weights.append(float(value))
        return {"strategy": strategy, "weights": weights}

    return {"strategy": strategy}
