"""Placed-instance -> synthetic ``gen-compose`` block adapter for ``klt route``.

GDS stores no instance names, so a direct child of the top cell is addressed
by its master cell name when only one direct instance uses that master, and
always as ``<master>#<k>``. See ``docs/cli/route.md``.
"""

from __future__ import annotations

from typing import Any

from .route_request import RouteError

#: ``(rotation steps, mirrored)`` of a ``kdb.Trans`` -> ``blocks[].orientation``
#: (the inverse of ``gen_compose._ORIENTATION_KDB_ARGS``). An instance whose
#: transform is not listed (R90/R270, or a mirror combined with either) is
#: rejected with ``unsupported_orientation`` when a pin names it.
_ORIENTATION_BY_TRANS: dict[tuple[int, bool], str] = {
    (0, False): "none",
    (2, True): "mirror_x",
    (0, True): "mirror_y",
    (2, False): "rotate_180",
}


def _select_top_cell(layout: Any, name: str | None, gds_path: str) -> Any:
    if name is not None:
        cell = layout.cell(name)
        if cell is None:
            raise RouteError(f"layout '{gds_path}' has no cell named '{name}'")
        return cell
    tops = layout.top_cells()
    if len(tops) != 1:
        names = ", ".join(sorted(c.name for c in tops)) or "none"
        raise RouteError(
            f"layout '{gds_path}' has {len(tops)} top cells ({names}) -- name one "
            "with layout.top_cell"
        )
    return tops[0]


def _enumerate_instances(top: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """``({instance id: info}, ambiguous bare names)`` for the direct children
    of ``top``.

    GDS carries no instance names, so an instance is addressed by its master
    cell name when only one direct instance uses that master, and always by
    ``<master>#<k>`` (``k`` = 0-based position among that master's instances
    in the top cell's instance order).
    """
    by_master: dict[str, list[Any]] = {}
    for inst in top.each_inst():
        by_master.setdefault(inst.cell.name, []).append(inst)
    instances: dict[str, dict[str, Any]] = {}
    ambiguous: list[str] = []
    for master, insts in by_master.items():
        for k, inst in enumerate(insts):
            info = {"master": master, "inst": inst}
            instances[f"{master}#{k}"] = info
            if len(insts) == 1:
                instances[master] = info
        if len(insts) > 1:
            ambiguous.append(master)
    return instances, ambiguous


def _resolve_instance(
    ref: str,
    instances: dict[str, dict[str, Any]],
    ambiguous: list[str],
    where: str,
) -> tuple[str, dict[str, Any]]:
    if ref in instances:
        info = instances[ref]
        return f"{info['master']}#{_index_of(instances, info)}", info
    if ref in ambiguous:
        options = sorted(k for k in instances if k.startswith(f"{ref}#"))
        raise RouteError(
            f"{where}.cell '{ref}' names a cell with {len(options)} instances in "
            f"the top cell -- address one as {', '.join(options)}",
            code="ambiguous_instance",
            details={"candidates": options},
        )
    known = sorted(instances)
    raise RouteError(
        f"{where}.cell '{ref}' is not a direct child instance of the top cell "
        f"-- known: {', '.join(known) or 'none'}",
        code="unknown_instance",
        details={"known": known},
    )


def _index_of(instances: dict[str, dict[str, Any]], info: dict[str, Any]) -> int:
    for key, value in instances.items():
        if value is info and "#" in key:
            return int(key.rsplit("#", 1)[1])
    raise AssertionError("instance is always registered under <master>#<k>")


def _orientation_of(inst: Any, instance_id: str) -> str:
    if inst.is_regular_array() and inst.na * inst.nb > 1:
        raise RouteError(
            f"instance '{instance_id}' is an array ({inst.na}x{inst.nb}); slice 1 "
            "routes to single instances only",
            code="unsupported_orientation",
            details={"instance": instance_id, "reason": "array"},
        )
    trans = inst.trans
    key = (trans.angle, bool(trans.is_mirror()))
    if inst.is_complex() or key not in _ORIENTATION_BY_TRANS:
        raise RouteError(
            f"instance '{instance_id}' is placed with an unsupported transform "
            f"(rotation {trans.angle * 90} deg, mirrored={trans.is_mirror()}"
            f"{', magnified/arbitrary angle' if inst.is_complex() else ''}); "
            "slice 1 supports only none, mirror_x, mirror_y and rotate_180",
            code="unsupported_orientation",
            details={
                "instance": instance_id,
                "angle_deg": trans.angle * 90,
                "mirrored": bool(trans.is_mirror()),
            },
        )
    return _ORIENTATION_BY_TRANS[key]


def _bbox_um(box: Any) -> dict[str, float]:
    return {"x0": box.left, "y0": box.bottom, "x1": box.right, "y1": box.top}


def _build_block(
    instance_id: str, info: dict[str, Any], gds_path: str, orientation: str
) -> dict[str, Any]:
    """One synthetic ``gen-compose`` block for a placed instance, in the
    shape ``_parse_blocks`` produces (oriented, still pre-translation)."""
    inst = info["inst"]
    disp = inst.dtrans.disp
    placed = _bbox_um(inst.dbbox())
    local = {
        "x0": placed["x0"] - disp.x,
        "y0": placed["y0"] - disp.y,
        "x1": placed["x1"] - disp.x,
        "y1": placed["y1"] - disp.y,
    }
    return {
        "id": instance_id,
        "source": "cell",
        "generator": None,
        "cell_name": info["master"],
        "gds_path": gds_path,
        "spelled_gds_path": gds_path,
        "bbox_um": local,
        "port_names": set(),
        "ports": {},
        "matched_group_id": None,
        "min_spacing_um": 0.0,
        "foreign_clearance_um": None,
        "orientation": orientation,
        "placed_cell_name": None,
        "navigable_regions": [],
        "abuts": set(),
        "derived_ports": [],
        "derived_port_warnings": [],
        "_offset_um": {"x": disp.x, "y": disp.y},
        "_placed_bbox_um": placed,
    }
