"""Plan-compliance check: does a *finished* layout still honour its plan?

``klt layout-plan execute`` turns a ``klt.layout_plan.request/1`` document
into a layout; this module answers the converse, read-only question for a
layout that may since have been hand-edited or re-routed: do the plan's
declared group presence, row membership/order, row stacking, row
``spacing_um``/``align`` and ``abutment[]`` edges/gaps still hold? It never
regenerates or touches geometry -- it compares instance bounding boxes in the
top cell against the plan.

Group identity is recovered from the sub-cell name: ``gen_compose`` names each
placed group sub-cell ``"<block_id>__<src_cell_name>"`` (and KLayout may add a
``$n`` suffix on a collision), so group ``g`` is the top-cell instance whose
cell name starts with ``"g__"``. A layout where those cells were flattened or
renamed reports ``group_missing`` -- never a pass.

Scope decisions:

- **Abutted groups are exempt from row geometry.** ``execute`` applies
  ``abutment[]`` *after* row placement and lets it override a row-derived
  position, so a group named in any ``abutment[]`` entry is not held to its
  row's order/spacing/align/stacking; the exempted checks are reported as
  ``skipped`` (reason ``abutment_overrides_row``), not silently dropped.
- **Tolerance.** ``execute`` snaps origins up onto the manufacturing grid, so
  a declared ``spacing_um``/``gap_um`` is a lower bound realised to within one
  grid step. :data:`DEFAULT_TOLERANCE_UM` (0.01 um, two sky130 5 nm steps)
  absorbs that; callers may override it.
- Sizing, pin edges and ``drc_hints`` are not compared (out of scope).

The result is shaped as a ``kind: "generic"`` evidence envelope so
``klt signoff`` accepts it without a new envelope kind.
"""

from __future__ import annotations

import re
from typing import Any

from .layout_plan import LayoutPlanError, validate_layout_plan_document

SCHEMA_VERSION = 1

#: Default compliance tolerance in um -- see the module docstring.
DEFAULT_TOLERANCE_UM = 0.01

#: Check names, as emitted in ``checks[].check``.
CHECK_GROUP_PRESENT = "group_present"
CHECK_ROW_ORDER = "row_order"
CHECK_ROW_STACK = "row_stack"
CHECK_ROW_SPACING = "row_spacing"
CHECK_ROW_ALIGN = "row_align"
CHECK_ABUTMENT = "abutment"

#: Violation names, as emitted in ``violations[].violation`` (the ``check``
#: row's own name for every check but ``group_present``).
VIOLATION_GROUP_MISSING = "group_missing"

_COLLISION_SUFFIX = re.compile(r"\$\d+$")


class LayoutPlanCheckError(LayoutPlanError):
    """The layout could not be read or has no usable top cell (exit 1)."""


def _load_group_bboxes(
    gds_path: str, cell_name: str | None, group_ids: list[str]
) -> tuple[str, dict[str, dict[str, float]]]:
    """Return ``(top_cell_name, {group_id: bbox_um})`` for the groups found as
    top-cell instances. Groups with no matching instance are absent."""
    import klayout.db as kdb

    layout = kdb.Layout()
    try:
        layout.read(gds_path)
    except Exception as exc:  # pya raises RuntimeError for unreadable streams
        raise LayoutPlanCheckError(
            f"could not read layout '{gds_path}': {exc}"
        ) from exc

    if cell_name is not None:
        top = layout.cell(cell_name)
        if top is None:
            raise LayoutPlanCheckError(
                f"layout '{gds_path}' has no cell named '{cell_name}'"
            )
    else:
        tops = layout.top_cells()
        if len(tops) != 1:
            names = ", ".join(sorted(c.name for c in tops)) or "none"
            raise LayoutPlanCheckError(
                f"layout '{gds_path}' has {len(tops)} top cells ({names}); "
                "pass --cell to pick one"
            )
        top = tops[0]

    bboxes: dict[str, dict[str, float]] = {}
    for inst in top.each_inst():
        name = _COLLISION_SUFFIX.sub("", inst.cell.name)
        for group_id in group_ids:
            if group_id in bboxes or not name.startswith(f"{group_id}__"):
                continue
            box = inst.dbbox()
            bboxes[group_id] = {
                "x0": box.left,
                "y0": box.bottom,
                "x1": box.right,
                "y1": box.top,
            }
    return top.name, bboxes


def _row(check: str, status: str, subject: dict, **detail: Any) -> dict[str, Any]:
    return {"check": check, **subject, **detail, "status": status}


def _num(value: float) -> float:
    return round(value, 6)


def _abutted_group_ids(abutment: list[dict[str, Any]]) -> set[str]:
    return {gid for entry in abutment for gid in (entry["a"], entry["b"])}


def _check_group_presence(
    group_ids: list[str], bboxes: dict[str, dict[str, float]]
) -> list[dict[str, Any]]:
    return [
        _row(
            CHECK_GROUP_PRESENT,
            "pass" if gid in bboxes else "violation",
            {"group": gid},
            expected=f"sub-cell instance named '{gid}__*' in the top cell",
            observed="present" if gid in bboxes else VIOLATION_GROUP_MISSING,
        )
        for gid in group_ids
    ]


def _skip(check: str, subject: dict, reason: str) -> dict[str, Any]:
    return _row(check, "skipped", subject, reason=reason)


def _check_rows(
    rows: list[dict[str, Any]],
    bboxes: dict[str, dict[str, float]],
    abutted: set[str],
    tol: float,
) -> list[dict[str, Any]]:
    """Row checks over the *active* groups of each row -- those present in the
    layout and not named in ``abutment[]``. An inactive group is skipped
    individually (``group_missing`` / ``abutment_overrides_row``); the rest of
    its row is still held to the plan."""

    def active(order: list[str]) -> list[str]:
        return [g for g in order if g in bboxes and g not in abutted]

    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        subject = {"row": index}
        order = row["order"]
        live = active(order)
        for gid in order:
            if gid not in live:
                reason = (
                    "group_missing" if gid not in bboxes else "abutment_overrides_row"
                )
                out.append(_skip(CHECK_ROW_ORDER, {**subject, "group": gid}, reason))

        if len(live) >= 2:
            observed_order = sorted(live, key=lambda g: bboxes[g]["x0"])
            xs = [bboxes[g]["x0"] for g in live]
            in_order = all(b >= a - tol for a, b in zip(xs, xs[1:], strict=False))
            out.append(
                _row(
                    CHECK_ROW_ORDER,
                    "pass" if in_order else "violation",
                    subject,
                    expected=live,
                    observed=live if in_order else observed_order,
                )
            )

        for left, right in zip(order, order[1:], strict=False):
            if left not in live or right not in live:
                continue
            gap = bboxes[right]["x0"] - bboxes[left]["x1"]
            ok = abs(gap - row["spacing_um"]) <= tol
            out.append(
                _row(
                    CHECK_ROW_SPACING,
                    "pass" if ok else "violation",
                    {**subject, "pair": [left, right]},
                    expected=_num(row["spacing_um"]),
                    observed=_num(gap),
                )
            )

        if len(live) >= 2:
            align = row["align"]
            if align == "bottom":
                values = [bboxes[g]["y0"] for g in live]
            elif align == "top":
                values = [bboxes[g]["y1"] for g in live]
            else:
                values = [(bboxes[g]["y0"] + bboxes[g]["y1"]) / 2 for g in live]
            spread = max(values) - min(values)
            out.append(
                _row(
                    CHECK_ROW_ALIGN,
                    "pass" if spread <= tol else "violation",
                    subject,
                    expected=f"{align}-aligned (spread <= {tol}um)",
                    observed=f"spread {_num(spread)}um",
                )
            )

    for index in range(1, len(rows)):
        below, above = active(rows[index - 1]["order"]), active(rows[index]["order"])
        subject = {"row": index}
        if not below or not above:
            out.append(_skip(CHECK_ROW_STACK, subject, "group_missing_or_abutted"))
            continue
        below_top = max(bboxes[g]["y1"] for g in below)
        above_bottom = min(bboxes[g]["y0"] for g in above)
        out.append(
            _row(
                CHECK_ROW_STACK,
                "pass" if above_bottom >= below_top - tol else "violation",
                subject,
                expected=f"row {index} above row {index - 1}",
                observed=_num(above_bottom - below_top),
            )
        )
    return out


def _abutment_gap(edge: str, a: dict[str, float], b: dict[str, float]) -> float:
    if edge == "right":
        return b["x0"] - a["x1"]
    if edge == "left":
        return a["x0"] - b["x1"]
    if edge == "top":
        return b["y0"] - a["y1"]
    return a["y0"] - b["y1"]  # "bottom"


def _check_abutments(
    abutment: list[dict[str, Any]],
    bboxes: dict[str, dict[str, float]],
    tol: float,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(abutment):
        subject = {"abutment": index, "pair": [entry["a"], entry["b"]]}
        if entry["a"] not in bboxes or entry["b"] not in bboxes:
            out.append(_skip(CHECK_ABUTMENT, subject, "group_missing"))
            continue
        gap = _abutment_gap(entry["edge"], bboxes[entry["a"]], bboxes[entry["b"]])
        ok = abs(gap - entry["gap_um"]) <= tol
        out.append(
            _row(
                CHECK_ABUTMENT,
                "pass" if ok else "violation",
                subject,
                edge=entry["edge"],
                expected=_num(entry["gap_um"]),
                observed=_num(gap),
            )
        )
    return out


def check_layout_plan(
    validated: dict[str, Any],
    gds_path: str,
    *,
    cell_name: str | None = None,
    tolerance_um: float = DEFAULT_TOLERANCE_UM,
) -> dict[str, Any]:
    """Check ``gds_path`` against an already-validated plan (the dict
    :func:`validate_layout_plan` returns). Returns the generic-evidence
    envelope documented in ``docs/cli/layout-plan-check.md``."""
    group_ids = [g["id"] for g in validated["device_groups"]]
    top_name, bboxes = _load_group_bboxes(gds_path, cell_name, group_ids)
    abutment = validated.get("abutment") or []
    rows = validated.get("rows") or []

    checks = _check_group_presence(group_ids, bboxes)
    checks += _check_rows(rows, bboxes, _abutted_group_ids(abutment), tolerance_um)
    checks += _check_abutments(abutment, bboxes, tolerance_um)

    violations = [c for c in checks if c["status"] == "violation"]
    skipped = [c for c in checks if c["status"] == "skipped"]
    compliant = not violations
    summary = (
        f"{len(checks) - len(skipped)} plan-compliance checks on '{top_name}': "
        + ("all hold" if compliant else f"{len(violations)} violation(s)")
        + (f", {len(skipped)} skipped" if skipped else "")
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "generic",
        "status": "pass" if compliant else "fail",
        "summary": summary,
        "source": gds_path,
        "cell_name": top_name,
        "gds_path": gds_path,
        "tolerance_um": tolerance_um,
        "compliant": compliant,
        "checks": checks,
        "violations": violations,
    }


def check_layout_plan_document(
    request: Any,
    gds_path: str,
    *,
    request_dir: str | None = None,
    cell_name: str | None = None,
    tolerance_um: float = DEFAULT_TOLERANCE_UM,
) -> dict[str, Any]:
    """:func:`check_layout_plan`, validating ``request`` first via
    :func:`validate_layout_plan_document`."""
    validated = validate_layout_plan_document(request, request_dir)
    return check_layout_plan(
        validated, gds_path, cell_name=cell_name, tolerance_um=tolerance_um
    )


def exit_code_for(response: dict[str, Any]) -> int:
    """0 when compliant, 3 when any check is a violation."""
    return 0 if response["compliant"] else 3
