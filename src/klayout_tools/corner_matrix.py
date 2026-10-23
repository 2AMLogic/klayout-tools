"""Check a ``klt sim`` request or report against a declared corner matrix.

Pure library: :func:`check_corner_matrix` returns plain JSON-serialisable data
and never prints; serialisation lives in ``cli/corner_matrix_cmd.py``.

The split of responsibility (resolving the sim-evidence-discipline spike's
item D against survey gap G3): the *repo* commits the matrix file and owns
which matrix counts as "full" and any ``subset_reason`` policy; ``klt`` owns
only the mechanical diff of declared tuples against observed tuples. The
check reports, it does not judge.

Expected tuples are expanded by ``klt sim``'s own ``_expand_corners`` (axes
plus ``exclude[]`` matching) and identified by its ``CornerPoint.corner_id``,
so the matrix and a request can never disagree about what a tuple is called.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from . import sim
from ._paths import _load_request_json

#: The only top-level keys a matrix file may carry. Anything else is refused
#: rather than silently narrowing (or failing to narrow) the expected grid.
_AXIS_KEYS = ("process", "supply_v", "temperature_c", "params")
_MATRIX_KEYS = frozenset({*_AXIS_KEYS, "exclude", "schema", "description"})

#: Corner statuses that mean the unit produced numbers worth grading.
#: ``fail`` is a simulated corner whose measurement left its limits.
_SIMULATED_STATUSES = frozenset({"pass", "fail"})


class CornerMatrixError(Exception):
    """Raised when the check cannot even be attempted: an unreadable or
    malformed matrix/request/report file, or an unknown matrix key."""


def _load_object(path: str, what: str) -> dict[str, Any]:
    data = _load_request_json(path, CornerMatrixError)
    if not isinstance(data, dict):
        raise CornerMatrixError(f"{what} must be a JSON object: {path}")
    return data


def expected_tuples(matrix: dict[str, Any]) -> list[str]:
    """The matrix's expected ``corner_id`` list, in ``klt sim`` expansion order."""
    unknown = sorted(set(matrix) - _MATRIX_KEYS)
    if unknown:
        raise CornerMatrixError(
            f"unknown matrix key(s) {unknown}; allowed: {sorted(_MATRIX_KEYS)}"
        )
    exclude = matrix.get("exclude") or []
    if not isinstance(exclude, list) or not all(isinstance(e, dict) for e in exclude):
        raise CornerMatrixError("matrix.exclude must be a list of objects")
    axes = {key: matrix[key] for key in _AXIS_KEYS if key in matrix}
    try:
        points = sim._expand_corners(axes, exclude)
    except sim.SimError as exc:
        raise CornerMatrixError(f"matrix is not a valid corner grid: {exc}") from exc
    return [point.corner_id for point in points]


def request_tuples(request: dict[str, Any]) -> list[str]:
    """The ``corner_id`` list a ``klt sim`` request would dispatch."""
    try:
        points = sim._expand_corners(
            request.get("corners") or {}, request.get("exclude") or []
        )
    except sim.SimError as exc:
        raise CornerMatrixError(f"request corners are invalid: {exc}") from exc
    return [point.corner_id for point in points]


def _duplicates(ids: list[str]) -> list[str]:
    return sorted(cid for cid, n in Counter(ids).items() if n > 1)


def _measurement_nonfinite(corner: dict[str, Any]) -> bool:
    for measurement in corner.get("measurements") or []:
        value = measurement.get("value")
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and not math.isfinite(value):
            return True
        if value is None and measurement.get("status") != "error":
            # An absent value on a corner reported as simulated is not a
            # finite number; an errored measurement is already flagged by
            # the corner's own status.
            return True
    return False


def check_corner_matrix(
    matrix_path: str, *, request_path: str | None = None, report_path: str | None = None
) -> dict[str, Any]:
    """Diff a declared matrix against a request (conformance) or a sim report
    (completeness). Exactly one of ``request_path`` / ``report_path``."""
    if (request_path is None) == (report_path is None):
        raise CornerMatrixError("give exactly one of --request or --report")
    expected = expected_tuples(_load_object(matrix_path, "matrix"))
    expected_dupes = _duplicates(expected)
    if expected_dupes:
        raise CornerMatrixError(
            f"matrix expands to duplicate tuple(s): {expected_dupes}"
        )
    expected_set = set(expected)

    result: dict[str, Any] = {"matrix": matrix_path}
    if request_path is not None:
        observed = request_tuples(_load_object(request_path, "request"))
        mode, source = "request", request_path
        extra_checks: dict[str, list[str]] = {}
    else:
        assert report_path is not None
        report = _load_object(report_path, "report")
        corners = report.get("corners")
        if not isinstance(corners, list):
            raise CornerMatrixError(f"report has no corners[] array: {report_path}")
        observed = [str(c.get("corner_id")) for c in corners if isinstance(c, dict)]
        mode, source = "report", report_path
        by_id: dict[str, dict[str, Any]] = {}
        for corner in corners:
            if isinstance(corner, dict):
                by_id.setdefault(str(corner.get("corner_id")), corner)
        in_matrix = [cid for cid in expected if cid in by_id]
        extra_checks = {
            "not_simulated": [
                cid
                for cid in in_matrix
                if by_id[cid].get("status") not in _SIMULATED_STATUSES
            ],
            "non_finite": [
                cid
                for cid in in_matrix
                if by_id[cid].get("status") in _SIMULATED_STATUSES
                and _measurement_nonfinite(by_id[cid])
            ],
            "out_of_bounds": [
                cid for cid in in_matrix if by_id[cid].get("status") == "fail"
            ],
        }

    observed_set = set(observed)
    missing = [cid for cid in expected if cid not in observed_set]
    extra = sorted(observed_set - expected_set)
    duplicate = _duplicates(observed)
    complete = not (missing or extra or duplicate or any(extra_checks.values()))
    result.update(
        {
            "mode": mode,
            "source": source,
            "complete": complete,
            "matrix_size": len(expected),
            "observed_count": len(observed),
            "missing": missing,
            "extra": extra,
            "duplicate": duplicate,
            **extra_checks,
        }
    )
    return result
