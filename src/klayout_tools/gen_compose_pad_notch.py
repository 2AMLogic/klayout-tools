"""Pad-introduced notch decision shared by ``klt gen-compose``'s two pad
self-notch checks (issue #6314; the mismatch between them is #7718):

* the ``ports_from_labels`` landing search,
  :func:`~.gen_compose_port_search._pad_self_notch` (#5608), once per
  candidate; and
* ``compose()``'s own-block check,
  :func:`~.gen_compose_routing._pad_self_notch_violation_um` (#1520).

Lives in its own module, imported by both, so the one decision cannot drift
between them and neither of the (size-ratcheted) files that call it has to
carry it. Depends on ``klayout.db`` only -- no ``gen_compose`` names.
"""

from __future__ import annotations

from typing import Any


def _notch_edge_pairs(region: Any, spacing_dbu: int) -> dict[frozenset, int]:
    """Every sub-``spacing_dbu`` same-polygon notch of ``region`` (merged
    first), keyed by its pair of facing edges -- each edge normalised to
    sorted endpoints and the pair to an unordered set, so the key does not
    depend on polygon orientation or which edge KLayout reports first -- with
    the notch width (dbu) as the value. A function of its own so a test can
    spy on exactly which geometry reaches ``notch_check``."""

    def edge_key(edge: Any) -> tuple[tuple[int, int], tuple[int, int]]:
        a, b = (edge.p1.x, edge.p1.y), (edge.p2.x, edge.p2.y)
        return (a, b) if a <= b else (b, a)

    return {
        frozenset((edge_key(pair.first), edge_key(pair.second))): pair.distance()
        for pair in region.merged().notch_check(spacing_dbu)
    }


def _pad_introduced_notches_dbu(
    layer_region: Any,
    pad_box: Any,
    spacing_dbu: int,
) -> list[int]:
    """Widths (dbu) of the sub-``spacing_dbu`` notches that dropping
    ``pad_box`` onto ``layer_region`` *adds*. Empty when it adds none.

    The pad merges with the layer polygons it touches, and a notch is a gap
    between two edges of one merged polygon, so a notch the pad takes part in
    can only live in the polygons that touch it: only those are handed to
    ``notch_check``, before and after inserting the pad, and only edge pairs
    present *after* and absent *before* count. Two properties follow:

    * **Cost** follows the touched polygons, not the layer -- polygons
      elsewhere on the layer (every other net) never reach ``notch_check``.
    * **Attribution**: a notch the layer already carries (e.g. a sub-spacing
      channel klt's deck flags but a clean macro can present, like sky130's
      ``m1.3ab`` exception) is in both sets and no longer rejects a pad that
      is nowhere near it. A pad that *changes* one -- narrows it, splits it,
      or leaves a shortened remainder -- still reports, because the pad takes
      part in it; a pad that covers it entirely leaves nothing to report.

    Clipping the layer to a window around the pad instead would be cheaper
    still but is wrong: a polygon crossing the window edge is cut into pieces
    and a notch between two pieces of what is one polygon on the whole layer
    stops being a notch (measured on #6314: a U-shape whose base lies outside
    the window flips from reject to accept, and the same happens on layers
    that carry no notch at all). Selecting whole polygons keeps that
    connectivity; on clean layers it reproduces the old whole-layer verdict
    exactly.

    ``layer_region`` need not be pre-merged (``interacting`` honours merged
    semantics); a caller that reuses one region across many pads should merge
    it once. ``pad_box`` is a ``kdb.Box`` in the same dbu.
    """
    import klayout.db as kdb

    touched = layer_region.interacting(kdb.Region(pad_box))
    before = _notch_edge_pairs(touched, spacing_dbu)
    touched.insert(pad_box)
    after = _notch_edge_pairs(touched, spacing_dbu)
    return [width for key, width in after.items() if key not in before]
