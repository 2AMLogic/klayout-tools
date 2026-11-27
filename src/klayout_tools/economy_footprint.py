"""Standard-cell footprint breakdown for ``klt economy --cell-footprint``
(issue #5180).

A cell's GDS bbox includes well/implant overhang, so summing bboxes overstates
std-cell area. This module reports, for the top cell's placed instances:

- the per-master **footprint** (abutment box, not drawn extent), with the
  ``source`` of each footprint so the fallback chain is observable:
  ``prboundary`` (the cell draws layer 235/4 -- the same rule as
  ``drc._tile_pitch_box``) -> ``lef`` (``SIZE w BY h`` from a supplied LEF) ->
  ``bbox`` (the cell's own bbox);
- a **category** split (logic / sequential / buffer / clock-tree for the
  logic-bearing cells; fill / tap / endcap for physical-only cells) from a
  name-pattern table for ``sky130_fd_sc_hd``; any other master is ``other``;
- an optional **grouping** of instances by the first ``SEP``-delimited
  component of the instance name (RTL sub-block attribution).

Instance names are read from instance user properties (key ``1`` -- the GDS
``PROPATTR`` convention -- or the string keys ``name``/``instance``). Plain
GDS from most P&R flows carries none; then ``grouping`` is reported as
``unavailable`` rather than fabricated.

Invariant: the sum of group footprints equals ``total_footprint_um2``.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._layout import load_layout, resolve_top_cell
from .economy import EconomyError, economy_report
from .lef_header import read_lef_header

#: Same layer as ``drc._PR_BOUNDARY_LAYER`` (sky130 ``prBoundary.boundary``).
PR_BOUNDARY_LAYER = (235, 4)

#: Instance-property keys that may hold the instance name, in priority order.
INSTANCE_NAME_KEYS: tuple[Any, ...] = (1, "name", "instance")

#: Group for instances whose name is absent or has no separator.
TOP_GROUP = "(top)"

#: Categories that carry no logic (physical-only), reported separately.
PHYSICAL_ONLY = ("fill", "tap", "endcap")
LOGIC_BEARING = ("logic", "sequential", "buffer", "clock-tree")
CATEGORIES = LOGIC_BEARING + PHYSICAL_ONLY + ("other",)

_HD_PREFIX = "sky130_fd_sc_hd__"

#: First match wins, so more specific patterns come first. Patterns apply to
#: the base name after ``sky130_fd_sc_hd__``; any other ``sky130_fd_sc_hd``
#: master (inverters, gates, muxes, adders, ...) is ``logic``.
_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("tap", re.compile(r"^tap")),
    ("endcap", re.compile(r"^endcap")),
    ("fill", re.compile(r"^(fill|decap|diode)")),
    ("clock-tree", re.compile(r"^(clkbuf|clkinv|clkdlybuf|s?dlclkp)")),
    ("sequential", re.compile(r"^(s?df|edf|s?dl[a-z]|dlx)")),
    ("buffer", re.compile(r"^(buf|bufbuf|bufinv|dlygate|dlymetal|probe)")),
)


def categorize_master(name: str) -> str:
    """Category of master ``name`` per the documented sky130_fd_sc_hd table;
    masters outside that library are ``other``."""
    if not name.startswith(_HD_PREFIX):
        return "other"
    base = name[len(_HD_PREFIX) :]
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.match(base):
            return category
    return "logic"


def load_lef_sizes(paths: tuple[str, ...], error: type[Exception]) -> dict[str, Any]:
    """``{MACRO name: (width_um, height_um)}`` from LEF files / directories.
    First path wins for a duplicated macro (flag order is precedence), same
    as ``extract_abstract._load_abstract_cell_lefs``."""
    sizes: dict[str, tuple[float, float]] = {}
    for path in paths:
        if os.path.isdir(path):
            entries = sorted(
                os.path.join(path, n)
                for n in os.listdir(path)
                if n.endswith((".lef", ".tlef"))
            )
        else:
            entries = [path]
        for entry in entries:
            try:
                header = read_lef_header(entry)
            except OSError as exc:
                raise error(f"could not read --lef '{entry}': {exc}") from exc
            for macro in header["macros"]:
                if macro["width_um"] is not None and macro["height_um"] is not None:
                    sizes.setdefault(
                        macro["name"], (macro["width_um"], macro["height_um"])
                    )
    return sizes


def _master_footprint(
    layout: Any, cell: Any, dbu: float, lef_sizes: dict[str, Any]
) -> tuple[float, str]:
    index = layout.find_layer(*PR_BOUNDARY_LAYER)
    if index is not None:
        box = cell.bbox_per_layer(index)
        if not box.empty():
            return box.area() * dbu * dbu, "prboundary"
    size = lef_sizes.get(cell.name)
    if size is not None:
        return size[0] * size[1], "lef"
    return cell.bbox().area() * dbu * dbu, "bbox"


def _instance_name(inst: Any) -> str | None:
    props = inst.properties()
    for key in INSTANCE_NAME_KEYS:
        value = props.get(key)
        if value is not None and str(value) != "":
            return str(value)
    return None


def _group_of(name: str | None, sep: str) -> str:
    if name is None or sep not in name:
        return TOP_GROUP
    return name.split(sep, 1)[0] or TOP_GROUP


def _round(value: float) -> float:
    return round(value, 6)


def footprint_block(
    layout: Any,
    top_cell: Any,
    dbu: float,
    bbox_area_um2: float,
    lef_sizes: dict[str, Any],
    group_sep: str | None,
) -> dict[str, Any]:
    """Build the additive ``cell_footprint`` report block (see module doc).
    Array instances expand to one member per element."""
    masters: dict[str, dict[str, Any]] = {}
    groups: dict[str, dict[str, Any]] = {}
    named = 0
    total = 0.0
    count = 0

    for inst in top_cell.each_inst():
        cell = layout.cell(inst.cell_index)
        entry = masters.get(cell.name)
        if entry is None:
            area, source = _master_footprint(layout, cell, dbu, lef_sizes)
            entry = {
                "master": cell.name,
                "category": categorize_master(cell.name),
                "footprint_um2": area,
                "source": source,
                "instance_count": 0,
            }
            masters[cell.name] = entry
        members = sum(1 for _ in inst.cell_inst.each_trans())
        name = _instance_name(inst)
        if name is not None:
            named += members
        entry["instance_count"] += members
        total += entry["footprint_um2"] * members
        count += members

        if group_sep is not None:
            group = groups.setdefault(
                _group_of(name, group_sep),
                {"instances": 0, "area": 0.0, "categories": {}},
            )
            group["instances"] += members
            group["area"] += entry["footprint_um2"] * members
            cats = group["categories"]
            cats[entry["category"]] = (
                cats.get(entry["category"], 0.0) + entry["footprint_um2"] * members
            )

    category_totals: dict[str, float] = {}
    for entry in masters.values():
        category_totals[entry["category"]] = (
            category_totals.get(entry["category"], 0.0)
            + entry["footprint_um2"] * entry["instance_count"]
        )

    def split(cats: dict[str, float]) -> dict[str, float]:
        return {c: _round(cats[c]) for c in CATEGORIES if c in cats}

    block: dict[str, Any] = {
        "instance_count": count,
        "total_footprint_um2": _round(total),
        "share_of_bbox": _round(total / bbox_area_um2) if bbox_area_um2 else None,
        "physical_only_um2": _round(
            sum(category_totals.get(c, 0.0) for c in PHYSICAL_ONLY)
        ),
        "category_um2": split(category_totals),
        "masters": [
            {
                **m,
                "footprint_um2": _round(m["footprint_um2"]),
                "total_um2": _round(m["footprint_um2"] * m["instance_count"]),
            }
            for m in sorted(masters.values(), key=lambda m: m["master"])
        ],
        "grouping": None,
    }

    if group_sep is not None:
        if count and named == 0:
            block["grouping"] = {
                "status": "unavailable",
                "separator": group_sep,
                "reason": (
                    "no instance carries a name property (keys 1/'name'/"
                    "'instance'); GDS from most P&R flows drops instance names"
                ),
            }
        else:
            block["grouping"] = {
                "status": "ok",
                "separator": group_sep,
                "named_instance_count": named,
                "groups": [
                    {
                        "group": g,
                        "instance_count": v["instances"],
                        "footprint_um2": _round(v["area"]),
                        "share_of_bbox": (
                            _round(v["area"] / bbox_area_um2) if bbox_area_um2 else None
                        ),
                        "category_um2": split(v["categories"]),
                    }
                    for g, v in sorted(
                        groups.items(), key=lambda kv: (-kv[1]["area"], kv[0])
                    )
                ],
            }
    return block


def economy_report_with_footprint(
    path: str,
    *,
    cell_footprint: bool = False,
    group_by_instance_prefix: str | None = None,
    lef_paths: tuple[str, ...] = (),
    **report_kwargs: Any,
) -> dict[str, Any]:
    """:func:`klayout_tools.economy.economy_report` plus the opt-in
    ``cell_footprint`` block.

    ``group_by_instance_prefix`` (a separator) implies ``cell_footprint``;
    ``lef_paths`` requires it. Other keyword arguments pass through to
    ``economy_report`` unchanged.
    """
    if group_by_instance_prefix is not None:
        if group_by_instance_prefix == "":
            raise EconomyError("--group-by-instance-prefix separator must not be empty")
        cell_footprint = True
    if lef_paths and not cell_footprint:
        raise EconomyError("--lef requires --cell-footprint")
    report = economy_report(path, **report_kwargs)
    if not cell_footprint:
        return report

    layout = load_layout(path, EconomyError)
    top_cell = resolve_top_cell(layout, report["top"], EconomyError)
    report["cell_footprint"] = footprint_block(
        layout,
        top_cell,
        layout.dbu,
        report["bbox_area_um2"],
        load_lef_sizes(tuple(lef_paths), EconomyError),
        group_by_instance_prefix,
    )
    return report
