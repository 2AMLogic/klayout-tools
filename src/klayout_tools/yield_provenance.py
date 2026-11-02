"""Run provenance and per-measurement ``spec_row`` helpers for ``klt yield``
(issue #2840).

Split out of :mod:`klayout_tools.yield_analysis` to keep that module under
its per-file size ceiling. Types are validated, values are never
interpreted: both fields are echoed onto the report, never graded.
"""

from __future__ import annotations

from typing import Any


def _yield_error(message: str) -> Exception:
    # Lazy import: yield_analysis imports this module at load time.
    from .yield_analysis import YieldError

    return YieldError(message)


def _spec_row_from(raw: Any, name: str, where: str) -> str | None:
    """Validate an optional per-measurement ``spec_row`` (issue #2840).

    Echoed verbatim on the report entry; never graded.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _yield_error(f"{where}: measurement '{name}' spec_row must be a string")
    return raw


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _provenance_from(raw: Any, where: str) -> dict[str, Any] | None:
    """Validate the optional run-level ``provenance`` block (issue #2840).

    Types are checked, values are never interpreted; the block is echoed
    verbatim (unknown keys included) into ``source.provenance``. Named
    ``provenance`` rather than ``campaign`` because ``klt yield-campaign``
    already emits a differently-shaped top-level ``campaign`` object.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _yield_error(f"{where}: 'provenance' must be an object")
    seed = raw.get("seed")
    if seed is not None and not _is_int(seed):
        raise _yield_error(f"{where}: provenance.seed must be an integer")
    rng = raw.get("rng")
    if rng is not None and not isinstance(rng, str):
        raise _yield_error(f"{where}: provenance.rng must be a string")
    draws = raw.get("draws_per_population")
    if draws is not None and (not _is_int(draws) or draws <= 0):
        raise _yield_error(
            f"{where}: provenance.draws_per_population must be a positive integer"
        )
    crn = raw.get("common_random_numbers")
    if crn is not None and not isinstance(crn, bool):
        raise _yield_error(
            f"{where}: provenance.common_random_numbers must be a boolean"
        )
    matrix = raw.get("corner_matrix")
    if matrix is not None and (
        not isinstance(matrix, list) or not all(isinstance(c, str) for c in matrix)
    ):
        raise _yield_error(
            f"{where}: provenance.corner_matrix must be an array of strings"
        )
    return dict(raw)


def _request_without_spec_rows(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strip ``spec_row`` from request measurements: it is Python-side
    provenance, echoed onto the report afterwards, and never reaches the
    native core (issue #2840).
    """
    return [{k: v for k, v in m.items() if k != "spec_row"} for m in entries]


def _attach_spec_rows(payload: dict[str, Any], row_by_name: dict[str, str]) -> None:
    """Echo each measurement's resolved ``spec_row`` onto its report entry
    (`null` where unset -- issue #2840).
    """
    for out_entry in payload.get("measurements") or []:
        if isinstance(out_entry, dict):
            name = out_entry.get("name")
            out_entry["spec_row"] = (
                row_by_name.get(name) if isinstance(name, str) else None
            )


def _spec_rows_for(
    entries: list[dict[str, Any]], limits_rows: dict[str, str]
) -> dict[str, str]:
    """Resolve each measurement's ``spec_row``: a ``--limits`` row wins over
    the sample document's own (issue #2840). Only measurements that produced
    a request entry appear here, and the result never reaches the native
    core -- it is echoed onto the report afterwards.
    """
    rows: dict[str, str] = {}
    for entry in entries:
        row = limits_rows.get(entry["name"], entry.get("spec_row"))
        if row is not None:
            rows[entry["name"]] = row
    return rows


def spec_rows_from_limits(raw: dict[str, Any], where: str) -> dict[str, str]:
    """Collect the ``spec_row`` of each entry of a limits file's measurement map."""
    rows: dict[str, str] = {}
    for name, value in raw.items():
        row = _spec_row_from(
            value.get("spec_row") if isinstance(value, dict) else None, name, where
        )
        if row is not None:
            rows[name] = row
    return rows


def nominal_provenance(nominal_report: dict[str, Any]) -> dict[str, Any] | None:
    """Lift the nominal campaign's well-typed RNG seed / draw count so the seed
    survives the two-step ``klt yield-samples`` -> ``klt yield`` flow.
    """
    env = nominal_report.get("environment")
    mc = env.get("monte_carlo") if isinstance(env, dict) else None
    if not isinstance(mc, dict):
        return None
    prov: dict[str, Any] = {}
    if _is_int(mc.get("seed")):
        prov["seed"] = mc["seed"]
    if _is_int(mc.get("n")) and mc["n"] > 0:
        prov["draws_per_population"] = mc["n"]
    return prov or None
