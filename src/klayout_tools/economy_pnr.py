"""Place-and-route decomposition of a layout's top bbox (issue #5181).

Split out of :mod:`klayout_tools.economy` to keep that module under the
file-size ratchet; used by ``economy_report(..., pnr_report=...)``.
"""

from __future__ import annotations

import json
from typing import Any


def _err(msg: str) -> Exception:
    from .economy import EconomyError  # lazy: economy imports this module

    return EconomyError(msg)


#: Absolute tolerance (um^2) within which the P&R decomposition terms must
#: sum to the bbox area. P&R reports round areas to 1e-4 um^2.
PNR_TOLERANCE_UM2 = 1e-3


def _load_pnr_report(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        raise _err(f"cannot read P&R report '{path}': {exc}") from exc
    except ValueError as exc:
        raise _err(f"P&R report '{path}' is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise _err(f"P&R report '{path}' must be a JSON object")
    return data


def _pnr_areas(report: dict[str, Any], path: str) -> tuple[float, float, str]:
    """(die_area_um2, core_area_um2, source) from a place-and-route report:
    top-level fields if present, else the last stage carrying both."""
    candidates: list[tuple[Any, str]] = [(report, "report")]
    stages = report.get("stages")
    if isinstance(stages, list):
        for st in reversed(stages):
            if isinstance(st, dict):
                candidates.append((st, f"stages[{st.get('name')}]"))
    for src, label in candidates:
        die, core = src.get("die_area_um2"), src.get("core_area_um2")
        if all(
            isinstance(v, (int, float)) and not isinstance(v, bool) for v in (die, core)
        ):
            return float(die), float(core), label
    raise _err(
        f"P&R report '{path}' has no die_area_um2/core_area_um2 "
        "(top level or in any stage)"
    )


def _pnr_physical_masters(report: dict[str, Any]) -> set[str]:
    """Tap/endcap/filler master names recorded in the report's ``power``
    block (and its ``row_rail`` sub-block); nothing is hardcoded per PDK."""
    power = report.get("power")
    names: set[str] = set()
    if not isinstance(power, dict):
        return names
    blocks = [power]
    if isinstance(power.get("row_rail"), dict):
        blocks.append(power["row_rail"])
    for block in blocks:
        for key in ("tapcell_master", "endcap_master"):
            if isinstance(block.get(key), str):
                names.add(block[key])
        fillers = block.get("filler_masters")
        if isinstance(fillers, list):
            names.update(f for f in fillers if isinstance(f, str))
    return names


def pnr_decomposition(
    kdb: Any,
    top_cell: Any,
    bbox: Any,
    dbu: float,
    pnr_report_path: str,
) -> dict[str, Any]:
    """Additive decomposition of the top bbox using P&R report metadata
    (issue #5181). Terms (um^2):

    - ``logical_cells`` / ``physical_only_cells``: union of direct-instance
      bboxes whose master is not / is a tap, endcap or filler master named
      by the report (physical-only area never double-counts logical).
    - ``core_whitespace``: report core area minus the two cell terms.
    - ``die_margin``: report die area minus core area.
    - ``guard_ring``: top bbox minus the bbox of the non-guard-ring
      instances; guard-ring instances are those whose master name contains
      ``guard`` (case-insensitive). 0 when none.

    ``residual_um2`` is the bbox minus the term sum; ``sums_to_total`` is
    ``|residual| <= tolerance_um2``.
    """
    report = _load_pnr_report(pnr_report_path)
    die_area, core_area, area_src = _pnr_areas(report, pnr_report_path)
    physical = _pnr_physical_masters(report)
    bbox_area_um2 = bbox.area() * dbu * dbu
    if core_area > bbox_area_um2 + PNR_TOLERANCE_UM2 or die_area > (
        bbox_area_um2 + PNR_TOLERANCE_UM2
    ):
        raise _err(
            f"P&R report die/core area ({die_area}/{core_area} um2) exceeds "
            f"the layout bbox area ({bbox_area_um2} um2); report does not "
            "describe this layout"
        )
    if core_area > die_area + PNR_TOLERANCE_UM2:
        raise _err(
            f"P&R report core_area_um2 ({core_area}) exceeds die_area_um2 ({die_area})"
        )

    logical = kdb.Region()
    phys = kdb.Region()
    block_box = kdb.Box()
    guard_seen = False
    for inst in top_cell.each_inst():
        child = inst.cell
        if child.bbox().empty():
            continue
        name = child.name
        is_guard = "guard" in name.lower()
        for trans in inst.cell_inst.each_cplx_trans():
            ibox = child.bbox().transformed(trans)
            if is_guard:
                guard_seen = True
                continue
            block_box += ibox
            (phys if name in physical else logical).insert(ibox)
    logical = logical.merged()
    phys = (phys.merged() - logical).merged()

    k = dbu * dbu
    logical_um2 = logical.area() * k
    phys_um2 = phys.area() * k
    guard_um2 = (bbox.area() - block_box.area()) * k if guard_seen else 0.0
    whitespace = max(core_area - logical_um2 - phys_um2, 0.0)
    margin = max(die_area - core_area, 0.0)

    terms = {
        "logical_cells": (
            logical_um2,
            "layout: direct instances not in the report's tap/endcap/filler masters",
        ),
        "physical_only_cells": (
            phys_um2,
            "layout: direct instances whose master is a report tap/endcap/filler",
        ),
        "core_whitespace": (
            whitespace,
            f"report {area_src}.core_area_um2 minus placed cell terms",
        ),
        "die_margin": (margin, f"report {area_src}.die_area_um2 minus core_area_um2"),
        "guard_ring": (
            guard_um2,
            "layout: top bbox minus bbox of non-guard-ring instances"
            if guard_seen
            else "no guard-ring instance found",
        ),
    }
    out: dict[str, Any] = {
        name: {"area_um2": area, "source": src} for name, (area, src) in terms.items()
    }
    total = sum(a for a, _ in terms.values())
    residual = bbox_area_um2 - total
    out.update(
        {
            "sum_um2": total,
            "bbox_area_um2": bbox_area_um2,
            "residual_um2": residual,
            "tolerance_um2": PNR_TOLERANCE_UM2,
            "sums_to_total": abs(residual) <= PNR_TOLERANCE_UM2,
            "logical_cell_area_um2": logical_um2,
            "overrun_ratio": (bbox_area_um2 / logical_um2 if logical_um2 else None),
            "pnr_report": pnr_report_path,
        }
    )
    return out


def check_pnr_args(pnr_report: str | None, attribute: bool) -> None:
    if pnr_report is not None and not attribute:
        raise _err("--pnr-report requires --attribute")


def attach_pnr(
    report: dict[str, Any],
    kdb: Any,
    top_cell: Any,
    bbox: Any,
    dbu: float,
    pnr_report_path: str | None,
) -> None:
    """Add ``attribution.pnr_decomposition`` to ``report`` when a report is
    given."""
    if pnr_report_path is not None:
        report["attribution"]["pnr_decomposition"] = pnr_decomposition(
            kdb, top_cell, bbox, dbu, pnr_report_path
        )
