"""Orientation, port geometry and via dimensions shared by composition helpers."""

from __future__ import annotations

import math
from typing import Any

from ._jsonnum import is_finite_number, is_int

#: Unit outward vector (dx, dy) for each orthogonal ``direction_deg`` a
#: ``klt gen`` port reports. Ports only ever face an axis (0/90/180/270 --
#: see ``gen.py``'s generators), so the router never has to snap a diagonal.
_DIRECTION_VECTORS: dict[int, tuple[int, int]] = {
    0: (1, 0),
    90: (0, 1),
    180: (-1, 0),
    270: (0, -1),
}

#: Supported ``blocks[].orientation`` values (#1166) -- a block's own
#: mirror/rotation, applied about that block's own local (pre-translation)
#: origin *before* ``offset_um`` translates it into the composed frame.
#: ``"mirror_x"`` negates local ``x`` (a horizontal flip -- a block's
#: right-edge port moves to its left edge, and vice versa; this is the
#: minimum case that unblocks a CMOS inverter's shared-drain net, per
#: #1164's root cause #1), ``"mirror_y"`` negates local ``y`` (a vertical
#: flip), ``"rotate_180"`` negates both. See :func:`_apply_orientation_um`
#: for the point transform and :data:`_ORIENTATION_KDB_ARGS` for the
#: equivalent ``kdb.Trans`` construction every geometry-writing consumer
#: (:func:`_write_composed_gds`, :func:`read_block_layer_geometry`) applies
#: to actually-drawn shapes, so a block's reported metadata (``bbox_um``,
#: ``ports[]``) never disagrees with its drawn geometry.
ORIENTATIONS = frozenset({"none", "mirror_x", "mirror_y", "rotate_180"})

#: ``kdb.Trans(rot, mirrx, x, y)`` arguments -- ``rot`` a 0..3 count of
#: 90-degree CCW rotation steps, ``mirrx`` whether to mirror at the x-axis
#: *before* that rotation is applied -- producing the identical point
#: transform as :func:`_apply_orientation_um` for each orientation. Verified
#: against ``klayout.db.Trans``'s own semantics: ``rot=0, mirrx=True`` maps
#: ``(x, y) -> (x, -y)`` (``"mirror_y"``), ``rot=2, mirrx=True`` maps
#: ``(x, y) -> (-x, y)`` (``"mirror_x"``), and ``rot=2, mirrx=False`` maps
#: ``(x, y) -> (-x, -y)`` (``"rotate_180"``).
_ORIENTATION_KDB_ARGS: dict[str, tuple[int, bool]] = {
    "none": (0, False),
    "mirror_x": (2, True),
    "mirror_y": (0, True),
    "rotate_180": (2, False),
}

#: ``direction_deg`` -> ``direction_deg`` remap for each orientation
#: (#1166): a mirrored/rotated block's ports face a different absolute
#: direction even though the port's own name/role is unchanged (e.g. a
#: ``mirror_x``'d block's drain, still named ``D``, now faces ``-x`` instead
#: of ``+x``). Applied once, in :func:`_parse_blocks`, to every port's own
#: ``direction_deg`` -- every downstream consumer (:func:`_DIRECTION_VECTORS`
#: lookups, ring-side classification, stub-widen) then reads an
#: already-correct direction without repeating this remap itself.
_ORIENTATION_DIRECTION_MAP: dict[str, dict[int, int]] = {
    "none": {0: 0, 90: 90, 180: 180, 270: 270},
    "mirror_x": {0: 180, 90: 90, 180: 0, 270: 270},
    "mirror_y": {0: 0, 90: 270, 180: 180, 270: 90},
    "rotate_180": {0: 180, 90: 270, 180: 0, 270: 90},
}


def _apply_orientation_um(x: float, y: float, orientation: str) -> tuple[float, float]:
    """Transform one local-frame point per a block's own ``orientation``
    (#1166), applied about that block's own origin -- *before* ``offset_um``
    translates it into the composed frame. See :data:`_ORIENTATIONS`'s
    docstring for the exact per-value semantics.
    """
    if orientation == "mirror_x":
        return -x, y
    if orientation == "mirror_y":
        return x, -y
    if orientation == "rotate_180":
        return -x, -y
    return x, y


#: Via-drop square side (um, issue #454) -- the same generic contact/via
#: budget every `klt gen` generator's own unit devices already use
#: (`gen.CONTACT_SIZE_UM`), so a via-drop's via is never a second, unvalidated
#: size. `compose()` widens it per via layer to the deck's own minimum (#1501)
#: and clamps it down to the layer's fixed size where the foundry rule is a
#: maximum too (#2585, :func:`_via_fixed_size_um`).
_VIA_DROP_SIZE_UM = 0.22


#: Landing-pad square side (um, issue #454) drawn on *both* sides of a
#: via-drop (the backbone's own ``route_layer`` and the target pin's own
#: layer), independent of the route's own ``width_um`` -- the same
#: `CONTACT_SIZE_UM + 2 * ENCLOSURE_MARGIN_UM` contact-enclosure convention
#: `gen.py`'s own unit-device layouts already use (e.g. `_bjt_unit_layout`'s
#: `contact_region_um`), not a new unvalidated margin. Drawing an explicit
#: landing pad -- rather than relying on the backbone's own trace width --
#: guarantees the via's enclosure requirement is met even when a caller
#: requests a `routing.width_um` narrower than a full contact-enclosure
#: footprint (e.g. sky130's own `li1.width.1` minimum, 0.17um).
_VIA_LANDING_SIZE_UM = _VIA_DROP_SIZE_UM + 2 * 0.1


class GenComposeError(Exception):
    """Raised when a composition request cannot be fulfilled.

    Covers an unresolvable PDK, a malformed request shape, a ``blocks[]``/
    ``connectivity[]`` reference to a nonexistent ``id``/port, an
    unsupported ``placement.strategy``, and a GDS read/write failure -- the
    CLI turns this into a clean stderr message + exit code 1, never a
    traceback (see ``docs/cli/gen-compose.md``'s exit code table).

    ``code`` / ``details`` are an optional machine-readable classification
    (``error.code`` and extra ``error`` fields in the JSON envelope); only the
    ``pins[].request`` failures (#3971) set them today.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


_ORIENTATIONS = ORIENTATIONS


def orient_port(port: dict[str, Any], orientation: str) -> dict[str, Any]:
    """One ``ports[]`` entry, transformed by a block's own ``orientation``
    (#1166): its ``x_um``/``y_um`` (if both are usable numbers -- mirrors
    :func:`_port_has_geometry`'s own tolerance for a port that reports none)
    via :func:`_apply_orientation_um`, and its ``direction_deg`` (if present)
    via :data:`_ORIENTATION_DIRECTION_MAP`. Every other field (``name``,
    ``width_um``, ``layer``, ...) is copied unchanged -- orientation moves a
    port's *position*, it never changes its *identity* or contact size.
    """
    oriented = dict(port)
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if is_finite_number(x_um) and is_finite_number(y_um):
        oriented["x_um"], oriented["y_um"] = _apply_orientation_um(
            float(x_um), float(y_um), orientation
        )
    direction_deg = port.get("direction_deg")
    if is_int(direction_deg):
        oriented["direction_deg"] = _ORIENTATION_DIRECTION_MAP[orientation].get(
            direction_deg, direction_deg
        )
    return oriented


_orient_port = orient_port


def _nearest_grid_point_within(
    composed: float, grid: float, limit: float | None
) -> float | None:
    """The grid point nearest ``composed`` that lies within ``limit`` of it
    (``None`` limit = unconstrained), or ``None`` when no candidate fits."""
    steps = composed / grid
    base = math.floor(steps + 0.5 + 1e-9)
    candidates = sorted((base, base - 1, base + 1), key=lambda c: abs(c - steps))
    for cand in candidates:
        value = cand * grid
        if limit is None or abs(value - composed) <= limit + 1e-9:
            return value
    return None


def _port_probe_position_um(port: dict[str, Any]) -> tuple[float, float] | None:
    """The port's own ``(x_um, y_um)``, or ``None`` when it declares no usable
    numeric position (the same "nothing to check" case the rest of the
    landing-connectivity path tolerates rather than fails). ``bool`` is
    rejected explicitly because it is a subclass of ``int``."""
    x_um, y_um = port.get("x_um"), port.get("y_um")
    if not is_finite_number(x_um):
        return None
    if not is_finite_number(y_um):
        return None
    return float(x_um), float(y_um)


def _port_has_geometry(port: Any) -> bool:
    """Whether ``port`` carries the ``{x_um, y_um, layer{layer, datatype}}``
    geometry a ``pins[]`` label needs to be placed. A block report that omits
    a port's position/layer cannot be labelled -- reported as a partial-success
    note rather than crashing (#210)."""
    if not isinstance(port, dict):
        return False
    # is_finite_number also refuses NaN/+-inf and an int too large for a float
    # (10**400), which every caller would otherwise hit as an uncaught
    # OverflowError/ValueError in its position arithmetic (#6539).
    if not (is_finite_number(port.get("x_um")) and is_finite_number(port.get("y_um"))):
        return False
    layer = port.get("layer")
    return (
        isinstance(layer, dict)
        and isinstance(layer.get("layer"), int)
        and isinstance(layer.get("datatype"), int)
    )


def _drop_array_extra_um(drop: dict[str, Any]) -> float:
    """Extra side length (um) a drop's cut array adds to its landing pad over
    the single-cut pad: ``(max(rows, cols) - 1) * pitch``; ``0.0`` without one."""
    array = drop.get("array")
    if not array:
        return 0.0
    return (max(array["rows"], array["cols"]) - 1) * array["pitch_um"]
