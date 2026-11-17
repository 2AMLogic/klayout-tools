"""``voltage_flavor`` ``drc_hints`` for the ``klt gen`` describe step.

Split out of :mod:`klayout_tools.gen_describe` (issue #6025) so the shared
helper could grow a ``drawn`` flag without growing that size-ratcheted
module; ``gen_describe`` imports :func:`_voltage_flavor_hints` from here.
Also holds the marker-drawing helper the ``mos_array``/``guard_ring`` PCells
share.
"""

from __future__ import annotations

from typing import Any

from .gen_layer_params import _voltage_flavor_mark_layer


def _voltage_flavor_hints(
    family: str, params: dict[str, Any], notes: list[str], drawn: bool = True
) -> dict[str, Any]:
    """``drc_hints`` fields for the optional ``voltage_flavor`` marker (issue
    #1054), shared by the ``mos_array``/``diff_pair``/``guard_ring``
    describes: echoes the requested flavour (``None`` when omitted) and
    whether a marker was drawn, appending an explanatory entry to ``notes``
    (in place) when a non-empty request drew none -- mirrors ``esd_device``'s
    own "notes it, never silently drops it" precedent rather than rejecting
    the request. ``drawn=False`` means the generator has nothing to mark
    (``guard_ring`` with no well tie, issue #6025)."""
    voltage_flavor = params["voltage_flavor"]
    mark_present = False
    if voltage_flavor:
        mark_present = _voltage_flavor_mark_layer(family, voltage_flavor) is not None
        if not mark_present:
            notes.append(
                f"params.voltage_flavor '{voltage_flavor}' has no marker layer "
                f"resolved for the resolved PDK family ('{family}') -- no marker "
                "was drawn"
            )
        elif not drawn:
            mark_present = False
            notes.append(
                f"params.voltage_flavor '{voltage_flavor}' marks the ring's well "
                "tie, but none is drawn (params.add_well is false or the "
                f"resolved PDK family ('{family}') checks no well) -- no marker "
                "was drawn"
            )
    return {
        "voltage_flavor": voltage_flavor or None,
        "voltage_flavor_mark_present": mark_present,
    }


def _draw_voltage_flavor_marks(
    pcell: Any, dbu: float, box_um: tuple[float, float, float, float]
) -> None:
    """Draw ``pcell``'s resolved ``voltage_flavor`` marker layer (and its
    extra layer, when it has one) over ``box_um``; a no-op without a marker."""
    from .gen import _insert_boxes

    if not pcell.voltage_flavor_mark_present:
        return
    layers = [pcell.voltage_flavor_mark_layer]
    if pcell.voltage_flavor_extra_mark_present:
        layers.append(pcell.voltage_flavor_extra_mark_layer)
    for layer in layers:
        _insert_boxes(pcell.cell, pcell.layout.layer(layer), dbu, [box_um])
