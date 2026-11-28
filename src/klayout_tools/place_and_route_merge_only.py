"""``klt place-and-route --from-def``: the DEF→GDS merge as a standalone
local entry point (issue #7721).

A consumer holding a **committed routed stream** -- the routed DEF and merged
GDS a finished ``klt place-and-route`` run lists as its stage artifacts --
and a post-fix ``klt`` pin could, before this module, neither apply the
merge-time repair family (:func:`~klayout_tools.place_and_route_min_area.
repair_min_area`, :func:`~klayout_tools.place_and_route_via_pad.trim_via_pads`
with its ``tip_check``/``router_fill_scan``, and
:func:`~klayout_tools.place_and_route_power_stack.repair_power_stack`) to
that stream nor measure what they would do to it short of a fresh
``place-and-route`` run. ``--resume`` reuses the merge only when the stage
``.odb`` checkpoints and ``*.commit.json`` records are still on disk with
matching input hashes, and those are not artifacts a result set commits
alongside its GDS/DEF -- so every evidence set committed before a repair
stayed red on every later pin, and the repairs' live effectiveness on a real
routed block was unmeasurable from committed evidence.

This module closes that gap additively: :func:`run_merge_from_def` runs
*only* the existing merge (:func:`~klayout_tools.place_and_route_gds_merge.
_merge_def_to_gds`, byte-for-byte unchanged, repairs included) on a
caller-supplied routed DEF, resolving the tech/cell LEF, standard-cell GDS
view and macro views through the same request-driven resolution the full
flow uses. No OpenROAD process is ever launched, no stage ODB or commit
record is read, and the merged GDS is written to its own
``<hdl_toplevel>_from_def.gds`` name so a re-merge can never clobber the
committed ``<hdl_toplevel>.gds`` it is being compared against.

Request shape: the same request document the full flow takes, with only the
fields the merge actually consumes required (``hdl_toplevel`` and ``pdk``
with its ``cell_library``); ``netlist``/``floorplan``/``seed`` may be
omitted, and every key :func:`klayout_tools.place_and_route.load_request`
allows is accepted here too, so a consumer can point ``--from-def`` at the
request file of the original run unchanged.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any

from . import output_lock
from ._paths import load_request_json, reject_unknown_keys, validate_request_shape
from ._provenance import content_hash
from .pdk import PdkNotFoundError, find_pdk
from .pdk_cells import _default_variant_shipping
from .place_and_route import (
    _ALLOWED_REQUEST_KEYS,
    _TECH_LEF_CORNERS,
    SCHEMA_VERSION,
    _stage_root,
    _validate_outputs,
)
from .place_and_route_base import PlaceAndRouteError
from .place_and_route_gds_merge import _merge_def_to_gds
from .place_and_route_portable import portable_response
from .place_and_route_request import (
    _resolve_gds_view,
    _resolve_lef,
    _validate_macros,
    _validate_output_dbu_um,
    _validate_portable_paths,
    _validate_post_route_spef,
)

#: Suffix of the GDS a merge-only run writes, next to the ``.def`` stem it is
#: merged from -- deliberately *not* the full flow's own ``<top>.gds`` name,
#: so re-merging a committed stream never overwrites the committed GDS a
#: consumer is comparing against (issue #7721's hash-retention criterion).
_FROM_DEF_GDS_SUFFIX = "_from_def.gds"


def _load_merge_request(request_path: str) -> dict[str, Any]:
    """The merge-only request contract: same envelope, same allowed keys,
    only the fields :func:`_merge_def_to_gds` consumes required.

    A consumer reuses the original run's request unchanged (every extra key
    it carries is accepted); a trimmed request naming just the top cell and
    the PDK works too, since no stage ever reads the netlist, floorplan or
    seed from here.
    """
    request = load_request_json(request_path, PlaceAndRouteError)
    request = validate_request_shape(
        request,
        "request file",
        error_cls=PlaceAndRouteError,
        required_fields=("hdl_toplevel", "pdk"),
    )
    reject_unknown_keys(
        request, _ALLOWED_REQUEST_KEYS, request_path, "", PlaceAndRouteError
    )
    _validate_outputs(request, request_path)
    return request


def _merge_request_fields(request: dict[str, Any], request_dir: str) -> dict[str, Any]:
    """Validate the request fields the merge consumes, returning them as a
    dict (``hdl_toplevel``, ``cell_library``, ``interconnect_corner``,
    ``macros``, ``output_dbu_um``, ``portable_paths``).

    Every check is the one :func:`klayout_tools.place_and_route_request_checks.
    _validate_request_level` performs for the same field, verbatim -- this is
    the merge-only subset of that contract, not a second opinion on it. The
    liberty ``corner`` is shape-checked only: the merge reads no liberty, so
    the corner resolves nothing here.
    """
    hdl_toplevel = request["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise PlaceAndRouteError("request.hdl_toplevel must be a non-empty string")

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PlaceAndRouteError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise PlaceAndRouteError(
            "request.pdk.corner must be a non-empty string when given"
        )
    interconnect_corner = pdk_spec.get("interconnect_corner", "nom")
    if interconnect_corner not in _TECH_LEF_CORNERS:
        raise PlaceAndRouteError(
            "request.pdk.interconnect_corner must be one of: "
            + ", ".join(sorted(_TECH_LEF_CORNERS))
        )
    # A request opting into the post-route SPEF pass is a request for work
    # this entry point cannot do (it needs the route stage's own OpenSTA
    # checkpoint); refuse it by name rather than silently dropping the
    # field from the response.
    if _validate_post_route_spef(request.get("post_route_spef")):
        raise PlaceAndRouteError(
            "--from-def runs only the DEF->GDS merge; request.post_route_spef "
            "needs the route stage's OpenSTA checkpoint a placement run "
            "produces"
        )
    return {
        "hdl_toplevel": hdl_toplevel,
        "cell_library": cell_library,
        "interconnect_corner": interconnect_corner,
        "macros": _validate_macros(request.get("macros"), request_dir, None),
        "output_dbu_um": _validate_output_dbu_um(request.get("output_dbu_um")),
        "portable_paths": _validate_portable_paths(request.get("portable_paths")),
    }


def run_merge_from_def(
    request_path: str,
    from_def: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Run only the DEF→GDS merge (repairs included) on ``from_def``.

    ``request_path`` is the request document resolving the PDK views -- the
    same file the original ``klt place-and-route`` run took (extra fields
    are accepted and ignored; only ``hdl_toplevel`` and ``pdk.cell_library``
    are required). ``pdk_variant``/``pdk_root`` are the CLI's ``--pdk``/
    ``--pdk-root`` flags, threaded to :func:`klayout_tools.pdk.find_pdk`
    exactly as the full flow threads them.

    No OpenROAD stage runs: no ODB checkpoint, commit record or metrics file
    is read or written, and the response carries no stage timing, routing or
    ODB-metric fields -- those exist only when a placement run produced
    them. The merged GDS is written to ``<stage_root>/<top>_from_def.gds``
    (``outputs.dir`` honored, populated directories allowed -- the whole
    point is re-merging into a directory of committed evidence), under the
    same advisory output-dir lock every cooperating ``klt`` writer takes.

    Returns the JSON-schema response (see ``docs/cli/place-and-route.md``,
    "--from-def"): the merge's own repair/scan report fields
    (``layer_map``, ``def_net_names``, ``min_area_repair``,
    ``via_pad_trim`` with its nested ``tip_check``/``router_fill_scan``,
    ``power_stack_repair``, ``m4_grid_repair``, ``pin_conductor``), the
    artifact paths
    (``def_path``/``gds_path``/``gds_dbu_um``), and an additive ``from_def``
    block reporting the actual merge inputs with the supplied DEF's content
    hash. Raises :class:`PlaceAndRouteError` for a missing DEF, an
    unresolvable PDK/LEF/GDS view, a top-cell mismatch, or any merge
    failure -- the existing checks, unchanged.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    request = _load_merge_request(request_path)

    if not os.path.isfile(from_def):
        raise PlaceAndRouteError(f"--from-def DEF not found: {from_def}")
    from_def = os.path.abspath(from_def)

    fields = _merge_request_fields(request, request_dir)
    hdl_toplevel = fields["hdl_toplevel"]
    cell_library = fields["cell_library"]
    macros = fields["macros"]
    output_dbu_um = fields["output_dbu_um"]
    portable_paths = fields["portable_paths"]

    try:
        pdk_info = find_pdk(variant=pdk_variant, root=pdk_root)
    except PdkNotFoundError as exc:
        raise PlaceAndRouteError(str(exc)) from exc
    # Issue #4204 parity with the full flow's own resolution: when no
    # explicit variant/`$PDK` choice was made and the default install lacks
    # the cell library, the first installed variant that ships it is used
    # instead -- the same substitution `resolve_liberty_for_cell_library`
    # applies before the flow's LEF/GDS resolution below, so both entry
    # points always merge from the same install.
    pdk_info = _default_variant_shipping(cell_library, pdk_info, pdk_variant, pdk_root)

    # Same view resolution the full flow performs after `_resolve_liberty`:
    # the LEF pair pinned to the resolved install (interconnect corner
    # honored), the standard-cell GDS view the merge itself resolves, and
    # the macro LEF/GDS views `_validate_macros` already resolved.
    tech_lef, cell_lef = _resolve_lef(
        cell_library, pdk_info, fields["interconnect_corner"]
    )
    cell_gds = _resolve_gds_view(pdk_info, cell_library)

    stage_root = _stage_root(request_dir, request)
    out_path = os.path.join(stage_root, f"{hdl_toplevel}{_FROM_DEF_GDS_SUFFIX}")
    try:
        os.makedirs(stage_root, exist_ok=True)
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not create output directory '{stage_root}': {exc}"
        ) from exc

    # The stage-root lock without the full flow's populated-`outputs.dir`
    # refusal (#2770/#4231): a merge-only run's whole input is the committed
    # evidence already sitting in that directory.
    with contextlib.ExitStack() as ownership:
        ownership.enter_context(output_lock.hold_output_dir(stage_root))
        merge_info = _merge_def_to_gds(
            def_path=from_def,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            pdk_info=pdk_info,
            cell_library=cell_library,
            hdl_toplevel=hdl_toplevel,
            macros=macros,
            out_path=out_path,
            output_dbu_um=output_dbu_um,
        )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "hdl_toplevel": hdl_toplevel,
        # The actual merge inputs (issue #7721: "report the actual merge
        # inputs and results"), with the supplied DEF's content hash so a
        # consumer can pin which committed routing the report graded.
        "from_def": {
            "def_path": from_def,
            "def_content_hash": content_hash(from_def),
            "tech_lef": tech_lef,
            "cell_lef": cell_lef,
            "cell_gds": cell_gds,
            "macro_lefs": [macro["lef"] for macro in macros],
            "macro_gds": [macro["gds"] for macro in macros if macro["gds"] is not None],
        },
        "def_path": from_def,
        "gds_path": out_path,
        "gds_dbu_um": merge_info["output_dbu_um"],
        "layer_map": {
            "path": merge_info["path"],
            "resolution": merge_info["resolution"],
        },
        "def_net_names": merge_info["def_net_names"],
        "min_area_repair": merge_info["min_area_repair"],
        "via_pad_trim": merge_info["via_pad_trim"],
        "power_stack_repair": merge_info["power_stack_repair"],
        "m4_grid_repair": merge_info["m4_grid_repair"],
        "pin_conductor": merge_info["pin_conductor"],
        "warnings": [],
    }
    if portable_paths:
        return portable_response(
            response, request_dir=request_dir, pdk_root=pdk_info["root"]
        )
    return response
