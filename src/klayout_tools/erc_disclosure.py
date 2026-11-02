"""Disclosure helpers for ``klt erc`` ``erc_coverage`` (issue #4820).

Two spec-declared disclosures that change *how work is reported* rather than
any finding computation, split out of ``klayout_tools.erc`` (which imports
these names back):

- the top-level ``ties_disclosure`` readers (issues #2234/#2541), and
- the top-level ``floating_gate_allowed`` carve-out (issue #4820): a floating
  gate wholly inside a declared micrometre box raises no
  ``erc.floating_gate`` finding, but is *disclosed, not passed* -- its
  per-gate id moves from ``checked`` to ``inapplicable`` with reason
  :data:`REASON_FLOATING_GATE_DISCLOSED`, and the measured effect of every
  box is echoed under ``provenance.floating_gate_allowed``.
"""

from __future__ import annotations

from typing import Any

from ._paths import _reject_unknown_keys
from .coverage import work_id
from .erc_spec import (
    _TIES_DISCLOSURE_COVERAGE_REASON,
    DISCLOSURE_KIND_UNEXPRESSIBLE,
    REASON_NO_TIES_DECLARED,
    ErcError,
    _parse_um_boxes,
)

REASON_FLOATING_GATE_DISCLOSED = "floating_gate_disclosed"
FLOATING_GATE_ALLOWED_KEY = "floating_gate_allowed"
_FLOATING_GATE_ALLOWED_KEYS = ("box", "reason")


def _ties_disclosure_reason(ties_disclosure: dict[str, Any] | None) -> str:
    """The ``erc_coverage.inapplicable`` reason for undeclared
    ``erc.missing_tie`` work, given the spec's ``ties_disclosure`` (or its
    absence) -- see :func:`_connectivity_coverage`."""
    if not ties_disclosure:
        return REASON_NO_TIES_DECLARED
    return _TIES_DISCLOSURE_COVERAGE_REASON[
        ties_disclosure.get("kind", DISCLOSURE_KIND_UNEXPRESSIBLE)
    ]


def _disclosed_undeclared_classes(
    ties_disclosure: dict[str, Any] | None,
) -> list[str]:
    """The tie classes a top-level ``ties_disclosure`` names as undeclared
    (``undeclared_classes``, issue #2541) -- ``[]`` when the spec declared
    no disclosure, or a disclosure that names none.

    Already validated by :func:`_validate_ties_disclosure` (non-empty
    strings, no duplicates, none of them a declared ``ties[]`` name), so
    this is a plain read: the list exists precisely so the undeclared work
    has an identity :func:`_connectivity_coverage` can record a reason
    against."""
    if not ties_disclosure:
        return []
    return list(ties_disclosure.get("undeclared_classes") or [])


def validate_floating_gate_allowed(
    spec: dict[str, Any], spec_path: str
) -> list[dict[str, Any]]:
    """The optional top-level ``floating_gate_allowed`` spec key:
    ``[{"box": [left, bottom, right, top], "reason": "<text>"}]`` (micrometre
    boxes, the shape ``nets[].unlabelled_allowed_boxes`` takes). Strictly
    validated: a malformed/inverted box, an empty reason or an unknown key is
    a spec error. Omitted/``null``/``[]`` -> ``[]`` (report byte-identical to
    a spec without the key)."""
    raw = spec.get(FLOATING_GATE_ALLOWED_KEY)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': '{FLOATING_GATE_ALLOWED_KEY}' must be an array "
            'of {"box": [left, bottom, right, top], "reason": "..."} objects'
        )
    entries: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        where = f"{FLOATING_GATE_ALLOWED_KEY}[{i}]"
        if not isinstance(item, dict):
            raise ErcError(f"spec '{spec_path}': {where} must be a JSON object")
        _reject_unknown_keys(
            item, _FLOATING_GATE_ALLOWED_KEYS, spec_path, where, ErcError
        )
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ErcError(
                f"spec '{spec_path}': {where}.reason must be a non-empty string"
            )
        if "box" not in item:
            raise ErcError(f"spec '{spec_path}': {where}.box is required")
        # One-element wrapper so the shared box validator's message points at
        # ``floating_gate_allowed[i].box[0]``.
        (box,) = _parse_um_boxes(
            {"box": [item["box"]]},
            "box",
            spec_path,
            i,
            section=FLOATING_GATE_ALLOWED_KEY,
        )
        entries.append({"box": box, "reason": reason.strip()})
    return entries


def excuse_floating_gates(
    floating_gates: list[tuple[dict[str, Any], Any, bool]],
    allowed: list[dict[str, Any]],
    dbu: float,
    bbox_dict: Any,
) -> tuple[set[str], list[dict[str, Any]]]:
    """Match would-be ``erc.floating_gate`` gates against ``allowed``.

    A floating gate is excused when its whole merged gate-role bbox lies
    inside one declared box (first declared box wins). Returns
    ``({gate_id, ...}, provenance_entries)``; every entry carries its
    ``matched_gates`` count, 0 included, so a stale box is visible. Only
    floating gates are matched, so full and ``findings_only`` runs agree."""
    entries = [
        {"box": list(a["box"]), "reason": a["reason"], "gates": []} for a in allowed
    ]
    excused: set[str] = set()
    for gate_entry, gate_region, is_floating in floating_gates:
        if not is_floating:
            continue
        bbox = gate_region.bbox()
        for decl, out in zip(allowed, entries, strict=True):
            left, bottom, right, top = (round(v / dbu) for v in decl["box"])
            if (
                bbox.left >= left
                and bbox.bottom >= bottom
                and bbox.right <= right
                and bbox.top <= top
            ):
                gate_id = gate_entry["gate_id"]
                excused.add(gate_id)
                out["gates"].append(
                    {
                        "gate_id": gate_id,
                        "bbox": bbox_dict(bbox),
                        "reason": decl["reason"],
                    }
                )
                break
    for out in entries:
        out["matched_gates"] = len(out["gates"])
    return excused, entries


def floating_gate_work(
    gates: list[dict[str, Any]], excused: set[str], compact: bool
) -> tuple[list[str], list[dict[str, str]]]:
    """``(checked, inapplicable)`` for the per-gate ``erc.floating_gate``
    work. Excused gates are disclosed, never checked; ``compact`` (issue
    #2965) keeps one aggregate id, only while some gate was really evaluated."""
    live = [g["gate_id"] for g in gates if g["gate_id"] not in excused]
    if compact:
        checked = [work_id("erc.floating_gate")] if live else []
    else:
        checked = [work_id("erc.floating_gate", gate_id) for gate_id in live]
    inapplicable = [
        {
            "id": work_id("erc.floating_gate", g["gate_id"]),
            "reason": REASON_FLOATING_GATE_DISCLOSED,
        }
        for g in gates
        if g["gate_id"] in excused
    ]
    return checked, inapplicable
