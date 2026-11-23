"""Reported ring ``ports[]`` entries for ``klt gen``'s describe side (#5656).

Ring openings use the shared plain-layer deck lookup for spacing insets
and the minimum strap width, also used by the composition layer rules.

Same conventions as ``gen_describe.py``: ``gen``/``gen_describe`` names are
imported *inside* the functions that use them, so this module never has a
load-time dependency back on ``gen.py`` (which imports ``gen_describe`` at
module scope). ``klayout_tools.decks`` has no such cycle, so its lookups
are imported at module scope.
"""

from __future__ import annotations

from typing import Any

from .decks.plain_rules import largest_plain_rule_um
from .gen_base import RING_SIDE_DIRECTIONS, GenError
from .gen_layer_params import _pdk_family


def _min_rule_um_for_layer(
    variant: str, layer: tuple[int, int] | None, check: str
) -> tuple[float, str] | None:
    """Resolve the variant before looking up its shared plain-layer rule."""
    if layer is None:
        return None
    try:
        family = _pdk_family(variant)
    except GenError:
        return None
    return largest_plain_rule_um(family, layer, check)


def _min_spacing_um_for_layer(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    """Resolve the tightest applicable minimum same-*spacing* DRC rule for
    ``layer`` from the resolved PDK family's own curated deck.

    Mirrors
    :func:`klayout_tools.gen_compose_layer_rules._min_width_um_for_layer`,
    generalised to ``"space"`` rules -- the exact lookup ``compose()`` runs
    through its own ``_min_spacing_um_for_layer`` closure (issue #1386) for
    the same deck, so ``klt gen``'s reported ring-opening strap window
    (``_ring_ports`` below, issue #5656) can inset by the identical
    threshold ``klt drc`` will judge a strap with. See
    :func:`_min_rule_um_for_layer` for the ``(threshold_um, rule_id)`` /
    ``None`` contract.
    """
    return _min_rule_um_for_layer(variant, layer, "space")


def _min_width_um_for_layer(
    variant: str, layer: tuple[int, int] | None
) -> tuple[float, str] | None:
    """Minimum DRC-legal width of a strap filling the ring opening."""
    return _min_rule_um_for_layer(variant, layer, "width")


def _ring_ports(
    ring: dict[str, Any],
    offset_um: tuple[float, float],
    tap_prefix: str,
    tap_layer: dict[str, Any],
    tap_width_um: float,
    gap_layer: dict[str, Any],
    tap_net: str | None = None,
    pdk_variant: str = "",
) -> list[dict[str, Any]]:
    """Reported ``ports[]`` entries for a drawn ring.

    One ``<tap_prefix><side>`` tap port at the midpoint of every side that is
    still covered by ring metal there, plus -- when the ring was cut with
    ``ring_gap_side`` (#434) -- one ``GAP_<side>`` entry marking the opening
    itself. A ``GAP_*`` entry is a *marker, not a conductor*: it reports where
    a route may cross the ring (``x_um``/``y_um`` = the opening's centre on
    the ring's own centre line, ``width_um`` = the opening's length along that
    side, ``direction_deg`` = the side's outward normal) on the layer a route
    would cross it on, and `klt gen-compose` rejects any attempt to wire or
    label it.

    A ``GAP_*`` entry also carries ``strap_window_um`` (issue #5656): the
    opening's own box inset along the ring side -- never across it -- by the
    resolved deck's minimum same-layer spacing for ``gap_layer``
    (:func:`_min_spacing_um_for_layer`, the same lookup and threshold a
    ``klt drc`` run judges a strap with), in the generated cell's own
    origin-relative frame like the marker's ``x_um``/``y_um``, each
    coordinate rounded to the 6-decimal micron
    precision deck thresholds themselves report at -- the pure-float inset
    arithmetic would otherwise leak representation noise (17-digit tails)
    no other reported coordinate in the response has. A strap whose extent
    along the ring side stays within the window is spacing-clean against
    the ring metal on both sides of the opening; the window's extent across
    the side is the ring band's full thickness, since a strap crossing the
    ring is constrained only along it. ``None`` -- never a zero-width or
    inverted box, which a consumer's geometry library would silently
    normalise into a phantom window exactly where a strap may not go --
    when the along-side extent is narrower than the layer's deck minimum
    *width* (:func:`_min_width_um_for_layer`) or is <= 0 after the spacing
    inset (an opening narrower than two spacings): either way no
    same-layer-legal strap fits, since a strap filling the window is only
    as wide as its along-side extent (issue #6425 -- the spacing inset alone
    can leave a positive sliver, e.g. sky130's ``ring_gap_um: 0.4`` leaves
    0.06um against ``li1.width.1``'s 0.17um). A window of exactly the
    minimum width still reports. ``None`` is the same absent-box encoding
    ``well_box_um``/``well_keepout_box_um`` already use. An unresolvable
    PDK family -- or a deck with no ``"space"`` rule for ``gap_layer`` --
    insets by ``0.0``, reporting the opening's own box, the same
    degrade-to-no-op the #1386 spacing lookup established; likewise a deck
    with no ``"width"`` rule for ``gap_layer`` applies no width floor, so
    the window is then ``None`` only when the extent is <= 0.

    ``offset_um`` places the ring inside the generated cell; it is not a
    compose placement. A compose consumer transforms the box by
    ``blocks[].orientation`` before adding ``blocks[].offset_um`` to obtain
    compose coordinates. See ``docs/cli/gen-compose.md`` for compose guidance.

    ``tap_net`` (issue #1421) names the net every tap port carries, reported
    as each entry's ``net``. ``None`` (the default, and every caller that
    predates ``well_island``) keeps the historical ``"net": null`` -- a ring
    whose tie net the generator has no way to know. It is never applied to a
    ``GAP_*`` entry: that marks the *absence* of ring conductor, so it carries
    no net by construction.
    """
    from .gen_describe import _box_um_field

    ox, oy = offset_um
    ports: list[dict[str, Any]] = []
    for side, direction in RING_SIDE_DIRECTIONS.items():
        xy = ring["ports"].get(side)
        if xy is None:
            continue  # this side's midpoint sits inside the ring opening
        ports.append(
            {
                "name": f"{tap_prefix}{side}",
                "net": tap_net,
                "layer": tap_layer,
                "x_um": xy[0] + ox,
                "y_um": xy[1] + oy,
                "width_um": tap_width_um,
                "direction_deg": direction,
            }
        )

    gap = ring["gap"]
    if gap is not None:
        layer_pair = (gap_layer["layer"], gap_layer["datatype"])
        spacing = _min_spacing_um_for_layer(pdk_variant, layer_pair)
        inset = spacing[0] if spacing is not None else 0.0
        min_width = _min_width_um_for_layer(pdk_variant, layer_pair)
        width_floor = min_width[0] if min_width is not None else 0.0
        x0, y0, x1, y1 = gap["box_um"]
        if gap["side"] in ("N", "S"):
            window = (x0 + inset + ox, y0 + oy, x1 - inset + ox, y1 + oy)
        else:
            window = (x0 + ox, y0 + inset + oy, x1 + ox, y1 - inset + oy)
        # Round to the micron precision deck thresholds report at, *before*
        # the degenerate check, so an opening exactly two spacings wide (a
        # mathematically zero extent like 1e-16) reports None rather than a
        # phantom window -- and the inset's float arithmetic leaves no
        # representation noise in the emitted coordinates. The along-side
        # extent is a difference of two rounded coordinates, so it is
        # rounded again before the width comparison: a window exactly the
        # minimum width wide (0.69 - 2 * 0.23 against 0.23) would otherwise
        # fall a float ulp short of its own floor and be wrongly nulled.
        window = tuple(round(v, 6) for v in window)
        along = round(
            (
                window[2] - window[0]
                if gap["side"] in ("N", "S")
                else window[3] - window[1]
            ),
            6,
        )
        ports.append(
            {
                "name": f"GAP_{gap['side']}",
                "net": None,
                "layer": gap_layer,
                "x_um": gap["x_um"] + ox,
                "y_um": gap["y_um"] + oy,
                "width_um": gap["opening_um"],
                "direction_deg": RING_SIDE_DIRECTIONS[gap["side"]],
                "strap_window_um": (
                    _box_um_field(window)
                    if along > 0.0 and along >= round(width_floor, 6)
                    else None
                ),
            }
        )
    return ports
