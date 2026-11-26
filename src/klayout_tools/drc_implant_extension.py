"""``DrcRule.extension_spec``: an ``"enclosed"`` rule measured between the
*edge portions* two foundry expressions select, instead of between two whole
regions (issue #7969).

Split out of :mod:`klayout_tools.drc` to respect its per-file size ratchet,
and following :mod:`klayout_tools.drc_text_exempt` (#5442) in keeping the
deck-owned descriptor (:class:`ImplantExtensionSpec`) beside its evaluation.

The motivating case is gf180mcu's ``PP.5``, a ``Pplus`` implant's extension
beyond its own ``PCOMP`` diffusion, as the foundry deck measures it
(``rule_decks/pplus.drc`` at ``10ee7fc7``):

- ``PP.5b`` -- every ``PCOMP`` edge against the ``Pplus`` edge portions that
  do not coincide with an ``NCOMP`` edge and lie inside ``Nwell``, or outside
  ``LVPWELL`` and inside ``DNWELL``: 0.16 um, ``projection``;
- ``PP.5dii`` -- the edge portions of a ``PCOMP`` wholly outside ``DNWELL``
  that do not coincide with an ``Nplus`` edge and lie within 0.429 um of a
  pure ``Nwell``, against the edges of a ``Pplus`` wholly outside ``DNWELL``:
  0.16 um, ``projection``;
- ``PP.5di`` -- the complement of ``PP.5dii``'s portion: 0.02 um.

One tap can carry portions of both ``PP.5d`` classes -- one edge within reach
of a well, the others not. The whole-polygon ``DerivedLayer.context`` split
(issue #3484) held every edge of such a tap to 0.16 um, which is where the
434 outside-well markers in the issue's ledger came from; keeping a ``Pplus``
edge that coincides with an ``NCOMP`` edge and measuring with ``euclidian``
metrics account for the other 11. Selecting a class by clipping the ``PCOMP``
instead would leave an artificial edge at the cut for the check to measure,
so the split is made on the edges of the unclipped checked region.

:func:`run_implant_extension` holds a ``selection="near"`` rule to the union
of the two 0.16 um expressions and a ``selection="far"`` rule to ``PP.5di``'s
portion, one marker per checked edge portion. The portions neither 0.16 um
expression looks at -- a ``PCOMP`` inside both ``DNWELL`` and ``LVPWELL``
(``PP.5ci``/``PP.5cii``), one straddling a ``DNWELL`` boundary, or an edge
portion inside the band that coincides with an ``Nplus`` edge -- keep the
rule's previous measurement: its own threshold with the engine's default
metric, against the ``Pplus`` edge portions those expressions drop.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: Which share of the checked edges a rule holds at its own ``threshold_dbu``.
SELECTIONS = ("near", "far")

_Spans = list[tuple[int, int, Any, Any]]


@dataclass(frozen=True)
class ImplantExtensionSpec:
    """Deck-owned inputs for a :class:`~klayout_tools.decks.DrcRule` whose
    ``check`` is ``"enclosed"`` and whose ``other_layer`` is an implant.

    The checked region is that implant's own diffusion. The edges of a
    checked polygon wholly outside ``dnwell`` are split by ``band_um``, a real
    micrometre distance rescaled against the layout's own database unit: the
    portion within reach of a ``nwell`` polygon that touches no ``dnwell``
    polygon belongs to ``selection="near"``, the rest to ``selection="far"``.
    A checked polygon that is not wholly outside ``dnwell`` is the near
    rule's.

    A layer absent from the stream is an empty region, never a reason to
    skip: with no well drawn, every edge is the far rule's share.

    Only ``selection="near"`` reads the rest: ``comp`` and ``nplus`` derive
    the opposite diffusion whose edges exempt a butting implant edge
    (``PP.5b``), and ``lvpwell`` is the well inside ``dnwell`` that ``PP.5b``'s
    second branch leaves out. An invalid descriptor raises
    :class:`ValueError` at construction; how it combines with its rule is
    :func:`validate_implant_extension`'s to check.
    """

    nwell: tuple[int, int]
    dnwell: tuple[int, int]
    band_um: float
    selection: str
    comp: tuple[int, int] | None = None
    nplus: tuple[int, int] | None = None
    lvpwell: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.band_um <= 0:
            raise ValueError("band_um must be positive")
        if self.selection not in SELECTIONS:
            raise ValueError(f"selection must be one of: {', '.join(SELECTIONS)}")
        if self.selection == "near" and None in (self.comp, self.nplus, self.lvpwell):
            raise ValueError('selection "near" requires comp, nplus and lvpwell')

    def layers(self) -> tuple[tuple[int, int], ...]:
        """Every layer this spec reads, in declaration order."""
        named = (self.nwell, self.dnwell, self.comp, self.nplus, self.lvpwell)
        return tuple(layer for layer in named if layer is not None)


def validate_implant_extension(rule: Any) -> None:
    """Reject a :class:`~klayout_tools.decks.DrcRule` whose
    ``extension_spec`` cannot be applied. Same "fail loudly with the rule id"
    contract as ``drc._validate_derived_layer``, and likewise called before
    any layer-presence shortcut, so a bad declaration fails even on a stream
    that lacks the rule's layers."""
    spec = rule.extension_spec
    if spec is None:
        return
    from .drc import DrcError  # local: `drc` imports this module

    if rule.check != "enclosed":
        raise DrcError(
            f"rule '{rule.id}': extension_spec is only valid on 'enclosed' "
            f"checks, not '{rule.check}'"
        )
    if rule.other_layer is None:
        raise DrcError(f"rule '{rule.id}': extension_spec requires other_layer")
    if rule.require_containment:
        raise DrcError(
            f"rule '{rule.id}': extension_spec cannot be combined with "
            "require_containment"
        )


def run_implant_extension(
    cell: Any, setup: Any, rule: Any, region: Any, other_region: Any
) -> tuple[Any, Any | None]:
    """``(edge_pairs, outside_region)`` for ``rule`` on one top ``cell``.

    The same shape ``drc._run_check`` returns, so ``_check_rule_on_cell``
    reports the rule like any other. ``region`` is the checked diffusion and
    ``other_region`` the implant, exactly as the rest of the engine resolved
    them for the rule.
    """
    import klayout.db as kdb

    spec = rule.extension_spec
    layout = setup.layout
    checked = region.merged()
    checked_edges = checked.edges()
    implant = other_region.merged()
    implant_edges = implant.edges()
    deep_well = _merged_region(cell, layout, spec.dnwell)
    # A "pure" well: `nwell.not_interacting(dnwell)` in `main.drc`, whole-polygon.
    nwell = _merged_region(cell, layout, spec.nwell)
    band = nwell.not_interacting(deep_well).sized(round(spec.band_um / layout.dbu))
    # `pp_5d_pcomp`: PP.5d measures only a PCOMP wholly outside DNWELL.
    outside = checked.outside(deep_well).edges()
    far_edges = outside - band
    distance = round(rule.threshold_dbu * setup.dbu_scale)
    escaped = _escaped(rule, checked, implant)

    if spec.selection == "far":
        return far_edges.enclosed_check(implant_edges, distance), escaped

    nplus = _merged_region(cell, layout, spec.nplus)
    lvpwell = _merged_region(cell, layout, spec.lvpwell)
    unbutted = implant_edges - (
        implant_edges & (_merged_region(cell, layout, spec.comp) & nplus).edges()
    )
    # The two 0.16 um expressions, as `pplus.drc` writes them.
    pp5b_pplus = (unbutted & nwell) + ((unbutted - lvpwell) & deep_well)
    pp_5dii_foreground = (outside - nplus.edges()) & band
    # What neither expression looks at keeps the rule's previous measurement.
    remainder_foreground = (checked_edges - far_edges) - pp_5dii_foreground
    remainder_background = unbutted - pp5b_pplus

    return (
        _one_per_portion(
            [
                pp_5dii_foreground.enclosed_check(
                    implant.outside(deep_well).edges(),
                    distance,
                    False,
                    metrics=kdb.Edges.Projection,
                ),
                checked_edges.enclosed_check(
                    pp5b_pplus, distance, False, metrics=kdb.Edges.Projection
                ),
                remainder_foreground.enclosed_check(remainder_background, distance),
            ]
        ),
        escaped,
    )


def _escaped(rule: Any, checked: Any, implant: Any) -> Any | None:
    """The rule's zero-overlap escape term (issue #318), or ``None`` when the
    rule opts out with ``report_escapes=False``."""
    if not rule.report_escapes:
        return None
    return checked.interacting(implant) - implant


def _one_per_portion(edge_pair_sets: list[Any]) -> Any:
    """``EdgePairs`` reduced to one pair per maximal checked-edge portion.

    The two 0.16 um expressions both select some portions, and a portion cut
    by the band differs in extent from the same edge uncut, so first edges
    that overlap or touch on one line are fused; each portion keeps the second
    edge of its longest contributing pair.  The fusion is ``Edges.merged()``'s
    own, including its reading of the zero-length edge ``Edges.enclosed_check``
    reports at a corner: one lying on a portion's line joins that portion, any
    other stands alone.
    """
    import klayout.db as kdb

    segments: list[tuple[Any, Any]] = []
    points: list[tuple[Any, Any]] = []
    for first, second in _plain_pairs(edge_pair_sets):
        (segments if first.length() else points).append((first, second))
    by_line: dict[tuple[int, int, int], _Spans] = {}
    for first, second in segments:
        ux, uy, offset = _line(first)
        lo, hi = sorted((first.x1 * ux + first.y1 * uy, first.x2 * ux + first.y2 * uy))
        by_line.setdefault((ux, uy, offset), []).append((lo, hi, first, second))
    lone: dict[tuple[int, int], Any] = {}
    for first, second in points:
        held = _holder(by_line, first)
        if held is None:
            lone.setdefault((first.x1, first.y1), second)
        else:
            ux, uy, spans = held
            along = first.x1 * ux + first.y1 * uy
            spans.append((along, along, first, second))
    merged = kdb.EdgePairs()
    for spans in by_line.values():
        _fuse(spans, merged)
    for (x, y), second in lone.items():
        merged.insert(kdb.EdgePair(kdb.Edge(x, y, x, y), second))
    return merged


def _fuse(spans: _Spans, merged: Any) -> None:
    """Add to ``merged`` one pair per maximal run of ``spans`` that overlap or
    touch along their line."""
    import klayout.db as kdb

    spans.sort(key=lambda span: span[:2])
    start = 0
    while start < len(spans):
        stop, end = start, spans[start][1]
        while stop + 1 < len(spans) and spans[stop + 1][0] <= end:
            stop += 1
            end = max(end, spans[stop][1])
        portion, second = _portion(spans[start : stop + 1])
        merged.insert(kdb.EdgePair(portion, second))
        start = stop + 1


def _portion(spans: _Spans) -> tuple[Any, Any]:
    """The fused first edge of overlapping ``spans`` and the second edge of
    the longest of them.  The fused edge runs the way that pair's first edge
    does, so the result is oriented as KLayout orients the pair it extends."""
    import klayout.db as kdb

    lo, hi = spans[0][0], max(span[1] for span in spans)
    first, last = spans[0][2], next(span[2] for span in spans if span[1] == hi)
    longest = max(spans, key=lambda span: span[1] - span[0])
    # A zero-length corner can sort first at the low endpoint.
    ux, uy, _ = _line(longest[2])
    start = _end_at(first, lo, ux, uy)
    end = _end_at(last, hi, ux, uy)
    if longest[2].dx() * ux + longest[2].dy() * uy < 0:
        start, end = end, start
    return kdb.Edge(start.x, start.y, end.x, end.y), longest[3]


def _holder(by_line: dict[tuple[int, int, int], _Spans], point: Any) -> Any:
    """``(ux, uy, spans)`` of the first line group a zero-length edge lies on
    and inside a span of, or ``None``."""
    for (ux, uy, offset), spans in by_line.items():
        along = point.x1 * ux + point.y1 * uy
        if point.x1 * uy - point.y1 * ux == offset and any(
            lo <= along <= hi for lo, hi, *_ in spans
        ):
            return ux, uy, spans
    return None


def _end_at(edge: Any, span_end: int, ux: int, uy: int) -> Any:
    """The endpoint of ``edge`` whose projection on ``(ux, uy)`` is ``span_end``."""
    return next(
        point for point in (edge.p1, edge.p2) if point.x * ux + point.y * uy == span_end
    )


def _line(edge: Any) -> tuple[int, int, int]:
    """Canonical unit direction and signed offset of ``edge``'s line: two
    edges share all three exactly when they lie on one line."""
    divisor = math.gcd(abs(edge.dx()), abs(edge.dy())) or 1
    ux, uy = edge.dx() // divisor, edge.dy() // divisor
    if (uy, ux) < (0, 0):
        ux, uy = -ux, -uy
    return ux, uy, edge.x1 * uy - edge.y1 * ux


def _plain_pairs(edge_pair_sets: list[Any]) -> list[tuple[Any, Any]]:
    """Every pair of every set, as plain edges free of carried properties."""
    import klayout.db as kdb

    return [
        (
            kdb.Edge(pair.first.x1, pair.first.y1, pair.first.x2, pair.first.y2),
            kdb.Edge(pair.second.x1, pair.second.y1, pair.second.x2, pair.second.y2),
        )
        for edge_pairs in edge_pair_sets
        for pair in edge_pairs.each()
    ]


def _merged_region(cell: Any, layout: Any, layer: tuple[int, int]) -> Any:
    """``layer``'s recursive shapes in ``cell``, merged; a layer absent from
    the stream is an empty region."""
    import klayout.db as kdb

    index = layout.find_layer(*layer)
    return (
        kdb.Region(cell.begin_shapes_rec(index)).merged()
        if index is not None
        else kdb.Region()
    )
