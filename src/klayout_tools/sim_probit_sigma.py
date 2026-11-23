"""Grade a grid-wide noise sigma from binary populations at two stimuli."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import NormalDist, mean
from typing import Any

from .coverage import work_id
from .sim_corner_point import CornerPoint
from .sim_errors import SimError

_FIELD = "request.monte_carlo.probit_sigma"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SimError(f"{_FIELD}.{field} must be a nonempty string")
    return value


def _number(value: Any, field: str) -> float:
    try:
        if type(value) in (float, int) and math.isfinite(value):
            return float(value)
    except OverflowError:
        pass
    raise SimError(f"{_FIELD}.{field} must be a finite number")


def _sources(value: Any, names: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != 2:
        raise SimError(f"{_FIELD}.sources requires two ordered sources")
    sources: list[dict[str, Any]] = []
    for source in value:
        if not isinstance(source, dict):
            raise SimError(f"{_FIELD}.sources entries must be objects")
        name = _text(source.get("measurement"), "sources[].measurement")
        if name not in names:
            raise SimError(f"{_FIELD} names an unknown source measurement {name!r}")
        level = _number(source.get("stimulus"), "sources[].stimulus")
        sources.append({"measurement": name, "stimulus": level})
    lo, hi = sources
    if lo["measurement"] == hi["measurement"] or lo["stimulus"] >= hi["stimulus"]:
        raise SimError(
            f"{_FIELD} requires distinct measurements and increasing stimuli"
        )
    return sources


def validate(
    monte_carlo: dict[str, Any], measurements: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Resolve the declaration against the request's source measurement names."""
    if "probit_sigma" not in monte_carlo:
        return None
    spec = monte_carlo["probit_sigma"]
    if not isinstance(spec, dict):
        raise SimError(f"{_FIELD} must be an object")
    names = {m["name"] for m in measurements}
    name = _text(spec.get("name"), "name")
    if name in names:
        raise SimError(f"{_FIELD}.name duplicates source measurement {name!r}")
    if spec.get("grid") not in ("mean", "worst"):
        raise SimError(f"{_FIELD}.grid must be 'mean' or 'worst'")
    limits = spec.get("limits")
    if not isinstance(limits, dict) or set(limits) != {"max"}:
        raise SimError(f"{_FIELD}.limits requires exactly one upper bound, 'max'")
    return {
        "name": name,
        "sources": _sources(spec.get("sources"), names),
        "unit": _text(spec.get("unit"), "unit"),
        "spec_row": _text(spec.get("spec_row"), "spec_row"),
        "grid": spec["grid"],
        "limits": {"max": _number(limits["max"], "limits.max")},
    }


def _read_decision(corner: dict[str, Any], name: str) -> tuple[int, str | None]:
    if corner.get("status") not in ("pass", "fail"):
        return 0, "untrusted_sample"
    found = [m for m in corner.get("measurements", []) if m.get("name") == name]
    if len(found) != 1:
        return 0, "missing_or_duplicate_measurement"
    result = found[0]
    if result.get("status") not in ("pass", "fail"):
        return 0, "untrusted_measurement"
    value = result.get("value")
    if type(value) not in (float, int) or value not in (0, 1):
        return 0, "nonbinary_measurement"
    return int(value), None


def _population(
    name: str, expected: list[str], observed: dict[str, list[dict[str, Any]]]
) -> dict[str, Any]:
    n = high = 0
    reasons: set[str] = set()
    if len(expected) != len(set(expected)):
        reasons.add("duplicate_requested_sample")
    for sample in expected:
        matches = observed.get(sample, [])
        if len(matches) != 1:
            reasons.add("missing_or_duplicate_sample")
            continue
        bit, reason = _read_decision(matches[0], name)
        if reason:
            reasons.add(reason)
            continue
        n += 1
        high += bit
    return {
        "measurement": name,
        "n": n,
        "high": high,
        "fraction": high / n if n else None,
        "reasons": sorted(reasons),
    }


def _fit(
    sources: list[dict[str, Any]], populations: list[dict[str, Any]]
) -> tuple[float | None, str | None]:
    for population in populations:
        if population["reasons"]:
            return None, population["reasons"][0]
    lo, hi = [p["fraction"] for p in populations]
    if lo in (0, 1) or hi in (0, 1):
        return None, "saturated_fraction"
    if lo >= hi:
        return None, "non_increasing_slope"
    q = NormalDist().inv_cdf
    sigma = (sources[1]["stimulus"] - sources[0]["stimulus"]) / (q(hi) - q(lo))
    if not math.isfinite(sigma):
        return None, "nonfinite_sigma"
    return sigma, None


def rollup(
    stats: dict[str, Any] | None,
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Account for every requested draw, including those missing from the results."""
    spec = (stats or {}).get("probit_sigma")
    if spec is None:
        return []
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for corner in corners:
        observed[corner["corner_id"]].append(corner)
    expected: dict[str, list[str]] = defaultdict(list)
    for point in points:
        base = point.corner_id.removesuffix(f"/mc{point.sample_index}")
        expected[base].append(point.corner_id)
    fits: list[dict[str, Any]] = []
    for base, samples in sorted(expected.items()):
        populations = [
            _population(source["measurement"], samples, observed)
            for source in spec["sources"]
        ]
        sigma, reason = _fit(spec["sources"], populations)
        fits.append(
            {
                "corner_id": base,
                "expected": len(samples),
                "populations": populations,
                "sigma": sigma,
                "reason": reason,
            }
        )
    source = "grid" if spec["grid"] == "mean" else None
    value = None
    status = "inconclusive"
    worst_case = None
    if fits and all(fit["sigma"] is not None for fit in fits):
        if spec["grid"] == "mean":
            value = mean(fit["sigma"] for fit in fits)
        else:
            binding = max(fits, key=lambda fit: fit["sigma"])
            value, source = binding["sigma"], binding["corner_id"]
            worst_case = {
                "value": value,
                "corner_id": source,
                "margin": spec["limits"]["max"] - value,
            }
        status = "pass" if value <= spec["limits"]["max"] else "fail"
    return [
        {
            **spec,
            "statistic": "probit_sigma",
            **({"reason": "empty_corner_grid"} if not fits else {}),
            "by_corner": fits,
            "source": source,
            "value": value,
            "status": status,
            "worst_case": worst_case,
        }
    ]


def coverage(
    entries: list[dict[str, Any]], checked: list[str], skipped: list[dict[str, str]]
) -> None:
    for entry in entries:
        identifier = work_id("grid_limit", entry["name"], "max")
        if entry["value"] is None:
            skipped.append({"id": identifier, "reason": "undefined_grid_statistic"})
        else:
            checked.append(identifier)
