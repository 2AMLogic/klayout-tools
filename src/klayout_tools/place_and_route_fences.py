"""OpenDB placement fences for opt-in exact-name instance groups."""

from __future__ import annotations

from typing import Any

from ._tcl import tcl_literal

# Install after linking, floorplan initialization and fixed-cell placement.
# OpenDB's EXCLUSIVE region writes a DEF FENCE. Both placers use the dbGroup
# attached to the region, and both objects survive write_db/read_db.
_INSTALL_FENCE = """\
proc klt_fence_instance {b member} {
    set inst [$b findInst $member]
    if {$inst != "NULL"} { return $inst }
    # The linked database escapes bus brackets and hierarchy separators.
    # Index these DEF spellings once, only when an exact db name was absent.
    variable klt_fence_escaped
    if {![info exists klt_fence_escaped]} {
        set klt_fence_escaped [dict create]
        foreach candidate [$b getInsts] {
            set raw [$candidate getName]
            set plain [string map [list [format %c 92] ""] $raw]
            if {$plain ne $raw} { dict lappend klt_fence_escaped $plain $candidate }
        }
    }
    if {![dict exists $klt_fence_escaped $member]} { return NULL }
    set hits [dict get $klt_fence_escaped $member]
    if {[llength $hits] != 1} { error "ambiguous instance '$member'" }
    return [lindex $hits 0]
}
proc klt_install_fence {name members x0 y0 x1 y1} {
    set b [ord::get_db_block]
    set core [$b getCoreArea]
    set scale [$b getDbUnitsPerMicron]
    set x0 [expr {max([$core xMin], wide(ceil($x0 * $scale)))}]
    set y0 [expr {max([$core yMin], wide(ceil($y0 * $scale)))}]
    set x1 [expr {min([$core xMax], wide(floor($x1 * $scale)))}]
    set y1 [expr {min([$core yMax], wide(floor($y1 * $scale)))}]
    if {$x1 <= $x0 || $y1 <= $y0} {
        error "placement fence '$name' has no area inside the core"
    }
    set id 0
    while {[$b findRegion klt_fence_$id] != "NULL" ||
           [$b findGroup klt_fence_$id] != "NULL"} {
        incr id
    }
    set region [odb::dbRegion_create $b klt_fence_$id]
    $region setRegionType EXCLUSIVE
    odb::dbBox_create $region $x0 $y0 $x1 $y1
    set group [odb::dbGroup_create $b klt_fence_$id]
    $region addGroup $group
    set area 0
    foreach member $members {
        set inst [klt_fence_instance $b $member]
        if {$inst == "NULL"} {
            error "placement fence '$name' has unknown instance '$member'"
        }
        set master [$inst getMaster]
        if {![$master isCoreAutoPlaceable]} {
            error "placement fence '$name' requires a standard cell for '$member'"
        }
        if {[$inst getGroup] != "NULL" || [$inst getRegion] != "NULL"} {
            error "placement fence '$name' conflicts with a constraint on '$member'"
        }
        if {[$inst isFixed]} {
            set bounds [$inst getBBox]
            if {[$bounds xMin] < $x0 || [$bounds yMin] < $y0 ||
                [$bounds xMax] > $x1 || [$bounds yMax] > $y1} {
                error "placement fence '$name' conflicts with fixed instance '$member'"
            }
        }
        set width [$master getWidth]
        set height [$master getHeight]
        incr area [expr {$width * $height}]
        if {$width > $x1 - $x0 || $height > $y1 - $y0 || $area > ($x1-$x0)*($y1-$y0)} {
            error "placement fence '$name' is too small for its requested cells"
        }
        $group addInst $inst
    }
}"""


def placement_fence_lines(groups: list[dict[str, Any]] | None) -> list[str]:
    fences = [group for group in groups or [] if group.get("enforce_region")]
    if not fences:
        return []
    lines = _INSTALL_FENCE.splitlines()
    for group in fences:
        members = " ".join(
            tcl_literal(name) for name in dict.fromkeys(group["instances"])
        )
        bounds = " ".join(str(value) for value in group["region_um"])
        name = tcl_literal(group["name"])
        command = f"klt_install_fence {name} [list {members}] {bounds}"
        lines.extend(
            [
                f"if {{[catch {{{command}}} reason]}} {{",
                '    puts stderr "Error: $reason"',
                "    exit 1",
                "}",
            ]
        )
    return lines


def fence_failure_context(stage: str, groups: list[dict[str, Any]]) -> str:
    """Identify the requested constraints if an engine placement step fails."""
    names = [repr(group["name"]) for group in groups if group.get("enforce_region")]
    return (
        f" (placement fences: {', '.join(names)})"
        if names and stage != "floorplan"
        else ""
    )


def checked_fence_reports(
    reports: list[dict[str, Any]] | None,
    groups: list[dict[str, Any]],
    error: type[Exception],
) -> list[dict[str, Any]] | None:
    """Require final DEF evidence for every requested fence member."""
    by_name = {report["name"]: report for report in reports or []}
    for group in groups:
        if group.get("enforce_region"):
            report = by_name.get(group["name"], {})
            if report.get("within_region") is not True or report.get("matched") != len(
                set(group["instances"])
            ):
                raise error(
                    f"final DEF does not satisfy placement fence {group['name']!r}"
                )
    return reports
