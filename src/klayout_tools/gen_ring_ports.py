"""Reported ring ``ports[]`` entries for ``klt gen``'s describe side (#5656).

Split out of ``gen_describe.py``: that file sits exactly at its per-file
size ratchet ceiling, so the ring-opening strap-window reporting landed
here instead of appending to it. ``_ring_ports`` moved over verbatim (its
``GAP_*`` marker now also carries ``strap_window_um``), together with the
``_min_spacing_um_for_layer`` deck lookup it insets by -- and, since #6425,
the ``_min_width_um_for_layer`` sibling it floors the window by. Their only
consumer is this reporting path, and keeping them in the ``gen`` group
avoids a ``gen`` -> ``gen_compose`` module edge (``import-boundaries.json``
allows ``gen_compose`` -> ``gen``, never the reverse, and
:func:`klayout_tools.gen_compose_layer_rules._min_width_um_for_layer` is a
private symbol of the ``gen_compose`` group): they mirror ``compose()``'s own
``_min_spacing_um_for_layer`` closure (issue #1386) and the width/area
siblings in :mod:`klayout_tools.gen_compose_layer_rules` rather than
calling into them.

Same conventions as ``gen_describe.py``: ``gen``/``gen_describe`` names are
imported *inside* the functions that use them, so this module never has a
load-time dependency back on ``gen.py`` (which imports ``gen_describe`` at
module scope). ``klayout_tools.decks`` has no such cycle, so its lookups
are imported at module scope.
"""

from __future__ import annotations

from typing import Any

from .decks import UnknownDeckError, get_deck, get_nominal_dbu


def _min_rule_um_for_layer(
    variant: str, layer: tuple[int, int] | None, check: str
) -> tuple[float, str] | None:
    """Resolve the largest plain same-layer ``check`` DRC rule for ``layer``
    from the resolved PDK family's own curated deck -- the *same*
    ``ExtractionDeck``/``DrcRule`` set ``klt drc`` judges generated and
    composed geometry with, never a private, hard-coded threshold.

    The shared body of :func:`_min_spacing_um_for_layer` (``"space"``) and
    :func:`_min_width_um_for_layer` (``"width"``). A layer can carry more
    than one such rule from different DRM sections; the largest is the true
    floor. Rules that name an ``other_layer`` or a ``derived_layer`` are
    cross-layer / derived-region checks, not a same-layer floor, and are
    skipped. Returns ``(threshold_um, rule_id)``, or ``None`` when ``layer``
    is ``None``, the layer has no such rule in the resolved deck, or the PDK
    family/deck cannot be resolved at all -- an unresolvable family degrades
    the lookup to a no-op, exactly like #1386's spacing lookup does.
    """
    from .gen import GenError, _pdk_family

    if layer is None:
        return None
    try:
        family = _pdk_family(variant)
        deck_rules = get_deck(family)
        nominal_dbu_um = get_nominal_dbu(family)
    except (GenError, UnknownDeckError):
        return None
    best: tuple[float, str] | None = None
    for rule in deck_rules:
        if (
            rule.check == check
            and rule.layer == layer
            and rule.other_layer is None
            and rule.derived_layer is None
        ):
            threshold_um = rule.threshold_dbu * nominal_dbu_um
            if best is None or threshold_um > best[0]:
                best = (threshold_um, rule.id)
    return best


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
    """Resolve the tightest applicable minimum-*width* DRC rule for ``layer``
    from the resolved PDK family's own curated deck (issue #6425).

    The ``gen``-group twin of
    :func:`klayout_tools.gen_compose_layer_rules._min_width_um_for_layer`
    (same deck, same plain-same-layer ``"width"`` rule selection, same
    widest-rule-wins tie-break), kept here so the ring-port reporting path
    does not need a ``gen`` -> ``gen_compose`` import the package's import
    boundaries do not allow. ``_ring_ports`` floors ``strap_window_um`` by
    it: a strap filling the window is only as wide as the window's
    along-side extent, so a window narrower than this width admits no
    DRC-legal strap however much spacing slack it has. See
    :func:`_min_rule_um_for_layer` for the ``(threshold_um, rule_id)`` /
    ``None`` contract.
    """
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
    ``klt drc`` run judges a strap with), in the composed frame like every
    other port field, each coordinate rounded to the 6-decimal micron
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

    ``tap_net`` (issue #1421) names the net every tap port carries, reported
    as each entry's ``net``. ``None`` (the default, and every caller that
    predates ``well_island``) keeps the historical ``"net": null`` -- a ring
    whose tie net the generator has no way to know. It is never applied to a
    ``GAP_*`` entry: that marks the *absence* of ring conductor, so it carries
    no net by construction.
    """
    from .gen import RING_SIDE_DIRECTIONS
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
