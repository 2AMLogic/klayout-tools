"""``DrcRule.band_spec``: an ``"enclosed"`` rule held to two different
thresholds, selected per edge by how deep the edge sits inside a context
layer (issue #7955).

Split out of :mod:`klayout_tools.drc` to respect its per-file size ratchet,
and following :mod:`klayout_tools.drc_text_exempt` (#5442) in keeping the
deck-owned descriptor (:class:`EdgeBandSpec`) beside its evaluation.

The motivating case is gf180mcu's ``NP.5d``. The foundry deck
(``rule_decks/nplus.drc``) measures a tap's Nplus extension two ways, picked
per *edge* by the edge's distance from the boundary of a pure Nwell
(``nwell_n_dn``):

- ``NP.5di`` -- an NCOMP edge in the outer band of the well, within 0.429 um
  of its boundary (``nwell_n_dn - nwell_n_dn.sized(-0.429.um)``), needs
  0.16 um, ``projection`` metric;
- ``NP.5dii`` -- an NCOMP edge deeper inside (``nwell_n_dn.sized(-0.429.um)``)
  needs 0.02 um, ``euclidian``.

A 1 um NCOMP square with 80 nm of Nplus on every side is entirely in the
second class: the foundry deck reports nothing, a flat 0.16 um rule reports
all four edges. That false positive is what this module removes.

The foundry's ``nwell.not_interacting(dnwell)`` selects whole well polygons.
Even a distant contact with DNWELL excludes the entire well from NP.5d.
Both well-depth checks measure to the edges of
``nplus.not(dnwell).not_outside(nwell)``. A DNWELL cut in the implant can
therefore set the enclosure of a tap inside a pure well.

The whole-polygon ``DerivedLayer.context`` split (issue #3484) cannot express
it. One tap can carry edges in both classes -- near the well's boundary on
one side, deep inside on the others -- and selecting a class by clipping
the tap polygon would leave an artificial edge at the cut for the enclosure
check to measure. So the split is made on the *edges* of the unclipped
checked region instead. Strict segments retain the shielded enclosure
pairs from the original region check. The two well-depth classes use the
foundry expressions on their selected edges.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor
from typing import Any

#: ``EdgeBandSpec`` metric names -> the ``klayout.db.Region`` constants.
_METRICS = {"euclidian": "Euclidian", "projection": "Projection"}


@dataclass(frozen=True)
class EdgeBandSpec:
    """Deck-owned inputs for a :class:`~klayout_tools.decks.DrcRule` whose
    ``check`` is ``"enclosed"``.

    The checked region's edges are split three ways, each measured against
    ``other_layer`` at its own threshold:

    - **band**: the part within ``band_width_um`` of the boundary of a
      context polygon that touches no ``exclude_layers`` polygon, at the
      rule's ``threshold_dbu`` with ``band_metrics``;
    - **interior**: the part of such a polygon deeper than ``band_width_um``
      from its boundary, at ``inner_threshold_dbu`` with ``inner_metrics``;
    - **everything else** -- no context under the edge, or a checked polygon
      that touches an ``exclude_layers`` polygon anywhere -- at
      ``threshold_dbu`` with the engine's default ``euclidian`` metric,
      exactly as the rule behaved without a ``band_spec``.

    An absent layer contributes an empty region. Without context every edge
    keeps the flat threshold. Without exclusions all context polygons qualify.
    Both well-depth classes measure to the edges of enclosing polygons left
    after subtracting the exclusion and selecting those overlapping context.
    Both selections are whole-polygon and err strict -- a context polygon that
    merely touches an excluded polygon is no context at all, and a checked
    polygon that merely touches one keeps the strict threshold on every edge.
    An edge exactly ``band_width_um`` from the boundary is in the band (the
    strict class), matching the foundry expressions this mirrors.

    An invalid descriptor raises :class:`ValueError` at construction; how it
    combines with its rule is :func:`validate_edge_band`'s to check.
    """

    context_layers: tuple[tuple[int, int], ...]
    exclude_layers: tuple[tuple[int, int], ...]
    band_width_um: float
    inner_threshold_dbu: int
    band_metrics: str = "projection"
    inner_metrics: str = "euclidian"

    def __post_init__(self) -> None:
        if not self.context_layers:
            raise ValueError("context_layers must name at least one layer")
        if self.band_width_um <= 0:
            raise ValueError("band_width_um must be positive")
        if self.inner_threshold_dbu < 0:
            raise ValueError("inner_threshold_dbu must be non-negative")
        for name in ("band_metrics", "inner_metrics"):
            if getattr(self, name) not in _METRICS:
                raise ValueError(
                    f"{name} must be one of: {', '.join(sorted(_METRICS))}"
                )

    def layers(self) -> tuple[tuple[int, int], ...]:
        """Every layer this spec reads, in declaration order."""
        return (*self.context_layers, *self.exclude_layers)


def validate_edge_band(rule: Any) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose ``band_spec``
    cannot be applied. Same "fail loudly with the rule id" contract as
    ``drc._validate_derived_layer``, and likewise called before any
    layer-presence shortcut, so a bad declaration fails even on a stream
    that lacks the rule's layers."""
    spec = rule.band_spec
    if spec is None:
        return
    from .drc import DrcError  # local: `drc` imports this module

    if rule.check != "enclosed":
        raise DrcError(
            f"rule '{rule.id}': band_spec is only valid on 'enclosed' checks, "
            f"not '{rule.check}'"
        )
    if rule.other_layer is None:
        raise DrcError(f"rule '{rule.id}': band_spec requires other_layer")
    if rule.require_containment:
        raise DrcError(
            f"rule '{rule.id}': band_spec cannot be combined with require_containment"
        )
    if spec.inner_threshold_dbu >= rule.threshold_dbu:
        raise DrcError(
            f"rule '{rule.id}': band_spec inner_threshold_dbu must be below "
            "threshold_dbu"
        )


def edge_band_fields(rule: Any, nominal_dbu_um: float) -> dict[str, Any]:
    """``--list-rules`` JSON for ``rule.band_spec``: an ``edge_band`` object
    naming both layer sets, the band width and the interior's own threshold
    in um. Empty for a rule without one, so no other rule's JSON changes."""
    spec = rule.band_spec
    if spec is None:
        return {}
    return {
        "edge_band": {
            "context_layers": [_layer_str(layer) for layer in spec.context_layers],
            "exclude_layers": [_layer_str(layer) for layer in spec.exclude_layers],
            "band_width_um": spec.band_width_um,
            "inner_threshold_um": round(spec.inner_threshold_dbu * nominal_dbu_um, 9),
        }
    }


def _foundry_dbu(distance_um: float, layout_dbu: float) -> int:
    """KLayout DRC rounds signed distances to nearest, with ties toward +infinity."""
    return floor(0.5 + distance_um / layout_dbu)


def run_edge_band_check(
    cell: Any, setup: Any, rule: Any, region: Any, other_region: Any
) -> tuple[Any, Any | None]:
    """``(edge_pairs, outside_region)`` for ``rule`` on one top ``cell``.

    The same shape ``drc._run_check`` returns, so ``_check_rule_on_cell``
    reports a banded rule like any other: ``EdgePairs`` through the ordinary
    per-pair path, and the zero-overlap escape term (issue #318) as polygons
    (``None`` when the rule opts out with ``report_escapes=False``). Pairs
    come in a fixed order -- band, interior, then the rest.
    """
    import klayout.db as kdb

    spec = rule.band_spec
    layout = setup.layout
    region = region.merged()
    other_region = other_region.merged()
    context = _joined_region(cell, layout, spec.context_layers)
    exclude = _joined_region(cell, layout, spec.exclude_layers)
    pure = context.not_interacting(exclude)
    inset_dbu = _foundry_dbu(-spec.band_width_um, layout.dbu)
    # A stream too coarse to resolve the band cannot place any edge in it:
    # relax nothing, rather than call the whole well "interior".
    inner = pure.sized(inset_dbu) if inset_dbu < 0 else kdb.Region()
    excluded, eligible = region.split_interacting(exclude)
    # The foundry selects whole taps with area inside any well, then their
    # perimeter segments in pure wells. External contact alone selects no tap.
    outside_wells, overlapping = eligible.split_outside(context)
    at_well, away_from_pure = overlapping.split_interacting(pure)
    well_edges = at_well.edges()
    band_edges = well_edges & (pure - inner)
    interior_edges = well_edges & inner
    well_implant_edges = (other_region - exclude).not_outside(context).edges()

    edge_pairs = kdb.EdgePairs()
    for edges, threshold_dbu, metrics in (
        (band_edges, rule.threshold_dbu, spec.band_metrics),
        (interior_edges, spec.inner_threshold_dbu, spec.inner_metrics),
    ):
        distance = _foundry_dbu(threshold_dbu * setup.nominal_dbu_um, layout.dbu)
        edge_pairs.insert(
            edges.enclosed_check(
                well_implant_edges,
                distance,
                metrics=getattr(kdb.Region, _METRICS[metrics]),
            )
        )

    distance = round(rule.threshold_dbu * setup.dbu_scale)
    strict = excluded + outside_wells + away_from_pure
    edge_pairs.insert(strict.enclosed_check(other_region, distance))
    crossing = at_well.not_inside(pure)
    edge_pairs.insert(_strict_parts(crossing, other_region, pure, distance))

    outside_region = None
    if rule.report_escapes:
        outside_region = region.interacting(other_region) - other_region
    return edge_pairs, outside_region


def _strict_parts(crossing: Any, implant: Any, wells: Any, distance: int) -> Any:
    """Shielded enclosure on the perimeter of crossing taps outside the wells."""
    import klayout.db as kdb

    result = kdb.EdgePairs()
    shielded = set(crossing.enclosed_check(implant, distance).each())
    if not shielded:
        return result

    # A marker's endpoints are rounded to the layout grid. On a slope the
    # marker need not be collinear with its source edge. Recover its two
    # source edges by reproducing the marker, then cut the original tap edge
    # at the well boundary. Whole-edge shielding would test a different
    # span, so use the ordinary region check above to select visible pairs.
    candidates = crossing.enclosed_check(
        implant, distance, whole_edges=True, shielded=False
    )
    well_index = kdb.Shapes()
    well_index.insert(wells)
    for pair in candidates.each():
        tap_edge = kdb.Edges(pair.first)
        partner = kdb.Edges(pair.second)
        for marker in tap_edge.enclosed_check(partner, distance).each():
            if marker in shielded:
                result.insert(_clip_strict_marker(pair, marker, well_index, distance))
    # Adjacent source edges can reproduce the same corner pair. Deduplicate
    # after clipping so distinct strict portions of a marker are preserved.
    return kdb.EdgePairs(list(dict.fromkeys(result.each())))


def _clip_strict_marker(
    source: Any, marker: Any, well_index: Any, distance: int
) -> Any:
    import klayout.db as kdb

    nearby = kdb.Region(
        [shape.polygon for shape in well_index.each_touching(source.first.bbox())]
    )
    result = kdb.EdgePairs()
    marker_box = marker.first.bbox()
    for part in (kdb.Edges(source.first) - nearby).each():
        # Both stretches belong to the same source edge. If the well cut is
        # beyond the violation, keep the original marker's grid coordinates.
        if marker_box.inside(part.bbox()):
            return kdb.EdgePairs(marker)
        if marker_box.touches(part.bbox()):
            result.insert(
                kdb.Edges(part).enclosed_check(kdb.Edges(source.second), distance)
            )
    return result


def _joined_region(cell: Any, layout: Any, layers: tuple[tuple[int, int], ...]) -> Any:
    """Merged union of ``layers``' recursive shapes in ``cell``; a layer absent
    from the stream contributes nothing."""
    import klayout.db as kdb

    region = kdb.Region()
    for layer in layers:
        index = layout.find_layer(*layer)
        if index is not None:
            region += kdb.Region(cell.begin_shapes_rec(index))
    return region.merged()


def _layer_str(layer: tuple[int, int]) -> str:
    return f"{layer[0]}/{layer[1]}"
