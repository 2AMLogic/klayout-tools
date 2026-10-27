"""Request validation for ``klt place-and-route``: the pure validators and
resolvers that take a request dict (or one of its sections) and return
validated values or raise :class:`PlaceAndRouteError`.

Split out of ``place_and_route.py`` (issue #3699), following the sibling-module
precedent of ``place_and_route_sta.py`` (issue #1808). Nothing here invokes
OpenROAD or emits a stage script -- that stays in ``place_and_route.py``. Every
public-by-convention name here is re-exported from
:mod:`klayout_tools.place_and_route`, so ``place_and_route.<name>`` (the CLI,
:mod:`klayout_tools.eval`, the batch backend and the test suite) keeps
resolving unchanged.

:class:`PlaceAndRouteError` and the validation-only tables
(``_MACRO_ORIENTATIONS``, ``_FLOORPLAN_METHOD_FIELDS``,
``_ROUTING_LAYER_STACK``, ``_PDN_PRESETS``) live here, so this module has no
module-scope dependency on ``place_and_route.py``. The few tables the Tcl
emitter shares (``STAGE_ORDER``, ``_ROUTING_LAYER_RANGE``,
``_NO_TAPCELL_LIBRARIES``) stay in ``place_and_route.py`` and are imported
inside the validators that read them.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable
from typing import Any

from .lef_header import read_lef_header
from .pdk import descriptor_views, lef_files
from .pdk_cells import resolve_liberty_for_cell_library

#: Valid ``request.macros[].orientation`` values -- OpenROAD's own
#: orientation vocabulary (the same set ``place_macro``/``place_cell``
#: accept), matching LEF's ``ORIENT`` enumeration.
_MACRO_ORIENTATIONS = frozenset(
    {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
)


#: Fields the floorplan spec's three mutually-exclusive methods each need,
#: used both to select the ``initialize_floorplan``/``read_def`` Tcl branch
#: and to detect a request naming fields from more than one method (mirrors
#: ORFS's own ``methods_defined > 1`` check, survey section 2).
_FLOORPLAN_METHOD_FIELDS: dict[str, tuple[str, ...]] = {
    "utilization": ("utilization_pct",),
    "explicit": ("die_area_um", "core_area_um"),
    "def": ("def_path",),
}


#: Per-cell-library **routing-layer stack**, bottom-up -- every layer the
#: platform's own tech LEF declares ``TYPE ROUTING``, in stack order (issue
#: #2473). :data:`_ROUTING_LAYER_RANGE` above names only the two endpoints of
#: the *signal* range ``set_routing_layers -signal`` opens; this table is what
#: lets :func:`_signal_routing_layers` expand that range into the concrete
#: layer set, and therefore what lets :func:`_validate_io` reject an
#: ``io.layer_h``/``io.layer_v`` naming a real routing layer the router is
#: nevertheless never allowed to route signals on.
#:
#: Why that matters (issue #2473's own root cause): a top-level pin is only
#: reachable if the router can draw *conductor of that layer* up to it.
#: ``place_pins`` will happily place a port on a below-range layer, and
#: OpenROAD then writes the port rectangle to that layer's LEF ``PIN``/
#: ``LEFPIN`` purpose -- for sky130's ``li1`` that is GDS ``67/16``, not the
#: ``67/20`` conductor an extraction deck reads -- while the actual routing
#: reaches the cell pin from above through ``mcon``. The result is a DRC-clean
#: GDS whose top-level pins do not electrically exist in ``klt extract``'s
#: output, with `klt lvs` still reporting ``"match"`` because its structural
#: comparer does not need pins. Rejecting the request is two minutes of
#: routing cheaper than discovering that downstream.
#:
#: Same verified-not-guessed posture as every other reference table in this
#: module, one source per entry:
#:
#: - ``sky130_fd_sc_hd`` -> ``li1``, ``met1``..``met5``. Verified live
#:   against a real sky130A install (volare,
#:   ``libs.ref/sky130_fd_sc_hd/techlef/sky130_fd_sc_hd__nom.tlef``): its
#:   six ``TYPE ROUTING`` layers are ``li1``, ``met1``, ``met2``, ``met3``,
#:   ``met4``, ``met5``, declared in that order. ``li1`` sitting *below*
#:   this library's own ``MIN_ROUTING_LAYER ?= met1`` is exactly what
#:   :data:`_ROUTING_LAYER_RANGE`'s own docstring already records ("its
#:   cells pin out on ``li1``, below ``met1`` entirely").
#: - ``gf180mcu_fd_sc_mcu9t5v0``/``gf180mcu_fd_sc_mcu7t5v0`` -> ``Metal1``..
#:   ``Metal5``, from the same platform tech LEF
#:   (``lef/gf180mcu_5LM_1TM_9K_9t_tech.lef``) :data:`_ROUTING_LAYER_RANGE`
#:   already cites for declaring "``Metal1``..``Metal5`` as its five ``TYPE
#:   ROUTING`` layers", with ``Metal1`` deliberately left out of the signal
#:   range for pin access.
#: - ``sg13g2_stdcell`` -> ``Metal1``..``Metal5``, ``TopMetal1``,
#:   ``TopMetal2``, from the same ``libs.ref/sg13g2_stdcell/lef/
#:   sg13g2_tech.lef`` :data:`_ROUTING_LAYER_RANGE` already cites for
#:   declaring "exactly seven routing layers", again with ``Metal1``
#:   reserved for pin access.
#:
#: A ``cell_library`` absent from this table (or whose
#: :data:`_ROUTING_LAYER_RANGE` endpoints are not both in its stack) simply
#: skips the check -- an unverified library keeps the pre-#2473 behaviour
#: rather than being rejected on a guess.
_ROUTING_LAYER_STACK: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": ("li1", "met1", "met2", "met3", "met4", "met5"),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
    ),
    "sg13g2_stdcell": (
        "Metal1",
        "Metal2",
        "Metal3",
        "Metal4",
        "Metal5",
        "TopMetal1",
        "TopMetal2",
    ),
}


#: Named, per-platform default PDN recipes selectable via
#: ``request.power.preset`` (issue #2123, follow-up to #2086/#2122). Same
#: "not derivable from the resolved PDK install itself, verified not
#: guessed" posture as :data:`_TAPCELL_CELLS`/:data:`_FILLER_CELLS`/
#: :data:`_ROUTING_LAYER_RANGE` above -- strap layers, widths, pitches and
#: via-stack tuning are a *platform* PDN decision, recorded in each
#: platform's own PDN config, not something a standard cell's LEF states.
#:
#: Why this table exists: before it, the only way to build a real PDN was
#: to transcribe a platform's ``pdn_grid_strategy_*.cfg``/``pdn.tcl`` field
#: for field into ``request.power.straps``/``.connects``. Issue #2086's
#: measured audit (:mod:`klayout_tools.place_and_route_power_audit`) now
#: *catches* a half-transcribed block (``power.placed.status`` becomes
#: ``"partial"`` and names the failed checks), but catching a silent
#: half-transcription after the fact is strictly worse than not needing the
#: transcription at all. Each entry below is this repo's own transcription
#: of the platform config, made once and cited, so a caller need not repeat
#: it. The resolved ``straps``/``connects`` then run through exactly the
#: same :func:`_validate_power` checks and are echoed in exactly the same
#: response fields an explicit request produces -- a preset is a shorthand,
#: never an opaque alternate code path.
#:
#: Values transcribed 2026-09-19 from ``The-OpenROAD-Project/
#: OpenROAD-flow-scripts`` @ ``95ebc50a258390f4c7896e5f04db743f62279c2d``
#: (the repo's own ``master`` head at that date):
#:
#: - ``"gf180mcu_7t_6M"`` -> ``flow/platforms/gf180/openROAD/pdn/
#:   pdn_grid_strategy_7t_6M.cfg`` (``gf180mcu_fd_sc_mcu7t5v0``; the file
#:   ``platforms/gf180/config.mk``'s own ``PDN_TCL ?= .../
#:   pdn_grid_strategy_$(TRACK_OPTION)_6M.cfg`` selects for
#:   ``TRACK_OPTION = 7t``), ``standard cell grid`` section verbatim::
#:
#:       add_pdn_stripe  -grid {block} -layer {Metal1} -width {0.600}
#:           -pitch {3.92} -offset {0} -followpins
#:       add_pdn_stripe  -grid {block} -layer {Metal4} -width {4.480}
#:           -spacing {0.56} -pitch {44.8} -offset {22.4}
#:       add_pdn_stripe  -grid {block} -layer {Metal5} -width {4.480}
#:           -pitch {89.6} -offset {44.8}
#:       add_pdn_connect -grid {block} -layers {Metal1 Metal4}
#:           -max_columns {5} -ongrid {Metal2 Metal3 Metal4}
#:           -split_cuts {Metal3 0.128}
#:       add_pdn_connect -grid {block} -layers {Metal4 Metal5}
#:
#:   (line-wrapped here only to fit this file's 88-column limit; the config
#:   itself writes each call on one line)
#:
#: - ``"gf180mcu_9t_6M"`` -> the sibling ``pdn_grid_strategy_9t_6M.cfg``
#:   (``gf180mcu_fd_sc_mcu9t5v0``, the platform's own ``TRACK_OPTION ?= 9t``
#:   default). Byte-for-byte identical to the ``7t`` config above except its
#:   ``Metal1`` row rail, which is the taller row's own
#:   ``-width {0.900} -pitch {5.040}`` instead of ``{0.600}``/``{3.92}``.
#: - ``"sky130hd"`` -> ``flow/platforms/sky130hd/pdn.tcl``
#:   (``sky130_fd_sc_hd``), ``standard cell grid`` section verbatim::
#:
#:       add_pdn_stripe -grid {grid} -layer {met1} -width {0.48}
#:           -pitch {5.44} -offset {0} -followpins
#:       add_pdn_stripe -grid {grid} -layer {met4} -width {1.600}
#:           -pitch {27.140} -offset {13.570}
#:       add_pdn_stripe -grid {grid} -layer {met5} -width {1.600}
#:           -pitch {27.200} -offset {13.600}
#:       add_pdn_connect -grid {grid} -layers {met1 met4}
#:       add_pdn_connect -grid {grid} -layers {met4 met5}
#:
#:   Both of that file's ``add_pdn_connect`` lines carry no via-stack flags
#:   at all, so this entry's ``connects`` is deliberately empty rather than
#:   listing two all-``None`` entries: an unnamed pair already emits exactly
#:   the plain ``add_pdn_connect -grid {grid} -layers {lower upper}`` call
#:   the config itself has (see :func:`_pdn_connect_spec`), and the response
#:   still echoes one ``power.connects[]`` entry per consecutive pair
#:   regardless (:func:`_pdn_connects_applied`).
#:
#: ``"sg13g2"`` (issue #2447) is the one entry that is **not** from ORFS:
#: IHP-Open-PDK ships no ORFS platform at all, so its recipe comes from
#: IHP's own LibreLane platform config instead --
#: ``libs.tech/librelane/config.tcl`` @ IHP-Open-PDK v0.3.0, plus that
#: file's per-library sibling ``libs.tech/librelane/sg13g2_stdcell/
#: config.tcl`` for the rail width::
#:
#:       set ::env(PDN_RAIL_LAYER) Metal1         ;# PDN_RAIL_OFFSET 0
#:       set ::env(PDN_VERTICAL_LAYER) TopMetal1
#:       set ::env(PDN_HORIZONTAL_LAYER) TopMetal2
#:       set ::env(PDN_VWIDTH) 2.2   ;# VSPACING 4.0 VPITCH 75.6 VOFFSET 13.6
#:       set ::env(PDN_HWIDTH) 2.2   ;# HSPACING 4.0 HPITCH 75.6 HOFFSET 13.6
#:       set ::env(PDN_RAIL_WIDTH) 0.44           ;# sg13g2_stdcell/config.tcl
#:
#: Exactly one number in that entry is **not** a transcription, and its
#: ``source`` string says so rather than reading like the line-for-line
#: citations the other three carry: the ``Metal1`` rail's ``pitch_um``
#: (7.56). LibreLane states no rail pitch -- ``-followpins`` takes its
#: placement from the rows themselves -- so 7.56 is this repo's own
#: derivation, 2x the 3.78 um ``CoreSite`` row height (one VDD/VSS rail
#: pair). LibreLane names no via-stack tuning for this platform either, so
#: this entry's ``connects`` is empty for the same reason ``"sky130hd"``'s
#: is. The whole block was driven through a real ``openroad`` + real
#: IHP-Open-PDK v0.3.0 run under issue #2441 (as an explicit
#: ``power.straps`` request) before being shipped here as a preset.
#:
#: Deliberately **not** carried over from those configs, because this
#: module's PDN path does not emit the corresponding Tcl for *any* request,
#: preset or explicit:
#:
#: - the ``global connections``/``voltage domains`` sections -- this module
#:   builds those from :data:`_POWER_PIN_PATTERNS` plus the caller's own
#:   ``power_net``/``ground_net`` (so a preset leaves both fields alone, and
#:   they keep their ``"VDD"``/``"VSS"`` defaults -- the same net names all
#:   three configs use);
#: - ``define_pdn_grid``'s own ``-pins {Metal5}``/``-pins {met5}`` -- see
#:   :func:`_row_rail_lines`' own docstring for why promoting PG nets into
#:   the top-level DEF ``PINS``/Verilog port list is deliberately avoided
#:   here;
#: - ``pdn.tcl``'s ``macro grids`` section (``define_pdn_grid -macro``),
#:   which is this module's already-documented v1 exclusion (see the module
#:   docstring's own "Scope deliberately excluded" note).
#:
#: Each entry's ``cell_library`` is the standard-cell library the recipe was
#: written for; a preset naming a different library than
#: ``request.pdk.cell_library`` is rejected by
#: :func:`_resolve_pdn_preset` rather than handed to ``pdngen``, where a
#: ``Metal4`` strap on a sky130 stack would surface as an obscure engine
#: error (or, worse, an empty grid the audit then grades ``"partial"``).
_PDN_PRESETS: dict[str, dict[str, Any]] = {
    "gf180mcu_7t_6M": {
        "cell_library": "gf180mcu_fd_sc_mcu7t5v0",
        "source": (
            "OpenROAD-flow-scripts@95ebc50a258390f4c7896e5f04db743f62279c2d:"
            "flow/platforms/gf180/openROAD/pdn/pdn_grid_strategy_7t_6M.cfg"
        ),
        "straps": (
            {
                "layer": "Metal1",
                "width_um": 0.600,
                "pitch_um": 3.92,
                "offset_um": 0.0,
                "followpins": True,
            },
            {
                "layer": "Metal4",
                "width_um": 4.480,
                "pitch_um": 44.8,
                "offset_um": 22.4,
                "spacing_um": 0.56,
            },
            {
                "layer": "Metal5",
                "width_um": 4.480,
                "pitch_um": 89.6,
                "offset_um": 44.8,
            },
        ),
        "connects": (
            {
                "layers": ["Metal1", "Metal4"],
                "max_columns": 5,
                "ongrid": ["Metal2", "Metal3", "Metal4"],
                "split_cuts": {"layer": "Metal3", "width_um": 0.128},
            },
        ),
    },
    "gf180mcu_9t_6M": {
        "cell_library": "gf180mcu_fd_sc_mcu9t5v0",
        "source": (
            "OpenROAD-flow-scripts@95ebc50a258390f4c7896e5f04db743f62279c2d:"
            "flow/platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg"
        ),
        "straps": (
            {
                "layer": "Metal1",
                "width_um": 0.900,
                "pitch_um": 5.040,
                "offset_um": 0.0,
                "followpins": True,
            },
            {
                "layer": "Metal4",
                "width_um": 4.480,
                "pitch_um": 44.8,
                "offset_um": 22.4,
                "spacing_um": 0.56,
            },
            {
                "layer": "Metal5",
                "width_um": 4.480,
                "pitch_um": 89.6,
                "offset_um": 44.8,
            },
        ),
        "connects": (
            {
                "layers": ["Metal1", "Metal4"],
                "max_columns": 5,
                "ongrid": ["Metal2", "Metal3", "Metal4"],
                "split_cuts": {"layer": "Metal3", "width_um": 0.128},
            },
        ),
    },
    "sg13g2": {
        "cell_library": "sg13g2_stdcell",
        "source": (
            "IHP-Open-PDK@v0.3.0:libs.tech/librelane/config.tcl (+ "
            "libs.tech/librelane/sg13g2_stdcell/config.tcl for "
            "PDN_RAIL_WIDTH); the Metal1 rail's pitch of 7.56 is NOT from "
            "that config -- it is this repo's own derivation, 2x the 3.78 "
            "um CoreSite row height (one VDD/VSS rail pair), since "
            "-followpins takes its placement from the rows themselves"
        ),
        "straps": (
            {
                "layer": "Metal1",
                "width_um": 0.44,
                "pitch_um": 7.56,
                "offset_um": 0.0,
                "followpins": True,
            },
            {
                "layer": "TopMetal1",
                "width_um": 2.2,
                "pitch_um": 75.6,
                "offset_um": 13.6,
                "spacing_um": 4.0,
            },
            {
                "layer": "TopMetal2",
                "width_um": 2.2,
                "pitch_um": 75.6,
                "offset_um": 13.6,
                "spacing_um": 4.0,
            },
        ),
        "connects": (),
    },
    "sky130hd": {
        "cell_library": "sky130_fd_sc_hd",
        "source": (
            "OpenROAD-flow-scripts@95ebc50a258390f4c7896e5f04db743f62279c2d:"
            "flow/platforms/sky130hd/pdn.tcl"
        ),
        "straps": (
            {
                "layer": "met1",
                "width_um": 0.48,
                "pitch_um": 5.44,
                "offset_um": 0.0,
                "followpins": True,
            },
            {
                "layer": "met4",
                "width_um": 1.600,
                "pitch_um": 27.140,
                "offset_um": 13.570,
            },
            {
                "layer": "met5",
                "width_um": 1.600,
                "pitch_um": 27.200,
                "offset_um": 13.600,
            },
        ),
        "connects": (),
    },
}


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

    ``code``, ``job_id`` and ``details`` are optional and set only by the
    ``batch`` backend (issue #3518, mirroring ``NetlistError``): a
    machine-readable ``error.code`` for the CLI's error envelope, the batch
    job id, and extra structured envelope fields.
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


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _resolve_netlist(netlist: Any, request_dir: str) -> str:
    if not isinstance(netlist, str) or not netlist:
        raise PlaceAndRouteError("request.netlist must be a non-empty string")
    path = netlist if os.path.isabs(netlist) else os.path.join(request_dir, netlist)
    if not os.path.isfile(path):
        raise PlaceAndRouteError(f"netlist not found: {netlist}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise PlaceAndRouteError(f"could not read netlist '{netlist}': {exc}") from exc
    return os.path.abspath(path)


def _validate_floorplan(floorplan: Any) -> dict[str, Any]:
    if not isinstance(floorplan, dict):
        raise PlaceAndRouteError("request.floorplan must be a JSON object")

    method = floorplan.get("method")
    if method not in _FLOORPLAN_METHOD_FIELDS:
        raise PlaceAndRouteError(
            "request.floorplan.method must be one of: "
            + ", ".join(sorted(_FLOORPLAN_METHOD_FIELDS))
        )

    methods_defined = [
        name
        for name, fields in _FLOORPLAN_METHOD_FIELDS.items()
        if any(floorplan.get(field) is not None for field in fields)
    ]
    if len(methods_defined) > 1:
        raise PlaceAndRouteError(
            "request.floorplan specifies fields from more than one floorplan "
            f"method: {', '.join(sorted(methods_defined))} -- exactly one "
            "method's fields may be set (mirroring OpenROAD-flow-scripts' "
            "own methods_defined > 1 check)"
        )

    required = _FLOORPLAN_METHOD_FIELDS[method]
    missing = [field for field in required if floorplan.get(field) is None]
    if missing:
        raise PlaceAndRouteError(
            f"request.floorplan.method '{method}' requires: {', '.join(required)} "
            f"(missing: {', '.join(missing)})"
        )

    if method in ("utilization", "explicit") and not floorplan.get("site"):
        raise PlaceAndRouteError(
            f"request.floorplan.site is required for method '{method}'"
        )

    if method == "explicit":
        for field in ("die_area_um", "core_area_um"):
            value = floorplan[field]
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 4
                or not all(isinstance(v, (int, float)) for v in value)
            ):
                raise PlaceAndRouteError(
                    f"request.floorplan.{field} must be an array of 4 numbers "
                    "[llx, lly, urx, ury]"
                )

    if floorplan.get("tracks") is not None:
        _validate_floorplan_tracks(floorplan["tracks"])

    return dict(floorplan)


_TRACK_KEYS = ("x_offset_um", "x_pitch_um", "y_offset_um", "y_pitch_um")


def _validate_floorplan_tracks(tracks: Any) -> None:
    """Issue #4088: validate ``request.floorplan.tracks``, the optional
    per-layer ``make_tracks`` definitions that replace the bare
    ``make_tracks`` (which aborts on a tech LEF with a negative ``OFFSET``)."""

    if not isinstance(tracks, list) or not tracks:
        raise PlaceAndRouteError(
            "request.floorplan.tracks must be a non-empty array of objects"
        )
    seen: set[str] = set()
    for index, entry in enumerate(tracks):
        where = f"request.floorplan.tracks[{index}]"
        if not isinstance(entry, dict):
            raise PlaceAndRouteError(f"{where} must be a JSON object")
        layer = entry.get("layer")
        if not isinstance(layer, str) or not layer:
            raise PlaceAndRouteError(f"{where}.layer must be a non-empty string")
        if layer in seen:
            raise PlaceAndRouteError(
                f"request.floorplan.tracks names layer '{layer}' more than once"
            )
        seen.add(layer)
        for key in _TRACK_KEYS:
            value = entry.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise PlaceAndRouteError(f"{where}.{key} must be a finite number")
            if key.endswith("pitch_um") and value <= 0:
                raise PlaceAndRouteError(f"{where}.{key} must be > 0")
            if key.endswith("offset_um") and value < 0:
                raise PlaceAndRouteError(f"{where}.{key} must be >= 0")


def validate_tech_lef_track_offsets(
    floorplan: dict[str, Any], tech_lef: str | None
) -> None:
    """Issue #4088: reject, before OpenROAD launches, a tech LEF whose routing
    layers declare a negative ``OFFSET`` (legal LEF, but the bare
    ``make_tracks`` aborts with IFP-0039 on it) unless ``floorplan.tracks``
    takes over track creation. Skipped when the LEF cannot be read here --
    the engine reports that itself."""

    if floorplan.get("tracks") is not None or not tech_lef:
        return
    try:
        header = read_lef_header(tech_lef)
    except OSError:
        return
    offenders = []
    for layer in header.get("layers", []):
        if layer.get("type") != "ROUTING":
            continue
        for axis in ("x", "y"):
            offset = layer.get(f"offset_{axis}_um")
            if offset is not None and offset < 0:
                offenders.append(f"{layer['name']} ({axis} OFFSET {offset:g})")
    if offenders:
        raise PlaceAndRouteError(
            "tech LEF declares a negative OFFSET on routing layer(s) "
            + ", ".join(offenders)
            + "; OpenROAD's automatic make_tracks rejects that (IFP-0039). "
            "Set request.floorplan.tracks to give explicit per-layer tracks"
        )


def _signal_routing_layers(cell_library: str) -> tuple[str, ...] | None:
    """Expand :data:`_ROUTING_LAYER_RANGE`'s ``"<low>-<high>"`` endpoints for
    ``cell_library`` into the concrete, ordered set of layers
    ``set_routing_layers -signal`` actually opens for signal routing (issue
    #2473), using :data:`_ROUTING_LAYER_STACK` for the stack order.

    ``None`` -- meaning "no opinion, skip any check built on this" -- when
    either table has no entry for this library, or when the range's own
    endpoints are not both present in the stack (a table drift this function
    refuses to paper over by guessing). Never raises: this is a *narrowing*
    guard's input, and an unverified library must keep behaving exactly as it
    did before the guard existed.
    """

    # Deferred: defined in `place_and_route.py`, which imports this module at
    # its own module scope (same pattern as `place_and_route_sta.py`).
    from .place_and_route import _ROUTING_LAYER_RANGE

    stack = _ROUTING_LAYER_STACK.get(cell_library)
    layer_range = _ROUTING_LAYER_RANGE.get(cell_library)
    if stack is None or layer_range is None:
        return None
    low, _, high = layer_range.partition("-")
    if low not in stack or high not in stack:
        return None
    low_index = stack.index(low)
    high_index = stack.index(high)
    if low_index > high_index:
        return None
    return stack[low_index : high_index + 1]


def _validate_io(io_spec: Any, cell_library: str) -> dict[str, Any] | None:

    # Deferred: defined in `place_and_route.py`, which imports this module at
    # its own module scope (same pattern as `place_and_route_sta.py`).
    from .place_and_route import _ROUTING_LAYER_RANGE

    if io_spec is None:
        return None
    if not isinstance(io_spec, dict):
        raise PlaceAndRouteError("request.io must be a JSON object")
    layer_h = io_spec.get("layer_h")
    layer_v = io_spec.get("layer_v")
    if not (
        isinstance(layer_h, str) and layer_h and isinstance(layer_v, str) and layer_v
    ):
        raise PlaceAndRouteError(
            "request.io.layer_h/layer_v must both be non-empty strings"
        )

    # Issue #2473: a top-level pin placed on a layer the router is never
    # allowed to route signals on is only *geometrically* placed -- OpenROAD
    # writes the DEF `PINS` port rectangle to that layer's LEF `PIN`/`LEFPIN`
    # purpose and reaches the cell pin from above through a via, leaving no
    # conductor of that layer under the port. The run still succeeds, the GDS
    # is DRC-clean, and `klt lvs` still reports "match" (its structural
    # comparer does not need pins) -- but `klt extract` sees no net there, so
    # most declared top-level pins silently do not exist in the extracted
    # netlist. Rejected here, at request time, rather than after a full
    # route: this is the cheapest point in the whole chain at which the
    # mistake is knowable.
    #
    # Deliberately narrow: only a layer this library's own routing stack
    # declares but its signal-routing *range* excludes is rejected. A name
    # the stack does not know at all (a typo, a layer from another PDK) is
    # left to OpenROAD's own loud error, and an unverified `cell_library`
    # (absent from either reference table) skips the check entirely -- so no
    # request that worked before can be rejected on a guess.
    routable = _signal_routing_layers(cell_library)
    if routable is not None:
        stack = _ROUTING_LAYER_STACK[cell_library]
        for field, value in (("layer_h", layer_h), ("layer_v", layer_v)):
            if value in routable or value not in stack:
                continue
            raise PlaceAndRouteError(
                f"request.io.{field} '{value}' is outside the signal routing "
                f"range for cell_library '{cell_library}' "
                f"({_ROUTING_LAYER_RANGE[cell_library]}): the router never "
                f"draws signal conductor on '{value}', so a top-level pin "
                f"placed there gets only a DEF PINS port rectangle on that "
                f"layer's LEF PIN/LEFPIN purpose with no conductor under it "
                f"-- the routed design is DRC-clean and `klt lvs` still "
                f"reports 'match', but `klt extract` promotes no top-level "
                f"pin for it (issue #2473). Use one of: "
                f"{', '.join(routable)}"
            )

    result: dict[str, Any] = {"layer_h": layer_h, "layer_v": layer_v}
    pin_constraints = _validate_pin_constraints(io_spec.get("pin_constraints"))
    if pin_constraints:
        result["pin_constraints"] = pin_constraints
    return result


def _validate_instance_groups(raw: Any) -> list[dict[str, Any]]:
    """Shape-validate ``request.instance_groups`` (issue #4344)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PlaceAndRouteError("request.instance_groups must be an array")
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        groups.append(
            _validate_instance_group(entry, f"request.instance_groups[{index}]", seen)
        )
    return groups


def _validate_instance_group(entry: Any, where: str, seen: set[str]) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise PlaceAndRouteError(f"{where} must be a JSON object")
    unknown = sorted(set(entry) - {"name", "instances", "max_extent_um"})
    if unknown:
        raise PlaceAndRouteError(f"{where} has unknown key(s): {', '.join(unknown)}")
    name = entry.get("name")
    if not (isinstance(name, str) and name.strip()):
        raise PlaceAndRouteError(f"{where}.name must be a non-empty string")
    if name in seen:
        raise PlaceAndRouteError(f"{where}.name '{name}' is not unique")
    seen.add(name)
    instances = entry.get("instances")
    if not (
        isinstance(instances, list)
        and instances
        and all(isinstance(i, str) and i.strip() for i in instances)
    ):
        raise PlaceAndRouteError(
            f"{where}.instances must be a non-empty array of strings"
        )
    item: dict[str, Any] = {"name": name, "instances": list(instances)}
    bound = entry.get("max_extent_um")
    if bound is not None:
        _check_max_extent_um(bound, where)
        item["max_extent_um"] = bound
    return item


def _check_max_extent_um(bound: Any, where: str) -> None:
    if (
        isinstance(bound, bool)
        or not isinstance(bound, (int, float))
        or not math.isfinite(bound)
        or bound <= 0
    ):
        raise PlaceAndRouteError(f"{where}.max_extent_um must be a number > 0")


_PIN_EDGES = ("north", "east", "south", "west")


def _validate_pin_constraints(raw: Any) -> list[dict[str, Any]]:
    """Shape-validate ``request.io.pin_constraints`` (issue #3885). Whether a
    pattern matches a real port is checked later, against the netlist."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PlaceAndRouteError("request.io.pin_constraints must be an array")
    return [
        _validate_pin_constraint(entry, f"request.io.pin_constraints[{index}]")
        for index, entry in enumerate(raw)
    ]


def _validate_pin_constraint(entry: Any, where: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise PlaceAndRouteError(f"{where} must be a JSON object")
    unknown = sorted(set(entry) - {"pins", "edge", "range_um", "ordered"})
    if unknown:
        raise PlaceAndRouteError(f"{where} has unknown key(s): {', '.join(unknown)}")
    pins = entry.get("pins")
    if not (
        isinstance(pins, list)
        and pins
        and all(isinstance(p, str) and p.strip() for p in pins)
    ):
        raise PlaceAndRouteError(f"{where}.pins must be a non-empty array of strings")
    edge = entry.get("edge")
    if edge not in _PIN_EDGES:
        raise PlaceAndRouteError(
            f"{where}.edge must be one of: {', '.join(_PIN_EDGES)}"
        )
    item: dict[str, Any] = {"pins": list(pins), "edge": edge}
    span = entry.get("range_um")
    if span is not None:
        item["range_um"] = _validate_pin_range(span, where)
    ordered = entry.get("ordered", False)
    if not isinstance(ordered, bool):
        raise PlaceAndRouteError(f"{where}.ordered must be a boolean")
    if ordered:
        item["ordered"] = True
    return item


def _validate_pin_range(span: Any, where: str) -> list[Any]:
    if not (
        isinstance(span, list)
        and len(span) == 2
        and all(
            isinstance(v, (int, float))
            and not isinstance(v, bool)
            and math.isfinite(v)
            and v >= 0
            for v in span
        )
        and span[0] < span[1]
    ):
        raise PlaceAndRouteError(f"{where}.range_um must be [lo, hi] with 0 <= lo < hi")
    return [span[0], span[1]]


def _resolve_pdn_preset(
    power: dict[str, Any], cell_library: str | None
) -> tuple[str | None, Any, Any]:
    """Resolve ``request.power.preset`` (issue #2123) into the raw
    ``straps``/``connects`` values :func:`_validate_power` then validates
    exactly as if the caller had written them out by hand.

    Returns ``(preset_name, straps, connects)``. With no ``preset`` key the
    caller's own ``straps``/``connects`` are passed straight through and
    ``preset_name`` is ``None`` -- byte-for-byte the behaviour from before
    this field existed.

    **``preset`` and explicit ``straps``/``connects`` are mutually
    exclusive**, not merged per-field. That decision is deliberate and is
    this issue's own open question resolved: a per-field override would
    reintroduce exactly the failure mode presets exist to remove -- a
    request that looks like it cites a platform config but silently carries
    one hand-edited field, "half-working silently" (issue #2086). Mutual
    exclusion makes the two modes distinguishable at a glance and makes any
    deviation from the cited config an explicit, whole-recipe choice. A
    caller wanting a tweaked variant copies the preset's straps out of the
    documented table (``docs/cli/place-and-route.md``, "Power delivery")
    and supplies them explicitly -- the response's own
    ``power.preset: null`` then records that this run is *not* the
    platform recipe.

    The preset's own ``cell_library`` must match ``request.pdk.cell_library``
    (when known): a ``gf180mcu_*`` recipe's ``Metal4``/``Metal5`` straps on a
    sky130 stack would otherwise reach ``pdngen`` as an obscure engine error
    or, worse, draw nothing at all and grade ``"partial"`` under issue
    #2086's audit.
    """
    preset_name = power.get("preset")
    if preset_name is None:
        return None, power.get("straps"), power.get("connects")
    if not (isinstance(preset_name, str) and preset_name):
        raise PlaceAndRouteError(
            "request.power.preset must be a non-empty string when given"
        )
    if preset_name not in _PDN_PRESETS:
        raise PlaceAndRouteError(
            f"unknown request.power.preset '{preset_name}' "
            f"(supported: {', '.join(sorted(_PDN_PRESETS))})"
        )
    conflicting = sorted({"straps", "connects"} & set(power))
    if conflicting:
        raise PlaceAndRouteError(
            f"request.power.preset '{preset_name}' and explicit "
            f"request.power.{'/'.join(conflicting)} are mutually exclusive "
            "-- supply one or the other, not both"
        )
    preset = _PDN_PRESETS[preset_name]
    if cell_library not in (None, preset["cell_library"]):
        raise PlaceAndRouteError(
            f"request.power.preset '{preset_name}' is a "
            f"'{preset['cell_library']}' recipe ({preset['source']}), but "
            f"request.pdk.cell_library is '{cell_library}' -- a preset may "
            "only be used with the standard-cell library it was transcribed "
            "for"
        )
    return (
        preset_name,
        [dict(strap) for strap in preset["straps"]],
        [
            {
                **connect,
                "layers": list(connect["layers"]),
                "ongrid": (list(connect["ongrid"]) if connect.get("ongrid") else None),
                "split_cuts": (
                    dict(connect["split_cuts"]) if connect.get("split_cuts") else None
                ),
            }
            for connect in preset["connects"]
        ],
    )


def _validate_power(
    power: Any,
    cell_library: str | None = None,
    library: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate the optional ``request.power`` block (issue #1091) -- power
    delivery net names and PDN strap geometry, driving ``tapcell``/
    ``add_global_connection``/``global_connect``/``pdngen``/
    ``filler_placement`` -- see this module's own docstring "Power delivery"
    section. ``None`` (the default, field omitted) preserves this module's
    original v1 behavior byte-for-byte: no power-delivery Tcl is ever
    emitted, unchanged from before this field existed.

    ``cell_library`` (issue #2123) is ``request.pdk.cell_library``, used
    only to reject a ``preset`` transcribed for a different standard-cell
    library -- see :func:`_resolve_pdn_preset`. ``None`` (the default)
    skips that cross-check, for callers validating a power block in
    isolation.
    """
    if power is None:
        return None
    if not isinstance(power, dict):
        raise PlaceAndRouteError("request.power must be a JSON object")

    power_net = power.get("power_net", "VDD")
    if not isinstance(power_net, str) or not power_net:
        raise PlaceAndRouteError(
            "request.power.power_net must be a non-empty string when given"
        )
    ground_net = power.get("ground_net", "VSS")
    if not isinstance(ground_net, str) or not ground_net:
        raise PlaceAndRouteError(
            "request.power.ground_net must be a non-empty string when given"
        )
    if power_net == ground_net:
        raise PlaceAndRouteError(
            "request.power.power_net and request.power.ground_net must differ"
        )

    preset_name, straps, connects = _resolve_pdn_preset(power, cell_library)
    if not isinstance(straps, list) or not straps:
        raise PlaceAndRouteError(
            "request.power.straps is required and must be a non-empty list"
        )

    validated_straps: list[dict[str, Any]] = []
    for i, strap in enumerate(straps):
        if not isinstance(strap, dict):
            raise PlaceAndRouteError(f"request.power.straps[{i}] must be an object")

        layer = strap.get("layer")
        if not isinstance(layer, str) or not layer:
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].layer is required and must be a "
                "non-empty string"
            )

        width_um = strap.get("width_um")
        if (
            not isinstance(width_um, (int, float))
            or isinstance(width_um, bool)
            or width_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].width_um is required and must be a "
                "positive number"
            )

        pitch_um = strap.get("pitch_um")
        if (
            not isinstance(pitch_um, (int, float))
            or isinstance(pitch_um, bool)
            or pitch_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].pitch_um is required and must be a "
                "positive number"
            )

        offset_um = strap.get("offset_um", 0.0)
        if not isinstance(offset_um, (int, float)) or isinstance(offset_um, bool):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].offset_um must be a number when given"
            )

        # `spacing_um` (issue #1133) -> `add_pdn_stripe -spacing` -- the
        # paired power/ground stripe spacing on this strap's own layer, used
        # when a grid draws power and ground as an adjacent pair on a single
        # layer rather than on a single pitch (e.g. gf180's own
        # `pdn_grid_strategy_9t_6M.cfg`: `add_pdn_stripe -layer {Metal4}
        # -width {4.480} -spacing {0.56} -pitch {44.8} -offset {22.4}`).
        # Optional, `None` by default -- omitted entirely from the emitted
        # `add_pdn_stripe` call rather than defaulted, preserving this
        # module's prior Tcl byte-for-byte when not given. Deliberately not
        # required to have >= 2 straps: it describes this stripe's own
        # power/ground pairing, independent of any other strap.
        spacing_um = strap.get("spacing_um")
        if spacing_um is not None and (
            not isinstance(spacing_um, (int, float))
            or isinstance(spacing_um, bool)
            or spacing_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].spacing_um must be a positive "
                "number when given"
            )

        followpins = strap.get("followpins", False)
        if not isinstance(followpins, bool):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].followpins must be a boolean when given"
            )

        validated_straps.append(
            {
                "layer": layer,
                "width_um": float(width_um),
                "pitch_um": float(pitch_um),
                "offset_um": float(offset_um),
                "spacing_um": float(spacing_um) if spacing_um is not None else None,
                "followpins": followpins,
            }
        )

    # `connects[]` (issue #1133) -> per-pair `add_pdn_connect` via-stack
    # tuning (`-max_columns`/`-ongrid`/`-split_cuts`) -- the platform's own
    # answer to a DRC question (cut splitting, on-grid landing, column
    # count) for the via stack between two strap layers. Optional; omitted
    # (the default, `[]`) preserves this module's prior bare
    # `add_pdn_connect -grid {grid} -layers {lower upper}` call for every
    # consecutive strap pair, byte-for-byte. Each entry's `layers` must name
    # one of `straps[]`'s own consecutive pairs, in that pair's own order --
    # this rejects a typo'd/nonexistent layer pair at validation time rather
    # than silently emitting a call this module never intended.
    validated_connects: list[dict[str, Any]] = []
    if connects is not None:
        if not isinstance(connects, list):
            raise PlaceAndRouteError("request.power.connects must be a list when given")

        strap_pairs = {
            (lower["layer"], upper["layer"])
            for lower, upper in zip(
                validated_straps, validated_straps[1:], strict=False
            )
        }
        seen_pairs: set[tuple[str, str]] = set()

        for i, connect in enumerate(connects):
            if not isinstance(connect, dict):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}] must be an object"
                )

            layers = connect.get("layers")
            if (
                not isinstance(layers, list)
                or len(layers) != 2
                or not all(isinstance(entry, str) and entry for entry in layers)
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers is required and must "
                    "be a 2-element list of non-empty strings"
                )
            pair = (layers[0], layers[1])
            if pair not in strap_pairs:
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers {list(pair)} does not "
                    "match any consecutive pair in request.power.straps"
                )
            if pair in seen_pairs:
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers {list(pair)} "
                    "duplicates an earlier request.power.connects entry"
                )
            seen_pairs.add(pair)

            max_columns = connect.get("max_columns")
            if max_columns is not None and (
                not isinstance(max_columns, int)
                or isinstance(max_columns, bool)
                or max_columns <= 0
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].max_columns must be a "
                    "positive integer when given"
                )

            ongrid = connect.get("ongrid")
            if ongrid is not None and (
                not isinstance(ongrid, list)
                or not ongrid
                or not all(isinstance(entry, str) and entry for entry in ongrid)
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].ongrid must be a non-empty "
                    "list of non-empty strings when given"
                )

            split_cuts = connect.get("split_cuts")
            if split_cuts is not None:
                if not isinstance(split_cuts, dict):
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts must be an "
                        "object when given"
                    )
                sc_layer = split_cuts.get("layer")
                if not isinstance(sc_layer, str) or not sc_layer:
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts.layer is "
                        "required and must be a non-empty string"
                    )
                sc_width_um = split_cuts.get("width_um")
                if (
                    not isinstance(sc_width_um, (int, float))
                    or isinstance(sc_width_um, bool)
                    or sc_width_um <= 0
                ):
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts.width_um is "
                        "required and must be a positive number"
                    )
                split_cuts = {"layer": sc_layer, "width_um": float(sc_width_um)}

            validated_connects.append(
                {
                    "layers": [pair[0], pair[1]],
                    "max_columns": max_columns,
                    "ongrid": list(ongrid) if ongrid is not None else None,
                    "split_cuts": split_cuts,
                }
            )

    # `tapcell.distance_um` (issue #3051) -> `tapcell -distance` -- overrides
    # the per-library pitch in `_TAPCELL_CELLS`. Optional; omitted (the
    # default, `None`) falls back to the table value, so the emitted Tcl is
    # byte-for-byte unchanged. It is a placement input, not evidence of any
    # diffusion-to-tap distance. A `_NO_TAPCELL_LIBRARIES` library emits no
    # `tapcell` line, so an override there is rejected rather than silently
    # ignored.
    tapcell_distance_um = _validate_tapcell(power.get("tapcell"), cell_library, library)

    return {
        "preset": preset_name,
        "power_net": power_net,
        "ground_net": ground_net,
        "tapcell_distance_um": tapcell_distance_um,
        "straps": validated_straps,
        "connects": validated_connects,
    }


def _validate_tapcell(
    tapcell: Any, cell_library: str | None, library: dict[str, Any] | None = None
) -> int | float | None:
    """Validate ``request.power.tapcell`` (issue #3051); return its
    ``distance_um`` override, or ``None`` when ``tapcell`` is omitted."""
    # Deferred: defined in `place_and_route.py`, which imports this module at
    # its own module scope (same pattern as `place_and_route_sta.py`).
    from .place_and_route import _NO_TAPCELL_LIBRARIES

    if tapcell is None:
        return None
    if not isinstance(tapcell, dict):
        raise PlaceAndRouteError(
            "request.power.tapcell must be a JSON object when given "
            "(request.power.tapcell.distance_um is its only field)"
        )
    unknown = sorted(set(tapcell) - {"distance_um"})
    if unknown:
        raise PlaceAndRouteError(
            f"request.power.tapcell has unknown key(s) {unknown}; "
            "only distance_um is supported"
        )
    distance_um = tapcell.get("distance_um")
    if (
        not isinstance(distance_um, (int, float))
        or isinstance(distance_um, bool)
        or not math.isfinite(distance_um)
        or distance_um <= 0
    ):
        raise PlaceAndRouteError(
            "request.power.tapcell.distance_um is required and must be a "
            "positive number"
        )
    if (
        library["tapcell"][0] is None
        if library is not None and "tapcell" in library
        else cell_library in _NO_TAPCELL_LIBRARIES
    ):
        raise PlaceAndRouteError(
            f"request.power.tapcell.distance_um cannot be honored: "
            f"standard-cell library '{cell_library}' has no tap/endcap "
            "cells, so no tapcell call is emitted"
        )
    return distance_um


def _effective_tapcell_distance(
    power: dict[str, Any], table_distance_um: int | float | None
) -> int | float | None:
    """The ``tapcell -distance`` pitch: the validated override, else the
    per-library table value."""
    override = power.get("tapcell_distance_um")
    return table_distance_um if override is None else override


def _validate_macros(
    macros: Any, request_dir: str, netlist_path: str | None = None
) -> list[dict[str, Any]]:
    """Validate the optional ``request.macros`` array (issue #438). Each
    entry fixes one hard-macro instance at a caller-given location during
    the ``"floorplan"`` stage -- see this module's docstring "Hard-macro
    placement" section. Returns ``[]`` when the field is omitted/``None``
    (unchanged behavior -- purely additive).

    ``netlist_path`` (issue #464) additionally cross-checks each macro's LEF
    pins with no ``PORT`` geometry at all against the netlist's own wiring --
    see this module's docstring "Macro-pin routability cross-check" section.
    ``None`` (the default, used by this module's own direct unit tests below
    that exercise validation in isolation, with no netlist in hand) skips
    that cross-check entirely -- it is purely additive to the pre-existing
    validation this function already performed."""
    if macros is None:
        return []
    if not isinstance(macros, list):
        raise PlaceAndRouteError("request.macros must be a list")

    validated: list[dict[str, Any]] = []
    for i, macro in enumerate(macros):
        if not isinstance(macro, dict):
            raise PlaceAndRouteError(f"request.macros[{i}] must be an object")

        instance = macro.get("instance")
        if not isinstance(instance, str) or not instance:
            raise PlaceAndRouteError(
                f"request.macros[{i}].instance is required and must be a "
                "non-empty string"
            )

        lef = macro.get("lef")
        if not isinstance(lef, str) or not lef:
            raise PlaceAndRouteError(
                f"request.macros[{i}].lef is required and must be a non-empty string"
            )
        lef_path = lef if os.path.isabs(lef) else os.path.join(request_dir, lef)
        if not os.path.isfile(lef_path):
            raise PlaceAndRouteError(f"request.macros[{i}].lef not found: {lef}")
        macro_cells = read_lef_header(lef_path)["macros"]
        if len(macro_cells) != 1:
            raise PlaceAndRouteError(
                f"request.macros[{i}].lef '{lef}' must declare exactly one MACRO "
                f"(found {len(macro_cells)})"
            )
        macro_cell_name = macro_cells[0]["name"]

        no_port_pin_names = sorted(
            pin["name"] for pin in macro_cells[0]["pins"] if not pin["has_port"]
        )
        if no_port_pin_names and netlist_path is not None:
            _reject_wired_port_less_pins(
                index=i,
                instance=instance,
                lef=lef,
                macro_cell_name=macro_cell_name,
                no_port_pin_names=no_port_pin_names,
                netlist_path=netlist_path,
            )

        for key in ("x_um", "y_um"):
            if not isinstance(macro.get(key), (int, float)) or isinstance(
                macro.get(key), bool
            ):
                raise PlaceAndRouteError(
                    f"request.macros[{i}].{key} is required and must be a number"
                )

        orientation = macro.get("orientation", "R0")
        if orientation not in _MACRO_ORIENTATIONS:
            raise PlaceAndRouteError(
                f"request.macros[{i}].orientation must be one of: "
                + ", ".join(sorted(_MACRO_ORIENTATIONS))
            )

        gds = macro.get("gds")
        gds_path: str | None = None
        if gds is not None:
            if not isinstance(gds, str) or not gds:
                raise PlaceAndRouteError(
                    f"request.macros[{i}].gds must be a non-empty string when given"
                )
            gds_path = gds if os.path.isabs(gds) else os.path.join(request_dir, gds)
            if not os.path.isfile(gds_path):
                raise PlaceAndRouteError(f"request.macros[{i}].gds not found: {gds}")

        validated.append(
            {
                "instance": instance,
                "lef": os.path.abspath(lef_path),
                "cell_name": macro_cell_name,
                "x_um": float(macro["x_um"]),
                "y_um": float(macro["y_um"]),
                "orientation": orientation,
                "gds": gds_path,
            }
        )

    instances = [m["instance"] for m in validated]
    if len(instances) != len(set(instances)):
        raise PlaceAndRouteError("request.macros[].instance values must be unique")

    return validated


#: A structural-Verilog module instantiation with **named** port
#: connections: ``<cell_name> <instance> ( .PORT(NET), ... ) ;`` -- the form
#: every real synthesis tool (Yosys included, per this module's own
#: ``read_verilog``/``link_design`` use) emits for a blackbox module
#: instance. Deliberately does **not** match a ``#( ... )`` parameter block
#: (an analog macro blackbox is never parameterized) or a positional
#: connection list (no declared port order is available to this module to
#: interpret one) -- either shape simply fails to match, and
#: :func:`_macro_instance_port_connections` reports "cannot confidently
#: locate" (``None``) rather than a wrong answer.
_MACRO_INSTANCE_RE_TEMPLATE = r"\b{cell}\b\s+\b{instance}\b\s*\("

#: One named port connection within an instantiation's own port list, e.g.
#: ``.Q1_1_G(net123)`` or ``.Q1_1_G()`` (left unconnected).
_PORT_CONNECTION_RE = re.compile(r"\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^()]*)\)")

_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_verilog_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _blank_verilog_comments(text: str) -> list[str]:
    """``text``'s lines with every comment blanked out but **line numbering
    preserved** -- one output entry per input line, comment characters
    replaced by spaces rather than deleted.

    :func:`_strip_verilog_comments` deletes comments outright, which
    collapses the line structure a multi-line ``/* ... */`` spans and makes
    the result useless for reporting a line number.
    :func:`_reject_unsupported_netlist_constructs` needs to name the exact
    1-based netlist line its diagnostic points at (issue #1973 -- the whole
    point is to beat OpenSTA's own ``line N, syntax error`` to the punch),
    so it needs this shape instead.
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        chars = list(line)
        i = 0
        while i < len(chars):
            if in_block:
                if line.startswith("*/", i):
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    in_block = False
                    continue
                chars[i] = " "
                i += 1
            elif line.startswith("/*", i):
                chars[i] = chars[i + 1] = " "
                i += 2
                in_block = True
            elif line.startswith("//", i):
                for j in range(i, len(chars)):
                    chars[j] = " "
                break
            else:
                i += 1
        out.append("".join(chars))
    return out


#: A ``signed`` qualifier on a port/wire declaration in a structural-Verilog
#: netlist -- ``output signed [15:0] sample;``, ``wire signed [7:0] mid;``,
#: ``input wire signed [3:0] a;`` (issue #1973). Anchored to a preceding
#: declaration keyword on purpose: an expression-level ``$signed(...)`` cast
#: is accepted by every downstream reader (and is the RTL-side replacement
#: this repo's own ``docs/guides/digital-review/rtl-style-guide.md``
#: recommends for a ``signed`` port), so it must never be flagged -- and
#: ``$signed(`` is never immediately preceded by one of these keywords plus
#: whitespace.
_NETLIST_SIGNED_DECL_RE = re.compile(
    r"\b(?:input|output|inout|wire|reg|logic)\s+signed\b"
)

#: A bare ``x``-valued Verilog constant literal -- ``5'hxx``, ``2'bxx``,
#: ``1'bx``, ``8'shX0`` (issue #1973). Requires at least one ``x``/``X``
#: digit, so an ordinary resolved constant (``1'b0``, ``16'd100``,
#: ``8'hbe``) never matches -- only the unresolved bits OpenSTA turns into
#: an unroutable ``GROUND``-typed net.
_NETLIST_X_CONSTANT_RE = re.compile(
    r"\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]*[xX][0-9a-fA-FxX_]*"
)


def _reject_unsupported_netlist_constructs(netlist_path: str) -> None:
    """Raise :class:`PlaceAndRouteError` naming the construct, the 1-based
    line number, and the offending line when ``netlist_path`` carries a
    ``signed`` port/wire qualifier or a bare ``x``-valued constant -- the
    two constructs that deterministically fail a real OpenROAD run several
    stages in, with a diagnostic pointing at a generated file the caller
    never wrote (issue #1973; see this module's docstring "Netlist
    pre-flight" section for the full rationale).

    Reports the **first** offending line in file order, whichever construct
    it is: both are already fatal, so there is nothing to gain from
    enumerating every occurrence, and the first one is the one a reader will
    go fix. A netlist this cannot read is not an error here --
    :func:`_resolve_netlist` has already established the file exists and is
    openable, and OpenROAD's own reader remains the authority on whether its
    contents parse at all.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return

    for lineno, line in enumerate(_blank_verilog_comments(text), start=1):
        signed_match = _NETLIST_SIGNED_DECL_RE.search(line)
        if signed_match is not None:
            raise PlaceAndRouteError(
                f"netlist '{netlist_path}' line {lineno} declares a 'signed' "
                f"port/wire ('{signed_match.group(0)}'): {line.strip()} -- "
                "OpenSTA's Verilog reader (used by the 'floorplan' stage) "
                "rejects the 'signed' keyword outright with a syntax error. "
                "Re-synthesize with `klt synthesize`, which strips these "
                "declarations, or remove the qualifier from the netlist and "
                "use a `$signed(...)` cast in the RTL instead."
            )

        x_match = _NETLIST_X_CONSTANT_RE.search(line)
        if x_match is not None:
            raise PlaceAndRouteError(
                f"netlist '{netlist_path}' line {lineno} drives a bare "
                f"x-valued constant ('{x_match.group(0)}'): {line.strip()} -- "
                "OpenSTA reads it as a constant and builds an empty "
                "GROUND-typed net from it, which TritonRoute then refuses "
                "with DRT-0305 (after floorplan, placement and CTS have all "
                "already succeeded). Re-synthesize with `klt synthesize`, "
                "which resolves x bits to concrete tie-cell-driven constants "
                "(`setundef -zero` ahead of `hilomap`); a Verilog `function` "
                "with a dangling argument is the usual source in RTL."
            )


def _macro_instance_port_connections(
    netlist_path: str, cell_name: str, instance_name: str
) -> dict[str, str] | None:
    """Best-effort structural-Verilog lookup of one macro instance's own
    named port connections (``{port_name: connection_expression}``), used
    only by :func:`_reject_wired_port_less_pins`'s netlist-wiring
    cross-check (issue #464).

    Returns ``None`` -- "cannot confidently locate" -- when the netlist
    cannot be read, or no ``<cell_name> <instance_name>( ... )``
    instantiation is found (a positional-connection instantiation, a
    non-Verilog/placeholder netlist, or the instance genuinely not
    referenced by that exact name/type pair). The caller treats ``None`` as
    "skip the cross-check for this macro" -- never as "no pins are wired".
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None

    text = _strip_verilog_comments(text)
    pattern = re.compile(
        _MACRO_INSTANCE_RE_TEMPLATE.format(
            cell=re.escape(cell_name), instance=re.escape(instance_name)
        )
    )
    match = pattern.search(text)
    if match is None:
        return None

    depth = 1
    i = match.end()
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return None  # unterminated -- malformed/truncated text, don't guess

    port_list_text = text[match.end() : i - 1]
    connections = {
        port_name: connection.strip()
        for port_name, connection in _PORT_CONNECTION_RE.findall(port_list_text)
    }
    return connections if connections else None


def _reject_wired_port_less_pins(
    *,
    index: int,
    instance: str,
    lef: str,
    macro_cell_name: str,
    no_port_pin_names: list[str],
    netlist_path: str,
) -> None:
    """Raise :class:`PlaceAndRouteError` when any of ``no_port_pin_names``
    (macro pins with no ``PORT`` geometry at all in the declared LEF) is
    actually wired to a real net in the netlist's own instantiation of this
    macro -- see this module's docstring "Macro-pin routability cross-check"
    section. A no-op when the netlist cannot be confidently parsed for this
    instance, or when every no-``PORT`` pin is left unconnected there."""
    connections = _macro_instance_port_connections(
        netlist_path, macro_cell_name, instance
    )
    if connections is None:
        return

    for pin_name in no_port_pin_names:
        net = connections.get(pin_name)
        if net:
            raise PlaceAndRouteError(
                f"request.macros[{index}] instance '{instance}' pin "
                f"'{pin_name}' has no PORT geometry in '{lef}' (MACRO "
                f"'{macro_cell_name}') but is wired to net '{net}' in the "
                "netlist -- OpenROAD's global router would fail with "
                "GRT-0029 once this pin is reached. The pin's declared "
                "layer likely does not resolve to a routing-type tech-LEF "
                "layer (see klt lef-abstract's 'unroutable_pins[]'/"
                "warnings[] for that run); fix the LEF abstract, or leave "
                f"'{pin_name}' unconnected in the netlist."
            )


def _validate_constraints(
    constraints: Any,
) -> tuple[
    str | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    """Validate ``request.constraints``.

    Returns ``(clock_port, clock_period_ns, max_transition_ns,
    max_capacitance_pf, max_fanout, input_delay_ns, output_delay_ns)``. The
    first two are the pre-existing clock fields -- required together,
    `None`/`None` when both are omitted (unchanged behavior). The three
    design-rule-constraint fields (issue #1709) are each independently
    optional regardless of clock presence: a caller may set e.g.
    `max_fanout` alone, or alongside the clock fields, or not at all --
    `None` per field when omitted, matching this function's own pre-existing
    "omitted preserves prior behavior exactly" convention.

    The last two (issue #1865) are the **I/O timing** constraints:
    `input_delay_ns` is the arrival time of every non-clock input port
    relative to the clock, `output_delay_ns` the required time at every
    output port. Each is independently optional -- but, unlike the
    design-rule fields, each *requires* `clock_port`/`clock_period_ns`,
    since `set_input_delay`/`set_output_delay` are both defined relative to
    a named clock and have no meaning without one.
    """
    if constraints is None:
        return None, None, None, None, None, None, None
    if not isinstance(constraints, dict):
        raise PlaceAndRouteError("request.constraints must be a JSON object")

    clock_port = constraints.get("clock_port")
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_port is not None or clock_period_ns is not None:
        if not (isinstance(clock_port, str) and clock_port):
            raise PlaceAndRouteError(
                "request.constraints.clock_port must be a non-empty string"
            )
        if not (isinstance(clock_period_ns, (int, float)) and clock_period_ns > 0):
            raise PlaceAndRouteError(
                "request.constraints.clock_period_ns must be a positive number"
            )
        clock_period_ns = float(clock_period_ns)
    else:
        clock_port, clock_period_ns = None, None

    max_transition_ns = constraints.get("max_transition_ns")
    if max_transition_ns is not None:
        if not (
            isinstance(max_transition_ns, (int, float))
            and not isinstance(max_transition_ns, bool)
            and max_transition_ns > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_transition_ns must be a positive number"
            )
        max_transition_ns = float(max_transition_ns)

    max_capacitance_pf = constraints.get("max_capacitance_pf")
    if max_capacitance_pf is not None:
        if not (
            isinstance(max_capacitance_pf, (int, float))
            and not isinstance(max_capacitance_pf, bool)
            and max_capacitance_pf > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_capacitance_pf must be a positive number"
            )
        max_capacitance_pf = float(max_capacitance_pf)

    max_fanout = constraints.get("max_fanout")
    if max_fanout is not None:
        if not (
            isinstance(max_fanout, (int, float))
            and not isinstance(max_fanout, bool)
            and max_fanout > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_fanout must be a positive number"
            )
        max_fanout = float(max_fanout)

    # Issue #1865: I/O timing constraints. Both are plain non-negative
    # numbers (0 is a meaningful, commonly-written value -- "the port is
    # valid exactly at the clock edge" -- so unlike the design-rule fields
    # above these are `>= 0`, not `> 0`), and both are relative to
    # `clock_port`, so neither can be honoured without one.
    input_delay_ns = _validate_io_delay(constraints.get("input_delay_ns"), "input")
    output_delay_ns = _validate_io_delay(constraints.get("output_delay_ns"), "output")
    if (input_delay_ns is not None or output_delay_ns is not None) and (
        clock_port is None
    ):
        raise PlaceAndRouteError(
            "request.constraints.input_delay_ns/output_delay_ns require "
            "request.constraints.clock_port/clock_period_ns -- set_input_delay/"
            "set_output_delay are defined relative to a named clock"
        )

    return (
        clock_port,
        clock_period_ns,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
    )


def _validate_io_delay(value: Any, which: str) -> float | None:
    """One of ``request.constraints.input_delay_ns``/``.output_delay_ns``
    (issue #1865): optional, and a non-negative number when given."""
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value != value  # NaN
        or value in (float("inf"), float("-inf"))
    ):
        raise PlaceAndRouteError(
            f"request.constraints.{which}_delay_ns must be a non-negative number"
        )
    return float(value)


_LIBRARY_KEYS = (
    "cts_buffer",
    "power_pins",
    "wire_rc",
    "route_layers",
    "antenna_diode",
    "tapcell",
    "filler",
)


def _positive_number(value: Any, where: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise PlaceAndRouteError(f"{where} must be a positive number")
    return float(value)


def _validate_library_power_pin(entry: Any, where: str) -> tuple[str, str, bool]:
    if not isinstance(entry, dict):
        raise PlaceAndRouteError(f"{where} must be a JSON object")
    unknown = sorted(set(entry) - {"net_role", "pattern", "primary"})
    if unknown:
        raise PlaceAndRouteError(f"{where} has unknown key(s): {', '.join(unknown)}")
    net_role = entry.get("net_role")
    if net_role not in ("power", "ground"):
        raise PlaceAndRouteError(f"{where}.net_role must be 'power' or 'ground'")
    pattern = entry.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        raise PlaceAndRouteError(f"{where}.pattern must be a non-empty string")
    primary = entry.get("primary", False)
    if not isinstance(primary, bool):
        raise PlaceAndRouteError(f"{where}.primary must be a boolean")
    return net_role, pattern, primary


def _validate_library_power_pins(raw: Any) -> list[tuple[str, str, bool]]:
    where = "request.library.power_pins"
    if not isinstance(raw, list) or not raw:
        raise PlaceAndRouteError(f"{where} must be a non-empty list of objects")
    pins = [
        _validate_library_power_pin(entry, f"{where}[{index}]")
        for index, entry in enumerate(raw)
    ]
    for net_role in ("power", "ground"):
        count = sum(1 for role, _, primary in pins if role == net_role and primary)
        if count != 1:
            raise PlaceAndRouteError(
                f"{where} must name exactly one primary pin for net_role "
                f"'{net_role}' (found {count})"
            )
    return pins


def _validate_library_wire_rc(rc: Any) -> tuple[float, float]:
    where = "request.library.wire_rc"
    if not isinstance(rc, dict):
        raise PlaceAndRouteError(f"{where} must be a JSON object")
    keys = ("resistance_ohm_per_um", "capacitance_pf_per_um")
    unknown = sorted(set(rc) - set(keys))
    if unknown:
        raise PlaceAndRouteError(f"{where} has unknown key(s): {', '.join(unknown)}")
    for key in keys:
        if key not in rc:
            raise PlaceAndRouteError(f"{where}.{key} is required")
    return (
        _positive_number(rc[keys[0]], f"{where}.{keys[0]}"),
        _positive_number(rc[keys[1]], f"{where}.{keys[1]}"),
    )


def _library_name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlaceAndRouteError(f"{where} must be a non-empty string")
    return value


def _library_object(raw: Any, where: str, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PlaceAndRouteError(f"{where} must be a JSON object")
    unknown = sorted(set(raw) - set(keys))
    if unknown:
        raise PlaceAndRouteError(f"{where} has unknown key(s): {', '.join(unknown)}")
    return raw


def _validate_library_route_layers(raw: Any) -> str:
    where = "request.library.route_layers"
    obj = _library_object(raw, where, ("low", "high"))
    for key in ("low", "high"):
        if key not in obj:
            raise PlaceAndRouteError(f"{where}.{key} is required")
    low = _library_name(obj["low"], f"{where}.low")
    high = _library_name(obj["high"], f"{where}.high")
    return f"{low}-{high}"


def _validate_library_antenna_diode(raw: Any) -> tuple[str, str] | None:
    """``None`` (JSON ``null``) states the library has no antenna diode:
    the ``route`` stage skips ``repair_antennas`` (issue #4274); an object
    names the diode cell and pin."""
    where = "request.library.antenna_diode"
    if raw is None:
        return None
    obj = _library_object(raw, where, ("cell", "pin"))
    for key in ("cell", "pin"):
        if key not in obj:
            raise PlaceAndRouteError(f"{where}.{key} is required")
    return (
        _library_name(obj["cell"], f"{where}.cell"),
        _library_name(obj["pin"], f"{where}.pin"),
    )


def _validate_library_tapcell(
    raw: Any,
) -> tuple[str | None, str | None, int | float | None]:
    """``None`` (JSON ``null``) states the library has no tap/endcap cells;
    an object names the masters and pitch."""
    where = "request.library.tapcell"
    if raw is None:
        return None, None, None
    obj = _library_object(raw, where, ("master", "endcap", "distance_um"))
    for key in ("master", "distance_um"):
        if key not in obj:
            raise PlaceAndRouteError(f"{where}.{key} is required")
    endcap = obj.get("endcap")
    if endcap is not None:
        endcap = _library_name(endcap, f"{where}.endcap")
    distance = _positive_number(obj["distance_um"], f"{where}.distance_um")
    return (
        _library_name(obj["master"], f"{where}.master"),
        endcap,
        int(distance) if distance.is_integer() else distance,
    )


def _validate_library_filler(raw: Any) -> tuple[str, ...]:
    where = "request.library.filler"
    if not isinstance(raw, list) or not raw:
        raise PlaceAndRouteError(f"{where} must be a non-empty list of strings")
    return tuple(_library_name(item, f"{where}[{i}]") for i, item in enumerate(raw))


def _validate_library_cts_buffer(buf: Any) -> str:
    if not isinstance(buf, str) or not buf.strip():
        raise PlaceAndRouteError(
            "request.library.cts_buffer must be a non-empty string"
        )
    return buf


_LIBRARY_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "cts_buffer": _validate_library_cts_buffer,
    "power_pins": _validate_library_power_pins,
    "wire_rc": _validate_library_wire_rc,
    "route_layers": _validate_library_route_layers,
    "antenna_diode": _validate_library_antenna_diode,
    "tapcell": _validate_library_tapcell,
    "filler": _validate_library_filler,
}


def _validate_library(library: Any) -> dict[str, Any] | None:
    """Optional ``request.library`` (issue #4086): per-library inputs for a
    standard-cell library the built-in tables
    (``_CTS_BUFFER_CELLS``/``_POWER_PIN_PATTERNS``) do not cover, or an
    override of a built-in entry. ``None`` (field omitted) leaves every
    generated script byte-identical. Returns only the keys that were given:
    ``cts_buffer`` (str), ``power_pins`` (list of ``(net_role, pattern,
    primary)``), ``wire_rc`` (``(resistance, capacitance)``),
    ``route_layers`` (``"low-high"``), ``antenna_diode`` (``(cell, pin)``, or ``None`` =
    the library has no antenna diode, so no antenna repair; issue #4274),
    ``tapcell`` (``(tap, endcap, distance_um)``, all ``None`` = the library
    has no tap cells) and ``filler`` (tuple of masters; issue #4127).
    """
    if library is None:
        return None
    if not isinstance(library, dict):
        raise PlaceAndRouteError("request.library must be a JSON object")
    unknown = sorted(set(library) - set(_LIBRARY_KEYS))
    if unknown:
        raise PlaceAndRouteError(
            f"request.library has unknown key(s): {', '.join(unknown)}"
        )
    out: dict[str, Any] = {}
    for key, validate in _LIBRARY_VALIDATORS.items():
        if key in library:
            out[key] = validate(library[key])
    return out


def _validate_seed(seed: Any) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PlaceAndRouteError("request.seed must be an integer")
    return seed


def _validate_target_stage(target_stage: Any) -> str:

    # Deferred: defined in `place_and_route.py`, which imports this module at
    # its own module scope (same pattern as `place_and_route_sta.py`).
    from .place_and_route import STAGE_ORDER

    if target_stage not in STAGE_ORDER:
        raise PlaceAndRouteError(
            "request.target_stage must be one of: " + ", ".join(STAGE_ORDER)
        )
    return target_stage


def _validate_route_critical_nets_percentage(value: Any) -> int:
    """Optional ``request.route_critical_nets_percentage`` (issue #939) --
    ``global_route``'s own ``-critical_nets_percentage`` flag: the
    percentage of worst-slack nets treated as timing-critical during
    congestion-removal iterations (0-100, the same bound OpenROAD's own
    ``sta::check_percent`` enforces). Omitted/``None`` defaults to ``0``,
    matching ``global_route``'s own default and this module's prior
    behaviour exactly -- the explicit A/B disable path this issue's
    acceptance criteria require. See this module's own docstring, "Timing-
    driven global routing + bounded antenna-repair iteration"."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaceAndRouteError(
            "request.route_critical_nets_percentage must be an integer"
        )
    if not (0 <= value <= 100):
        raise PlaceAndRouteError(
            "request.route_critical_nets_percentage must be between 0 and 100"
        )
    return value


#: Upper bound on ``request.max_antenna_repair_iterations`` (issue #939) --
#: each additional iteration re-runs a full ``repair_antennas``/
#: ``detailed_route`` pair (a real, potentially expensive detailed-routing
#: pass), so this option is deliberately *bounded*, not unlimited, per this
#: issue's own scope ("a bounded multi-pass option"). Chosen generously
#: relative to ORFS's own default posture (``MAX_REPAIR_ANTENNAS_ITER_DRT``
#: is unset in ORFS's own default flow, i.e. inactive/1) while still ruling
#: out a runaway request.
_MAX_ANTENNA_REPAIR_ITERATIONS_CAP = 8


def _validate_max_antenna_repair_iterations(value: Any) -> int:
    """Optional ``request.max_antenna_repair_iterations`` (issue #939) -- a
    bounded multi-pass generalisation of the ``"route"`` stage's existing
    single ``repair_antennas``+``detailed_route`` reroute pass (issue #759).
    Omitted/``None`` defaults to ``1``; ``0`` disables repair/reroute passes.
    See this module's own docstring, "Timing-driven
    global routing + bounded antenna-repair iteration"."""
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be an integer"
        )
    if not (0 <= value <= _MAX_ANTENNA_REPAIR_ITERATIONS_CAP):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be between 0 and "
            f"{_MAX_ANTENNA_REPAIR_ITERATIONS_CAP}"
        )
    return value


def _validate_sweep_corners(value: Any) -> list[str] | None:
    """Optional ``request.pdk.sweep_corners`` (issue #1092) -- scopes the
    post-route multi-corner sweep (issue #949, :func:`_run_corner_sweep`) to
    a caller-named subset of the ``.lib`` corners ``request.pdk.cell_library``
    ships, instead of always sweeping every shipped corner
    (:func:`klayout_tools.pdk.list_lib_corners`'s own unfiltered return).

    A design has exactly one operating supply -- for a cell library shipping
    several (e.g. 15 ``.lib`` files spanning three nominal-supply families),
    the unscoped sweep is dominated by decks the design never runs at (this
    issue's own motivating gap). ``None`` (omitted, the default) reproduces
    today's "sweep everything the cell library ships" behaviour exactly --
    the backward-compatible default this field's own acceptance criteria
    require. An explicit ``[]`` is honoured literally (sweep zero corners --
    ``worst_setup_slack_ns``/``worst_hold_slack_ns``/``corners`` all degrade
    the same way a pre-``"route"`` run's do, via :func:`_run_corner_sweep`'s
    own empty-``corners``-list short-circuit), distinct from omitting the
    field entirely.

    Each requested name is validated against the *unfiltered* shipped-corner
    set once ``cell_library``/``pdk_info`` are resolved (see the ``"route"``
    stage branch in :func:`run_place_and_route`) -- an unknown name raises
    :class:`PlaceAndRouteError` naming every corner that *is* valid, matching
    this module's existing unresolvable-request-field convention (e.g.
    :func:`_validate_route_critical_nets_percentage`'s sibling validators).
    This function only validates *shape* (a list of non-empty strings);
    membership validation needs the resolved corner set and so cannot happen
    here.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PlaceAndRouteError(
            "request.pdk.sweep_corners must be a list of non-empty strings"
        )
    return value


def _validate_portable_paths(value: Any) -> bool:
    """Optional ``request.portable_paths`` (issue #3820): rewrite every
    absolute host path in the response to a committable form (see
    :mod:`klayout_tools.place_and_route_portable`). Omitted/``None`` is
    ``False``: the default response is unchanged."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlaceAndRouteError("request.portable_paths must be a boolean")
    return value


def _validate_post_route_spef(value: Any) -> bool:
    """Optional ``request.post_route_spef`` (issue #948, Epic #700 Phase 3,
    ``docs/design/post-route-sta-survey.md`` §4.1) -- opts in to a real
    post-route parasitic-extraction A/B pass on the ``"route"`` stage:
    ``klt extract --parasitics`` runs against the merged routed GDS, its
    output is written as SPEF, and a fresh OpenSTA session seeded from the
    ``"route"`` stage's own checkpoint re-reports slack/violation counts via
    ``read_spef`` -- *alongside*, never replacing, that stage's existing
    ``estimate_parasitics -global_routing``-derived top-level fields (see
    this module's own docstring and :func:`_post_route_spef_metrics`).

    Omitted/``None`` defaults to ``False`` -- this feature adds real
    wall-clock cost on top of every existing ``"route"``-stage caller (a
    `klt extract --parasitics` pass over the merged GDS, plus a second
    `openroad` subprocess invocation), so it is opt-in, matching this
    module's own convention for every other flag whose added cost a survey
    flagged rather than assumed free (`route_critical_nets_percentage`,
    `max_antenna_repair_iterations`) -- the explicit A/B disable path this
    module's other flags already established.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlaceAndRouteError("request.post_route_spef must be a boolean")
    return value


def _validate_post_route_sdf(value: Any, *, post_route_spef: bool) -> bool:
    """Optional ``request.post_route_sdf`` (issue #1002, Epic #700 Phase 3,
    ``docs/design/post-route-sta-survey.md`` §4.3) -- opts in to writing the
    ``post_route_spef`` OpenSTA session's own delays out as an IEEE-1497 SDF
    file (``write_sdf``, immediately after that session's ``read_spef``), for
    ``klt functional-verification``'s ``options.sdf`` block to back-annotate
    onto a gate-level re-run of the design's own testbench.

    Omitted/``None`` defaults to ``False``: like every other flag in this
    module whose cost a survey flagged rather than assumed free, it is
    opt-in, and it has no meaning at all without the session that produces
    it.

    **Requires ``post_route_spef: true``**, and says so rather than silently
    writing nothing. That is not an implementation convenience -- it is the
    whole point of §4.3's own sequencing: an SDF written from a session whose
    parasitics came from ``estimate_parasitics -global_routing`` would carry
    the coarse global-routing estimate's delays while *looking* exactly like
    a post-route measurement to every downstream simulation that annotates
    it. The `write_sdf` call therefore rides inside the real-parasitics
    session or not at all.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlaceAndRouteError("request.post_route_sdf must be a boolean")
    if value and not post_route_spef:
        raise PlaceAndRouteError(
            "request.post_route_sdf requires request.post_route_spef: true -- "
            "the SDF is written from the post-route read_spef session, so an "
            "SDF written without it would carry estimate_parasitics "
            "-global_routing delays while looking like a post-route measurement"
        )
    return value


# --------------------------------------------------------------------------- #
# PDK / LEF / liberty resolution
# --------------------------------------------------------------------------- #


def _resolve_liberty(
    cell_library: str,
    requested_corner: str | None,
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.
    ``variant``/``root`` (the CLI's ``--pdk``/``--pdk-root`` flags, threaded
    through from :func:`run_place_and_route`) select a specific installed PDK
    variant/root exactly as :func:`klayout_tools.pdk.find_pdk` does; ``None``
    for either leaves that resolver's own default search order in effect.
    Raises :class:`PlaceAndRouteError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`).

    Thin wrapper around :func:`klayout_tools.pdk.resolve_liberty_for_cell_library`
    (issue #1652) -- that shared implementation (including the IHP
    single-underscore liberty-filename fallback, issue #1790) is what
    ``synthesize.py`` and ``post_route_sta.py``'s own ``_resolve_liberty``
    wrappers call too, so all three modules stay in sync by construction;
    only the exception type raised on each failure differs per module.
    """
    return resolve_liberty_for_cell_library(
        cell_library,
        requested_corner,
        PlaceAndRouteError,
        variant=variant,
        root=root,
    )


def _resolve_lef(
    cell_library: str,
    pdk_info: dict[str, Any],
    interconnect_corner: str = "nom",
) -> tuple[str, str]:
    """Resolve the tech + merged-cell LEF pair for ``cell_library``, pinned
    to the *same* PDK install/variant :func:`_resolve_liberty` already
    resolved (never re-searches). ``interconnect_corner`` (issue #1100,
    ``request.pdk.interconnect_corner``, one of ``"min"``/``"nom"``/
    ``"max"``) selects which tech-LEF parasitic corner
    :func:`klayout_tools.pdk.lef_files` resolves; the default ``"nom"``
    preserves this function's original byte-identical behaviour. Raises
    :class:`PlaceAndRouteError` when either file is missing -- including
    when a caller-selected corner is not staged by the resolved PDK install
    (``lef_files``'s own ``None``-means-absent convention), never a silent
    fallback to nominal."""
    lefs = lef_files(
        cell_library,
        variant=pdk_info["variant"],
        root=pdk_info["root"],
        corner=interconnect_corner,
    )
    tech_lef = lefs["tech_lef"]
    cell_lef = lefs["cell_lef"]
    if tech_lef is None or cell_lef is None:
        missing = [
            name
            for name, path in (("tech_lef", tech_lef), ("cell_lef", cell_lef))
            if path is None
        ]
        detail = (
            f" (requested interconnect_corner '{interconnect_corner}')"
            if tech_lef is None
            else ""
        )
        raise PlaceAndRouteError(
            f"LEF not found for deck: standard-cell library '{cell_library}' "
            f"under resolved PDK install '{pdk_info['variant']}' at "
            f"'{pdk_info['root']}' is missing: {', '.join(missing)}{detail}"
        )
    return tech_lef, cell_lef


def _resolve_gds_view(pdk_info: dict[str, Any], cell_library: str) -> str:
    mapped = descriptor_views(pdk_info, cell_library).get("gds")
    if mapped is not None:
        return mapped
    libs_ref = pdk_info["assets"]["libs_ref"]
    gds_path = os.path.join(libs_ref or "", cell_library, "gds", f"{cell_library}.gds")
    if libs_ref is None or not os.path.isfile(gds_path):
        raise PlaceAndRouteError(
            f"standard-cell GDS view not found for '{cell_library}' under "
            f"resolved PDK install '{pdk_info['variant']}' (expected "
            f"'{gds_path}')"
        )
    return gds_path
