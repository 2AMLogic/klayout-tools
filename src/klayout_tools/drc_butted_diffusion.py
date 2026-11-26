"""Butted-diffusion operand selection for ``klt drc``: the pinned GF180
Nplus/Pplus tables drop the implant geometry of a butted NCOMP/PCOMP pair
before they measure (issue #7970).

A butted pair is an NCOMP and a PCOMP sharing an edge -- one ``Comp`` split
between the two implants. Along that edge neither implant extends past its
diffusion, and the foundry deck deliberately does not measure it there:

- **Spacing** (``NP.3``, ``PP.3``) excludes the implant *polygons* of the
  pair, whole: ``np3_nplus = nplus.not_interacting(ncomp_butted)`` with
  ``ncomp_butted = ncomp.interacting(pcomp)``, and ``pplus`` mirrored. A rule
  declares this with ``other_layer_selection="not_butted"``
  (:func:`select_not_butted`).
- **Enclosure** (``NP.5b``, ``PP.5di``) excludes the coincident *edge
  segments*, and only those: ``NP.5b`` encloses NCOMP with
  ``nplus.edges.not(nplus_butted_edges)`` (``nplus_butted_edges =
  nplus.edges.and(pcomp.edges)``), ``PP.5di`` measures
  ``pcomp.edges.not(nplus_edges)``. The stubs an implant edge keeps beyond
  the coincident stretch stay in, and are harmless only because both rules
  measure by ``projection``: with ``euclidian`` they pair with the butted
  edge corner-to-corner. A rule declares the side, the mask and the metric
  with :class:`ButtedEdgeSpec`.

Spacing is measured with the primitive the foundry statement itself compiles
to, ``Region.separation_check`` (shielded). Enclosure keeps the rule's own
shielded ``Region.enclosed_check`` -- the operand the curated engine has
always read for these rules -- and applies the selection to its pairs: one
whose masked side is butted throughout is not measured, the rest is measured
again on what remains, by the declared metric (:class:`ButtedEdgeSelection`).

Split out of :mod:`klayout_tools.drc` to respect its per-file size ratchet,
following :mod:`klayout_tools.drc_edge_band` (#7955), whose rest class goes
through :func:`rest_pairs` for the one class of edges the foundry deck's
``NP.5b`` covers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: ``ButtedEdgeSpec.metrics`` names -> the ``klayout.db.Edges`` constants.
_METRICS = {"euclidian": "Euclidian", "projection": "Projection"}
_SIDES = ("checked", "other")


@dataclass(frozen=True)
class ButtedEdgeSpec:
    """Deck-owned edge-level operand selection for an ``"enclosed"`` rule.

    The segments of the ``side`` operand's edges (``"checked"``: the rule's
    own region, ``"other"``: ``other_layer``) that coincide with an edge of
    the mask region are butted-diffusion edges: they are dropped before the
    rule measures, and what remains is measured with ``metrics``. The mask
    is ``mask_layers`` joined, intersected with ``mask_intersect`` when that
    is set -- gf180mcu's ``NP.5b`` masks the enclosing ``Nplus`` edges with
    ``PCOMP`` (``Pplus`` and ``Comp``), ``PP.5di`` the checked ``PCOMP``
    edges with ``Nplus``.

    On a rule that also declares ``DrcRule.band_spec`` the selection applies
    to the rest class only: the band and the interior are the foundry
    deck's ``NP.5di``/``NP.5dii``, which keep their own operand.

    A mask layer absent from the stream is an empty mask: nothing is
    dropped. An invalid descriptor raises :class:`ValueError` at
    construction; how it combines with its rule is
    :func:`validate_butted`'s to check.
    """

    side: str
    mask_layers: tuple[tuple[int, int], ...]
    mask_intersect: tuple[int, int] | None = None
    metrics: str = "projection"

    def __post_init__(self) -> None:
        if self.side not in _SIDES:
            raise ValueError(f"side must be one of: {', '.join(_SIDES)}")
        if not self.mask_layers:
            raise ValueError("mask_layers must name at least one layer")
        if self.metrics not in _METRICS:
            raise ValueError(f"metrics must be one of: {', '.join(sorted(_METRICS))}")

    def layers(self) -> tuple[tuple[int, int], ...]:
        """Every layer this spec reads, in declaration order."""
        if self.mask_intersect is None:
            return self.mask_layers
        return (*self.mask_layers, self.mask_intersect)


def validate_butted(rule: Any) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose butted-diffusion
    selections cannot be applied. Same "fail loudly with the rule id"
    contract as ``drc._validate_derived_layer``, and likewise called before
    any layer-presence shortcut."""
    from .drc import DrcError  # local: `drc` imports this module

    spec = rule.butted_spec
    if spec is not None:
        if rule.check != "enclosed":
            raise DrcError(
                f"rule '{rule.id}': butted_spec is only valid on 'enclosed' "
                f"checks, not '{rule.check}'"
            )
        if rule.other_layer is None:
            raise DrcError(f"rule '{rule.id}': butted_spec requires other_layer")
    if rule.other_layer_selection == "not_butted":
        derived = rule.derived_layer
        if derived is None or derived.intersect_with is None:
            raise DrcError(
                f"rule '{rule.id}': not_butted requires a derived_layer with "
                "intersect_with"
            )


def butted_fields(rule: Any) -> dict[str, Any]:
    """``--list-rules`` JSON for ``rule.butted_spec``: a ``butted_edges``
    object naming the side, the mask and the metric. Empty for a rule
    without one, so no other rule's JSON changes."""
    spec = rule.butted_spec
    if spec is None:
        return {}
    mask_intersect = (
        None if spec.mask_intersect is None else _layer_str(spec.mask_intersect)
    )
    return {
        "butted_edges": {
            "side": spec.side,
            "mask_layers": [_layer_str(layer) for layer in spec.mask_layers],
            "mask_intersect": mask_intersect,
            "metrics": spec.metrics,
        }
    }


def select_not_butted(
    rule: Any, cell: Any, layout: Any, other_region: Any, base_region: Any
) -> Any:
    """``other_layer``'s polygons that touch no butted pair: the pinned
    deck's ``nplus.not_interacting(ncomp_butted)``.

    The pair's two diffusions come from the one layer both implants share,
    ``derived_layer.intersect_with``: the rule's own unscoped derived region
    (``pcomp`` for the Nplus rules) and the same layer under ``other_layer``
    (``ncomp``). A polygon of ``other_layer`` touching the latter is dropped,
    whether it butts, overlaps or contains it. ``base_region`` is the
    derived layer's resolved ``base`` in ``cell``.
    """
    from .drc import _derived_region_unscoped  # local: `drc` imports this module

    derived = rule.derived_layer
    assert derived is not None and derived.intersect_with is not None  # validated
    shared = _joined_region(cell, layout, (derived.intersect_with,))
    own = _derived_region_unscoped(rule, base_region, shared, layout.dbu)
    return other_region.not_interacting(
        (shared & other_region.merged()).interacting(own)
    )


@dataclass(frozen=True)
class ButtedEdgeSelection:
    """A rule's :class:`ButtedEdgeSpec` resolved on one cell: the mask's
    edges, indexed for neighbourhood queries."""

    spec: ButtedEdgeSpec
    index: Any  # klayout.Shapes holding the mask's klayout.db.Edge's

    @property
    def options(self) -> dict[str, Any]:
        """The spec's metric, as the check primitives take it."""
        import klayout.db as kdb

        return {"metrics": getattr(kdb.Edges, _METRICS[self.spec.metrics])}

    def crosses(self, pair: Any) -> bool:
        """Whether a mask edge touches the pair's masked side."""
        masked = pair.second if self.spec.side == "other" else pair.first
        return not self._touching(masked).is_empty()

    def measure(self, checked: Any, pair: Any, distance: int) -> Any:
        """``checked`` -- the pair's checked edge, or a stretch of it --
        measured against the pair's other edge by the spec's metric, with
        the butted segments dropped from the masked side."""
        import klayout.db as kdb

        outer = kdb.Edges(pair.second)
        if self.spec.side == "other":
            outer = outer - self._touching(pair.second)
            checked = kdb.Edges(checked)
        else:
            checked = kdb.Edges(checked) - self._touching(checked)
        return checked.enclosed_check(outer, distance, **self.options)

    def pairs(self, pairs: Any, distance: int) -> Any:
        """``pairs`` -- a shielded ``Region.enclosed_check`` result -- with
        the butted segments dropped from the masked side of each pair. A
        pair whose masked side is butted throughout is not measured; what
        remains of it is measured again, by the spec's metric. At the
        engine's default ``euclidian`` a pair no mask edge touches is passed
        through as it is."""
        import klayout.db as kdb

        kept = kdb.EdgePairs()
        default_metric = self.spec.metrics == "euclidian"
        for pair in pairs.each():
            if default_metric and not self.crosses(pair):
                kept.insert(pair)
            else:
                kept.insert(self.measure(pair.first, pair, distance))
        return kept

    def _touching(self, edge: Any) -> Any:
        """The mask edges touching ``edge``'s bounding box."""
        import klayout.db as kdb

        return kdb.Edges(
            [shape.edge for shape in self.index.each_touching(edge.bbox())]
        )


def butted_edge_selection(
    rule: Any, cell: Any, layout: Any
) -> ButtedEdgeSelection | None:
    """``rule.butted_spec`` resolved against ``cell``, or ``None`` when the
    rule has none. The mask is the merged region the spec names, as edges."""
    import klayout.db as kdb

    spec = rule.butted_spec
    if spec is None:
        return None
    mask = _joined_region(cell, layout, spec.mask_layers)
    if spec.mask_intersect is not None:
        mask = mask & _joined_region(cell, layout, (spec.mask_intersect,))
    index = kdb.Shapes()
    index.insert(mask.edges())
    return ButtedEdgeSelection(spec, index)


def rest_pairs(
    region: Any, other_region: Any, distance: int, selection: ButtedEdgeSelection | None
) -> Any:
    """The enclosure pairs of the class no ``band_spec`` relaxes: the
    shielded ``Region.enclosed_check`` the rule ran before it had a band or
    a butted selection, through ``selection`` when the rule carries one."""
    pairs = region.enclosed_check(other_region, distance)
    if selection is None:
        return pairs
    return selection.pairs(pairs, distance)


def run_butted_edge_check(
    cell: Any, setup: Any, rule: Any, region: Any, other_region: Any
) -> tuple[Any, Any | None]:
    """``(edge_pairs, outside_region)`` for an ``"enclosed"`` rule that
    carries ``butted_spec`` but no ``band_spec``: one class, every edge of
    the merged checked region through :func:`rest_pairs`. The escape term
    (issue #318) is the one ``drc._run_check`` reports."""
    region = region.merged()
    other_region = other_region.merged()
    distance = round(rule.threshold_dbu * setup.dbu_scale)
    selection = butted_edge_selection(rule, cell, setup.layout)
    edge_pairs = rest_pairs(region, other_region, distance, selection)

    outside_region = None
    if rule.report_escapes:
        outside_region = region.interacting(other_region) - other_region
    return edge_pairs, outside_region


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
