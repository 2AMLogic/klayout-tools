"""Error type, stage order and output markers shared by placement and routing."""

from __future__ import annotations

import re
from typing import Any

#: The four stages ``target_stage`` names, in execution order. See
#: "Stage granularity and invocation shape" in docs/cli/place-and-route.md.
STAGE_ORDER = ("floorplan", "place", "cts", "route")


#: Per-cell-library ``klt extract --deck`` name (issue #948, Epic #700
#: Phase 3) -- used only by the optional ``request.post_route_spef`` path
#: (see :func:`_post_route_spef_metrics`) to resolve which curated
#: ``klayout_tools.decks`` extraction deck matches this run's PDK family.
#: The keys are LEF/Liberty library names; their values are curated
#: extraction deck names accepted by ``get_parasitics_deck``.
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``"sg13g2"`` -- the IHP SG13G2 deck
#: family already registered in ``klayout_tools.decks.__init__`` (used
#: today by ``klt extract``/``klt drc``/``klt lvs`` for this same PDK); no
#: new deck family is introduced here, this table just bridges to it.
#:
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #5825) -> ``"gf180mcu"`` -- 7t and 9t
#: route over the same ``Metal2-Metal5`` stack, so one deck serves both.
_EXTRACT_DECK_FOR_CELL_LIBRARY: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu",
    "gf180mcu_fd_sc_mcu7t5v0": "gf180mcu",
    "sg13g2_stdcell": "sg13g2",
    "asap7sc7p5t_rvt": "asap7",
    "asap7sc7p5t_28_R": "asap7",
}


def _require_extract_deck_for_cell_library(cell_library: str) -> str:
    """Return the ``klt extract`` deck for ``cell_library`` or fail loudly.

    Shared by request validation (so an unsupported library is refused in
    milliseconds, before any engine runs) and by
    :func:`_post_route_spef_metrics` (the late guard).
    """
    extract_deck = _EXTRACT_DECK_FOR_CELL_LIBRARY.get(cell_library)
    if extract_deck is None:
        raise PlaceAndRouteError(
            "request.post_route_spef requires a known klt extract deck for "
            f"cell_library '{cell_library}' (supported: "
            + ", ".join(sorted(_EXTRACT_DECK_FOR_CELL_LIBRARY))
            + ")"
        )
    return extract_deck


#: Default `global_placement -density` target, used when
#: `request.placement_density` (issue #4716) is omitted; verified live at
#: this value against the worked example (docstring above).
_GLOBAL_PLACEMENT_DENSITY = 0.6


#: Markers delimiting each `report_check_types -violators` block in a
#: stage's captured stdout, so :func:`count_violations` can isolate the
#: setup-check block from the hold-check block within one run's combined
#: output.
_SETUP_VIOLATIONS_BEGIN = "===KLT_SETUP_VIOLATIONS_BEGIN==="


_SETUP_VIOLATIONS_END = "===KLT_SETUP_VIOLATIONS_END==="


_HOLD_VIOLATIONS_BEGIN = "===KLT_HOLD_VIOLATIONS_BEGIN==="


_HOLD_VIOLATIONS_END = "===KLT_HOLD_VIOLATIONS_END==="


#: Same marker convention as the setup/hold pair above, isolating the
#: post-route corner sweep's own design-rule-check reports (issue #1709's
#: "Two smaller things found alongside" item 1, folded into that issue's own
#: Builder scope by its 2026-09-15 revision) so :func:`count_violations` can
#: isolate the max-transition block from the max-capacitance block within the
#: sweep invocation's own combined stdout -- see
#: :func:`_design_rule_check_lines`.
_MAX_TRANSITION_VIOLATIONS_BEGIN = "===KLT_MAX_TRANSITION_VIOLATIONS_BEGIN==="


_MAX_TRANSITION_VIOLATIONS_END = "===KLT_MAX_TRANSITION_VIOLATIONS_END==="


_MAX_CAPACITANCE_VIOLATIONS_BEGIN = "===KLT_MAX_CAPACITANCE_VIOLATIONS_BEGIN==="


_MAX_CAPACITANCE_VIOLATIONS_END = "===KLT_MAX_CAPACITANCE_VIOLATIONS_END==="


#: Issue #2357: a **second**, library-only pass of the same two
#: ``report_check_types`` blocks above, run *before*
#: :func:`_design_rule_constraint_lines` applies any caller-stated
#: ``constraints.max_transition_ns``/``.max_capacitance_pf`` override to
#: ``[current_design]`` -- see :func:`_design_rule_check_lines`'s
#: ``transition_begin``/``transition_end``/``capacitance_begin``/
#: ``capacitance_end`` parameters and
#: :func:`~klayout_tools.place_and_route_sta._corner_sweep_script_lines`
#: for where the two passes are ordered. Distinct markers so
#: :func:`count_violations` can isolate this pass's own stdout block from
#: the effective (library-*and*-constraint) one the original markers above
#: still isolate.
_MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN = (
    "===KLT_MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN==="
)


_MAX_TRANSITION_LIBRARY_VIOLATIONS_END = (
    "===KLT_MAX_TRANSITION_LIBRARY_VIOLATIONS_END==="
)


_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN = (
    "===KLT_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN==="
)


_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END = (
    "===KLT_MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END==="
)


#: Same marker convention as the setup/hold pair above, isolating
#: `check_antennas`'s own stdout (run post-`repair_antennas`, `"route"`
#: stage only) so :func:`_count_antenna_violations` can parse its summary
#: count line without picking up unrelated output.
_ANTENNA_VIOLATIONS_BEGIN = "===KLT_ANTENNA_VIOLATIONS_BEGIN==="


_ANTENNA_VIOLATIONS_END = "===KLT_ANTENNA_VIOLATIONS_END==="


#: Same marker convention as the setup/hold/antenna pairs above, isolating
#: the ``request.post_route_spef`` net-name-correlation check's own report
#: (issue #948, extended to two lines by #951) -- see
#: :func:`_spef_sta_script_lines`/:func:`_count_spef_nets_annotated`. Line 1
#: is the SPEF-side ``"<annotated> <total>"`` pair (how many of the SPEF's own
#: net names resolve to a net in the linked design), line 2 the design-side
#: pair (how many of the *design's* nets the SPEF names at all).
_SPEF_NET_CHECK_BEGIN = "===KLT_SPEF_NET_CHECK_BEGIN==="


_SPEF_NET_CHECK_END = "===KLT_SPEF_NET_CHECK_END==="


_SPEF_NET_CHECK_RE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")


class PlaceAndRouteError(Exception):
    """Raised when a place-and-route run cannot even reach the requested
    ``target_stage``: a missing/malformed request file, an unresolvable/
    unreadable netlist, an unresolvable ``pdk.cell_library``/``corner``/LEF,
    a floorplan spec naming more than one method, or an OpenROAD engine
    error partway through a stage.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching ``klt synthesize``'s ``SynthesizeError``. Per the
    contract spike section 5's "Partial-completion design": a run that
    reaches (or exceeds) the requested ``target_stage`` is *always* a
    success (``exit 0``, ``stage_reached`` >= ``target_stage``) even when it
    stopped short of a *later* stage the request never asked for -- this
    exception is raised only when a run fails to reach the stage it was
    actually asked to reach.

    Optional fields carry a machine-readable ``code``, a batch ``job_id``
    and structured ``details``, such as congestion evidence from a failed
    local route.
    """

    def __init__(
        self,
        *args: object,
        code: str | None = None,
        job_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(*args)
        self.code = code
        self.job_id = job_id
        self.details = details
