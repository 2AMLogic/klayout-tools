"""``klt sim-merge``: recombine per-request ``klt sim`` reports (issue #3045).

A corner grid that cannot be expressed as one request (the executing runner
is older than the client, say) is submitted as several requests -- typically
one per process corner -- and the reports have to be recombined. That
recombination is easy to get subtly wrong (copying shard 1's per-measurement
rollup describes only shard 1's corners), so this module does it with the
tool's own rollup code instead of leaving every consumer to re-implement it:

* ``corners[]`` is the concatenation of the shards' corners in manifest order;
* ``measurements[]`` (status + ``worst_case``), ``coverage``, the
  ``corner_count``/``passed``/``failed``/``errored``/``inconclusive``
  counters, ``metrics``, ``diagnostic_counts`` and the report ``status`` are
  *recomputed* over that union by the helpers :func:`klayout_tools.sim.run_sim`
  itself uses (``_rollup_measurements``, ``_build_coverage``,
  ``_grade_corners``, ``_diagnostic_counts``) -- ``coverage.checked`` ids
  carry corner indices local to one report, so the recomputation re-bases
  them;
* fields that describe one report (``environment.netlist_sha256``,
  ``environment.remote``, ``provenance.input.content_hash``, and any other
  ``environment``/``provenance`` field whose value differs between shards)
  become per-shard arrays aligned with ``merge.shards`` rather than being left
  standing as if they described the union.

The inputs are the shard reports plus a *manifest* of the planned requests
(``docs/cli/sim.md`` "Merging per-request reports"). An incomplete or
inconsistent set is refused with a distinct :class:`MergeError` code per
failure class; nothing is merged from a set that is not exactly the planned
one.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

from . import sim
from ._provenance import sha256_file

MERGE_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

#: Refusal classes and their ``klt sim-merge`` exit codes. ``1`` (could not
#: run: unreadable/invalid input) and ``2`` (usage) follow the house
#: convention; ``3``/``4`` are reserved for the *merged* report's verdict, as
#: for ``klt sim``; the refusals take the next distinct codes.
EXIT_MISSING_SHARD = 5
EXIT_DUPLICATE_SHARD = 6
EXIT_UNEXPECTED_SHARD = 7
EXIT_CORNER_MISMATCH = 8
EXIT_REQUEST_MISMATCH = 9
EXIT_INCONSISTENT_SHARDS = 10

CODE_MISSING_SHARD = "missing_shard"
CODE_DUPLICATE_SHARD = "duplicate_shard"
CODE_UNEXPECTED_SHARD = "unexpected_shard"
CODE_CORNER_MISMATCH = "corner_mismatch"
CODE_REQUEST_MISMATCH = "request_mismatch"
CODE_INCONSISTENT_SHARDS = "inconsistent_shards"

_EXIT_BY_CODE = {
    CODE_MISSING_SHARD: EXIT_MISSING_SHARD,
    CODE_DUPLICATE_SHARD: EXIT_DUPLICATE_SHARD,
    CODE_UNEXPECTED_SHARD: EXIT_UNEXPECTED_SHARD,
    CODE_CORNER_MISMATCH: EXIT_CORNER_MISMATCH,
    CODE_REQUEST_MISMATCH: EXIT_REQUEST_MISMATCH,
    CODE_INCONSISTENT_SHARDS: EXIT_INCONSISTENT_SHARDS,
}

#: When several refusal classes apply at once the exit code is the first
#: class in this order (the message still lists every problem found).
_REFUSAL_PRIORITY = (
    CODE_REQUEST_MISMATCH,
    CODE_CORNER_MISMATCH,
    CODE_UNEXPECTED_SHARD,
    CODE_DUPLICATE_SHARD,
    CODE_MISSING_SHARD,
    CODE_INCONSISTENT_SHARDS,
)

#: ``environment`` fields that always become per-shard arrays, even when every
#: shard happens to agree (or only one shard has the field).
_PER_REPORT_ENVIRONMENT = ("netlist_sha256", "remote")

#: Per-measurement fields a shard must agree on for the union to be one grid.
_SPEC_FIELDS = ("unit", "limits", "plausible_range", "step", "derived_from")


class MergeError(Exception):
    """A merge that could not run (``exit_code`` 1) or was refused (5-10)."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = _EXIT_BY_CODE.get(code, 1) if code else 1


def _load_json(path: str, what: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise MergeError(f"{what} not found: {path}") from None
    except (OSError, ValueError) as exc:
        raise MergeError(f"{what} is not readable JSON ({path}): {exc}") from None
    if not isinstance(data, dict):
        raise MergeError(f"{what} must be a JSON object: {path}")
    return data


def _validate_shard(index: int, shard: Any) -> str:
    """Validate one manifest ``shards[]`` entry; return its id."""
    if not isinstance(shard, dict):
        raise MergeError(f"manifest shards[{index}] must be an object")
    shard_id = shard.get("id")
    if not isinstance(shard_id, str) or not shard_id:
        raise MergeError(f"manifest shards[{index}].id must be a non-empty string")
    corners = shard.get("corners")
    if (
        not isinstance(corners, list)
        or not corners
        or not all(isinstance(c, str) for c in corners)
    ):
        raise MergeError(
            f"manifest shard {shard_id!r}: 'corners' must be a non-empty "
            "list of corner ids"
        )
    for key in ("request", "request_sha256"):
        if key in shard and not isinstance(shard[key], str):
            raise MergeError(f"manifest shard {shard_id!r}: {key!r} must be a string")
    if "request_sha256" in shard and "request" not in shard:
        raise MergeError(
            f"manifest shard {shard_id!r}: 'request_sha256' needs 'request'"
        )
    return shard_id


def load_manifest(path: str) -> dict[str, Any]:
    """Read and validate a shard manifest (see ``docs/cli/sim.md``)."""
    manifest = _load_json(path, "manifest")
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise MergeError(
            f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}, "
            f"got {manifest.get('schema_version')!r}"
        )
    shards = manifest.get("shards")
    if not isinstance(shards, list) or not shards:
        raise MergeError("manifest 'shards' must be a non-empty list")
    ids: set[str] = set()
    signatures: dict[tuple[str, ...], str] = {}
    for index, shard in enumerate(shards):
        shard_id = _validate_shard(index, shard)
        if shard_id in ids:
            raise MergeError(f"manifest shard id {shard_id!r} is listed twice")
        ids.add(shard_id)
        twin = signatures.setdefault(tuple(shard["corners"]), shard_id)
        if twin != shard_id:
            raise MergeError(
                f"manifest shards {twin!r} and {shard_id!r} plan the same corner "
                "list, so a report cannot be matched to one of them"
            )
    return manifest


def _check_requests(manifest: dict[str, Any], manifest_path: str) -> list[str]:
    """Problems with the planned request files (``request_sha256`` pins)."""
    base = os.path.dirname(os.path.abspath(manifest_path))
    problems: list[str] = []
    for shard in manifest["shards"]:
        pinned = shard.get("request_sha256")
        if pinned is None:
            continue
        try:
            actual = sha256_file(os.path.join(base, shard["request"]))
        except OSError as exc:
            actual = None
            detail = exc.strerror or str(exc)
        else:
            detail = "not an existing file"
        if actual is None:
            problems.append(
                f"shard {shard['id']!r}: planned request unreadable "
                f"({shard['request']}): {detail}"
            )
            continue
        if actual != pinned:
            problems.append(
                f"shard {shard['id']!r}: request {shard['request']} has sha256 "
                f"{actual}, but the plan pinned {pinned} -- the submitted request "
                "is not the planned one"
            )
    return problems


def _report_corner_ids(report: dict[str, Any], path: str) -> list[str]:
    corners = report.get("corners")
    if report.get("mode") == "decks":
        raise MergeError(f"{path}: deck-list reports are not mergeable")
    if not isinstance(corners, list) or not all(
        isinstance(c, dict) and isinstance(c.get("corner_id"), str) for c in corners
    ):
        raise MergeError(f"{path}: not a klt sim report (no corners[] list)")
    if (report.get("environment") or {}).get("monte_carlo") is not None:
        raise MergeError(
            f"{path}: a Monte Carlo report cannot be merged -- sample seeds and "
            "per-sample statistics depend on the whole sample sequence"
        )
    return [c["corner_id"] for c in corners]


def _assign_reports(
    manifest: dict[str, Any], report_paths: list[str]
) -> tuple[list[dict[str, Any] | None], dict[str, list[str]]]:
    """Match each report to the planned shard whose corner list it carries.

    Returns the report per shard (``None`` where missing) and the problems,
    grouped by refusal code.
    """
    shards = manifest["shards"]
    by_signature = {tuple(s["corners"]): i for i, s in enumerate(shards)}
    planned_ids = {c for s in shards for c in s["corners"]}
    assigned: list[dict[str, Any] | None] = [None] * len(shards)
    assigned_path: list[str | None] = [None] * len(shards)
    problems: dict[str, list[str]] = {}

    def note(code: str, message: str) -> None:
        problems.setdefault(code, []).append(message)

    for path in report_paths:
        report = _load_json(path, "report")
        ids = _report_corner_ids(report, path)
        index = by_signature.get(tuple(ids))
        if index is None:
            if planned_ids & set(ids):
                note(
                    CODE_CORNER_MISMATCH,
                    f"{path}: corners {ids} match no planned shard "
                    "(it overlaps the plan but is not exactly one shard's corners)",
                )
            else:
                note(
                    CODE_UNEXPECTED_SHARD,
                    f"{path}: corners {ids} are not part of the plan",
                )
        elif assigned[index] is not None:
            note(
                CODE_DUPLICATE_SHARD,
                f"{path}: second report for shard {shards[index]['id']!r} "
                f"(first: {assigned_path[index]})",
            )
        else:
            assigned[index] = report
            assigned_path[index] = path
    for shard, matched in zip(shards, assigned, strict=True):
        if matched is None:
            note(CODE_MISSING_SHARD, f"no report for shard {shard['id']!r}")
    return assigned, problems


def _measurement_specs(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild the request's ``measurements_spec`` from a report's rollup."""
    specs: list[dict[str, Any]] = []
    for entry in report.get("measurements") or []:
        spec: dict[str, Any] = {"name": entry["name"]}
        for key in _SPEC_FIELDS:
            if key in entry:
                spec[key] = entry[key]
        spec.setdefault("unit", None)
        spec.setdefault("limits", None)
        specs.append(spec)
    return specs


def _union(values: list[Any], *, force_list: bool = False) -> Any:
    """One value when every shard agrees, else the per-shard array."""
    if not force_list and all(v == values[0] for v in values):
        return copy.deepcopy(values[0])
    return copy.deepcopy(values)


def _union_blocks(
    blocks: list[dict[str, Any]], *, per_report: tuple[str, ...] = ()
) -> dict[str, Any]:
    keys: list[str] = []
    for block in blocks:
        keys.extend(k for k in block if k not in keys)
    merged: dict[str, Any] = {}
    for key in keys:
        values = [block.get(key) for block in blocks]
        if key == "input" and all(isinstance(v, dict) or v is None for v in values):
            merged[key] = (
                None
                if all(v is None for v in values)
                else _union_blocks(
                    [v or {} for v in values], per_report=("content_hash",)
                )
            )
        else:
            merged[key] = _union(values, force_list=key in per_report)
    return merged


def _check_consistent(
    manifest: dict[str, Any], reports: list[dict[str, Any]]
) -> list[str]:
    first_specs = _measurement_specs(reports[0])
    first_id = manifest["shards"][0]["id"]
    problems: list[str] = []
    for shard, report in zip(manifest["shards"], reports, strict=True):
        if report.get("schema_version") != reports[0].get("schema_version"):
            problems.append(
                f"shard {shard['id']!r}: schema_version "
                f"{report.get('schema_version')!r} differs from "
                f"{reports[0].get('schema_version')!r}"
            )
        if _measurement_specs(report) != first_specs:
            problems.append(
                f"shard {shard['id']!r}: measurement names, units or limits "
                f"differ from shard {first_id!r}"
            )
        if report.get("netlist") != reports[0].get("netlist"):
            problems.append(
                f"shard {shard['id']!r}: simulated netlist {report.get('netlist')!r} "
                f"differs from {reports[0].get('netlist')!r}"
            )
    return problems


def merge_reports(manifest_path: str, report_paths: list[str]) -> dict[str, Any]:
    """Merge per-request ``klt sim`` reports into the single-request report.

    Raises :class:`MergeError` -- with a refusal ``code`` when the set is not
    exactly the planned one -- and otherwise returns the merged report (a
    ``klt sim`` report plus an additive ``merge`` block naming the shards).
    """
    manifest = load_manifest(manifest_path)
    problems: dict[str, list[str]] = {}
    request_problems = _check_requests(manifest, manifest_path)
    if request_problems:
        problems[CODE_REQUEST_MISMATCH] = request_problems
    assigned, report_problems = _assign_reports(manifest, report_paths)
    for code, messages in report_problems.items():
        problems.setdefault(code, []).extend(messages)
    if problems:
        code = next(c for c in _REFUSAL_PRIORITY if c in problems)
        raise MergeError(
            "refusing to merge: "
            + "; ".join(m for c in _REFUSAL_PRIORITY for m in problems.get(c, [])),
            code=code,
        )

    reports = [r for r in assigned if r is not None]
    inconsistent = _check_consistent(manifest, reports)
    if inconsistent:
        raise MergeError(
            "refusing to merge: " + "; ".join(inconsistent),
            code=CODE_INCONSISTENT_SHARDS,
        )
    return _merge(manifest, reports)


def _merge(manifest: dict[str, Any], reports: list[dict[str, Any]]) -> dict[str, Any]:
    corners = [copy.deepcopy(c) for r in reports for c in r["corners"]]
    specs = _measurement_specs(reports[0])

    measurements = sim._rollup_measurements(specs, corners, None)
    coverage = sim._build_coverage(specs, corners)
    graded = sim._grade_corners(corners, measurements, coverage)

    merged: dict[str, Any] = copy.deepcopy(reports[0])
    merged.update(
        {
            "status": graded["status"],
            "corner_count": len(corners),
            "passed": graded["passed"],
            "failed": graded["failed"],
            "errored": graded["errored"],
            "inconclusive": graded["inconclusive"],
            "diagnostic_counts": sim._diagnostic_counts(corners),
            "metrics": {
                sim._CORNER_COUNT_METRIC_NAME: len(corners),
                sim._CORNER_PASSED_COUNT_METRIC_NAME: graded["passed"],
                sim._CORNER_FAILED_COUNT_METRIC_NAME: graded["failed"],
                sim._CORNER_ERRORED_COUNT_METRIC_NAME: graded["errored"],
                sim._CORNER_INCONCLUSIVE_COUNT_METRIC_NAME: graded["inconclusive"],
            },
            "environment": _union_blocks(
                [r.get("environment") or {} for r in reports],
                per_report=_PER_REPORT_ENVIRONMENT,
            ),
            "coverage": coverage,
            "measurements": measurements,
            "corners": corners,
        }
    )
    provenances = [
        r["provenance"] for r in reports if isinstance(r.get("provenance"), dict)
    ]
    if len(provenances) == len(reports):
        merged["provenance"] = _union_blocks(provenances)
    if any("plots" in r for r in reports):
        merged["plots"] = [p for r in reports for p in r.get("plots") or []]
    merged["merge"] = {
        "schema_version": MERGE_SCHEMA_VERSION,
        "shard_count": len(reports),
        "shards": [
            {
                "id": shard["id"],
                "request": shard.get("request"),
                "request_sha256": shard.get("request_sha256"),
                "corner_count": len(report["corners"]),
            }
            for shard, report in zip(manifest["shards"], reports, strict=True)
        ],
    }
    return merged
