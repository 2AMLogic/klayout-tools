"""Item 8 (characterization report) envelope handling (issues #5688, #6939, #7259).

Split out of :mod:`klayout_tools.signoff_attest` (file-size ratchet). Item 8
documents ``citations`` as paths to *evidence envelopes*, so each cited file is
checked to be one: it must parse as JSON and be accepted by the same
recogniser ``--manifest`` grading applies to a citation
(:func:`klayout_tools.signoff._classify`), so the two agree on what "evidence
envelope" means. Recognition only -- the envelope's own verdict and any kind
restriction stay with the caller's ``envelope_checks`` command.

Issue #6939 adds the one deliberate exception: a committed record a flow
klt did not run (an ORFS result collect, a vendor tool report, a lab
measurement sheet) is cited through ``external_records`` -- an explicit,
per-path opt-out of recognition. A listed path's ``envelope`` check records
``kind: "external"`` and passes on presence; an *unlisted* non-klt citation
still fails exactly as before, so the #5688 guard survives. klt records the
record's hash and runs the caller's freshness command; it did not produce
the numbers and does not verify them. This module also owns the digital
metric bindings (``corners`` / ``metrics``), including the optional
``"transform": "reciprocal"`` pair that binds a quantity carried as a
period, so the frozen :mod:`signoff_attest` module stays at its size
baseline: ``AttestationError`` and the repo-path normalizer arrive as
callables, the way :mod:`signoff_attest_pins` takes its helpers (it cannot
import :mod:`signoff_attest` -- that module imports this).

Issue #7259 adds the declared gap: ``unmeasured_metrics`` names, per
``{corner, metric, reason}``, a required metric nobody measured, so a
partial characterization binds what it did measure instead of choosing
between a red attestation and a metrics-free green one that binds nothing.
The pair's slot in the check list holds a passing ``unmeasured_metric``
disclosure in place of the ``metric`` check that would have failed -- item
9's ``unbenched_rows`` is the precedent. The item's key allowlist lives here
too (:data:`ITEM8_KEYS`), the way item 9's lives in its module.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence, Set
from typing import Any

from . import signoff_attest_gaprows as gaprows
from ._jsonnum import is_finite_number, is_number
from .signoff import _classify, _classify_failure_reason
from .signoff_attest_pins import json_node
from .signoff_base import SignoffError, _read_envelope
from .signoff_drc_crosscheck import crosscheck_project

#: Item 8's accepted inventory keys (``signoff_attest._ITEM_KEYS[8]``).
ITEM8_KEYS = frozenset(
    {
        "report",
        "spec_rows",
        "citations",
        "envelope_checks",
        "external_records",
        "corners",
        "metrics",
        "unmeasured_metrics",
        "uncited_rows",
    }
)

#: The keys one ``unmeasured_metrics`` entry carries, and no others.
_UNMEASURED_KEYS = frozenset({"corner", "metric", "reason"})

#: A metric-binding transform: ``value = scale / raw``, so a record carrying
#: a minimum period binds the frequency metric (ns -> MHz with ``scale`` of
#: ``1000``). A closed set of one -- free-form expressions would execute
#: caller text.
_TRANSFORMS = ("reciprocal",)


def _recognition(root: str, path: str) -> tuple[str | None, str | None]:
    """``(kind, reason)`` for ``root/path``.

    ``kind`` is the envelope kind, or ``None`` when the file cannot be read
    (missing, directory, bad gzip), is not valid JSON or carries nonfinite
    literals, is not a JSON object, or is not a recognised envelope. The read
    goes through :func:`klayout_tools.signoff._read_envelope` -- the same
    gzip-transparent, nonfinite-refusing reader ``--manifest`` grading applies
    to a citation -- so the two agree on which committed forms are evidence
    (issue #5452).

    ``reason`` (issue #5986) is the diagnosis ``--manifest`` grading gives a
    document that *did* parse as JSON and was refused: ``"unrecognized_envelope"``
    (not an object, matches no shape, or malformed for one) or
    ``"envelope_version_skew"`` (a response from an older klt -- the remedy is
    to re-run the check, not to cite another file), via the same
    :func:`klayout_tools.signoff._classify_failure_reason`. It is ``None`` for
    a recognised envelope and for an unreadable one, which stays reasonless.

    Issue #6809: a cross-check envelope is projected to its single-engine
    equivalent before classification, exactly as ``--manifest`` grading does
    (issue #5815), so a whole ``--engine both`` citation is recognised ``drc``
    here too. Anything the projector refuses stays as-is and fails
    recognition below, still carrying the appropriate reason."""
    try:
        envelope = _read_envelope(os.path.join(root, path))
    except SignoffError:
        return None, None
    try:
        if not isinstance(envelope, Mapping):
            raise SignoffError(f"envelope '{path}' is not a JSON object")
        envelope = crosscheck_project(envelope) or envelope
        return _classify(envelope, path), None
    except SignoffError as exc:
        return None, _classify_failure_reason(exc)


def evidence_checks(
    root: str,
    citations: Mapping[str, Sequence[str]],
    file_check: Callable[[str, str, str], dict[str, Any]],
    external: Set[str] = frozenset(),
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """``(cited, present, envelope)``: the sorted cited paths, and per path the
    existence check and the recognition check (role ``evidence``, ``kind``
    ``None`` on failure, plus the additive ``reason`` when a parsed document
    was refused). A passing recognition check implies the file exists, so the
    spec-row check needs only it. A path in ``external`` (issue #6939) skips
    recognition: its ``envelope`` check records ``kind: "external"`` and
    passes on presence alone, so a committed non-klt record is citable while
    an unrecognised file cited by accident still fails (#5688)."""
    cited = sorted({path for paths in citations.values() for path in paths})
    present = {path: file_check(root, path, "evidence") for path in cited}
    envelopes = {}
    for path in cited:
        if path in external:
            envelopes[path] = {
                "check": "envelope",
                "role": "evidence",
                "path": path,
                "kind": "external",
                "passed": present[path]["passed"],
            }
            continue
        kind, reason = _recognition(root, path)
        envelopes[path] = {
            "check": "envelope",
            "role": "evidence",
            "path": path,
            "kind": kind,
            "passed": kind is not None,
            **({"reason": reason} if reason is not None else {}),
        }
    return cited, present, envelopes


def validated_external_records(
    inventory: Mapping[str, Any],
    cited: Sequence[str],
    root: str,
    error: Callable[[str], Exception],
    normalize_path: Callable[[Any, str, str], str],
) -> list[str]:
    """Item 8's optional ``external_records`` (issue #6939): the sorted,
    normalised repo paths of committed records produced outside klt. Each
    listed path must be cited by at least one spec row -- the same refusal an
    ``envelope_checks`` entry naming an uncited path gets -- and the ordinary
    repo-path rules apply through ``normalize_path`` (absolute, ``..`` and
    symlinked paths are refused). Empty, or a non-list / non-string member,
    is a malformed inventory."""
    raw = inventory.get("external_records")
    if raw is None:
        return []
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(path, str) and path for path in raw)
    ):
        raise error(
            "'external_records' must be a non-empty list of repo-relative paths"
        )
    external = sorted({normalize_path(path, "external_records", root) for path in raw})
    uncited = [path for path in external if path not in set(cited)]
    if uncited:
        raise error(
            "'external_records' names path(s) no spec row cites: " + ", ".join(uncited)
        )
    return external


def validated_metrics(
    inventory: Mapping[str, Any],
    root: str,
    error: Callable[[str], Exception],
    normalize_path: Callable[[Any, str, str], str],
    required_metrics: Sequence[str],
) -> tuple[list[str], dict[str, dict[str, dict[str, Any]]]]:
    """``(corners, metrics)`` for a digital partition; ``([], {})`` for an
    analog one (neither key declared). ``metrics[corner][name]`` binds a
    required metric to ``{"envelope": path, "field": dotted.path}``, with the
    optional ``{"transform": "reciprocal", "scale": n}`` pair of issue #6939
    (``value = scale / raw``)."""
    has_corners, has_metrics = "corners" in inventory, "metrics" in inventory
    if has_corners != has_metrics:
        raise error(
            "'corners' and 'metrics' go together (a digital partition's corner "
            "set and the metric bindings for it)"
        )
    if not has_corners:
        return [], {}
    corners = inventory["corners"]
    if (
        not isinstance(corners, list)
        or not corners
        or not all(isinstance(c, str) and c for c in corners)
        or len(set(corners)) != len(corners)
    ):
        raise error("'corners' must be a non-empty list of unique names")
    raw = inventory["metrics"]
    if not isinstance(raw, Mapping):
        raise error("'metrics' must map corner -> metric bindings")
    unknown = sorted(set(raw) - set(corners))
    if unknown:
        raise error(f"'metrics' names corner(s) not in 'corners': {', '.join(unknown)}")
    metrics = {
        corner: _validated_corner_metrics(
            corner, raw.get(corner, {}), root, error, normalize_path, required_metrics
        )
        for corner in corners
    }
    return list(corners), metrics


def validated_unmeasured_metrics(
    inventory: Mapping[str, Any],
    corners: Sequence[str],
    metrics: Mapping[str, Mapping[str, Any]],
    error: Callable[[str], Exception],
    required_metrics: Sequence[str],
) -> dict[tuple[str, str], str]:
    """Item 8's optional ``unmeasured_metrics`` (issue #7259), as the
    ``(corner, metric) -> reason`` lookup :func:`metric_checks` consults:
    ``[{corner, metric, reason}]``, each naming one declared corner and one
    required metric that has **no** binding. The list belongs to the digital
    block -- declaring it without ``corners``/``metrics`` is refused, there
    being no corner set to check names against -- and so is a pair
    ``metrics`` also binds: measured and unmeasured is a contradiction, not
    a choice for klt. A duplicate pair, an unknown key or name, and a blank
    reason are a malformed inventory like any other."""
    raw = inventory.get("unmeasured_metrics")
    if raw is None:
        return {}
    if not corners:
        raise error(
            "'unmeasured_metrics' declares a gap in a digital partition's "
            "corner block -- declare 'corners' and 'metrics' too"
        )
    if not isinstance(raw, list):
        raise error("'unmeasured_metrics' must be a list of {corner, metric, reason}")
    declared: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(raw):
        field = f"unmeasured_metrics[{index}]"
        corner, name, reason = _unmeasured_entry(
            field, entry, corners, required_metrics, error
        )
        if name in metrics.get(corner, {}):
            raise error(
                f"'{field}' declares {corner}/{name} unmeasured, but 'metrics' "
                "binds it -- a pair is measured or unmeasured, not both"
            )
        if (corner, name) in declared:
            raise error(
                f"unmeasured metric '{corner}/{name}' is declared more than once"
            )
        declared[(corner, name)] = reason
    return declared


def _unmeasured_entry(
    field: str,
    entry: Any,
    corners: Sequence[str],
    required_metrics: Sequence[str],
    error: Callable[[str], Exception],
) -> tuple[str, str, str]:
    """``(corner, metric, reason)`` of one well-formed declaration."""
    if not isinstance(entry, Mapping) or set(entry) != _UNMEASURED_KEYS:
        raise error(f"'{field}' must be an object with 'corner', 'metric' and 'reason'")
    corner, name, reason = entry["corner"], entry["metric"], entry["reason"]
    if not isinstance(corner, str) or corner not in corners:
        raise error(f"'{field}.corner' must be one of 'corners': {', '.join(corners)}")
    if not isinstance(name, str) or name not in required_metrics:
        raise error(f"'{field}.metric' must be one of: {', '.join(required_metrics)}")
    if not isinstance(reason, str) or not reason.strip():
        raise error(f"'{field}.reason' must be a non-empty string")
    return corner, name, reason


def _validated_corner_metrics(
    corner: str,
    bound: Any,
    root: str,
    error: Callable[[str], Exception],
    normalize_path: Callable[[Any, str, str], str],
    required_metrics: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if not isinstance(bound, Mapping):
        raise error(f"'metrics.{corner}' must be an object")
    unknown = sorted(set(bound) - set(required_metrics))
    if unknown:
        raise error(
            f"'metrics.{corner}' has unknown metric(s): {', '.join(unknown)} "
            f"(accepted: {', '.join(required_metrics)})"
        )
    result: dict[str, dict[str, Any]] = {}
    for name, binding in bound.items():
        field = f"metrics.{corner}.{name}"
        if (
            not isinstance(binding, Mapping)
            or not {"envelope", "field"} <= set(binding)
            or set(binding) - {"envelope", "field", "transform", "scale"}
            or not isinstance(binding["field"], str)
            or not binding["field"]
        ):
            raise error(
                f'\'{field}\' must be {{"envelope": path, "field": "dotted.path"}}'
            )
        entry: dict[str, Any] = {
            "envelope": normalize_path(binding["envelope"], field, root),
            "field": binding["field"],
        }
        entry.update(_validated_transform(binding, field, error))
        result[name] = entry
    return result


def _validated_transform(
    binding: Mapping[str, Any], field: str, error: Callable[[str], Exception]
) -> dict[str, Any]:
    """The optional ``transform``/``scale`` pair on one metric binding
    (issue #6939): ``{"transform": "reciprocal", "scale": <finite > 0>}``,
    both or neither, no other transform. Refused here (a malformed
    inventory); a raw value the transform cannot consume (zero, missing,
    non-numeric) is a *failed metric check* instead, the split the module
    already uses."""
    has_transform, has_scale = "transform" in binding, "scale" in binding
    if has_transform != has_scale:
        raise error(
            f"'{field}.transform' and '{field}.scale' go together -- a "
            "reciprocal binding needs both"
        )
    if not has_transform:
        return {}
    if binding["transform"] not in _TRANSFORMS:
        raise error(
            f"'{field}.transform' must be one of "
            f"{', '.join(repr(t) for t in _TRANSFORMS)}, "
            f"got {binding['transform']!r}"
        )
    scale = binding["scale"]
    if not is_finite_number(scale) or scale <= 0:
        raise error(f"'{field}.scale' must be a finite number > 0")
    return {"transform": binding["transform"], "scale": scale}


def metric_value(root: str, binding: Mapping[str, Any]) -> tuple[Any, Any]:
    """``(value, raw)`` for one metric binding: the finite number at the
    binding's dotted ``field`` of the JSON envelope at ``envelope``, or
    ``None`` when unreadable/absent/non-numeric. With a ``transform`` the
    value is ``scale / raw`` (issue #6939) and ``raw`` is the untransformed
    node -- a zero, non-numeric or overflowing raw yields a ``None`` value
    (a failed metric check), never an exception."""
    raw = json_node(root, binding["envelope"], binding["field"])
    if "transform" not in binding:
        return (raw if is_number(raw) else None), None
    if not is_finite_number(raw):
        return None, raw
    try:
        value = binding["scale"] / raw
    except (ZeroDivisionError, OverflowError):
        return None, raw
    return (value if is_finite_number(value) else None), raw


def metric_checks(
    root: str,
    normalized: Mapping[str, Any],
    present: Mapping[str, Mapping[str, Any]],
    required_metrics: Sequence[str],
) -> list[dict[str, Any]]:
    """One ``metric`` check (role ``digital``) per declared corner x required
    metric, in that order. A binding whose envelope is not a cited, present
    path -- or whose named field is not consumable -- fails its check. A
    transformed binding's check additively records ``raw_value``,
    ``transform`` and ``scale`` (issue #6939); an untransformed one is
    byte-identical to before. A pair declared in ``unmeasured_metrics``
    (issue #7259) holds its slot with a passing ``unmeasured_metric``
    disclosure -- corner, metric and reason, no value -- instead."""
    checks: list[dict[str, Any]] = []
    unmeasured = normalized["unmeasured_metrics"]
    for corner in sorted(normalized["corners"]):
        for name in required_metrics:
            if (corner, name) in unmeasured:
                checks.append(
                    {
                        "check": "unmeasured_metric",
                        "role": "digital",
                        "corner": corner,
                        "metric": name,
                        "reason": unmeasured[(corner, name)],
                        "passed": True,
                    }
                )
                continue
            binding = normalized["metrics"].get(corner, {}).get(name)
            value: Any = None
            raw: Any = None
            if binding is not None and binding["envelope"] in present:
                value, raw = metric_value(root, binding)
            check: dict[str, Any] = {
                "check": "metric",
                "role": "digital",
                "corner": corner,
                "metric": name,
                "envelope": binding["envelope"] if binding else None,
                "field": binding["field"] if binding else None,
                "value": value,
                "passed": value is not None,
            }
            if binding and "transform" in binding:
                check["raw_value"] = raw
                check["transform"] = binding["transform"]
                check["scale"] = binding["scale"]
            checks.append(check)
    return checks


def _error(message: str) -> Exception:
    """An ``AttestationError`` (imported lazily: ``signoff_attest`` imports us)."""
    from klayout_tools.signoff_attest import AttestationError

    return AttestationError(message)


def validated_uncited_rows(
    inventory: Mapping[str, Any],
    spec_rows: Sequence[str],
    citations: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    """Item 8's ``uncited_rows``: ``[{"row": str, "issue": int}]``
    (optional), the counterpart of item 9's ``unbenched_rows`` -- a declared
    spec row that intentionally carries no citation yet, with the issue that
    owns closing the gap. The row must be declared in ``spec_rows`` and must
    have no citation: a gap declaration that coexists with evidence, or
    names an unknown row, is a malformed inventory (exit ``1``), not a
    failed check."""
    rows = gaprows.validated_gap_rows(inventory, "uncited_rows", "uncited")
    declared = set(spec_rows)
    for entry in rows:
        row = entry["row"]
        if row not in declared:
            raise _error(
                f"'uncited_rows' names row '{row}', which 'spec_rows' does not declare"
            )
        if citations.get(row):
            raise _error(
                f"'uncited_rows' names row '{row}', which 'citations' cites -- a "
                "declared gap must not coexist with evidence"
            )
    return rows


def row_checks(
    normalized: Mapping[str, Any], envelopes: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Item 8's per-spec-row checks, sorted by row id: a ``citation`` check
    passing iff the row cites at least one recognised envelope -- except a row
    declared in ``uncited_rows``, which emits its passing ``uncited_row``
    check instead of a failing ``citation`` (issue #5856): the gap and its
    owner issue stay visible to the grader rather than the row disappearing
    from ``spec_rows``."""
    uncited = {entry["row"]: entry for entry in normalized["uncited_rows"]}
    checks: list[dict[str, Any]] = []
    for row in sorted(normalized["spec_rows"]):
        if row in uncited:
            checks.append(gaprows.gap_row_check(uncited[row], "uncited_row"))
            continue
        paths = normalized["citations"][row]
        checks.append(
            {
                "check": "citation",
                "role": "spec_row",
                "row": row,
                "envelopes": paths,
                "passed": bool(paths) and all(envelopes[p]["passed"] for p in paths),
            }
        )
    return checks
