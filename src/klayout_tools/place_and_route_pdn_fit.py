"""Request-level cross-check between ``request.power``'s PDN strap geometry
and ``request.floorplan``'s core size for `klt place-and-route` (issue
#2170).

Why this module exists
----------------------

``_validate_power`` and ``_validate_floorplan`` each validate their own
block in isolation, and both can pass on a request that is *jointly*
impossible: a platform's own verbatim strap geometry has an implicit
minimum core dimension, and a small (or utilization-derived) floorplan can
be below it. Nothing in the request is individually wrong -- the straps are
the platform's numbers, the utilization is the ported default -- so the run
proceeds and fails deep inside the ``"floorplan"`` stage, after
``initialize_floorplan``/``place_macro``/``make_tracks`` have already run::

    [ERROR PDN-0185] Insufficient width (55.44 um) to add straps on layer
    Metal5 in grid "grid" with total strap width 49.3 um and offset 44.8 um

That message names neither request field, and it arrives minutes into a run
rather than at request-validation time. This module derives the same
quantity ``pdngen`` derives, before OpenROAD is invoked, so the request is
rejected up front with a message naming both ``request.floorplan`` and
``request.power``.

The formula, mirrored from ``pdngen``'s own source
---------------------------------------------------

Verified against ``The-OpenROAD-Project/OpenROAD`` ``src/pdn/src/straps.cpp``
(``master``, read 2026-09-20) -- these are the exact three pieces of
arithmetic behind the ``PDN-0185`` above:

1. ``Straps::Straps`` -- when ``-spacing`` is not given, the stripe spacing
   defaults to ``pitch / <net count> - width``, snapped **down** to the
   layer's manufacturing grid.
2. ``Straps::getStrapGroupWidth`` -- a strap "group" is one stripe per net
   in the grid's voltage domain: ``net_count * width + (net_count - 1) *
   spacing``. `klt place-and-route` always emits a single core voltage
   domain carrying exactly one power and one ground net
   (``set_voltage_domain -name {CORE} -power {...} -ground {...}``), so
   ``net_count`` is :data:`GRID_NET_COUNT` (2) here, never a guess.
3. ``Straps::checkLayerOffsetSpecification`` -- the failing comparison is
   ``grid_width < offset + strap_group_width``, where ``grid_width`` is the
   core's ``dy()`` for a layer whose LEF routing direction is
   ``HORIZONTAL`` and its ``dx()`` otherwise (``VoltageDomain::
   getDomainArea`` returns ``block->getCoreArea()`` for a core-domain grid).

Cross-checked against this issue's own reported numbers: the gf180 ``7t``/
``9t`` PDN config's ``Metal5`` stripe (``-width {4.480} -pitch {89.6}
-offset {44.8}``, no ``-spacing``) gives a default spacing of ``89.6 / 2 -
4.48 = 40.32``, a group width of ``2 * 4.48 + 40.32 = 49.28`` (the report's
"total strap width 49.3 um") and a minimum core dimension of ``44.8 +
49.28 = 94.08`` um (the report's "94.1 um").

``followpins`` straps are deliberately exempt: ``FollowPins::
checkLayerSpecifications`` overrides the base-class check and only validates
the layer width -- a row rail follows the rows it is drawn on rather than
being placed at ``offset`` from the core edge.

Never a false positive: every core dimension is an UPPER bound
--------------------------------------------------------------

A pre-flight rejection of a request OpenROAD would have accepted is far
worse than the cryptic failure this module exists to replace, so every
comparison is deliberately made against an **upper bound** of the core
dimension ``pdngen`` will actually see:

- ``method: "explicit"`` -- ``core_area_um`` is the caller's own requested
  core box. OpenROAD snaps its lower-left corner *up* to a site multiple and
  builds rows inside it (``InitFloorplan::makeRows``), so the core
  ``pdngen`` sees is never *larger* than the requested box.
- ``method: "utilization"`` -- no core size exists anywhere in Python: it is
  derived inside OpenROAD. :func:`core_extent_um` re-derives it with
  ``initialize_floorplan``'s own formula (``InitFloorplan::
  makeDieUtilization``: ``core_area = design_area / utilization``,
  ``core_width = sqrt(core_area / aspect_ratio)``, ``core_height =
  core_width * aspect_ratio``) over the standard-cell area of the very
  netlist this run will place (:func:`standard_cell_area_um2`). OpenROAD
  truncates that ``core_width`` to whole database units and then snaps rows
  inward, so the unrounded value used here is again an upper bound.
- ``method: "def"`` -- the core comes from an existing DEF's own rows, which
  this module does not read; the check is **skipped** entirely (see
  :func:`core_extent_um` returning ``None``).

Every input this module cannot resolve -- an unparseable netlist, a
standard-cell master with no ``SIZE`` in the resolved cell LEF, a tech LEF
that does not declare the strap layer's ``DIRECTION`` -- degrades to
*skipping* or to the *larger* of the two core dimensions, never to a guess
that could reject a valid request. In those cases a caller still sees
``pdngen``'s own ``PDN-0185``, exactly as before this module existed.

Headless invariant: pure Python text/arithmetic, no ``pya``/``klayout.db``
import at all.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from .lef_header import read_lef_header
from .verilog_netlist import parse_gate_level_verilog

#: Stripes per strap group -- one per net in the grid's voltage domain.
#: `klt place-and-route` emits exactly one core voltage domain with one
#: power and one ground net (:func:`klayout_tools.place_and_route.
#: _power_delivery_lines`), so ``pdngen``'s own ``getNetCount()`` is always
#: 2 for the grids this command builds.
GRID_NET_COUNT = 2

#: ``SIZE <width> BY <height> ;`` inside a LEF ``MACRO`` body.
_MACRO_SIZE_RE = re.compile(r"^SIZE\s+([0-9.eE+-]+)\s+BY\s+([0-9.eE+-]+)\s*;")


class CoreExtent(NamedTuple):
    """An upper bound on the core box ``pdngen`` will see, plus a
    human-readable statement of where it came from.

    ``width_um``/``height_um`` are the core's x/y extents in micrometres;
    ``derivation`` is a short phrase naming the ``request.floorplan`` fields
    it was computed from, for the rejection message; ``exact`` is ``True``
    only when the request itself states the core box (``method:
    "explicit"``) rather than this module re-deriving OpenROAD's own
    utilization arithmetic.
    """

    width_um: float
    height_um: float
    derivation: str
    exact: bool


class TechContext(NamedTuple):
    """The two tech-LEF facts this check uses: each routing layer's
    ``DIRECTION`` (which of the core's two dimensions a strap on it steps
    along) and the ``MANUFACTURINGGRID`` (which ``pdngen`` snaps a defaulted
    stripe spacing down to). Both are ``{}``/``None`` when the tech LEF is
    absent or unreadable -- see the module docstring's "never a false
    positive" note for how each absence degrades.
    """

    layer_directions: dict[str, str]
    manufacturing_grid_um: float | None


def read_tech_context(tech_lef_path: str | None) -> TechContext:
    """:class:`TechContext` for the tech LEF at ``tech_lef_path``.

    Never raises: an unreadable/unparseable file (or ``None``) yields an
    empty context, which makes the fit check fall back to the *larger* core
    dimension and an unsnapped spacing -- both upper bounds, so a valid
    request is still never rejected.
    """
    if not tech_lef_path:
        return TechContext({}, None)
    try:
        header = read_lef_header(tech_lef_path)
    except OSError:
        return TechContext({}, None)
    directions = {
        layer["name"]: layer["direction"].upper()
        for layer in header["layers"]
        if isinstance(layer.get("direction"), str)
    }
    grid = header.get("manufacturing_grid_um")
    return TechContext(directions, grid if isinstance(grid, (int, float)) else None)


def strap_group_width_um(
    strap: Mapping[str, Any], *, manufacturing_grid_um: float | None = None
) -> float:
    """``pdngen``'s own ``Straps::getStrapGroupWidth()`` for one validated
    ``request.power.straps[]`` entry, in micrometres.

    ``strap`` is a :func:`klayout_tools.place_and_route._validate_power`
    output entry (``width_um``/``pitch_um``/``offset_um``/``spacing_um``
    already normalized, ``spacing_um`` ``None`` when the caller omitted it).
    With ``spacing_um`` omitted, ``pdngen`` defaults it to ``pitch /
    net_count - width`` snapped down to ``manufacturing_grid_um`` -- mirrored
    exactly when that grid is known, left unsnapped (an upper bound, by at
    most one grid step) when it is not.
    """
    width = float(strap["width_um"])
    spacing = resolved_spacing_um(strap, manufacturing_grid_um=manufacturing_grid_um)
    return GRID_NET_COUNT * width + (GRID_NET_COUNT - 1) * spacing


def resolved_spacing_um(
    strap: Mapping[str, Any], *, manufacturing_grid_um: float | None = None
) -> float:
    """The stripe spacing ``pdngen`` will actually use for ``strap`` -- the
    caller's own ``spacing_um`` when given, otherwise ``Straps::Straps``'s
    own ``pitch / net_count - width`` default, snapped down to
    ``manufacturing_grid_um`` when that is known.
    """
    spacing = strap.get("spacing_um")
    if spacing is not None:
        return float(spacing)
    spacing = float(strap["pitch_um"]) / GRID_NET_COUNT - float(strap["width_um"])
    if manufacturing_grid_um:
        spacing = math.floor(spacing / manufacturing_grid_um + 1e-9) * (
            manufacturing_grid_um
        )
    return spacing


def strap_min_core_um(
    strap: Mapping[str, Any], *, manufacturing_grid_um: float | None = None
) -> float:
    """The smallest core dimension ``pdngen`` will accept for one strap --
    ``offset + getStrapGroupWidth()``, the right-hand side of
    ``Straps::checkLayerOffsetSpecification``'s own ``grid_width < offset +
    strap_width`` comparison.
    """
    return float(strap["offset_um"]) + strap_group_width_um(
        strap, manufacturing_grid_um=manufacturing_grid_um
    )


def parse_macro_sizes(text: str) -> dict[str, tuple[float, float]]:
    """``{<macro name>: (width_um, height_um)}`` for every ``MACRO`` block in
    LEF ``text`` that declares a ``SIZE``.

    A deliberately minimal scan rather than
    :func:`klayout_tools.lef_header.parse_lef_header`: the input here is a
    *merged standard-cell* LEF (hundreds of macros, thousands of pins, a few
    megabytes), and the only attribute needed is each master's footprint.
    Reading the pins too would cost far more than the check is worth. Never
    raises on malformed input -- an unrecognised block is simply not
    extracted, matching ``lef_header``'s own best-effort contract.
    """
    sizes: dict[str, tuple[float, float]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("MACRO "):
            current = line.split()[1]
        elif current is None:
            continue
        elif line.startswith("SIZE "):
            match = _MACRO_SIZE_RE.match(line)
            if match is not None:
                sizes[current] = (float(match.group(1)), float(match.group(2)))
        elif line == f"END {current}":
            current = None
    return sizes


def read_macro_sizes(paths: Sequence[str]) -> dict[str, tuple[float, float]]:
    """:func:`parse_macro_sizes` merged over every LEF in ``paths``, later
    files winning on a name collision. An unreadable file contributes
    nothing rather than raising -- the caller treats a master it cannot size
    as "unknown" and skips the check entirely.
    """
    sizes: dict[str, tuple[float, float]] = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                sizes.update(parse_macro_sizes(handle.read()))
        except OSError:
            continue
    return sizes


def standard_cell_area_um2(
    netlist_path: str,
    hdl_toplevel: str,
    macro_sizes: Mapping[str, tuple[float, float]],
) -> float | None:
    """``initialize_floorplan``'s own ``design_area`` for this run's netlist,
    in square micrometres -- the sum of every instantiated master's LEF
    footprint (``InitFloorplan::designArea()``: ``master->getHeight() *
    master->getWidth()`` over ``block->getInsts()``).

    Returns ``None`` -- meaning "do not check" -- whenever the answer cannot
    be derived exactly: an unreadable or non-flat-gate-level netlist, no
    module named ``hdl_toplevel``, or **any** instantiated master missing
    from ``macro_sizes``. A partial sum would understate the design area,
    which would understate the derived core size, which could reject a
    request OpenROAD would have accepted -- so it is never returned.

    Physical-only cells (tapcells, endcaps, fillers) are correctly absent:
    ``initialize_floorplan`` runs before ``tapcell``/``filler_placement``,
    so OpenROAD's own ``design_area`` does not count them either.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            modules = parse_gate_level_verilog(handle.read())
    except Exception:
        # Any parse/IO failure degrades to "do not check" -- this is a
        # best-effort pre-flight, never a second netlist gate (the real one
        # is `_reject_unsupported_netlist_constructs`).
        return None
    top = next((m for m in modules if m.get("name") == hdl_toplevel), None)
    if top is None:
        return None
    total = 0.0
    for instance in top["instances"]:  # type: ignore[index]
        size = macro_sizes.get(str(instance["cell"]))
        if size is None:
            return None
        total += size[0] * size[1]
    return total


def core_extent_um(
    floorplan: Mapping[str, Any], *, cell_area_um2: float | None = None
) -> CoreExtent | None:
    """An upper bound on the core box ``pdngen`` will see for this
    ``request.floorplan``, or ``None`` when one cannot be derived.

    ``"explicit"`` reads ``core_area_um`` directly. ``"utilization"``
    re-derives OpenROAD's own arithmetic from ``cell_area_um2`` (see
    :func:`standard_cell_area_um2`) and returns ``None`` without it.
    ``"def"`` always returns ``None``: that core comes from an existing
    DEF's rows, which this module deliberately does not read.
    """
    method = floorplan.get("method")
    if method == "explicit":
        core_area_um = list(floorplan["core_area_um"])
        llx, lly, urx, ury = (float(value) for value in core_area_um)
        return CoreExtent(
            abs(urx - llx),
            abs(ury - lly),
            f'floorplan.method "explicit", core_area_um {core_area_um}',
            True,
        )
    if method != "utilization" or cell_area_um2 is None:
        return None
    utilization_pct = float(floorplan["utilization_pct"])
    if utilization_pct <= 0 or cell_area_um2 <= 0:
        return None
    aspect_ratio = floorplan.get("aspect_ratio")
    # `initialize_floorplan`'s own default when `-aspect_ratio` is omitted.
    aspect_ratio = 1.0 if aspect_ratio is None else float(aspect_ratio)
    if aspect_ratio <= 0:
        return None
    core_area = cell_area_um2 / (utilization_pct / 100.0)
    width = math.sqrt(core_area / aspect_ratio)
    return CoreExtent(
        width,
        width * aspect_ratio,
        f'floorplan.method "utilization", utilization_pct {utilization_pct:g} and '
        f"aspect_ratio {aspect_ratio:g} over {cell_area_um2:.2f} um^2 of "
        "standard-cell area in this run's own netlist",
        False,
    )


def _core_axis(layer: str, core: CoreExtent, layer_directions: Mapping[str, str]):
    """``(<axis label>, <available um>)`` for a strap on ``layer``.

    A ``HORIZONTAL`` layer's straps step along y (``pdngen`` compares them
    against the core's ``dy()``), a ``VERTICAL`` layer's along x
    (``dx()``). With no declared direction the *larger* of the two is used,
    so an undeclared layer can only ever under-report a problem, never
    invent one.
    """
    direction = layer_directions.get(layer)
    if direction == "HORIZONTAL":
        return "height", core.height_um
    if direction == "VERTICAL":
        return "width", core.width_um
    return "extent", max(core.width_um, core.height_um)


def _spacing_phrase(strap: Mapping[str, Any], spacing_um: float) -> str:
    if strap.get("spacing_um") is not None:
        return f"spacing_um {spacing_um:.4g}"
    return (
        f"a {spacing_um:.4g} um spacing, pdngen's own "
        f"pitch_um/{GRID_NET_COUNT} - width_um default"
    )


def pdn_fit_error(
    power: Mapping[str, Any],
    floorplan: Mapping[str, Any],
    *,
    core: CoreExtent,
    tech: TechContext,
) -> str | None:
    """The rejection message for the first ``request.power.straps[]`` entry
    that cannot fit ``core``, or ``None`` when every strap fits.

    Reports the first offender rather than all of them, matching every other
    ``request.*`` validation error in
    :mod:`klayout_tools.place_and_route`.
    """
    for index, strap in enumerate(power["straps"]):
        if strap.get("followpins"):
            # `FollowPins::checkLayerSpecifications` overrides the base-class
            # offset/width check -- a row rail follows the rows, so it has no
            # offset-from-the-core-edge minimum at all.
            continue
        grid_um = tech.manufacturing_grid_um
        required = strap_min_core_um(strap, manufacturing_grid_um=grid_um)
        axis, available = _core_axis(strap["layer"], core, tech.layer_directions)
        if required <= available:
            continue
        group = strap_group_width_um(strap, manufacturing_grid_um=grid_um)
        spacing = resolved_spacing_um(strap, manufacturing_grid_um=grid_um)
        estimate = "" if core.exact else " (derived)"
        return (
            f"request.power.straps[{index}] (layer {strap['layer']}) needs at "
            f"least {required:.4g} um of core {axis}: offset_um "
            f"{float(strap['offset_um']):.4g} + {group:.4g} um for the "
            f"power/ground stripe pair (2 x width_um "
            f"{float(strap['width_um']):.4g} + "
            f"{_spacing_phrase(strap, spacing)}). request.floorplan gives a "
            f"core {axis} of only {available:.4g} um{estimate} -- "
            f"{core.derivation}. Widen the floorplan, or lower this strap's "
            "offset_um/pitch_um: pdngen would otherwise reject the grid "
            "mid-'floorplan' stage with PDN-0185 \"Insufficient width\", "
            "naming neither request field."
        )
    return None


def check_request(
    *,
    floorplan: Mapping[str, Any],
    power: Mapping[str, Any] | None,
    tech_lef: str | None = None,
    cell_lefs: Sequence[str] = (),
    netlist_path: str | None = None,
    hdl_toplevel: str | None = None,
) -> str | None:
    """The whole pre-flight, end to end: ``None`` when this
    ``floorplan``/``power`` pair is fine (or cannot be judged), otherwise the
    message :func:`klayout_tools.place_and_route.run_place_and_route` raises
    as a ``PlaceAndRouteError``.

    ``power`` is :func:`klayout_tools.place_and_route._validate_power`'s
    output (``None`` when ``request.power`` was omitted -- nothing to
    check). ``cell_lefs`` are the LEFs whose ``MACRO`` ``SIZE`` statements
    size this netlist's masters: the resolved standard-cell LEF plus any
    ``request.macros[].lef``.
    """
    if power is None or not power.get("straps"):
        return None
    cell_area = None
    if floorplan.get("method") == "utilization" and netlist_path and hdl_toplevel:
        cell_area = standard_cell_area_um2(
            netlist_path, hdl_toplevel, read_macro_sizes(cell_lefs)
        )
    core = core_extent_um(floorplan, cell_area_um2=cell_area)
    if core is None:
        return None
    return pdn_fit_error(power, floorplan, core=core, tech=read_tech_context(tech_lef))


__all__ = [
    "GRID_NET_COUNT",
    "CoreExtent",
    "TechContext",
    "check_request",
    "core_extent_um",
    "parse_macro_sizes",
    "pdn_fit_error",
    "read_macro_sizes",
    "read_tech_context",
    "standard_cell_area_um2",
    "strap_group_width_um",
    "strap_min_core_um",
]
