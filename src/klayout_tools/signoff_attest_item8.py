"""Item 8 (characterization report) envelope handling (issues #5688, #6939).

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
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence, Set
from typing import Any

from ._jsonnum import is_finite_number, is_number
from .signoff import (
    SignoffError,
    _classify,
    _classify_failure_reason,
    _read_envelope,
)
from .signoff_attest_pins import json_node
from .signoff_drc_crosscheck import crosscheck_project

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
    byte-identical to before."""
    checks: list[dict[str, Any]] = []
    for corner in sorted(normalized["corners"]):
        for name in required_metrics:
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
