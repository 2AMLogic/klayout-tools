"""Opt-in `sta` evidence requirements for T1 item 5: ``require_annotation_complete``
(issue #3258, moved here from :mod:`.signoff`) and ``require_complete_constraints``
(issue #5234).

``require_complete_constraints`` grading:

``klt sta`` reports a per-session ``constraint_completeness`` block (issue
#2780) that is separate from ``timing_status``: a design can report finite
slack and still carry unconstrained endpoints or unclocked registers. An
evidence entry citing a `sta` envelope may set
``"require_complete_constraints": true``; item 5 then grades ``unmet`` when
any graded corner's ``constraint_completeness.status`` is not literal
``"complete"`` (missing, ``null``, malformed and ``unknown`` all fail
closed). Off by default, so existing citations grade unchanged.
"""

from __future__ import annotations

from typing import Any

_REASON_CHECK_FAILED = "check_failed"


def _scopes(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """The corner set graded by ``_sta_timing_passed``: every ``corners[]``
    entry of a multi-corner response, or the flat envelope itself."""
    corners = envelope.get("corners")
    if isinstance(corners, list):
        return [c for c in corners if isinstance(c, dict)]
    return [envelope]


def _check_count(block: dict[str, Any], key: str) -> int | None:
    """One completeness check's ``count``; ``None`` when the check was
    ``unavailable`` (or malformed) -- never coerced to ``0``."""
    check = block.get(key)
    if not isinstance(check, dict) or check.get("status") != "ok":
        return None
    count = check.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    return count


def constraint_completeness_status(scope: dict[str, Any]) -> str | None:
    """A corner's ``constraint_completeness.status`` (``None`` if absent)."""
    block = scope.get("constraint_completeness")
    if not isinstance(block, dict):
        return None
    status = block.get("status")
    return status if isinstance(status, str) else None


def sta_constraint_shortfalls(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Corners whose ``constraint_completeness.status`` is not ``"complete"``,
    as ``{corner, status, unconstrained_endpoints, unclocked_registers}``
    (counts only; ``None`` when that check was unavailable)."""
    shortfalls: list[dict[str, Any]] = []
    # A multi-corner response with no usable entry proves nothing either.
    scopes = _scopes(envelope)
    if isinstance(envelope.get("corners"), list) and not scopes:
        return [
            {
                "corner": None,
                "status": None,
                "unconstrained_endpoints": None,
                "unclocked_registers": None,
            }
        ]
    for scope in scopes:
        if constraint_completeness_status(scope) == "complete":
            continue
        block = scope.get("constraint_completeness")
        block = block if isinstance(block, dict) else {}
        shortfalls.append(
            {
                "corner": scope.get("corner"),
                "status": constraint_completeness_status(scope),
                "unconstrained_endpoints": _check_count(
                    block, "unconstrained_endpoints"
                ),
                "unclocked_registers": _check_count(block, "unclocked_registers"),
            }
        )
    return shortfalls


def sta_constraint_refusal(
    check_kind: str, spec: dict[str, Any], envelope: dict[str, Any]
) -> tuple[str, str, None, dict[str, Any]] | None:
    """The ``unmet`` grade for a cited `sta` envelope with incomplete
    constraints when the entry opted in; ``None`` when off, not ``sta``, or
    every graded corner is complete."""
    if check_kind != "sta" or spec.get("require_complete_constraints") is not True:
        return None
    shortfalls = sta_constraint_shortfalls(envelope)
    if not shortfalls:
        return None
    return (
        "unmet",
        _REASON_CHECK_FAILED,
        None,
        {"sta_constraints_incomplete": shortfalls},
    )


def sta_annotation_shortfalls(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """The corners of a `klt sta` envelope whose ``spef_annotation`` block is
    present but does not report ``annotation_complete`` as literal ``True``
    (issue #3258), as ``{"corner", "annotation_complete",
    "annotation_warning"}`` entries.

    Graded over the same corner set as :func:`_sta_timing_passed` (every
    corner of a multi-corner response, or the flat envelope itself). A corner
    whose ``spef_annotation`` is absent or ``null`` (no SPEF supplied) is not
    a shortfall: the requirement cannot turn a no-SPEF run into a parasitics
    claim. A present block whose ``annotation_complete`` is missing, ``null``
    or non-boolean *is* one (fail-closed).
    """
    corners = envelope.get("corners")
    scopes = (
        [c for c in corners if isinstance(c, dict)]
        if isinstance(corners, list)
        else [envelope]
    )
    shortfalls: list[dict[str, Any]] = []
    for scope in scopes:
        block = scope.get("spef_annotation")
        if block is None:
            continue
        if not isinstance(block, dict):
            shortfalls.append(
                {
                    "corner": scope.get("corner"),
                    "annotation_complete": None,
                    "annotation_warning": None,
                }
            )
            continue
        if block.get("annotation_complete") is True:
            continue
        shortfalls.append(
            {
                "corner": scope.get("corner"),
                "annotation_complete": block.get("annotation_complete"),
                "annotation_warning": block.get("annotation_warning"),
            }
        )
    return shortfalls


def sta_annotation_refusal(
    check_kind: str, spec: dict[str, Any], envelope: dict[str, Any]
) -> tuple[str, str, None, dict[str, Any]] | None:
    """Issue #3258: the ``unmet`` grade for a cited `sta` envelope whose SPEF
    annotation was not fully applied, when the evidence entry opted in with
    ``require_annotation_complete``; ``None`` when the requirement is off, the
    kind is not ``sta``, or every annotated corner is complete. The offending
    corner(s)' ``annotation_warning`` ride along as the item's detail.
    """
    if check_kind != "sta" or spec.get("require_annotation_complete") is not True:
        return None
    shortfalls = sta_annotation_shortfalls(envelope)
    if not shortfalls:
        return None
    return (
        "unmet",
        _REASON_CHECK_FAILED,
        None,
        {"sta_annotation_incomplete": shortfalls},
    )
