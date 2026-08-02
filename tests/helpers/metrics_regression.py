"""Golden-metrics regression helper (issue #248).

The geometry-level regression tests (`tests/test_corpus.py`, `tests/
test_render_corpus.py`) compare a `klt` verb's *full* JSON/GDS output against
a byte-for-byte golden fixture. That is the right bar for output that must
not drift at all, but it is the wrong bar for a *scalar metric* -- device
count, cell/instance count, DRC violation count -- where the interesting
regression is "did this number move outside an acceptable band", not "is the
entire response identical".

This module is that narrower, tolerance-based comparator:

- :func:`flatten_metrics` turns a `klt` verb's own JSON response (already a
  plain dict of JSON-serialisable primitives -- see `docs/json-contract.md`)
  into a flat ``{"dotted.key": <int | float>}`` mapping, dropping non-numeric
  fields (paths, hashes, status strings) that either aren't metrics or would
  make a golden fixture spuriously environment-/run-dependent.
- :func:`load_golden_metrics` reads a golden-metrics JSON file (`tests/
  golden_metrics/*.json`, see that directory's `README.md`).
- :func:`diff_metrics` / :func:`assert_metrics_match` compare a flattened
  metrics dict against the golden mapping, honouring each metric's
  `tol_pct` tolerance band, and fail loudly (not silently) on schema drift
  in either direction: a golden metric missing from the actual output, or
  an actual metric the golden file does not declare.

This module runs no `klt` verbs itself -- callers (test fixtures) build the
metrics dict from whichever verbs are relevant to their own scenario, then
hand it to :func:`assert_metrics_match`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Number = int | float

__all__ = [
    "MetricsMismatchError",
    "flatten_metrics",
    "load_golden_metrics",
    "diff_metrics",
    "assert_metrics_match",
]


class MetricsMismatchError(AssertionError):
    """Raised by :func:`assert_metrics_match` when metrics drift from golden."""


def flatten_metrics(data: dict[str, Any], prefix: str = "") -> dict[str, Number]:
    """Flatten a nested JSON-shaped dict to a flat ``{dotted.key: number}`` map.

    Rules (deliberately conservative -- only numbers that are meaningful,
    stable "metrics" survive):

    - A nested ``dict`` is recursed into, joining keys with ``.`` (e.g.
      ``{"drc": {"violation_count": 0}}`` -> ``{"drc.violation_count": 0}``).
    - A ``list`` is **not** recursed into element-by-element -- its elements
      are typically per-item objects (a device, a net, a violation), not
      metrics in their own right -- instead its length is recorded as
      ``"<key>_count"`` (e.g. a 5-element ``devices`` list becomes
      ``"devices_count": 5``).
    - ``bool`` values are dropped (``bool`` is an ``int`` subclass in Python,
      but ``True``/``False`` are not metrics).
    - ``int``/``float`` values are kept as-is.
    - Every other type (``str``, ``None``, ...) is dropped -- paths,
      hashes, and status strings are not numeric metrics, and pinning them
      would make a golden fixture spuriously environment-/run-dependent
      (e.g. a `netlist_sha256` embeds an absolute tmp-path-derived comment).
    """
    out: dict[str, Number] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_metrics(value, full_key))
        elif isinstance(value, list):
            out[f"{full_key}_count"] = len(value)
        elif isinstance(value, bool):
            continue
        elif isinstance(value, int | float):
            out[full_key] = value
        # str / None / other scalar types: not a numeric metric, skip.
    return out


def load_golden_metrics(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load a golden-metrics JSON file's ``metrics`` mapping.

    Each entry is normalized to ``{"value": <number>, "tol_pct": <number>}``:
    a bare number (e.g. ``"device_count": 5``) is treated as
    ``tol_pct: 0`` (exact match); an object form (e.g.
    ``{"value": 812.4, "tol_pct": 5}``) is used as given.
    """
    raw_doc = json.loads(Path(path).read_text())
    raw = raw_doc.get("metrics", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: 'metrics' must be an object")

    normalized: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        if isinstance(entry, dict):
            if "value" not in entry:
                raise ValueError(f"{path}: metric {key!r} is missing 'value'")
            normalized[key] = {
                "value": entry["value"],
                "tol_pct": entry.get("tol_pct", 0),
            }
        else:
            normalized[key] = {"value": entry, "tol_pct": 0}
    return normalized


def diff_metrics(
    golden: dict[str, dict[str, Any]], actual: dict[str, Number]
) -> list[str]:
    """Compare ``actual`` (from :func:`flatten_metrics`) against ``golden``.

    Returns a list of human-readable diff descriptions -- empty when every
    golden metric is present in ``actual`` within tolerance, and ``actual``
    carries no metric absent from ``golden``. Both directions of schema
    drift are reported (never silently ignored):

    - a metric ``golden`` declares but ``actual`` does not produce, and
    - a metric ``actual`` produces but ``golden`` does not declare.

    Tolerance comparison is a **percent-of-golden-value** band,
    ``abs(actual - golden) / abs(golden) * 100``, and the boundary is
    **inclusive**: a value exactly ``tol_pct`` away from golden passes. A
    ``tol_pct`` of ``0`` (the default when a golden entry is a bare number)
    means exact match. A golden value of exactly ``0`` with a non-zero
    ``tol_pct`` falls back to an absolute-value comparison against
    ``tol_pct`` (percent-of-zero is undefined) -- i.e. ``abs(actual) <=
    tol_pct`` -- since a metric golden at zero (e.g. a clean DRC run) is a
    common, meaningful case this helper must not divide-by-zero on.
    """
    diffs: list[str] = []
    golden_keys = set(golden)
    actual_keys = set(actual)

    for key in sorted(golden_keys - actual_keys):
        diffs.append(
            f"missing metric {key!r}: golden file expects it, "
            "actual output has no such key"
        )

    for key in sorted(actual_keys - golden_keys):
        diffs.append(
            f"unexpected metric {key!r} = {actual[key]!r}: present in actual "
            "output but not declared in the golden file (schema drift -- add "
            "it to the golden file deliberately if this is intentional)"
        )

    for key in sorted(golden_keys & actual_keys):
        expected = golden[key]["value"]
        tol_pct = golden[key]["tol_pct"]
        got = actual[key]

        if not tol_pct:
            if got != expected:
                diffs.append(
                    f"metric {key!r} mismatch: golden={expected!r} actual={got!r}"
                )
            continue

        if expected:
            pct_off = abs(got - expected) / abs(expected) * 100
            if pct_off > tol_pct:
                diffs.append(
                    f"metric {key!r} out of tolerance: golden={expected!r} "
                    f"actual={got!r} ({pct_off:.2f}% off, tol_pct={tol_pct})"
                )
        else:
            # golden==0: percent-of-zero is undefined -- tol_pct is instead
            # the absolute allowance around zero (see docstring).
            if abs(got) > tol_pct:
                diffs.append(
                    f"metric {key!r} out of tolerance: golden={expected!r} "
                    f"actual={got!r} (exceeds absolute tol_pct={tol_pct} "
                    "allowance used when golden value is 0)"
                )

    return diffs


def assert_metrics_match(golden_path: str | Path, actual: dict[str, Number]) -> None:
    """Raise :class:`MetricsMismatchError` if ``actual`` drifts from golden.

    ``actual`` is a flat metrics dict, typically :func:`flatten_metrics`'s
    output (merged across however many `klt` verbs the caller's fixture
    runs). Regenerate the golden file deliberately (see `tests/
    golden_metrics/README.md`) rather than editing this comparison to make
    a real regression pass.
    """
    golden = load_golden_metrics(golden_path)
    diffs = diff_metrics(golden, actual)
    if diffs:
        bullet_list = "\n  - ".join(diffs)
        raise MetricsMismatchError(
            f"golden-metrics regression against {golden_path}:\n  - {bullet_list}"
        )
