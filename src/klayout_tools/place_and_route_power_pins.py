"""Global-connect pin patterns used to build and describe an OpenROAD PDN."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

#: Per-cell-library ``add_global_connection`` pin-pattern rules for the
#: optional ``request.power`` PDN stage (issue #1091) -- same not-derivable-
#: from-the-install, verified-not-guessed posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE`/
#: :data:`_ANTENNA_DIODE_CELLS` above, but sourced from each platform's own
#: PDN grid config rather than `config.mk`: `add_global_connection`'s own
#: `-pin_pattern` alias list (`VDDPE`/`VDDCE`/etc.) is a *platform*
#: convention describing how real macro/IO-ring instances name their PG
#: pins, not something derivable from a standard cell's own LEF `PIN ...
#: USE POWER/GROUND` entries.
#:
#: Each tuple is `(net_role, pin_pattern, is_primary)`. `net_role` selects
#: which of `request.power.power_net`/`.ground_net` the generated
#: `add_global_connection -net {...}` call names; exactly one entry per role
#: carries `is_primary=True`, emitting that call's `-power`/`-ground` flag --
#: the pin `pdngen` treats as that net's primary, connectivity-defining
#: port, mirroring `add_global_connection`'s own flag semantics (confirmed
#: live, `openroad -no_init`, `help add_global_connection`, this repo's own
#: sandboxed OpenROAD 26Q3-1278-g4421880472 install, 2026-08-17). The
#: pattern list itself is copied verbatim from each platform's own PDN
#: config, fetched 2026-08-17 from
#: `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`:
#: - `sky130_fd_sc_hd` -> `platforms/sky130hd/pdn.tcl`
#: - `gf180mcu_fd_sc_mcu9t5v0` ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg`
#: - `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_7t_6M.cfg`'s `global
#:   connections` section -- byte-for-byte the same `add_global_connection`
#:   pattern list as the `9t` config above (`PDN_TCL ?= .../pdn_grid_strategy_
#:   $(TRACK_OPTION)_6M.cfg` selects the sibling file per `TRACK_OPTION`, but
#:   both files' global-connection rules are identical), fetched 2026-09-11
#:   from the same `OpenROAD-flow-scripts` @ `master`.
#: - `sg13g2_stdcell` (issue #1784) -> IHP's own LibreLane platform config
#:   (`libs.tech/librelane/config.tcl`, IHP-Open-PDK v0.3.0) names exactly
#:   `SCL_POWER_PINS "VDD"` / `SCL_GROUND_PINS "VSS"` -- no macro/IO-ring
#:   pin-alias list at all, unlike sky130/gf180mcu's `VDDPE`/`VDDCE`/etc
#:   families (this platform's own PDN config has no macro-power-domain
#:   convention to alias). Cross-checked against the installed
#:   `libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef`: every standard cell's
#:   only power/ground pins are `VDD` (`USE POWER`) / `VSS` (`USE GROUND`),
#:   with no other `USE POWER`/`USE GROUND` pin name anywhere in the file --
#:   so this entry is deliberately just the two primary patterns, no
#:   `is_primary=False` aliases.
_POWER_PIN_PATTERNS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "sky130_fd_sc_hd": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "VPWR", False),
        ("power", "VPB", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "VGND", False),
        ("ground", "VNB", False),
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "sg13g2_stdcell": (
        ("power", "^VDD$", True),
        ("ground", "^VSS$", True),
    ),
}


def applied_power_pins(
    cell_library: str, library: dict[str, Any] | None
) -> Sequence[tuple[str, str, bool]]:
    if library is not None and "power_pins" in library:
        return library["power_pins"]
    return _POWER_PIN_PATTERNS[cell_library]


def hd_rail_labels(
    power: dict[str, Any], pins: Sequence[tuple[str, str, bool]]
) -> dict[str, str] | None:
    """Identify only independently known HD rail labels with unambiguous rules.

    Arbitrary OpenROAD regular expressions stay opaque. A literal that only
    partly matches a rail is also inconclusive without the engine's matcher.
    """
    matched: dict[str, set[str]] = {"VPWR": set(), "VGND": set()}
    for role, pattern, _primary in pins:
        literal = re.fullmatch(r"\^?([A-Za-z0-9_]+)\$?", pattern)
        if literal is None:
            return None
        name = literal.group(1)
        for label, roles in matched.items():
            if label == name:
                roles.add(role)
            elif name in label:
                return None
    if matched != {"VPWR": {"power"}, "VGND": {"ground"}}:
        return None
    return {"VPWR": power["power_net"], "VGND": power["ground_net"]}


def rail_label_evidence(
    power: dict[str, Any], cell_library: str, library: dict[str, Any] | None
) -> dict[str, Any]:
    if cell_library != "sky130_fd_sc_hd":
        return {}
    return {
        "library_rail_labels": hd_rail_labels(
            power, applied_power_pins(cell_library, library)
        )
    }
