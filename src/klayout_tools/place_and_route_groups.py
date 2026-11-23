"""Instance-group geometry validation and optional fixed standard-cell placement.

Split out of ``place_and_route_request`` to keep that module under its
per-file size ceiling.  The request module passes its own error class in so
this module does not import it (which would be circular).
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from ._jsonnum import is_finite_number
from ._tcl import tcl_literal

_GROUP_KEYS = frozenset({"name", "instances", "max_extent_um", "region_um", "order"})


def _is_finite_number(value: Any) -> bool:
    return is_finite_number(value)


def _check_region_um(region: Any, where: str, error: type[Exception]) -> None:
    if not (
        isinstance(region, list)
        and len(region) == 4
        and all(_is_finite_number(v) for v in region)
        and region[0] < region[2]
        and region[1] < region[3]
    ):
        raise error(
            f"{where}.region_um must be [x0, y0, x1, y1] finite numbers "
            "with x0 < x1 and y0 < y1"
        )


def _fixed_origin(
    order: dict[str, Any], where: str, error: type[Exception]
) -> list[int | float]:
    origin = order["fixed_origin_um"]
    if (
        not isinstance(origin, list)
        or len(origin) != 2
        or not all(map(is_finite_number, origin))
    ):
        raise error(
            f"{where}.order.fixed_origin_um must be two finite coordinates [x, y]"
        )
    if order.get("pitch_um") is None:
        raise error(f"{where}.order.fixed_origin_um requires order.pitch_um")
    return list(origin)


def validate_fixed_members(
    groups: list[dict[str, Any]], error: type[Exception], stage: str = "route"
) -> None:
    owners: dict[str, str] = {}
    for group in groups:
        if "fixed_origin_um" not in group.get("order", {}):
            continue
        if stage == "floorplan":
            raise error(
                f"instance group {group['name']!r}: order.fixed_origin_um requires "
                "target_stage 'place', 'cts', or 'route'"
            )
        for name in group["instances"]:
            if name in owners:
                raise error(
                    f"instance group {group['name']!r}: member {name!r} is already "
                    f"fixed by {owners[name]!r}"
                )
            owners[name] = group["name"]


def _check_group_order(
    order: Any, instances: list[str], where: str, error: type[Exception]
) -> dict[str, Any]:
    if not isinstance(order, dict):
        raise error(f"{where}.order must be a JSON object")
    unknown = sorted(set(order) - {"axis", "pitch_um", "fixed_origin_um"})
    if unknown:
        raise error(f"{where}.order has unknown key(s): {', '.join(unknown)}")
    axis = order.get("axis")
    if axis not in ("x", "y"):
        raise error(f"{where}.order.axis must be 'x' or 'y'")
    result: dict[str, Any] = {"axis": axis}
    pitch = order.get("pitch_um")
    if pitch is not None:
        if not _is_finite_number(pitch) or pitch <= 0:
            raise error(f"{where}.order.pitch_um must be a number > 0")
        result["pitch_um"] = pitch
    if "fixed_origin_um" in order:
        result["fixed_origin_um"] = _fixed_origin(order, where, error)
    if any(ch in name for name in instances for ch in "*?"):
        raise error(f"{where}.order requires exact instance names (no glob patterns)")
    return result


def validate_group_geometry(
    entry: dict[str, Any], instances: list[str], where: str, error: type[Exception]
) -> dict[str, Any]:
    """Return the validated ``region_um`` / ``order`` items present in *entry*."""
    item: dict[str, Any] = {}
    region = entry.get("region_um")
    if region is not None:
        _check_region_um(region, where, error)
        item["region_um"] = list(region)
    order = entry.get("order")
    if order is not None:
        item["order"] = _check_group_order(order, instances, where, error)
    return item


def reject_unknown_group_keys(
    entry: dict[str, Any], where: str, error: type[Exception]
) -> None:
    """Raise *error* when *entry* carries a key outside the group vocabulary."""
    unknown = sorted(set(entry) - _GROUP_KEYS)
    if unknown:
        raise error(f"{where} has unknown key(s): {', '.join(unknown)}")


# Work in OpenDB database units. setLocation uses the oriented lower-left
# corner written to DEF, including on mirrored rows.
_FIXED_PLACEMENT_TCL = r"""
proc klt_fixed_dbu {numerator denominator dbu context} {
    set scaled [expr {$numerator * $dbu}]
    if {$scaled % $denominator != 0} {
        error "$context: coordinate is not representable in DEF database units"
    }
    return [expr {$scaled / $denominator}]
}
proc klt_fixed_row {block master x y} {
    foreach row [$block getRows] {
        if {[$row getSite] ne [$master getSite]} { continue }
        set bounds [$row getBBox]
        lassign [$row getOrigin] rx ry
        set step [$row getSpacing]
        if {$step <= 0} {
            if {[$row getSiteCount] != 1 || $x != $rx || $y != $ry} { continue }
        } elseif {[$row getDirection] eq "HORIZONTAL"} {
            if {$y != $ry || ($x - $rx) % $step != 0} { continue }
        } elseif {$x != $rx || ($y - $ry) % $step != 0} {
            continue
        }
        set w [$master getWidth]
        set h [$master getHeight]
        if {[$row getOrient] in {R90 R270 MXR90 MYR90}} { lassign [list $h $w] w h }
        if {$x >= [$bounds xMin] && $y >= [$bounds yMin]
            && $x + $w <= [$bounds xMax] && $y + $h <= [$bounds yMax]} {
            return $row
        }
    }
    return NULL
}
proc klt_fix_member {block verify group name xn xd yn yd} {
    upvar 1 boxes boxes
    set context "instance group '$group', member '$name'"
    set inst [$block findInst $name]
    if {$inst eq "NULL"} {
        set escaped [string map [list {[} {\[} {]} {\]}] $name]
        set inst [$block findInst $escaped]
    }
    if {$inst eq "NULL"} { error "$context: unknown linked instance" }
    set master [$inst getMaster]
    if {![string match CORE* [$master getType]]} {
        error "$context: expected a standard cell"
    }
    set dbu [$block getDbUnitsPerMicron]
    set x [klt_fixed_dbu $xn $xd $dbu $context]
    set y [klt_fixed_dbu $yn $yd $dbu $context]
    set row [klt_fixed_row $block $master $x $y]
    if {$row eq "NULL"} { error "$context: no legal row/site at ($x, $y) DBU" }
    if {[$inst isFixed] && ([$inst getLocation] ne [list $x $y]
                            || [$inst getOrient] ne [$row getOrient])} {
        error "$context: conflicts with the existing fixed placement"
    }
    if {$verify} {
        if {[$inst getPlacementStatus] ne "FIRM"} {
            error "$context: placement is no longer FIXED"
        }
    } else {
        $inst setOrient [$row getOrient]
        $inst setLocation $x $y
        $inst setPlacementStatus FIRM
    }
    set box [$inst getBBox]
    dict for {other obstacle} $boxes {
        if {$other eq $inst} { continue }
        lassign $obstacle label fixed
        if {[$box xMin] < [$fixed xMax] && [$fixed xMin] < [$box xMax]
            && [$box yMin] < [$fixed yMax] && [$fixed yMin] < [$box yMax]} {
            error "$context: overlaps $label at ($x, $y) DBU"
        }
    }
    dict set boxes $inst [list "fixed instance '$name'" $box]
}
proc klt_fixed_groups {verify} {
    set block [ord::get_db_block]
    set boxes {}
    foreach inst [$block getInsts] {
        if {[$inst isFixed]} {
            set label "fixed instance '[$inst getName]'"
            dict set boxes $inst [list $label [$inst getBBox]]
        }
    }
    foreach blockage [$block getBlockages] {
        if {![$blockage isSoft] && [$blockage getMaxDensity] == 0} {
            set box [$blockage getBBox]
            dict set boxes $blockage [list "placement blockage" $box]
        }
    }
""".strip()


def fixed_group_commands(
    groups: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """Return Tcl declarations, pre-placement commands and post-stage checks."""
    calls = []
    for group in groups:
        order = group.get("order", {})
        if "fixed_origin_um" not in order:
            continue
        x, y = (Fraction(str(v)) for v in order["fixed_origin_um"])
        pitch = Fraction(str(order["pitch_um"]))
        for name in group["instances"]:
            calls.append(
                f"    klt_fix_member $block $verify {tcl_literal(group['name'])} "
                f"{tcl_literal(name)} "
                f"{x.numerator} {x.denominator} {y.numerator} {y.denominator}"
            )
            if order["axis"] == "x":
                x += pitch
            else:
                y += pitch
    if not calls:
        return [], [], []
    declarations = [*_FIXED_PLACEMENT_TCL.splitlines(), *calls, "}"]
    return declarations, ["klt_fixed_groups 0"], ["klt_fixed_groups 1"]
