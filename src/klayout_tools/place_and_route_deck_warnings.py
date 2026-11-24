"""Deck-known configuration warnings for ``klt place-and-route`` (issue #7311).

Every other ``warnings[]`` producer reports what the run's DEF was *measured*
to contain (:mod:`klayout_tools.place_and_route_power_audit`) or what the
request left out (antenna repair, the clock constraint, the layer map). None
of them could say "this accepted request shape is known to trip the bundled
deck" -- which two gf180mcu shapes are:

- an explicit ``power.connects[].split_cuts``: the ORFS
  ``-split_cuts {Metal3 0.128}`` tuning left 2,624
  ``metal2/3/4.ongrid.1``, ``via2/via3.ongrid.1`` and ``via2/via3.width.1``
  hits under ``klt drc --deck gf180mcu`` on a routed ``gcd`` (#4301), which
  is why the ``gf180mcu_*_6M`` presets omit it;
- the per-library ``tapcell -distance`` default (100 um for both gf180mcu
  libraries, transcribed from ORFS) against the deck's 15 um 5 V/6 V
  ``comp.tap_distance.{nwell,psub}.mv.1`` tap reach (#3793).

:func:`deck_warnings` derives one string per trigger from the validated
request and its ``power`` echo alone -- no DRC run, no refusal, no default
change -- so ``status``, exit code and ``route_drc_violation_count`` keep
their meanings, and the CLI mirrors the entries to stderr like every other
``warnings[]`` string. Whether the deck misfires on merged via arrays or
``pdngen`` is genuinely off-grid is deliberately left open (#4348): these
strings only make the known-dirty configurations visible. The module lives
outside ``place_and_route.py`` to keep that file under its size ceiling
(the ``place_and_route_unconstrained.py`` precedent).
"""

from __future__ import annotations

from typing import Any

from .decks import TapDistanceSpec, get_deck

#: The gf180mcu 5 V standard-cell libraries both triggers are stated for.
#: Every other supported library is out of scope: the measured ``-split_cuts``
#: evidence and the MV tap-distance rules are both gf180mcu facts.
GF180MCU_MV_LIBRARIES = ("gf180mcu_fd_sc_mcu9t5v0", "gf180mcu_fd_sc_mcu7t5v0")

#: The deck both triggers are measured against.
GF180MCU_DECK = "gf180mcu"

#: The one ``-split_cuts`` value measured against that deck (#4301, on a
#: routed ``gcd``). Any other value is *unverified*, not clean.
MEASURED_DIRTY_SPLIT_CUTS = ("Metal3", 0.128)

#: ``klt drc --deck gf180mcu`` rule ids the measured ``-split_cuts`` run
#: tripped on the PDN via stacks (``VIA_via2_3_*`` cells and their landing
#: pads).
SPLIT_CUTS_DRC_RULES = (
    "metal2.ongrid.1",
    "metal3.ongrid.1",
    "metal4.ongrid.1",
    "via2.ongrid.1",
    "via3.ongrid.1",
    "via2.width.1",
    "via3.width.1",
)

#: The deck's 5 V/6 V tap-reach rules (DRM ``DF.13_MV``/``DF.14_MV``, added by
#: #3793) that apply to the 5 V cells both libraries above are made of.
TAP_DISTANCE_MV_RULES = (
    "comp.tap_distance.nwell.mv.1",
    "comp.tap_distance.psub.mv.1",
)


def _um(value: float) -> str:
    """``15.0``/``100``/``12.5`` -> ``15``/``100``/``12.5``."""
    return f"{value:g}"


def gf180mcu_mv_tap_distance_um() -> float | None:
    """The deck's own 5 V/6 V tap reach in um, read from its rule table
    rather than restated as a second literal, so the warning and ``klt drc``
    cannot drift apart. ``None`` when the deck no longer declares those
    rules -- the warning then stays silent instead of citing rules that do
    not exist."""
    limits = [
        rule.region_spec.max_distance_um
        for rule in get_deck(GF180MCU_DECK)
        if rule.id in TAP_DISTANCE_MV_RULES
        and isinstance(rule.region_spec, TapDistanceSpec)
    ]
    return min(limits) if limits else None


def split_cuts_warning(layer: str, width_um: float, lower: str, upper: str) -> str:
    """The ``-split_cuts`` entry for the ``add_pdn_connect`` between
    ``lower`` and ``upper``. The measured-dirty claim names only the ORFS
    value it was measured on; the applied value is named separately."""
    measured_layer, measured_width = MEASURED_DIRTY_SPLIT_CUTS
    return (
        "gf180mcu deck warning (issue #7311): request.power.connects applied "
        f"add_pdn_connect -split_cuts {{{layer} {width_um}}} between {lower} "
        f"and {upper}; klt drc --deck {GF180MCU_DECK} is measured DRC-dirty "
        f"for -split_cuts {{{measured_layer} {measured_width}}} (issue #4301: "
        + ", ".join(SPLIT_CUTS_DRC_RULES)
        + " on the PDN via stacks) and other split_cuts values are unverified; "
        "the gf180mcu_9t_6M and gf180mcu_7t_6M presets deliberately omit "
        "split_cuts; route_drc_violation_count 0 is OpenROAD's own check only, "
        f"so run klt drc --deck {GF180MCU_DECK} on gds_path"
    )


def tap_distance_warning(
    distance_um: float, limit_um: float, override_um: float | None
) -> str:
    """The tap-pitch entry for an effective ``distance_um`` above the deck's
    ``limit_um``. ``override_um`` is the validated
    ``request.power.tapcell.distance_um`` (``None`` = the per-library default
    was used), which the text names as the pitch's source. Worded as "may
    fail": OpenROAD's ``tapcell -distance`` is a placement input whose
    relation to the deck's cell-to-tap distance is not verified here."""
    source = (
        "a request.power.tapcell.distance_um override"
        if override_um is not None
        else "the per-library default"
    )
    nwell_rule, psub_rule = TAP_DISTANCE_MV_RULES
    return (
        "gf180mcu deck warning (issue #7311): effective tapcell pitch "
        f"{_um(distance_um)} um ({source}) exceeds the {_um(limit_um)} um reach "
        f"of klt drc --deck {GF180MCU_DECK}'s {nwell_rule} and {psub_rule} "
        "rules (5V/6V cells), so the placed design may fail them; the pitch "
        "is a placement input, not a measured diffusion-to-tap distance -- "
        f"run klt drc --deck {GF180MCU_DECK} on gds_path, or lower "
        "request.power.tapcell.distance_um"
    )


def _tap_distance_warning(
    power: dict[str, Any], power_info: dict[str, Any]
) -> str | None:
    distance_um = power_info["tapcell_distance_um"]
    limit_um = gf180mcu_mv_tap_distance_um()
    if distance_um is None or limit_um is None or distance_um <= limit_um:
        return None
    return tap_distance_warning(distance_um, limit_um, power["tapcell_distance_um"])


def deck_warnings(
    cell_library: str,
    power: dict[str, Any] | None,
    power_info: dict[str, Any],
) -> list[str]:
    """Static, request-derived ``warnings[]`` entries for one run: the
    ``-split_cuts`` entry for every ``power.connects[]`` echo entry that
    carries one, then the tap-pitch entry when the effective pitch exceeds
    the deck's MV reach. ``[]`` unless ``cell_library`` is a
    :data:`GF180MCU_MV_LIBRARIES` entry and ``request.power`` was supplied.

    ``power`` is the validated ``request.power`` block (``None`` when the
    request omitted it) and ``power_info`` the response's ``power`` echo, so
    the strings name exactly what ``power.connects``/``power.
    tapcell_distance_um`` report for the same run."""
    if power is None or cell_library not in GF180MCU_MV_LIBRARIES:
        return []
    warnings = [
        split_cuts_warning(
            connect["split_cuts"]["layer"],
            connect["split_cuts"]["width_um"],
            *connect["layers"],
        )
        for connect in power_info["connects"]
        if connect.get("split_cuts") is not None
    ]
    tap_warning = _tap_distance_warning(power, power_info)
    if tap_warning is not None:
        warnings.append(tap_warning)
    return warnings
