"""`sta` envelope-level grading for T1 item 5 (issue #7711): the top-level
``status`` guard and the opt-in ``require_routed_geometry`` requirement.

``status`` (default behaviour):

``klt sta`` always reports ``status: "ok"`` -- a failed run never emits the
envelope (``docs/cli/sta.md``) -- so a present ``status`` that is anything
else can only come from a hand-edited or foreign envelope that declares its
own run not-ok. Such an envelope grades ``unmet``/``check_failed`` with
``detail.sta_status`` naming the value. An *absent* ``status`` is unchanged,
so an envelope written before the field existed is not failed retroactively.

``require_routed_geometry`` (opt-in):

``geometry_source`` is a caller-supplied label (``"routed"`` |
``"placement_estimate"`` | ``"netlist_estimate"``), surfaced on the citation
but **not** part of the default predicate: item 5 is the pre-layout
corner-timing item, and ``klt sta`` is documented for characterising pre-route
DEFs. An evidence entry citing a `sta` envelope may set
``"require_routed_geometry": true``; item 5 then grades ``unmet`` unless the
top-level ``geometry_source`` is the literal string ``"routed"`` (any other
string, missing, ``null`` and non-string all fail closed). Off by default, so
existing citations grade unchanged -- the same additive shape as
``require_annotation_complete`` (#3258) and ``require_complete_constraints``
(#5234).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .signoff_sta_constraints import sta_annotation_refusal, sta_constraint_refusal

_REASON_CHECK_FAILED = "check_failed"

#: The boolean opt-in keys an evidence entry citing a `sta` envelope may set.
#: Each must be a real ``bool`` when present; a non-boolean value makes the
#: entry malformed (``invalid_evidence``) rather than silently reading as off.
STA_BOOL_OPTIONS = (
    "require_annotation_complete",
    "require_complete_constraints",
    "require_routed_geometry",
)


def sta_timing_shape(envelope: Mapping[str, Any]) -> bool:
    return "timing_status" in envelope or isinstance(envelope.get("corners"), list)


def sta_manifest_fields(
    spec: Mapping[str, Any], envelope: Mapping[str, Any]
) -> frozenset[str]:
    """Fields whose original values receive a specific STA manifest verdict."""
    fields = {"status"} if "status" in envelope else set()
    if spec.get("require_routed_geometry") is True:
        fields.add("geometry_source")
    return frozenset(fields)


def sta_status_ok(envelope: Mapping[str, Any]) -> bool:
    """Whether the envelope's own top-level ``status`` leaves it gradable: it
    is absent, or the literal string ``"ok"`` (``null`` and every other value
    mean the envelope declares its run not-ok)."""
    return "status" not in envelope or envelope["status"] == "ok"


def sta_status_detail(kind: str, envelope: Mapping[str, Any]) -> dict[str, Any]:
    """``{"sta_status": <value>}`` for a `sta` envelope that failed the
    :func:`sta_status_ok` guard; ``{}`` otherwise."""
    if kind != "sta" or sta_status_ok(envelope):
        return {}
    return {"sta_status": envelope["status"]}


def sta_geometry_refusal(
    check_kind: str, spec: Mapping[str, Any], envelope: Mapping[str, Any]
) -> tuple[str, str, None, dict[str, Any]] | None:
    """The ``unmet`` grade for a cited `sta` envelope whose ``geometry_source``
    is not literal ``"routed"`` when the entry opted in; ``None`` when off,
    not ``sta``, or routed."""
    if check_kind != "sta" or spec.get("require_routed_geometry") is not True:
        return None
    source = envelope.get("geometry_source")
    if isinstance(source, str) and source == "routed":
        return None
    return (
        "unmet",
        _REASON_CHECK_FAILED,
        None,
        {"sta_geometry_not_routed": {"geometry_source": source}},
    )


def sta_opt_in_refusal(
    check_kind: str, spec: dict[str, Any], envelope: dict[str, Any]
) -> tuple[str, str, None, dict[str, Any]] | None:
    """The first ``unmet`` grade among the `sta` opt-in requirements an
    evidence entry set: annotation, constraints, then routed geometry."""
    return (
        sta_annotation_refusal(check_kind, spec, envelope)
        or sta_constraint_refusal(check_kind, spec, envelope)
        or sta_geometry_refusal(check_kind, spec, envelope)
    )
