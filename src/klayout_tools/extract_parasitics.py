"""RC-parasitics subsystem for ``klt extract --parasitics``/``--critical-net``.

Split out of ``extract.py`` (issue #1572, following the #1195/PR #1200
precedent that split SPEF export into ``extract_spef.py``): the geometry
helpers that measure a net's resistive squares and area/perimeter
(:func:`_n_squares`, :func:`_instance_drawn_mask`, :func:`_net_area_perim_um`,
:func:`_bbox_overlap`, :func:`_net_pair_key`), the lumped R/C model itself
(:func:`_compute_parasitics`, 543 lines) and its dead-metal cross-check
(:func:`_detect_dead_metal`), the star/distributed-ladder topology helpers
(:func:`_terminal_star_positions_um`, :func:`_terminal_star_weights`,
:func:`_distributed_rc_order`, :func:`_distributed_rc_segments`), the writer
that turns a computed model into netlist devices
(:func:`_inject_parasitics`, 518 lines), the substrate DC-tie pass
(:func:`_tie_substrate_nets_to_ground`), and the net-naming helpers shared by
all of the above (:func:`spice_safe_net_name`, :func:`_net_identity_name`,
:func:`_unique_net_name`).

This is a **private implementation module, not part of the public API** --
mirroring ``extract_spef.py``'s own framing. ``extract.py`` re-exports
:func:`spice_safe_net_name` (``from .extract_parasitics import
spice_safe_net_name as spice_safe_net_name``) because it has external
importers of its own (``lvs.py``, ``netlist_digest.py``) as well as many call
sites inside ``extract.py`` itself, so every existing ``from klayout_tools.
extract import spice_safe_net_name`` call site keeps working unchanged.
:func:`_n_squares`, :func:`_distributed_rc_order`, and
:func:`_distributed_rc_segments` have no remaining callers in ``extract.py``
(only ``tests/test_extract.py`` imported them directly) so they are *not*
re-exported -- that test file's imports were updated to point at this module
instead. Everything else is reached only through ``run_extract`` (which
calls :func:`_inject_parasitics`) and ``_extract_netlist`` (which calls
:func:`_compute_parasitics` and :func:`_detect_dead_metal`), the three call
sites this subsystem's whole interface narrows down to.

A handful of functions below (:func:`_detect_dead_metal`,
:func:`_inject_parasitics`, :func:`_tie_substrate_nets_to_ground`) still need
a few names that remain defined in ``extract.py`` itself
(:func:`~klayout_tools.extract._bbox_um_rounded`, the
``_PARAM_PRECISION_UM``/``_MIN_PARASITIC_R_OHM``/``_SPICE_GLOBAL_GROUND_NODE``/
``SUBSTRATE_DC_TIE_RESISTANCE_OHM`` tunables, and
:func:`~klayout_tools.extract._is_synthesized_substrate_net_name`) -- each has
other callers or definitions living in ``extract.py``, so it stays there
rather than moving along with this subsystem. Those imports are deferred
(``from .extract import ...`` *inside* the function body, not at this
module's top level) because ``extract.py`` imports *this* module at its own
top level (the same "``from .extract_parasitics import ...``" pattern
``extract_spef.py``/``extract_abstract.py`` already established) -- a
module-level import back the other way would be a circular import evaluated
mid-load; deferring it to call time, by which point both modules have
finished importing, avoids that entirely.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ._layout import region as _region
from .decks import ExtractionDeck, ParasiticsDeck
from .extract_abstract import _sanitize_instance_name
from .pdk_models import equivalent_rectangle_um

if TYPE_CHECKING:
    import klayout.db as kdb


def _n_squares(area_um2: float, perimeter_um: float) -> float:
    """Estimate the number of resistive *squares* of a net's copper on one
    layer from its total area and perimeter.

    First-order geometric approximation: model the layer's shapes as one
    equivalent rectangle with the same area ``A`` and perimeter ``P``, whose
    side lengths ``L`` and ``W`` are the roots of ``t^2 - (P/2) t + A = 0``;
    the square count is then ``L / W`` (``>= 1``). This is exact for a single
    rectangular wire and reduces to ``1`` for a square. When the shapes are
    "rounder" than any rectangle allows (negative discriminant -- e.g. a
    single square-ish pad, or fragmented geometry), it clamps to ``1``.

    Deliberately simple and fixed (issue #216: "a single, fixed, first-order
    lumped model -- no fast/accurate mode selector"); it over-counts squares
    for L-shaped or multi-fragment nets, which biases the resulting series
    resistance conservatively high rather than low.
    """
    if area_um2 <= 0.0 or perimeter_um <= 0.0:
        return 0.0
    dims = equivalent_rectangle_um(area_um2, perimeter_um)
    if dims is None:
        # "Rounder" than any rectangle allows (negative discriminant -- e.g. a
        # single square-ish pad, or fragmented geometry): clamp to one square.
        return 1.0
    length, width = dims
    return max(1.0, length / width)


def _instance_drawn_mask(
    layout: kdb.Layout, top_cell: kdb.Cell, drawn_layer: tuple[int, int] | None
) -> kdb.Region:
    """The merged union of ``drawn_layer``'s geometry across every instance
    placed (directly or transitively) under ``top_cell`` -- i.e. everything
    *not* drawn directly in ``top_cell`` itself -- for the
    ``--parasitics-top-cell-only`` hierarchy split (issue #1704; see
    ``docs/design/parasitics-hierarchy-attribution-spike.md`` section 3.1).

    Built from the **layout layer index** (``layout.find_layer(*drawn_layer)``),
    never from an ``l2n.register()`` handle -- the latter are
    ``LayoutToNetlist``-internal identifiers meaningful only to
    ``polygons_of_net``, an unrelated integer space from a ``kdb.Layout``
    layer index; mixing the two silently reads a plausible-looking wrong
    layer rather than raising.

    ``min_depth = 1`` on the recursive shape iterator skips the depth-0
    shapes (the ones drawn directly on ``top_cell``), so the result is
    exactly the instance subtree's own geometry, transforms applied --
    deliberately *not* ``all_drawn - own_drawn``: that difference form hands
    any top-cell/instance overlap back to the top cell, the bias the spike
    doc's section 3.3 rejects. ``net_region - instance_drawn`` (this mask)
    charges the overlap to the instance side instead, so a caller who has
    already extracted a sub-block separately can cleanly subtract its
    contribution back out of a composed top-level net's R/C.

    Returns an empty ``Region`` when ``drawn_layer`` is ``None`` or the layer
    carries no shapes anywhere in the stream (``find_layer`` returns
    ``None``) -- in either case any net's geometry on that role is empty too
    (built the same way, via ``_layout.region()``), so the split is a no-op.
    """
    import klayout.db as kdb

    if drawn_layer is None:
        return kdb.Region()
    layer_index = layout.find_layer(*drawn_layer)
    if layer_index is None:
        return kdb.Region()
    it = top_cell.begin_shapes_rec(layer_index)
    it.min_depth = 1
    return kdb.Region(it).merged()


def _net_area_perim_um(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    dbu: float,
    indices: list[int],
    subtract_indices: list[int] | None = None,
    instance_mask: kdb.Region | None = None,
) -> tuple[float, float, float, float]:
    """``(area_um2, perimeter_um, top_cell_area_um2, top_cell_perim_um)`` of
    ``net``'s shapes across the given registered layer ``indices`` (each an
    index returned by ``LayoutToNetlist.register``), with any
    ``subtract_indices`` layers geometrically removed first.

    ``subtract_indices`` lets the poly role exclude the transistor gate
    regions from a net's poly shapes before measuring (issue #226): the gate
    sits over the channel, not the substrate the coefficients describe, and
    its capacitance is already captured by the device model. The subtraction
    is a purely local operation on the per-net ``Region`` returned by
    ``polygons_of_net`` -- it registers no extra ``LayoutToNetlist`` layer, so
    the connectivity graph is untouched.

    ``instance_mask`` (``--parasitics-top-cell-only``, issue #1704) is this
    role's :func:`_instance_drawn_mask` -- the merged union of the role's
    drawn layer under every instance beneath the top cell. When given, the
    returned tuple's trailing ``(top_cell_area_um2, top_cell_perim_um)`` are
    ``region - instance_mask``'s area/perimeter: the portion of this net's
    shapes on this role attributed to the top cell rather than an instance
    (spike doc sections 3.2/3.3). ``None`` (the default) skips this
    entirely -- both trailing elements are ``0.0`` and no extra ``Region``
    subtraction runs, byte-identical to this function's pre-#1704
    behaviour."""
    import klayout.db as kdb

    region = kdb.Region()
    for index in indices:
        region += l2n.polygons_of_net(net, index)
    for index in subtract_indices or ():
        region -= l2n.polygons_of_net(net, index)
    area_um2 = region.area() * dbu * dbu
    perim_um = region.perimeter() * dbu
    top_cell_area_um2 = 0.0
    top_cell_perim_um = 0.0
    if instance_mask is not None:
        top_cell_part = region - instance_mask
        top_cell_area_um2 = top_cell_part.area() * dbu * dbu
        top_cell_perim_um = top_cell_part.perimeter() * dbu
    return area_um2, perim_um, top_cell_area_um2, top_cell_perim_um


def _bbox_overlap(a: kdb.Box, b: kdb.Box) -> bool:
    """Cheap bounding-box prefilter for the vertical-overlap coupling pass
    (issue #760): wraps ``kdb.Box.overlaps`` so the (expensive, C++-side)
    actual ``Region & Region`` boolean below only runs for net-pairs whose
    per-level bounding boxes genuinely intersect. On a routed block the vast
    majority of net-pairs on two adjacent levels do not share any XY
    footprint at all, so this alone turns an ``O(n*m)`` candidate space into
    a small fraction actually reaching the boolean AND -- no halo/neighbour
    search structure needed, matching the roadmap's own cost estimate
    (`docs/design/extract-fidelity-roadmap.md` Stage 2a: "no halo search, no
    neighbour-search structure")."""
    return a.overlaps(b)


def _net_pair_key(name_a: str, name_b: str) -> tuple[str, str]:
    """Canonical (order-independent) key for an unordered net pair, used to
    accumulate a net-to-net coupling total across every contributing
    adjacent-level pair (issue #760) -- sorted so ``(A, B)`` and ``(B, A)``
    always collide into the same accumulator entry."""
    return (name_a, name_b) if name_a <= name_b else (name_b, name_a)


def _compute_parasitics(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    dbu: float,
    deck: ExtractionDeck,
    parasitics_deck: ParasiticsDeck,
    layer_index: dict[str, int],
    metal_index: list[int],
    layout: kdb.Layout | None = None,
    top_cell: kdb.Cell | None = None,
    critical_nets: frozenset[str] | None = None,
    parasitics_nets: frozenset[str] | None = None,
    parasitics_top_cell_only: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute one first-order lumped ``(R, C)`` per net, plus net-to-net
    vertical-overlap coupling capacitance, from the extracted per-net/
    per-layer geometry (issue #760, Extract Stage 2a).

    For each net (except the deck's substrate/ground net, which is the AC
    ground the ground capacitances return to):

    - **C to ground** = sum over conductor roles of ``area_um2 * cap_area +
      perimeter_um * cap_perim`` (femtofarads), the lumped ground capacitance
      of the net's interconnect -- with one correction (below): a metal
      role's ``area_um2`` term has any area coupled to a *different* net on
      an adjacent level (see next) subtracted out first, so that charge
      moves from the ground term to the coupling term rather than being
      counted in both.
    - **series R** = sum over conductor roles of ``sheet_res * n_squares``
      (ohms), the net's lumped interconnect resistance. Unaffected by
      coupling: resistance is a property of the conductor's own geometry, not
      of what does or does not sit above/below it.

    Roles map to the registered geometry layers: ``poly`` the poly region with
    the transistor gate regions subtracted out (issue #226 -- gate capacitance
    lives in the device model), and ``metals[i]`` each metal-stack layer
    (index-aligned with the deck's ``metals``). The optional ``diffusion`` role
    (NMOS+PMOS source/drain) is left unset by the shipped decks: the M cards'
    ``AS``/``AD``/``PS``/``PD`` already feed the device model's junction
    capacitance, so a diffusion cap term would double-count it.

    **Vertical-overlap coupling (issue #760):** for each adjacent pair of
    metal levels ``(i, i+1)`` with a curated ``parasitics_deck.metal_overlaps``
    coefficient, and each pair of *distinct* nets with geometry on that pair
    of levels, the area of ``net_a``'s shapes on level ``i`` intersected with
    ``net_b``'s shapes on level ``i+1`` (a plain ``Region & Region`` boolean,
    KLayout's own already-registered per-net regions -- no halo/neighbour
    search) becomes a coupling capacitance between ``net_a`` and ``net_b``
    (``overlap_area_um2 * metal_overlaps[i]``), and is subtracted from
    *both* nets' ground area term on their own respective level (``net_a``'s
    on level ``i``, ``net_b``'s on level ``i+1``) -- charge is *moved*, never
    duplicated. The per-level deduction is the **geometric union** of every
    partner's overlap region with that net on that level, not the sum of
    their areas: a level participates in two adjacent pairs (as upper plate
    of ``(i-1, i)`` and lower plate of ``(i, i+1)``), so a straight via stack
    would otherwise have the same square micron deducted twice and its charge
    destroyed rather than moved. Same-net overlap (a via stack tying one net's
    own two levels together) is excluded by construction: coupling is only
    ever computed between two *distinct* nets, so a net's own via stack
    contributes nothing
    here (it is ordinary connectivity, already merged into one net upstream).
    **Lateral (same-layer, sidewall) coupling, for critical nets only (issue
    #976, Epic #709 Phase 2a):** unlike vertical-overlap coupling above,
    which is unconditional across the whole layout, this pass only ever
    considers a same-layer net pair where at least one side's name is in
    ``critical_nets`` -- ``None``/empty (the default) skips it entirely,
    byte-identical to this function's pre-#976 behaviour. This restriction
    is deliberate, not a shortcut of convenience: a full-layout lateral pass
    is the roadmap's own "medium cost" Stage 2b (a real neighbour-search
    cost across every same-layer pair on a routed block, not the cheap
    bounding-box-prefiltered ``Region & Region`` boolean vertical overlap
    uses -- see ``docs/design/extract-fidelity-roadmap.md``'s Stage 2b cost
    estimate). Scoping the search to a caller-declared "nets that matter"
    set (Epic #709 Phase 2's own framing: high-impedance nodes, a SAR ADC's
    CDAC top plate, a PLL loop filter) keeps this increment's cost bounded
    to exactly the nets a caller cares about while still closing the gap
    ``PARASITIC_MODEL_SCOPE["coupling"]`` names.

    **Scoping the ground pass itself (issue #1700, the cost half of issue
    #1699):** ``parasitics_nets`` (``klt extract --parasitics-net``,
    repeatable) restricts which nets Pass 1 measures at all -- ``None``/empty
    (the default) measures every net, byte-identical to this function's
    pre-#1700 behaviour. The membership test is the identical
    ``spice_safe_net_name(_net_identity_name(net)) in <frozenset>`` check the
    lateral pass already applies to ``critical_nets`` (issue #1162's escaped
    spelling), reused rather than introducing a second net-classification
    mechanism. Pass 1 is where the per-net ``polygons_of_net`` /
    ``_net_area_perim_um`` calls live, so skipping a net there is the actual
    saving; the consequence is that an unnamed net also contributes no
    ``metal_regions`` entry, and therefore can neither give nor receive
    *coupling* capacitance -- only a pair of both-named nets can couple under
    a scoped run. That is deliberate and is the point: a coupling term
    computed against a partner whose own ground model was never built would
    be attributing charge to a net that does not appear in the output at all.
    Unlike ``critical_nets``, which only ever *adds* a pass, this one
    subtracts work, so a caller trading completeness for runtime must say so
    explicitly.

    **Top-cell-only hierarchy split, ground terms only (issue #1704, the
    implementation half of the spike in #1702 --
    ``docs/design/parasitics-hierarchy-attribution-spike.md``):**
    ``parasitics_top_cell_only`` (``klt extract --parasitics-top-cell-only``)
    additionally splits each net's ground R/C into the portion drawn
    directly in ``top_cell`` versus the portion drawn inside an instantiated
    sub-block, reported as the additive ``resistance_ohm_top_cell``/
    ``capacitance_ff_top_cell`` ground-entry fields below. ``None``/``False``
    (the default) computes neither field and runs no extra ``Region`` work
    at all -- byte-identical to this function's pre-#1704 behaviour.

    Built once per curated layer (poly, diffusion's constituent SD indices,
    each ``metals[i]``), **not** once per net or per instance -- via
    :func:`_instance_drawn_mask`, the merged union of that role's *drawn*
    layer (``deck.poly``/``deck.active``/``deck.metals[i]``, **not** the
    ``layer_index``/``metal_index`` values this function otherwise reads --
    those are ``LayoutToNetlist.register()`` handles, an unrelated integer
    space) across every instance beneath ``top_cell``
    (``top_cell.begin_shapes_rec(layer, min_depth=1)``). Each net's
    already-computed ground ``Region`` for that role (before any coupling
    deduction -- this stays entirely inside Pass 1, no new state enters Pass
    2's coupling bookkeeping) is then split as ``net_region -
    instance_drawn``: the **subtraction** form, not an intersection with an
    "own-drawn" mask, so any top-cell/instance overlap (e.g. a top-level
    strap routed over a std cell's own pin, the normal case rather than a
    corner case) is charged to the **instance** side -- the only form that
    keeps "subtract what I already extracted for this sub-block separately"
    arithmetically sound (spike doc section 3.3). The top-cell portion is
    fed through the exact same ``cap_area_ff_um2``/``cap_perim_ff_um``/
    ``sheet_res_ohm_sq`` + ``_n_squares`` formulas Pass 1 already applies to
    the raw (pre-coupling-deduction) totals.

    **Known limitation, documented rather than fixed: the split is exact in
    area but not in perimeter.** A conductor that genuinely straddles the
    instance/top-cell boundary is geometrically cut by the subtraction, and
    cutting a shape adds ``2 x (cut-line length)`` to the two pieces'
    combined perimeter relative to the uncut shape. Both the fringe-C term
    and the ``_n_squares`` sheet-R approximation are perimeter-sensitive, so
    for a net whose conductor actually crosses an instance boundary,
    ``resistance_ohm_top_cell`` plus the complementary instance-side R (and
    likewise C) comes out **slightly larger** than this net's own
    ``resistance_ohm``/``capacitance_ff`` total -- proportional to the
    number and length of such crossings. This is expected, not a bug: a net
    lying wholly on one side of the boundary (fully top-cell-drawn, or fully
    inside one instance) stays exactly additive; only a genuinely spanning
    net's split needs a tolerance rather than exact equality.

    For each metal level ``i`` with a curated
    ``parasitics_deck.metal_sidewalls[i]`` coefficient and
    ``parasitics_deck.metal_sidewall_lookback_um[i]`` lookback distance, and
    each qualifying pair of *distinct* nets with geometry on that level,
    ``net_a``'s region and ``net_b``'s region are checked with KLayout's own
    ``Region.separation_check`` (the same primitive ``drc.py`` already uses
    for ``check="separation"`` DRC rules) at that lookback distance; the
    summed length of the facing edge pairs it reports
    (``EdgePairs.first_edges().length()``) becomes a coupling capacitance
    between the two nets (``facing_length_um * metal_sidewalls[i]``). Unlike
    the vertical pass, **this charge is not deducted from either net's
    substrate fringe term** -- a known simplification (magic's own
    fringe-shielding model needs its ``defaultsidewall`` second parameter's
    semantics resolved first, an explicitly open question -- see
    ``metal_sidewall_lookback_um``'s docstring), flagged here rather than
    silently assumed away. Same-net and same-*name* pairs are skipped for
    the identical reasons the vertical pass skips them (see above).

    Returns a 2-tuple:

    - Ground list (sorted by ``(net name, net_id)`` for deterministic
      output): ``{"net", "net_id", "resistance_ohm", "capacitance_ff",
      "by_layer", "resistance_ohm_top_cell", "capacitance_ff_top_cell"}`` for
      every net with non-zero *raw* (pre-coupling-deduction)
      ground-eligible geometry --
      including a net whose ground capacitance was fully moved to coupling
      by the correction above (``capacitance_ff`` can be ``0.0`` in that
      case; the net still needs a star/hub so a coupling ``C`` card has
      somewhere to attach). A net with no eligible interconnect geometry at
      all is omitted, exactly as before this feature existed. ``by_layer``
      (issue #1701) is a per-role/per-metal-level breakdown -- see the "Pass
      3" comment above `results.append` for the exact shape and the
      sum-equals-total invariant it satisfies. ``resistance_ohm_top_cell``/
      ``capacitance_ff_top_cell`` (issue #1704) are ``None`` unless
      ``parasitics_top_cell_only`` was given -- see this docstring's
      "Top-cell-only hierarchy split" paragraph above.
    - Coupled-pair list (sorted by ``(net_a, net_b)`` for deterministic
      output): ``{"net_a", "net_b", "capacitance_ff", "levels",
      "lateral_levels"}`` for every distinct net pair with non-zero
      vertical-overlap and/or (critical-net-scoped) lateral coupling --
      **one entry per pair**, even when both kinds contribute, so
      :func:`_inject_parasitics`'s "one two-terminal `C` card per pair"
      invariant holds regardless of how many geometry passes fed it.
      ``capacitance_ff`` is the pair's total coupling capacitance (vertical
      plus lateral, summed across every contributing level/level-pair);
      ``levels`` is the sorted list of ``[lower_metal_index,
      upper_metal_index]`` adjacent-level pairs that contributed vertical
      coupling (issue #760, empty if none); ``lateral_levels`` (issue #976)
      is the sorted list of same-``metal_index`` levels that contributed
      lateral coupling (empty if none, and always empty when
      ``critical_nets`` is not given -- byte-identical to this field's
      pre-#976 absence). Empty when neither ``parasitics_deck.metal_overlaps``
      nor (``critical_nets``-scoped) ``parasitics_deck.metal_sidewalls``
      curate a coefficient, or the layout has no qualifying coupling
      geometry between distinct nets.

    ``net_id`` is ``net.cluster_id`` -- the id ``LayoutToNetlist`` already
    uses internally to key a net to its layout cluster (see the
    ``cluster_id == 0`` sentinel handling below), unique across every net
    *object* in ``circuit``, unlike ``net.expanded_name()`` (issue #765: two
    genuinely distinct nets, e.g. separate un-strapped ``VGND`` islands, can
    carry the identical layout label). Callers that only care about the
    schematic-equivalent name can keep using ``net``; a caller that needs to
    distinguish same-named islands -- or :func:`_inject_parasitics`, which
    must resolve each entry back to the exact net object this function
    measured -- keys on ``net_id`` instead.

    **Pairs are keyed by SPICE-safe net *name*, not by net object**, matching
    the node the coupling ``C`` card actually lands on:
    :func:`_inject_parasitics` hangs each pair between two **per-name** hubs
    (its ``hub_by_net`` map is keyed on ``entry["net"]``), so when several
    genuinely distinct net objects share one layout label -- the ``gcd``
    corpus block has 105 separate, un-strapped ``VGND`` rail islands and 88
    ``VPWR`` ones -- every pair naming that label resolves to a single
    (last-registered) island's hub. Two consequences, both deliberate:
    overlap between two same-named nets is **skipped entirely** (its charge
    stays on ground) because both of its terminals would resolve to that one
    same hub, making the "coupling capacitor" a self-loop that would inflate
    the reported total while contributing nothing electrically; and overlap
    between two *different* names accumulates into one pair however many net
    objects on each side contributed, matching the one hub per name that the
    card attaches to.

    That name-keying is *this* pass's own aggregation choice, not something
    forced on it from downstream. Since issue #765 the ground entries above
    resolve by ``net_id``, and KLayout's ``NetlistSpiceWriter`` renames
    duplicates when it writes them (two nets both labelled ``VGND`` are
    written as the distinct nodes ``VGND`` and ``VGND$1``) -- so neither the
    injection step nor the emitted netlist collapses same-labelled islands
    into one node. Coupling is therefore modelled one level coarser than the
    per-net ground terms; a per-net-object coupling model is a named
    follow-on, not a behaviour this function claims today.
    """
    if circuit is None:
        return [], []

    import klayout.db as kdb

    # (LayerRC, [include indices], [subtract indices]) for every non-metal
    # role that has both a coefficient set and at least one present layer.
    # The subtract list is empty except for the poly role, which removes the
    # transistor gate regions (see below). Metal roles are handled separately
    # below since they participate in the vertical-overlap coupling pass.
    #
    # Every declared MOS flavour's own SD/gate registrations (issue #1111,
    # `layer_index[f"nfet_sd_{flavour}"]` etc. -- see `_extract_netlist`'s own
    # registration loop) are folded in alongside the default `nfet_sd`/
    # `pfet_sd`/`nfet_gate`/`pfet_gate` indices below, so a flavoured
    # transistor's own diffusion/gate geometry is measured (or excluded from
    # the poly role) exactly like an unflavoured one -- neither role would
    # otherwise "see" it at all, silently under-measuring a net that happens
    # to carry a flavoured device.
    flavour_sd_indices: list[int] = []
    flavour_gate_indices: list[int] = []
    for flavour in deck.mos_flavours:
        flavour_sd_indices.append(layer_index[f"nfet_sd_{flavour.flavour}"])
        flavour_sd_indices.append(layer_index[f"pfet_sd_{flavour.flavour}"])
        flavour_gate_indices.append(layer_index[f"nfet_gate_{flavour.flavour}"])
        flavour_gate_indices.append(layer_index[f"pfet_gate_{flavour.flavour}"])

    # Each tuple's leading `str` is the role name reported in a net's
    # `by_layer[]` breakdown (issue #1701) -- `"diffusion"`/`"poly"`, matching
    # the `ParasiticsDeck` field name the coefficient came from.
    non_metal_roles: list[tuple[str, Any, list[int], list[int]]] = []
    if parasitics_deck.diffusion is not None:
        non_metal_roles.append(
            (
                "diffusion",
                parasitics_deck.diffusion,
                [layer_index["nfet_sd"], layer_index["pfet_sd"], *flavour_sd_indices],
                [],
            )
        )
    if parasitics_deck.poly is not None:
        # Exclude the transistor gate regions from the poly role (issue #226):
        # gate poly sits over the channel (not the substrate these coefficients
        # describe) and its capacitance is already in the device model, so the
        # nfet/pfet gate shapes are subtracted from the net's poly shapes before
        # measuring. These registrations back the device connectivity too and
        # are left untouched -- only the parasitic measurement subtracts them.
        non_metal_roles.append(
            (
                "poly",
                parasitics_deck.poly,
                [layer_index["poly"]],
                [
                    layer_index["nfet_gate"],
                    layer_index["pfet_gate"],
                    *flavour_gate_indices,
                ],
            )
        )

    nets: list[kdb.Net] = []
    for net in circuit.each_net():
        name = net.expanded_name()
        # Every per-isolated-region substrate identity (issue #1128, named
        # `f"{substrate_net}_iso{n}"` -- see `ExtractionDeck.
        # substrate_isolation`'s docstring) is skipped here the same way the
        # deck-wide `substrate_net` global is: each is itself a synthesized
        # local AC-ground reference for its own isolated region, not an
        # ordinary signal net whose own ground capacitance should be
        # measured. No-op when the deck declares no `substrate_isolation`
        # (no net is ever named this way).
        if name == deck.substrate_net or name.startswith(f"{deck.substrate_net}_iso"):
            continue
        if net.cluster_id == 0:
            # Belt-and-braces (issue #563): `cluster_id` is the key
            # `LayoutToNetlist` uses to find a net's shapes, and `0` is the
            # sentinel for "not tied to a layout cluster". Passing such a net
            # to `polygons_of_net` faults inside KLayout's hierarchical
            # network processor with an unhandled internal `RuntimeError`
            # rather than returning an empty region.
            # `_purge_preserving_named_nets` restores the real cluster id on
            # every net it rescues, so no net reaching here from
            # `_extract_netlist` should hit this branch -- but a net with no
            # cluster has no geometry to measure by definition, so skipping
            # it is the correct (and crash-free) answer for any future caller
            # that hands us a synthesised net.
            continue
        if parasitics_nets is not None and (
            spice_safe_net_name(_net_identity_name(net)) not in parasitics_nets
        ):
            # `--parasitics-net` scoping (issue #1700): this net was not
            # named, so none of Pass 1's per-net geometry queries run for it
            # -- which is the entire cost saving, since `polygons_of_net` is
            # called once per net per conductor role. Membership re-escapes
            # the identity spelling for exactly the reason the lateral pass'
            # `critical_nets` check does (issue #1162): the caller names nets
            # using the escaped spelling this module reports everywhere else.
            continue
        nets.append(net)

    # Pass 1: non-metal ground R/C (unaffected by coupling) plus, per net,
    # each metal level's raw region/area/perimeter -- cached for the
    # coupling pass below and for the final (possibly-reduced) ground C.
    base_c_ff: dict[kdb.Net, float] = {}
    base_r_ohm: dict[kdb.Net, float] = {}
    num_metals = len(parasitics_deck.metals)
    # metal_regions[i]: {net: Region} for every net with non-empty geometry
    # on metal level i -- only populated for levels with a curated LayerRC
    # (a level with none contributes no ground R/C either, matching the
    # pre-existing `metals_without_coefficient` gap semantics).
    metal_regions: list[dict[kdb.Net, kdb.Region]] = [dict() for _ in range(num_metals)]
    net_metal_area_um2: dict[tuple[kdb.Net, int], float] = {}
    net_metal_perim_um: dict[tuple[kdb.Net, int], float] = {}
    # Per-(net, role-name)/(net, metal-index) R/C, retained past the
    # summation into `base_c_ff`/`base_r_ohm` purely to populate each net's
    # `by_layer[]` breakdown below (issue #1701) -- these are the exact
    # per-role/per-level terms already folded into the scalar totals, not a
    # second computation. Metal capacitance is *not* cached here: it still
    # needs the coupling deduction (Pass 2/3 below) applied first, so its
    # `by_layer` counterpart is captured where that deduction is known.
    net_role_r_ohm: dict[tuple[kdb.Net, str], float] = {}
    net_role_c_ff: dict[tuple[kdb.Net, str], float] = {}
    net_metal_r_ohm: dict[tuple[kdb.Net, int], float] = {}

    # `--parasitics-top-cell-only` (issue #1704): one `_instance_drawn_mask`
    # per curated layer, built once here -- independent of net count *and*
    # instance count -- rather than inside the per-net loop below. `deck`
    # (not `parasitics_deck`) is the source of each role's *drawn*
    # (layer, datatype) pair; `layer_index`/`metal_index` are deliberately
    # never consulted here (see `_instance_drawn_mask`'s docstring). Both
    # dicts/lists stay empty when the flag is off, so every lookup below is a
    # no-op `dict.get(..., None)`/`None` and no extra `Region` work runs at
    # all -- byte-identical to this function's pre-#1704 behaviour.
    non_metal_masks: dict[str, kdb.Region] = {}
    metal_masks: list[kdb.Region | None] = [None] * num_metals
    top_cell_c_ff: dict[kdb.Net, float] = {}
    top_cell_r_ohm: dict[kdb.Net, float] = {}
    if parasitics_top_cell_only:
        assert layout is not None and top_cell is not None, (
            "parasitics_top_cell_only requires layout/top_cell"
        )
        role_drawn_layer = {"diffusion": deck.active, "poly": deck.poly}
        for role_name, _layer_rc, _indices, _subtract in non_metal_roles:
            non_metal_masks[role_name] = _instance_drawn_mask(
                layout, top_cell, role_drawn_layer.get(role_name)
            )
        for i in range(num_metals):
            if parasitics_deck.metals[i] is None:
                continue
            drawn_layer = deck.metals[i] if i < len(deck.metals) else None
            metal_masks[i] = _instance_drawn_mask(layout, top_cell, drawn_layer)

    for net in nets:
        r_ohm = 0.0
        c_ff = 0.0
        top_r_ohm = 0.0
        top_c_ff = 0.0
        for role_name, layer_rc, indices, subtract in non_metal_roles:
            area_um2, perim_um, top_area_um2, top_perim_um = _net_area_perim_um(
                l2n,
                net,
                dbu,
                indices,
                subtract,
                instance_mask=(
                    non_metal_masks.get(role_name) if parasitics_top_cell_only else None
                ),
            )
            if area_um2 <= 0.0:
                continue
            role_c_ff = (
                area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            role_r_ohm = layer_rc.sheet_res_ohm_sq * _n_squares(area_um2, perim_um)
            c_ff += role_c_ff
            r_ohm += role_r_ohm
            net_role_c_ff[(net, role_name)] = role_c_ff
            net_role_r_ohm[(net, role_name)] = role_r_ohm
            if parasitics_top_cell_only and top_area_um2 > 0.0:
                top_c_ff += (
                    top_area_um2 * layer_rc.cap_area_ff_um2
                    + top_perim_um * layer_rc.cap_perim_ff_um
                )
                top_r_ohm += layer_rc.sheet_res_ohm_sq * _n_squares(
                    top_area_um2, top_perim_um
                )
        base_c_ff[net] = c_ff
        base_r_ohm[net] = r_ohm

        for i in range(num_metals):
            layer_rc = parasitics_deck.metals[i]
            if layer_rc is None or i >= len(metal_index):
                continue
            region = l2n.polygons_of_net(net, metal_index[i])
            if region.is_empty():
                continue
            area_um2 = region.area() * dbu * dbu
            perim_um = region.perimeter() * dbu
            if area_um2 <= 0.0:
                continue
            net_metal_area_um2[(net, i)] = area_um2
            net_metal_perim_um[(net, i)] = perim_um
            metal_r_ohm = layer_rc.sheet_res_ohm_sq * _n_squares(area_um2, perim_um)
            net_metal_r_ohm[(net, i)] = metal_r_ohm
            base_r_ohm[net] += metal_r_ohm
            metal_regions[i][net] = region
            # `--parasitics-top-cell-only` (issue #1704): the metal role's
            # top-cell share, computed from this net's raw (pre-coupling-
            # deduction) metal region -- deliberately outside Pass 2/3's
            # coupling bookkeeping, see this function's own docstring.
            if parasitics_top_cell_only and metal_masks[i] is not None:
                top_cell_part = region - metal_masks[i]
                top_area_um2 = top_cell_part.area() * dbu * dbu
                top_perim_um = top_cell_part.perimeter() * dbu
                if top_area_um2 > 0.0:
                    top_c_ff += (
                        top_area_um2 * layer_rc.cap_area_ff_um2
                        + top_perim_um * layer_rc.cap_perim_ff_um
                    )
                    top_r_ohm += layer_rc.sheet_res_ohm_sq * _n_squares(
                        top_area_um2, top_perim_um
                    )

        if parasitics_top_cell_only:
            top_cell_c_ff[net] = top_c_ff
            top_cell_r_ohm[net] = top_r_ohm

    # Pass 2: vertical-overlap coupling between adjacent metal levels.
    # `deduction_regions[(net, i)]` accumulates the *geometry* of `net`'s
    # level-i area that has been attributed to a coupling partner instead of
    # ground, across every adjacent-level pair that touches level `i`.
    #
    # This is a Region union, not a scalar area sum, and that distinction is
    # load-bearing (PR #764 review): level `i` participates in two adjacent
    # pairs -- as the upper plate of `(i-1, i)` and as the lower plate of
    # `(i, i+1)` -- so a straight via stack (a net with routing above *and*
    # below the same XY footprint, pervasive in any routed block) has the same
    # physical area claimed by two different partners. Summing the two overlap
    # areas would deduct that area twice and silently destroy charge; unioning
    # the overlap Regions and measuring once guarantees each square micron is
    # removed from the ground term at most once, however many partners claim
    # it. The coupling capacitors themselves are unaffected: each *pair* still
    # gets the full mutual-overlap area, which is what the plate model wants.
    deduction_regions: dict[tuple[kdb.Net, int], kdb.Region] = {}
    # `coupling_ff[pair_key]` / `coupling_levels[pair_key]`: the accumulated
    # coupling capacitance and contributing `[lower, upper]` level-index
    # pairs for one unordered net pair.
    coupling_ff: dict[tuple[str, str], float] = {}
    coupling_levels: dict[tuple[str, str], set[tuple[int, int]]] = {}

    for i in range(num_metals - 1):
        coef = (
            parasitics_deck.metal_overlaps[i]
            if i < len(parasitics_deck.metal_overlaps)
            else None
        )
        if coef is None:
            continue
        lower_nets = metal_regions[i]
        upper_nets = metal_regions[i + 1]
        if not lower_nets or not upper_nets:
            continue
        upper_items = [
            (net_b, _net_identity_name(net_b), region_b)
            for net_b, region_b in upper_nets.items()
        ]
        for net_a, region_a in lower_nets.items():
            bbox_a = region_a.bbox()
            name_a = _net_identity_name(net_a)
            for net_b, name_b, region_b in upper_items:
                if net_a is net_b:
                    # Same-net overlap (a via stack) is excluded by
                    # construction: coupling is only ever between two
                    # *distinct* nets (issue #760's explicit edge case).
                    continue
                if name_a == name_b:
                    # Two *distinct* nets that carry the same layout label
                    # (e.g. `gcd`'s 105 separate, un-strapped `VGND` rail
                    # islands). Pairs here are keyed by SPICE-safe *name*
                    # (`_net_pair_key`), and `_inject_parasitics` attaches
                    # each pair's `C` card between two per-*name* hubs (its
                    # `hub_by_net` map), so both terminals of a same-name
                    # pair would land on one and the same hub net -- a
                    # self-loop, contributing nothing electrically while
                    # inflating `total_coupling_capacitance_ff`. (That is a
                    # property of this name-keyed pair/hub model, not of the
                    # netlist: since issue #765 the *ground* entries resolve
                    # by `net_id`, and KLayout's SPICE writer renames a
                    # duplicate net on write -- `VGND` and `VGND$1` -- so the
                    # islands are not collapsed into one node downstream.)
                    # Skipped *before* the
                    # deduction below, so that area simply stays on the
                    # ground term, exactly as it did pre-#760; charge is
                    # still conserved.
                    continue
                if not _bbox_overlap(bbox_a, region_b.bbox()):
                    continue
                overlap = region_a & region_b
                if overlap.is_empty():
                    continue
                overlap_area_um2 = overlap.area() * dbu * dbu
                if overlap_area_um2 <= 0.0:
                    continue

                for ded_key in ((net_a, i), (net_b, i + 1)):
                    acc = deduction_regions.get(ded_key)
                    if acc is None:
                        acc = kdb.Region()
                        deduction_regions[ded_key] = acc
                    # `insert` appends the overlap polygons unmerged; the
                    # single `merged()` below collapses each accumulator once,
                    # which is both cheaper and exactly the set union wanted.
                    acc.insert(overlap)

                key = _net_pair_key(name_a, name_b)
                coupling_ff[key] = coupling_ff.get(key, 0.0) + overlap_area_um2 * coef
                coupling_levels.setdefault(key, set()).add((i, i + 1))

    # Pass 2b: lateral (same-layer sidewall) coupling, for a caller-declared
    # "critical nets" set only (issue #976, Epic #709 Phase 2a) -- see this
    # function's docstring for why this pass is scoped down rather than
    # running unconditionally like the vertical pass above.
    lateral_coupling_ff: dict[tuple[str, str], float] = {}
    lateral_coupling_levels: dict[tuple[str, str], set[int]] = {}

    if critical_nets:
        for i in range(num_metals):
            coef = (
                parasitics_deck.metal_sidewalls[i]
                if i < len(parasitics_deck.metal_sidewalls)
                else None
            )
            lookback_um = (
                parasitics_deck.metal_sidewall_lookback_um[i]
                if i < len(parasitics_deck.metal_sidewall_lookback_um)
                else None
            )
            if coef is None or not lookback_um or lookback_um <= 0.0:
                continue
            level_nets = metal_regions[i]
            if len(level_nets) < 2:
                continue
            lookback_dbu = max(1, round(lookback_um / dbu))
            items = [
                (net, _net_identity_name(net), region)
                for net, region in level_nets.items()
            ]
            for a_index, (net_a, name_a, region_a) in enumerate(items):
                # `critical_nets` is caller-supplied (`--critical-net`), so it
                # names nets using the same escaped spelling this module
                # reports everywhere else (issue #1162) -- `name_a`/`name_b`
                # themselves stay in the unescaped identity namespace (see
                # `_net_identity_name`'s docstring), so the membership check
                # re-escapes just for this comparison.
                a_is_critical = spice_safe_net_name(name_a) in critical_nets
                halo_bbox = region_a.bbox().enlarged(
                    kdb.Point(lookback_dbu, lookback_dbu)
                )
                for net_b, name_b, region_b in items[a_index + 1 :]:
                    if not (
                        a_is_critical or spice_safe_net_name(name_b) in critical_nets
                    ):
                        continue
                    if net_a is net_b:
                        # Same edge case the vertical pass excludes (a via
                        # stack tying one net's own geometry together is
                        # ordinary connectivity, not coupling).
                        continue
                    if name_a == name_b:
                        # Same collision-name edge case the vertical pass
                        # skips -- see its comment above.
                        continue
                    if not halo_bbox.overlaps(region_b.bbox()):
                        continue
                    edge_pairs = region_a.separation_check(region_b, lookback_dbu)
                    if edge_pairs.is_empty():
                        continue
                    facing_length_um = edge_pairs.first_edges().length() * dbu
                    if facing_length_um <= 0.0:
                        continue
                    key = _net_pair_key(name_a, name_b)
                    lateral_coupling_ff[key] = (
                        lateral_coupling_ff.get(key, 0.0) + facing_length_um * coef
                    )
                    lateral_coupling_levels.setdefault(key, set()).add(i)

    # Collapse each (net, level) accumulator to a single non-overlapping area,
    # once, now that every contributing partner has been folded in.
    deduction_um2: dict[tuple[kdb.Net, int], float] = {
        ded_key: region.merged().area() * dbu * dbu
        for ded_key, region in deduction_regions.items()
    }

    # Pass 3: finalize each net's ground C, applying the coupling deduction
    # (if any) to its metal-level area terms only -- perimeter/fringe and
    # every non-metal role are untouched by coupling.
    #
    # `net_metal_c_ff[(net, i)]` caches each metal level's *effective*
    # (post-coupling-deduction) capacitance contribution -- exactly the term
    # folded into `c_ff` below -- purely so the `by_layer[]` breakdown built
    # a few lines down can report the same number the net's scalar total was
    # actually built from (issue #1701), rather than re-deriving it from the
    # raw (pre-deduction) area a second time.
    net_metal_c_ff: dict[tuple[kdb.Net, int], float] = {}
    results: list[dict[str, Any]] = []
    for net in nets:
        raw_c_ff = base_c_ff.get(net, 0.0)
        c_ff = raw_c_ff
        for i in range(num_metals):
            layer_rc = parasitics_deck.metals[i]
            if layer_rc is None:
                continue
            area_um2 = net_metal_area_um2.get((net, i))
            if area_um2 is None:
                continue
            perim_um = net_metal_perim_um[(net, i)]
            raw_c_ff += (
                area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            deduction = deduction_um2.get((net, i), 0.0)
            effective_area_um2 = max(0.0, area_um2 - deduction)
            metal_c_ff = (
                effective_area_um2 * layer_rc.cap_area_ff_um2
                + perim_um * layer_rc.cap_perim_ff_um
            )
            c_ff += metal_c_ff
            net_metal_c_ff[(net, i)] = metal_c_ff
        if raw_c_ff <= 0.0:
            # `raw_c_ff` is exactly the pre-#760 ground capacitance (every
            # role's full area *and* perimeter term, no coupling deduction),
            # so this net-inclusion test is bit-for-bit the one this function
            # applied before coupling existed: which nets appear in the
            # output cannot change, only how much of each net's charge sits
            # on the ground term vs. a coupling term.
            #
            # No ground-eligible geometry at all (before any coupling
            # deduction) means no load to hang a series R off of -- a bare
            # series R to nothing is meaningless, so skip the net. A net
            # whose geometry exists but was *entirely* claimed by coupling
            # still reaches here (raw_c_ff > 0), so it still gets a
            # star/hub for a coupling `C` card to attach to, even though its
            # own reported `capacitance_ff` can be `0.0`.
            continue

        # `by_layer[]` (issue #1701): exposes the per-role/per-metal-level
        # R/C terms already summed into `resistance_ohm`/`capacitance_ff`
        # above, so a caller composing a hierarchical netlist from
        # separately-extracted sub-blocks can attribute a net's R/C to a
        # layer instead of hand-subtracting scalar totals. One entry per
        # role/level with non-zero geometry on this net -- non-metal roles
        # (``"diffusion"``/``"poly"``, in `non_metal_roles`' declared order)
        # first, then each metal level (``"metal<i>"``, 0-based, matching
        # `deck.metals`' own indexing) in ascending order. A role/level with
        # no geometry on this net is omitted entirely, exactly like the
        # per-net inclusion test above -- so a net entirely on one layer
        # gets a single-entry list. Metal capacitance is the *effective*
        # (post-coupling-deduction) term -- the one actually folded into
        # `capacitance_ff` -- not the raw pre-deduction area, so summing
        # `by_layer[].resistance_ohm`/`capacitance_ff` across a net's
        # entries reproduces that net's `resistance_ohm`/`capacitance_ff`
        # totals exactly (modulo ordinary floating-point rounding).
        #
        # Not adjusted for a subsequent `--mom-net`/`--mom-rlc-net`
        # substitution: those overwrite this entry's `resistance_ohm`/
        # `capacitance_ff` in place, downstream of this function, with a
        # value this lumped-RC model did not compute (a `klt mom` field
        # solve, or an opaque caller-supplied override) -- `by_layer` still
        # reflects the lumped-RC decomposition that would have produced the
        # pre-substitution total. See `docs/cli/extract.md`'s `--mom-net`/
        # `--mom-rlc-net` sections.
        by_layer: list[dict[str, Any]] = []
        for role_name, _layer_rc, _indices, _subtract in non_metal_roles:
            role_key = (net, role_name)
            if role_key not in net_role_c_ff and role_key not in net_role_r_ohm:
                continue
            by_layer.append(
                {
                    "layer": role_name,
                    "resistance_ohm": round(net_role_r_ohm.get(role_key, 0.0), 4),
                    "capacitance_ff": round(net_role_c_ff.get(role_key, 0.0), 6),
                }
            )
        for i in range(num_metals):
            metal_key = (net, i)
            if metal_key not in net_metal_area_um2:
                continue
            by_layer.append(
                {
                    "layer": f"metal{i}",
                    "resistance_ohm": round(net_metal_r_ohm.get(metal_key, 0.0), 4),
                    "capacitance_ff": round(net_metal_c_ff.get(metal_key, 0.0), 6),
                }
            )

        results.append(
            {
                # Unescaped identity spelling (issue #1162) -- this feeds
                # `_inject_parasitics`'s real net/instance-name construction
                # below; the JSON `parasitics.nets[].net` value is derived
                # from it via `spice_safe_net_name` at the point it enters
                # the response, not baked in here.
                "net": _net_identity_name(net),
                "net_id": net.cluster_id,
                "resistance_ohm": round(base_r_ohm.get(net, 0.0), 4),
                "capacitance_ff": round(max(0.0, c_ff), 6),
                "by_layer": by_layer,
                # Additive fields (issue #1704): the top-cell-drawn share of
                # this net's ground R/C -- see this function's own
                # "Top-cell-only hierarchy split" docstring paragraph.
                # `None` unless `parasitics_top_cell_only` was given (the
                # accumulator dicts stay empty otherwise, so `.get()` misses
                # rather than reporting a computed `0.0`).
                "resistance_ohm_top_cell": (
                    round(top_cell_r_ohm[net], 4) if net in top_cell_r_ohm else None
                ),
                "capacitance_ff_top_cell": (
                    round(max(0.0, top_cell_c_ff[net]), 6)
                    if net in top_cell_c_ff
                    else None
                ),
            }
        )

    results.sort(key=lambda entry: (entry["net"], entry["net_id"]))

    # One entry per pair, vertical and lateral contributions merged (issue
    # #976) -- `_inject_parasitics`'s "one two-terminal `C` card per pair"
    # invariant (its own docstring) must hold regardless of how many
    # geometry passes fed a given pair, so a pair present in both
    # `coupling_ff` (vertical) and `lateral_coupling_ff` (lateral) gets one
    # combined entry, not two entries that would mint two devices under the
    # same instance name.
    coupled_pairs: list[dict[str, Any]] = []
    for key in set(coupling_ff) | set(lateral_coupling_ff):
        name_a, name_b = key
        total_ff = coupling_ff.get(key, 0.0) + lateral_coupling_ff.get(key, 0.0)
        if total_ff <= 0.0:
            continue
        levels_sorted = sorted(coupling_levels.get(key, ()))
        lateral_levels_sorted = sorted(lateral_coupling_levels.get(key, ()))
        coupled_pairs.append(
            {
                "net_a": name_a,
                "net_b": name_b,
                "capacitance_ff": round(total_ff, 6),
                "levels": [[lo, hi] for lo, hi in levels_sorted],
                "lateral_levels": list(lateral_levels_sorted),
            }
        )
    coupled_pairs.sort(key=lambda entry: (entry["net_a"], entry["net_b"]))

    return results, coupled_pairs


def _detect_dead_metal(
    l2n: kdb.LayoutToNetlist,
    circuit: kdb.Circuit | None,
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    dbu: float,
    routing_layers: list[tuple[str, tuple[int, int], kdb.Region, int]],
) -> list[dict[str, Any]]:
    """Report every routing-stack cluster that joins no extracted net (issue
    #676) -- "dead metal".

    ``routing_layers`` is one ``(role, (layer, datatype), region, index)``
    tuple per registered ``metals``/``vias`` level: ``region`` is the exact
    :class:`kdb.Region` handed to ``LayoutToNetlist.register`` (so black-box
    masking and the MiM top-via exclusion are already applied to it) and
    ``index`` is that call's returned layer index. For each level, the union
    of ``polygons_of_net(net, index)`` over every surviving net is what the
    extraction *did* account for; whatever is left after subtracting it is
    geometry no ``nets[]`` entry mentions.

    **Electrical contact, not projection.** The netted union comes from the
    extracted connectivity graph, which joins two metal levels only through a
    declared via layer -- so a wire passing under or over another wire with no
    via between them stays two nets, and an isolated shape stays dead however
    much it overlaps a live one in XY. That is the distinction a naive
    ``Region.interacting()`` check across adjacent layers gets wrong.

    **Why the surviving netlist is the yardstick.** KLayout gives *every*
    connected cluster a net at ``extract_netlist()`` time, floating ones
    included, so "has a net" is trivially true before the purge and this
    function must run after it. A floating cluster that reaches no device is
    exactly what ``Netlist.purge()`` drops, and a caller reading ``nets[]``
    can no longer find its geometry anywhere -- which is the reported
    complaint. A *labelled* floating cluster (bond pad, seal ring, power
    strap) is rescued by :func:`_purge_preserving_named_nets` with its
    ``cluster_id`` intact, so it stays netted and is deliberately **not**
    reported: a named net is findable, whatever it does or does not touch.

    Returns one entry per connected dead cluster (not per drawn polygon):
    ``{"role", "layer", "datatype", "bbox_um", "shapes", "area_um2"}``, sorted
    by ``(layer, datatype, left, bottom)``. ``role`` is ``metal<i>``/``via<i>``
    with ``<i>`` indexing the deck's own ``metals``/``vias`` tuple (``0`` is
    the bottom-most level), matching the names ``_extract_netlist`` registers
    those layers under. ``shapes`` counts the *drawn* shapes on that stream
    layer interacting with the cluster, so a human knows how much geometry to
    go look at; a cluster that is only a fragment of a drawn shape (the part
    of it left outside a black-box region, say) still counts that whole shape.
    """
    import klayout.db as kdb

    # Deferred (call-time) import, not a module-level one: `extract.py`
    # imports this module at its own module scope (mirroring the
    # `extract_spef.py`/`extract_abstract.py` precedent), so a module-level
    # `from .extract import ...` here would be a circular import evaluated
    # during that very load. `_bbox_um_rounded`/`_PARAM_PRECISION_UM` stay in
    # `extract.py` (both have other callers there) -- this defers the
    # back-reference until `_detect_dead_metal` is actually called, by which
    # point `extract.py` has finished importing.
    from .extract import _PARAM_PRECISION_UM, _bbox_um_rounded

    nets = (
        [net for net in circuit.each_net() if net.cluster_id != 0]
        if circuit is not None
        else []
    )

    dead_metal: list[dict[str, Any]] = []
    for role, (layer, datatype), region, index in routing_layers:
        if region.is_empty():
            continue
        netted = kdb.Region()
        for net in nets:
            netted += l2n.polygons_of_net(net, index)
        dead = region - netted
        if dead.is_empty():
            continue
        drawn = _region(layout, top_cell, (layer, datatype))
        # Raw-polygon semantics: with KLayout's default merged semantics two
        # abutting drawn boxes count as one polygon, which would report
        # "1 shape" for a cluster a human has to go edit two shapes to fix.
        drawn.merged_semantics = False
        for component in dead.merged().each():
            cluster = kdb.Region(component)
            box = component.bbox()
            dead_metal.append(
                {
                    "role": role,
                    "layer": layer,
                    "datatype": datatype,
                    "bbox_um": _bbox_um_rounded(box, dbu),
                    "shapes": drawn.interacting(cluster).count(),
                    "area_um2": round(cluster.area() * dbu * dbu, _PARAM_PRECISION_UM),
                }
            )

    dead_metal.sort(
        key=lambda entry: (
            entry["layer"],
            entry["datatype"],
            entry["bbox_um"]["left"],
            entry["bbox_um"]["bottom"],
        )
    )
    return dead_metal


def _terminal_star_positions_um(terminal_refs: list[Any]) -> list[tuple[float, float]]:
    """The approximate ``(x_um, y_um)`` location of each of ``terminal_refs``,
    read from its owning device's ``Device.trans`` (already reported in real
    micrometres -- unlike most of this module, no ``dbu`` scaling is needed).

    This is a **coarse** proxy for a terminal's true physical location
    (issue #592): KLayout's connectivity extraction records one placement
    transform per *device* (typically the center of its recognition shape),
    not a distinct position per terminal, so every terminal on the same
    device instance shares that device's single location. Good enough to
    rank a net's terminals by their approximate spread for the star-topology
    resistance split; not a substitute for true per-segment routing
    measurement (out of scope -- issue #592's deferred Option 2)."""
    positions: list[tuple[float, float]] = []
    for ref in terminal_refs:
        disp = ref.device().trans.disp
        positions.append((disp.x, disp.y))
    return positions


def _terminal_star_weights(positions: list[tuple[float, float]]) -> list[float]:
    """Normalized (sum to ``1.0``) per-terminal resistance-split weights for
    the star topology: proportional to each position's Euclidean distance
    from the centroid of ``positions``, so a terminal placed farther from a
    net's other terminals is assigned more of that net's total resistance --
    a coarse, position-aware stand-in for "farther terminals see more
    interconnect" without a full per-segment routing model (issue #592).

    Degenerates to an equal ``1/N`` split when every position coincides
    (including ``N == 1``, where the lone terminal necessarily sits at the
    "centroid") -- this is what makes a single-terminal net's star reduce to
    exactly the pre-#592 Gamma-shunt's one lumped resistor: same total value,
    not a smaller or larger one."""
    n = len(positions)
    if n == 0:
        return []
    cx = sum(p[0] for p in positions) / n
    cy = sum(p[1] for p in positions) / n
    distances = [math.hypot(x - cx, y - cy) for x, y in positions]
    total = sum(distances)
    if total <= 0.0:
        return [1.0 / n] * n
    return [d / total for d in distances]


def _distributed_rc_order(positions: list[tuple[float, float]]) -> list[int]:
    """An index permutation of ``positions`` approximating a physical tap
    order along a net's dominant spread axis (``--distributed-rc``, issue
    #977, Epic #709 Phase 2b): a ladder needs a linear chain, not an
    unordered set, so terminals must be placed in *some* sequence before
    :func:`_distributed_rc_segments` can derive adjacent-pair segment R/C.

    Finds the pair of positions with the greatest pairwise Euclidean
    distance (the same coarse "how spread out is this net" signal
    :func:`_terminal_star_weights` already leans on), projects every
    position onto the axis between that pair, and sorts by the projection
    -- a deterministic, position-only proxy for "which end is which" that
    needs no routing/skeleton geometry (:func:`_terminal_star_positions_um`'s
    own docstring: terminal position is a device-placement proxy, not true
    routed geometry).

    Degenerates to the input order (``[0, 1, ..., n - 1]``) for ``n <= 2``
    (nothing to order between two points) and whenever every position
    coincides (the projection axis is undefined) -- harmless, since every
    segment length is then ``0.0`` too and :func:`_distributed_rc_segments`'s
    equal-split fallback takes over regardless of order.
    """
    n = len(positions)
    if n <= 2:
        return list(range(n))

    best_pair = (0, 1)
    best_dist = -1.0
    for i in range(n):
        xi, yi = positions[i]
        for j in range(i + 1, n):
            xj, yj = positions[j]
            dist = math.hypot(xj - xi, yj - yi)
            if dist > best_dist:
                best_dist = dist
                best_pair = (i, j)
    if best_dist <= 0.0:
        return list(range(n))

    ai, aj = best_pair
    ax, ay = positions[ai]
    bx, by = positions[aj]
    ux, uy = bx - ax, by - ay
    norm = math.hypot(ux, uy)
    ux, uy = ux / norm, uy / norm
    projections = [
        ((x - ax) * ux + (y - ay) * uy, idx) for idx, (x, y) in enumerate(positions)
    ]
    projections.sort(key=lambda item: (item[0], item[1]))
    return [idx for _, idx in projections]


def _distributed_rc_segments(
    positions: list[tuple[float, float]],
    r_total_ohm: float,
    c_total_ff: float,
) -> tuple[list[int], list[float], list[float]]:
    """Break one net's total lumped resistance/capacitance into a chain
    ("ladder") along ``positions``' approximate physical order, for
    ``--distributed-rc`` (issue #977, Epic #709 Phase 2b) -- the multi-
    segment alternative to :func:`_inject_parasitics`'s star topology.

    Returns ``(order, segment_r_ohm, node_c_ff)``, all keyed to
    ``positions``' own indices:

    - ``order``: :func:`_distributed_rc_order`'s index permutation (length
      ``N``, one entry per terminal) -- the ladder's node sequence.
    - ``segment_r_ohm``: ``N - 1`` per-segment series resistances, one
      between each adjacent pair in ``order``. Each segment's share of
      ``r_total_ohm`` is proportional to that pair's Euclidean distance
      (its approximate physical length), so the segments always sum back
      to exactly ``r_total_ohm`` -- the same "legs sum back to the net's
      total" invariant :func:`_terminal_star_weights` already guarantees
      for the star topology.
    - ``node_c_ff``: ``N`` per-terminal ground capacitances, in ``order``'s
      sequence. Each terminal's share of ``c_total_ff`` is the *average* of
      its one or two adjacent segments' length share (an interior node
      touches two segments, an end node touches one) -- the standard "half
      the capacitance of each adjoining segment" lumped-element
      discretization of a distributed RC line. Node capacitances also sum
      back to exactly ``c_total_ff``.

    Degenerates to an equal split (``r_total_ohm / (N - 1)`` per segment,
    ``c_total_ff / N`` per node) when every position coincides -- the same
    "no spatial signal available" fallback :func:`_terminal_star_weights`
    already uses, and still conserves both totals.

    ``positions`` must have at least 2 entries -- a distributed ladder needs
    at least one segment; a net with 0 or 1 terminal has nothing to chain
    and stays on the star/Gamma-shunt path in :func:`_inject_parasitics`.
    """
    order = _distributed_rc_order(positions)
    n = len(order)
    ordered = [positions[i] for i in order]
    seg_lengths = [
        math.hypot(ordered[i + 1][0] - ordered[i][0], ordered[i + 1][1] - ordered[i][1])
        for i in range(n - 1)
    ]
    total_length = sum(seg_lengths)

    if total_length <= 0.0:
        segment_r_ohm = [r_total_ohm / (n - 1)] * (n - 1)
        node_c_ff = [c_total_ff / n] * n
        return order, segment_r_ohm, node_c_ff

    segment_r_ohm = [r_total_ohm * (length / total_length) for length in seg_lengths]
    node_c_ff = []
    for i in range(n):
        adjacent_lengths = seg_lengths[max(i - 1, 0) : min(i + 1, n - 1)]
        share = sum(adjacent_lengths) / (2.0 * total_length)
        node_c_ff.append(c_total_ff * share)
    return order, segment_r_ohm, node_c_ff


def _inject_parasitics(
    kdb: Any,
    circuit: kdb.Circuit,
    parasitic_nets: list[dict[str, Any]],
    coupled_pairs: list[dict[str, Any]],
    ground_net_name: str,
    distributed_rc_nets: frozenset[str] | None = None,
    mom_rlc_inductor: tuple[str, float] | None = None,
) -> dict[str, Any]:
    """Inject a star-topology parasitic RC per net (or a distributed
    multi-segment ladder for a caller-declared subset, see below), plus one
    two-terminal coupling capacitor per net pair with non-zero
    vertical-overlap and/or (``--critical-net``-scoped) lateral coupling
    capacitance, into ``circuit`` and return the JSON ``parasitics`` summary
    block (issue #592, extended by issues #760, #976, and #977).

    ``mom_rlc_inductor`` (``(net_name, inductance_nh)``, issue #988, Epic
    #709 Phase 3a) -- ``None`` unless ``--mom-rlc-net`` was given together
    with ``--mom-rlc-inductance-nh``, in which case, for every net named
    ``net_name`` here that ends up on the *lumped* (star/Gamma-shunt)
    model path below, one series inductor (``kdb.DeviceClassInductor``,
    henries) is spliced between that net's hub and its ground capacitor
    (``hub --L--> <fresh node> --C--> ground``) instead of the capacitor
    hanging directly off the hub. There is no inductance term anywhere in
    this function's default RC-only model for this to *replace* -- it is
    purely additive, unlike ``mom_rlc_net``'s own R/C values (substituted
    into ``parasitic_nets`` entries by the caller, before this function
    ever runs -- see ``run_extract``'s ``mom_rlc_net`` docstring paragraph).
    Never reached for a net on the *distributed* ladder path (``run_extract``
    rejects that combination up front as mutually exclusive).

    For each ``parasitic_nets`` entry **not** named in ``distributed_rc_nets``
    (the overwhelmingly common case, and the only one before issue #977), the
    net itself becomes the star's **hub**. Every device terminal that was
    connected directly to the net is moved onto a fresh per-terminal "leg"
    net, and a series resistor bridges each leg back to the hub -- so two
    terminals on the same net now sit in series through two resistors
    (``leg_a --R--> hub --R--> leg_b``), instead of sharing one node with no
    resistance between them (the pre-#592 topology this replaces). A single
    capacitor still hangs the net's total lumped ground capacitance off the
    hub (``hub --C--> <substrate_net>``), created if absent -- even when that
    capacitance rounds to ``0.0`` because every bit of it moved to coupling
    (issue #760: the hub still needs to exist for a coupling ``C`` card to
    attach to). Each leg's resistance is the net's total computed resistance
    distributed across its terminals by :func:`_terminal_star_weights` -- a
    terminal farther from the net's other connections gets more of the
    total, and the weights always sum to ``1.0`` so a net's leg resistances
    sum back to its total. A net with no device terminal at all (real
    geometry with nothing electrically attached) falls back to exactly the
    pre-#592 Gamma-shunt: one resistor from the net to a fresh internal
    node, with the capacitor on that node.

    **Distributed (multi-segment) RC ladder (``--distributed-rc``, issue
    #977, Epic #709 Phase 2b):** for a ``parasitic_nets`` entry named in
    ``distributed_rc_nets`` *and* carrying 2 or more device terminals, the
    star above is replaced by a chain: :func:`_distributed_rc_segments`
    orders the terminals along their approximate physical spread and splits
    the net's total R into ``N - 1`` series segment resistors (one between
    each adjacent ordered pair of per-terminal leg nets) and its total C into
    ``N`` per-leg ground capacitors (each leg gets its own capacitor to
    ``ground_net_name``, instead of one shared hub capacitor) -- see that
    function's docstring for the exact per-segment/per-node split. A net
    named in ``distributed_rc_nets`` with fewer than 2 device terminals (no
    chain to build) falls back to the star/Gamma-shunt path above unchanged.
    The ladder's own **hub** (the node the coupled-pair pass below attaches
    a coupling capacitor to, if this net has one) is its *middle* leg -- a
    coarse choice, since coupling geometry is not in general localized to
    one exact point on a real routed net; flagged here rather than silently
    assumed away, the same "known simplification" spirit as the lateral
    pass's own not-deducted-from-ground-fringe note in
    :func:`_compute_parasitics`'s docstring. That middle position reuses the
    original net object itself (not a fresh leg net) -- the same reason the
    star topology reuses ``net`` as its own hub: ``net`` can be a promoted
    top-level pin (or otherwise referenced by identity from outside this
    function), and every ladder position moving its terminal onto a brand
    new leg would leave ``net`` with nothing attached to it at all, silently
    orphaning that pin inside the written ``.SUBCKT`` body.

    After every ``parasitic_nets`` entry has its hub established, one
    two-terminal ``C`` card is created per ``coupled_pairs`` entry, directly
    between the two nets' **hub** nodes (not the raw net objects -- a net
    with device terminals moved its own connectivity onto leg nets, so the
    hub is the correct attachment point for anything that used to sit on the
    net itself). A pair naming a net absent from ``parasitic_nets`` (should
    not happen in practice: any net with coupling geometry has non-zero raw
    ground-eligible area by construction, see :func:`_compute_parasitics`) is
    silently skipped rather than raising, matching this function's existing
    tolerance for an unresolvable net name.

    This is purely additive from the perspective of the schematic-equivalent
    view built *before* this call (`devices[]`/`nets[]`, see `run_extract`):
    no existing device instance is removed, no pin is touched, and the
    written SPICE stays a `.SUBCKT` body directly consumable by ``klt sim``
    (every new node is internal; the subcircuit's pin interface is
    untouched). It is *not* additive to the circuit object's own internal
    wiring the way the old shunt topology was: moving a device terminal onto
    a leg net changes which SPICE node that device's `R`/`C`/`M` card names,
    even though the electrical net it represents -- and everything
    `devices[]`/`nets[]` reports about it -- is unchanged.

    The resistor and capacitor device classes are added unnamed so KLayout's
    ``NetlistSpiceWriter`` emits bare ``R``/``C`` cards with no trailing model
    token (simulator-safe).

    Resolved by ``net_id`` (``Net.cluster_id``), not by name (issue #765):
    two genuinely distinct nets can carry the identical layout label (e.g.
    separate un-strapped ``VGND`` islands nothing straps together), and
    ``net.expanded_name()`` collides across them while ``cluster_id`` does
    not -- see :func:`_compute_parasitics`'s docstring. Resolving by name
    used a last-write-wins dict, so every same-named entry silently
    resolved to whichever net object happened to be inserted last: the
    first such entry moved that net's terminals onto legs, and every
    other same-named entry found no terminals left, fell through to the
    Gamma-shunt fallback, and emitted a device instance name derived from
    the same (colliding) net string -- duplicate SPICE instance names, and
    R/C computed for one island silently attached to a different island's
    terminals. A per-entry ``instance_name`` counter below keeps device
    instance names unique even now that every entry resolves to its own,
    correct net object.
    """
    # Deferred (call-time) import -- see `_detect_dead_metal`'s identical
    # comment above for why this can't be a module-level import.
    # `_MIN_PARASITIC_R_OHM` stays in `extract.py` (it is that module's
    # own tunable constant).
    from .extract import _MIN_PARASITIC_R_OHM

    res_class = kdb.DeviceClassResistor()
    cap_class = kdb.DeviceClassCapacitor()
    ind_class = kdb.DeviceClassInductor()
    netlist = circuit.netlist()
    netlist.add(res_class)
    netlist.add(cap_class)
    netlist.add(ind_class)

    ground = circuit.net_by_name(ground_net_name)
    if ground is None:
        ground = circuit.create_net(ground_net_name)

    # Keyed by `cluster_id`, not by name (issue #765): `cluster_id` is
    # unique per net *object* within a circuit (it is the id
    # `LayoutToNetlist` itself uses to tie a net to its layout cluster),
    # while `expanded_name()` is not -- two distinct nets can carry the
    # same layout label. `existing_names` (used below purely for
    # collision-avoidance when minting fresh leg/hub net *names*) still
    # needs the set of every current net name, so it is built separately via
    # `_net_identity_name` (issue #696) exactly as before -- the *unescaped*
    # namespace fresh leg/hub nets are actually minted into (issue #1162;
    # see `_net_identity_name`'s docstring for why baking the escaped
    # spelling in here would double-escape the written netlist).
    nets_by_id = {net.cluster_id: net for net in circuit.each_net()}
    existing_names = {_net_identity_name(net) for net in circuit.each_net()}

    # Tracks how many entries so far have sanitized to a given base
    # instance name (issue #765): two entries can share a `net` string
    # (same layout label, distinct net objects), and `_sanitize_instance_name`
    # is a pure function of that string, so without this counter their
    # emitted `R`/`C` device instance names would collide even though each
    # entry now resolves to its own correct net object. The first entry to
    # reach a given base name keeps it unsuffixed (no behavior change for
    # the overwhelmingly common non-colliding case); every subsequent one
    # gets a `_dup<n>` suffix.
    instance_name_counts: dict[str, int] = {}

    # Per-net `coupled[]` view built from `coupled_pairs` before the main
    # loop below, so each `report_nets` entry can carry its own counterpart
    # list directly (issue #760).
    coupled_by_net: dict[str, list[dict[str, Any]]] = {}
    for pair in coupled_pairs:
        for this_net, other_net in (
            (pair["net_a"], pair["net_b"]),
            (pair["net_b"], pair["net_a"]),
        ):
            coupled_by_net.setdefault(this_net, []).append(
                {
                    # Unescaped identity spelling (issue #1162), matching
                    # `this_net`'s own namespace so sorting below stays
                    # stable -- escaped to the netlist's own spelling only
                    # at the `report_nets` construction site further down,
                    # via `spice_safe_net_name`.
                    "net": other_net,
                    "capacitance_ff": pair["capacitance_ff"],
                    "levels": pair["levels"],
                    # Additive field (issue #976): same-layer levels that
                    # contributed lateral coupling to this pair -- see
                    # `_compute_parasitics`'s docstring. Always `[]` unless
                    # `--critical-net` named one side of this pair.
                    "lateral_levels": pair["lateral_levels"],
                }
            )
    for entries in coupled_by_net.values():
        entries.sort(key=lambda entry: entry["net"])

    report_nets: list[dict[str, Any]] = []
    hub_by_net: dict[str, kdb.Net] = {}
    total_r = 0.0
    total_c_ff = 0.0
    total_r_count = 0
    # Actual capacitor *device* count (issue #977): 1 per lumped
    # (star/Gamma-shunt) net, `N` per distributed net's `N` per-leg
    # capacitors -- unlike `len(report_nets)` (one *entry* per net
    # regardless of model), this always equals the number of `C` cards the
    # written SPICE netlist actually carries.
    total_c_count = 0
    # `mom_rlc_inductor` series-inductor device count/total (issue #988) --
    # 0/0.0 unless `mom_rlc_inductor` was given, same "always present,
    # 0-when-unused" convention `total_coupling_capacitance_ff` already
    # follows.
    total_l_count = 0
    total_l_nh = 0.0
    for entry in parasitic_nets:
        net = nets_by_id.get(entry["net_id"])
        if net is None:
            continue

        base_instance_name = _sanitize_instance_name(entry["net"])
        dup_count = instance_name_counts.get(base_instance_name, 0)
        instance_name_counts[base_instance_name] = dup_count + 1
        instance_name = (
            base_instance_name
            if dup_count == 0
            else f"{base_instance_name}_dup{dup_count}"
        )
        r_total_ohm = max(entry["resistance_ohm"], _MIN_PARASITIC_R_OHM)
        c_farad = entry["capacitance_ff"] * 1e-15

        # Snapshot before mutating: moving a terminal off `net` below changes
        # what `net.each_terminal()` would yield mid-iteration.
        terminal_refs = list(net.each_terminal())

        terminal_reports: list[dict[str, Any]] = []
        segment_reports: list[dict[str, Any]] = []
        rc_model = "lumped"
        net_c_count = 0
        if (
            distributed_rc_nets is not None
            # `distributed_rc_nets` is caller-supplied (`--critical-net`
            # naming a `--distributed-rc` target): re-escaped for the
            # comparison, same rationale as the lateral-coupling pass above
            # (issue #1162).
            and spice_safe_net_name(entry["net"]) in distributed_rc_nets
            and len(terminal_refs) >= 2
        ):
            # Distributed (multi-segment) RC ladder (issue #977, Epic #709
            # Phase 2b) -- see this function's docstring and
            # `_distributed_rc_segments`'s own docstring for the exact
            # per-segment/per-node derivation.
            rc_model = "distributed"
            positions = _terminal_star_positions_um(terminal_refs)
            order, segment_r_ohm, node_c_ff = _distributed_rc_segments(
                positions, r_total_ohm, entry["capacitance_ff"]
            )

            # The *middle* ladder position reuses the original `net` object
            # itself, exactly like the star topology's own `hub = net` above
            # -- not a fresh net. `net` may be a promoted top-level pin (or
            # otherwise referenced by identity from outside this function);
            # every *other* position already moves its terminal onto a fresh
            # leg net the same way the star topology does, but if *every*
            # position did that here, `net` itself would end this function
            # with no device, resistor, or capacitor attached to it at all --
            # silently orphaning that pin inside the written `.SUBCKT` body.
            # Reusing `net` at exactly one position (this function's own
            # coupling-attachment point, "the middle leg" per this function's
            # docstring) keeps that position's identity intact for free,
            # mirroring the star's own reuse.
            mid_index = len(order) // 2

            legs: list[kdb.Net] = [net] * len(order)
            leg_names: list[str] = [entry["net"]] * len(order)
            for position_in_order, terminal_index in enumerate(order):
                term_ref = terminal_refs[terminal_index]
                device = term_ref.device()
                terminal_def = term_ref.terminal_def()

                if position_in_order == mid_index:
                    leg = net
                    leg_name = entry["net"]
                    # Already connected to `net` -- nothing to move.
                else:
                    leg_name = _unique_net_name(
                        entry["net"], existing_names, suffix=f"__t{terminal_index}"
                    )
                    existing_names.add(leg_name)
                    leg = circuit.create_net(leg_name)
                    device.disconnect_terminal(terminal_def.id())
                    device.connect_terminal(terminal_def.id(), leg)

                legs[position_in_order] = leg
                leg_names[position_in_order] = leg_name

                node_c_farad = node_c_ff[position_in_order] * 1e-15
                node_cap = circuit.create_device(
                    cap_class, f"{instance_name}_n{position_in_order}"
                )
                node_cap.connect_terminal("A", leg)
                node_cap.connect_terminal("B", ground)
                node_cap.set_parameter("C", node_c_farad)
                net_c_count += 1

                terminal_reports.append(
                    {
                        "device": device.expanded_name(),
                        "terminal": terminal_def.name,
                        # Report-boundary escape (issue #1162): `leg_name`
                        # is this leg's real, unescaped net identity (see
                        # `_net_identity_name`'s docstring); `spice_safe_
                        # net_name` makes this JSON value byte-identical to
                        # the written netlist's own node spelling for it.
                        "leg_net": spice_safe_net_name(leg_name),
                        "order": position_in_order,
                        "capacitance_ff": round(node_c_ff[position_in_order], 6),
                    }
                )

            for seg_index, seg_r_ohm in enumerate(segment_r_ohm):
                seg_r_ohm_clamped = max(seg_r_ohm, _MIN_PARASITIC_R_OHM)
                r_dev = circuit.create_device(
                    res_class, f"{instance_name}_seg{seg_index}"
                )
                r_dev.connect_terminal("A", legs[seg_index])
                r_dev.connect_terminal("B", legs[seg_index + 1])
                r_dev.set_parameter("R", seg_r_ohm_clamped)
                total_r_count += 1
                segment_reports.append(
                    {
                        # Report-boundary escape (issue #1162), same
                        # rationale as `terminal_reports[].leg_net` above.
                        "net_a": spice_safe_net_name(leg_names[seg_index]),
                        "net_b": spice_safe_net_name(leg_names[seg_index + 1]),
                        "resistance_ohm": round(seg_r_ohm_clamped, 4),
                    }
                )

            # The coupled-pair pass below attaches to the ladder's *middle*
            # leg -- see this function's docstring's "known simplification"
            # note. It is `net` itself (see above), so `hub_name` here is
            # `entry["net"]`, exactly the star topology's own convention.
            hub = legs[mid_index]
            hub_name = leg_names[mid_index]
        elif terminal_refs:
            hub = net
            hub_name = entry["net"]
            positions = _terminal_star_positions_um(terminal_refs)
            weights = _terminal_star_weights(positions)
            for i, (term_ref, weight) in enumerate(
                zip(terminal_refs, weights, strict=True)
            ):
                leg_name = _unique_net_name(
                    entry["net"], existing_names, suffix=f"__t{i}"
                )
                existing_names.add(leg_name)
                leg = circuit.create_net(leg_name)

                device = term_ref.device()
                terminal_def = term_ref.terminal_def()
                device.disconnect_terminal(terminal_def.id())
                device.connect_terminal(terminal_def.id(), leg)

                leg_r_ohm = max(r_total_ohm * weight, _MIN_PARASITIC_R_OHM)
                r_dev = circuit.create_device(res_class, f"{instance_name}_t{i}")
                r_dev.connect_terminal("A", leg)
                r_dev.connect_terminal("B", hub)
                r_dev.set_parameter("R", leg_r_ohm)
                total_r_count += 1

                terminal_reports.append(
                    {
                        "device": device.expanded_name(),
                        "terminal": terminal_def.name,
                        # Report-boundary escape (issue #1162), same
                        # rationale as the distributed-model case above.
                        "leg_net": spice_safe_net_name(leg_name),
                        "resistance_ohm": round(leg_r_ohm, 4),
                    }
                )
        else:
            # No device terminal to fan a star out to (e.g. real routed
            # geometry with nothing electrically attached) -- fall back to
            # the pre-#592 Gamma-shunt so the net's capacitance still has
            # somewhere to attach.
            hub_name = _unique_net_name(entry["net"], existing_names)
            existing_names.add(hub_name)
            hub = circuit.create_net(hub_name)

            r_dev = circuit.create_device(res_class, instance_name)
            r_dev.connect_terminal("A", net)
            r_dev.connect_terminal("B", hub)
            r_dev.set_parameter("R", r_total_ohm)
            total_r_count += 1

        net_inductance_nh = 0.0
        if rc_model == "lumped":
            # `--mom-rlc-net`/`--mom-rlc-inductance-nh` (issue #988, Epic
            # #709 Phase 3a): splice one series inductor between the hub and
            # the ground capacitor for exactly the net `mom_rlc_inductor`
            # names -- see this function's own `mom_rlc_inductor` docstring
            # paragraph. `c_ground_node` is the capacitor's own "A" terminal
            # below: `hub` unless this splice applies, in which case it is
            # the fresh node between the inductor and the capacitor.
            c_ground_node = hub
            # `mom_rlc_inductor[0]` is caller-supplied (`--mom-rlc-net`):
            # re-escaped for the comparison, same rationale as
            # `distributed_rc_nets` above (issue #1162).
            if (
                mom_rlc_inductor is not None
                and spice_safe_net_name(entry["net"]) == mom_rlc_inductor[0]
            ):
                net_inductance_nh = mom_rlc_inductor[1]
                l_node_name = _unique_net_name(
                    entry["net"], existing_names, suffix="__l"
                )
                existing_names.add(l_node_name)
                l_node = circuit.create_net(l_node_name)
                l_dev = circuit.create_device(ind_class, f"{instance_name}_l")
                l_dev.connect_terminal("A", hub)
                l_dev.connect_terminal("B", l_node)
                l_dev.set_parameter("L", net_inductance_nh * 1e-9)
                c_ground_node = l_node
                total_l_count += 1
                total_l_nh += net_inductance_nh
            # The distributed ladder above already created one capacitor per
            # leg (summing back to the net's total, see
            # `_distributed_rc_segments`'s docstring) -- creating a second,
            # hub-level capacitor here would double-count the net's
            # capacitance.
            c_dev = circuit.create_device(cap_class, instance_name)
            c_dev.connect_terminal("A", c_ground_node)
            c_dev.connect_terminal("B", ground)
            c_dev.set_parameter("C", c_farad)
            net_c_count = 1

        hub_by_net[entry["net"]] = hub
        total_r += r_total_ohm
        total_c_ff += entry["capacitance_ff"]
        total_c_count += net_c_count
        report_nets.append(
            {
                # Report-boundary escape (issue #1162): `entry["net"]` is
                # this net's unescaped identity spelling (see
                # `_net_identity_name`'s docstring); `spice_safe_net_name`
                # makes this JSON value byte-identical to the written
                # netlist's own node spelling for it.
                "net": spice_safe_net_name(entry["net"]),
                # Additive field (issue #765): disambiguates entries whose
                # `net` string collides across distinct net objects (e.g.
                # separate un-strapped `VGND` islands) -- see
                # `_compute_parasitics`'s docstring. `net` itself is
                # unchanged, so this is not a breaking schema change.
                "net_id": entry["net_id"],
                "resistance_ohm": entry["resistance_ohm"],
                "capacitance_ff": entry["capacitance_ff"],
                # Additive field (issue #1701): the per-role/per-metal-level
                # R/C breakdown `_compute_parasitics` built for this net --
                # see that function's docstring/inline comment above its own
                # `by_layer` construction for the exact shape, the
                # sum-equals-total invariant, and the one documented
                # exception (a subsequent `--mom-net`/`--mom-rlc-net`
                # substitution above does not adjust this list). `[]` only
                # for a net whose ground-eligible geometry has no curated
                # `ParasiticsDeck` coefficient at all -- effectively
                # unreachable here, since such a net would also have no raw
                # ground capacitance and never reach `parasitic_nets` in the
                # first place. Passed through verbatim: layer names are not
                # net names, so no `spice_safe_net_name` re-escaping applies.
                "by_layer": entry.get("by_layer", []),
                # Additive fields (issue #1704): this net's top-cell-drawn
                # share of ground R/C, computed by `_compute_parasitics` when
                # `--parasitics-top-cell-only` was given -- see that
                # function's "Top-cell-only hierarchy split" docstring
                # paragraph for the algorithm and the documented
                # not-exactly-additive-in-perimeter caveat for a net whose
                # conductor spans an instance boundary. `None` (the default,
                # via `entry.get`) when the flag was never given.
                "resistance_ohm_top_cell": entry.get("resistance_ohm_top_cell"),
                "capacitance_ff_top_cell": entry.get("capacitance_ff_top_cell"),
                # Additive field (issue #988): the series inductor spliced in
                # for this net by `mom_rlc_inductor` -- `0.0` (the default)
                # for every net unless `--mom-rlc-net`/
                # `--mom-rlc-inductance-nh` named this one.
                "inductance_nh": net_inductance_nh,
                "hub_net": spice_safe_net_name(hub_name),
                # Additive field (issue #977): `"lumped"` (the pre-#977
                # star/Gamma-shunt model, always this value unless
                # `--distributed-rc` named this net) or `"distributed"` (the
                # multi-segment ladder above).
                "rc_model": rc_model,
                "terminals": terminal_reports,
                # Additive field (issue #977): the ladder's per-segment
                # resistors, in `order` sequence -- `[]` unless
                # `rc_model == "distributed"`.
                "segments": segment_reports,
                "coupled": [
                    {**c, "net": spice_safe_net_name(c["net"])}
                    for c in coupled_by_net.get(entry["net"], [])
                ],
            }
        )

    # One two-terminal `C` card per coupled net pair, between the two nets'
    # **hub** nodes -- built only after every entry above has established its
    # hub, since a pair can name either side in either order (issue #760).
    total_cc_ff = 0.0
    cc_count = 0
    for pair in coupled_pairs:
        hub_a = hub_by_net.get(pair["net_a"])
        hub_b = hub_by_net.get(pair["net_b"])
        if hub_a is None or hub_b is None:
            # Should not happen (see docstring): a net with coupling
            # geometry always has non-zero raw ground-eligible area, so it
            # always reaches `parasitic_nets` and gets a hub. Skipped rather
            # than raised, matching this function's existing tolerance for
            # an unresolvable net name a few lines up.
            continue
        cc_instance_name = _sanitize_instance_name(f"{pair['net_a']}_{pair['net_b']}")
        cc_dev = circuit.create_device(cap_class, f"cc_{cc_instance_name}")
        cc_dev.connect_terminal("A", hub_a)
        cc_dev.connect_terminal("B", hub_b)
        cc_dev.set_parameter("C", pair["capacitance_ff"] * 1e-15)
        total_cc_ff += pair["capacitance_ff"]
        cc_count += 1

    # DC reference for the deck's synthesized substrate identities (issue
    # #1263) -- last, so it sees every net this function created and cannot
    # collide with one of them.
    substrate_dc_tie = _tie_substrate_nets_to_ground(
        circuit, res_class, ground_net_name
    )

    return {
        "r_count": total_r_count,
        "c_count": total_c_count,
        "cc_count": cc_count,
        # Additive fields (issue #988): `mom_rlc_inductor`'s series-inductor
        # device count/total -- `0`/`0.0` (the default) unless
        # `--mom-rlc-net`/`--mom-rlc-inductance-nh` was given.
        "l_count": total_l_count,
        "total_resistance_ohm": round(total_r, 4),
        "total_capacitance_ff": round(total_c_ff, 6),
        "total_coupling_capacitance_ff": round(total_cc_ff, 6),
        "total_inductance_nh": round(total_l_nh, 6),
        "nets": report_nets,
        # Additive field (issue #1263): the substrate DC-reference shunt(s)
        # written into the `.SUBCKT` body -- see
        # `_tie_substrate_nets_to_ground`'s docstring.
        "substrate_dc_tie": substrate_dc_tie,
    }


def _tie_substrate_nets_to_ground(
    circuit: kdb.Circuit,
    res_class: kdb.DeviceClass,
    substrate_net: str,
) -> dict[str, Any]:
    """Give every *synthesized* substrate identity in ``circuit`` a DC path
    to SPICE's global ground node ``0``, via one large shunt resistor per net
    (``R<net>_dctie <net> 0 1e12``), and report what was written (issue
    #1263). The tied net names are also declared SPICE-global by the caller
    (``run_extract``, via ``create_model_binding_delegate``'s ``global_nets``
    -- see issue #1503 below): this function only builds the *name list* and
    the shunt devices; the ``.GLOBAL`` card itself is written later, once per
    netlist, by the SPICE writer delegate's ``write_header`` hook.

    **Why this is needed.** ``--parasitics`` hangs each net's lumped ground
    capacitance off the deck's ``substrate_net`` (``vsubs`` for sky130 and
    gf180mcu), and ``connect_global`` mints that net -- plus any
    per-isolated-region ``f"{substrate_net}_iso{n}"`` variant (issue #1128)
    -- out of nothing: no layout can draw a label for it. Nothing in the
    written netlist then gives it a defined DC value. A caller who
    ``.include``s the extracted file and ``X``-instantiates its ``.SUBCKT``
    -- the exact convention ``docs/cli/extract.md`` documents -- therefore
    hands ngspice a node whose only connections are capacitors, and the
    ``.op``/transient solve hits ``Warning: singular matrix: check node
    x<dut>.vsubs`` on it. The reported ngspice recovery (dynamic gmin
    stepping, then true gmin stepping, then source stepping) can still
    return *a* number, but not a reproducible one -- so the failure mode is
    a silently untrustworthy post-layout result, not a hard error.

    Being a *pin* does not save it: ``make_top_level_pins()`` promotes the
    substrate net like any other named net *when that net already exists at
    promotion time* (e.g. a MOS body terminal strapped it during device
    extraction) -- but the net this function ties is minted by
    ``_inject_parasitics`` itself, which runs *after* ``make_top_level_
    pins()``/``_reconcile_top_pins`` have already finished (see "Pin
    interface untouched" below), so it is never a candidate for promotion in
    the first place. A promoted pin wired to an equally-undriven node in the
    caller's testbench would float just the same regardless -- the missing
    DC path, not the pin-exposure status, is the defect this function fixes.

    **Why a shunt resistor to node ``0``, plus ``.GLOBAL`` (issue #1503).**
    Node ``0`` is SPICE's *global* ground: it means the same node inside a
    subcircuit body as at the top level, needing no cooperation from the
    instantiating testbench. That much was true before issue #1503 and still
    holds -- the shunt itself needs nothing from the caller. What issue
    #1503 fixes is the *other* end of the resistor: pre-#1503, the tied net
    itself (``vsubs``, or a ``_iso<n>`` variant) was neither a pin nor
    ``.global`` -- purely local to the ``.SUBCKT`` body -- so an
    ``X``-instantiated testbench's own same-named node was a *different*,
    electrically disconnected SPICE node from the instance's internal one
    (``vsubs`` at the top level vs. ``x1.vsubs`` once flattened). The DC tie
    kept the *instance's own* node from floating (no more singular-matrix
    error), but the parasitic ground-capacitance model it anchors was then
    silently computed against a node the testbench could not reach or drive.
    Declaring the net ``.GLOBAL`` (written once, before the first
    ``.SUBCKT``, by the SPICE writer's ``write_header`` hook -- see
    ``create_model_binding_delegate``) makes every occurrence of that literal
    net name, in every scope, refer to the *same* physical node: the
    top-level testbench's own ``vsubs``, this ``.SUBCKT``'s internal
    reference, and every other ``X``-instantiation of it, are now all one
    node -- exactly the property a caller driving or measuring the substrate
    reference at the top level needs. A ``.global`` card was previously
    treated as conventionally top-level-only and rejected inside a
    ``.SUBCKT`` body; per ngspice's own ``.global`` semantics (it may appear
    anywhere and applies netlist-wide regardless of position) writing it once
    at the top of the file -- not inside the body -- satisfies both: the
    written file keeps a conventional top-level ``.global`` card, and the
    coverage is netlist-wide.

    **A declared pin's local formal-argument binding still wins.** Where a
    synthesized substrate net *is* also a declared ``.SUBCKT`` pin (the MOS
    body-terminal case above), each instantiation's own call-site argument
    already gives that pin's local name a specific bound node distinct from
    a global declaration -- SPICE resolves an occurrence against a
    subcircuit's own formal pin list before falling back to a ``.global``
    name, so declaring the same literal name ``.global`` alongside it is
    inert for that one subcircuit (no conflict) while still applying to any
    other scope that references the same name without binding it as a pin.

    **Idempotent with a hand-authored tie.** A testbench that already
    supplies its own ``.global vsubs`` + ``Vsubs vsubs 0 DC 0`` (or
    ``.options rshunt=1e12``) keeps working unchanged: a 1 Tohm resistor in
    parallel with an ideal 0 V source draws ~0 A and moves no node voltage;
    a duplicate ``.global vsubs`` declaration (this function's own, plus the
    testbench's) is itself idempotent -- ngspice tolerates re-declaring the
    same name global. Existing fixtures that hand-author the workaround are
    left in place for exactly that reason.

    **Harmless where nothing floats.** On a net that already has a real DC
    path, adding a 1 Tohm leakage path to ground is below every simulator
    tolerance -- the same property that makes ngspice's own blanket
    ``.options rshunt`` safe (the reporter measured byte-identical results
    with and without it on a non-extracted leg).

    **Pin interface untouched.** No pin is created, promoted, or demoted
    here: net ``0`` is minted after ``make_top_level_pins()``/``_reconcile_
    top_pins`` have already run, so it stays internal and the written
    ``.SUBCKT``'s declared pin count is unchanged -- which is what keeps
    ``klt pex``'s ``pin_count_mismatch``/``flat_dut_mismatch`` diagnostics
    (issue #1258) reading the same interface they did before. The new
    ``.GLOBAL`` card (issue #1503) does not change this either: it is a
    top-of-file directive, not a pin declaration.

    Returns the additive ``parasitics.substrate_dc_tie`` report block:
    ``{"resistance_ohm": float, "nets": [{"net": str, "device": str}, ...],
    "node_scope": "global"}`` -- ``nets`` empty (and no card written, no
    ``.GLOBAL`` line emitted) when this circuit carries no synthesized
    substrate identity at all. ``node_scope`` is additive (issue #1503): a
    constant ``"global"`` describing that every ``nets[].net`` identity is
    declared SPICE-global (not merely tied to ground), so a caller can
    detect the node-scoping guarantee programmatically instead of reading
    the generated SPICE for a ``.GLOBAL`` card.
    """
    # Deferred (call-time) import -- see `_detect_dead_metal`'s identical
    # comment above for why this can't be a module-level import.
    # `_is_synthesized_substrate_net_name`/`_SPICE_GLOBAL_GROUND_NODE`/
    # `SUBSTRATE_DC_TIE_RESISTANCE_OHM` stay in `extract.py` (each has other
    # callers/definitions there).
    from .extract import (
        _SPICE_GLOBAL_GROUND_NODE,
        SUBSTRATE_DC_TIE_RESISTANCE_OHM,
        _is_synthesized_substrate_net_name,
    )

    tied_nets = [
        net
        for net in circuit.each_net()
        if _is_synthesized_substrate_net_name(_net_identity_name(net), substrate_net)
    ]
    entries: list[dict[str, str]] = []
    if tied_nets:
        reference = circuit.net_by_name(_SPICE_GLOBAL_GROUND_NODE)
        if reference is None:
            reference = circuit.create_net(_SPICE_GLOBAL_GROUND_NODE)
        existing_devices = {device.expanded_name() for device in circuit.each_device()}
        for net in sorted(tied_nets, key=_net_identity_name):
            identity = _net_identity_name(net)
            instance_name = _unique_net_name(
                _sanitize_instance_name(identity), existing_devices, suffix="_dctie"
            )
            existing_devices.add(instance_name)
            tie = circuit.create_device(res_class, instance_name)
            tie.connect_terminal("A", net)
            tie.connect_terminal("B", reference)
            tie.set_parameter("R", SUBSTRATE_DC_TIE_RESISTANCE_OHM)
            entries.append(
                {
                    # Report-boundary escape (issue #1162), so this JSON
                    # value is byte-identical to the netlist's own node
                    # spelling -- same convention as `nets[].net` above.
                    "net": spice_safe_net_name(identity),
                    "device": f"R{instance_name}",
                }
            )
    return {
        "resistance_ohm": SUBSTRATE_DC_TIE_RESISTANCE_OHM,
        "nets": entries,
        # Additive field (issue #1503): constant `"global"` -- every
        # `nets[].net` identity above is also declared SPICE-global (see
        # this function's own docstring), not merely tied to ground. Present
        # even when `nets` is `[]` (nothing to scope, but the policy is
        # still accurately described as "global" for this build).
        "node_scope": "global",
    }


def spice_safe_net_name(name: str) -> str:
    """Rewrite a KLayout ``Net.expanded_name()`` string to the exact spelling
    KLayout's own ``NetlistSpiceWriter`` writes for that net's *node*
    references in the ``.SUBCKT``/instance lines of the written SPICE file
    (issue #696, issue #1162).

    Two independent rewrites, both mirroring escaping ``NetlistSpiceWriter``
    already applies when it writes a net as a node reference (as opposed to
    the raw form it keeps in its own leading ``* pin ...``/``* net ...``
    comments):

    1. **Merged labels (issue #696).** ``Net.expanded_name()`` joins every
       distinct text label found on one electrical net with ``,`` (see
       :func:`_detect_merged_net_labels`'s docstring, issue #470) -- but a
       SPICE node token cannot contain a comma (a common argument
       separator), so ``NetlistSpiceWriter`` writes the *same* joined net
       using ``|`` instead wherever it appears as an actual node reference.
    2. **Anonymous nets (issue #1162).** A net with no drawn label at all
       gets KLayout's auto-generated ``$<n>`` placeholder as its
       ``expanded_name()`` (e.g. ``$2``) -- but ngspice (and the wider
       SPICE3/HSPICE-descended dialect family) treats a token that *starts*
       with ``$`` as an inline-comment marker, silently truncating the rest
       of the card. ``NetlistSpiceWriter`` backslash-escapes a leading ``$``
       (``\\$2``, confirmed against a live ``NetlistSpiceWriter`` run: a
       node named ``$weird`` writes as ``\\$weird``, while a *mid-token*
       ``$`` such as ``mid$dle`` is left alone -- only the leading
       character triggers the ngspice comment hazard) wherever it appears
       as a node reference; this function does the same.

    Before this function existed (for case 1) and before issue #1162 (for
    case 2), every net name this module put into the JSON response
    (``nets[].name``, ``devices[].nets[...]``, ``merged_net_labels[].net``,
    ``parasitics.nets[].net``, ``parasitics.nets[].terminals[].leg_net``)
    used the *unescaped* form, while the written netlist used the escaped
    form -- the same net, spelled two different ways depending which
    artifact you read it from, with no way for a caller to know the two
    strings named the same node short of hard-coding the substitutions
    themselves (and, for the anonymous-net case, a caller that copied the
    unescaped JSON spelling verbatim into a hand-authored SPICE card would
    reproduce the very comment-truncation hazard ``NetlistSpiceWriter``
    itself already avoids). Calling this on every net name before it enters
    the response makes it byte-identical to the netlist's own spelling
    everywhere it is reported (also applied by ``klt lvs``'s
    ``net_correspondence``/``mismatches[].net`` via ``lvs.py``'s
    ``_name_or_none``, sourced from the same ``Net.expanded_name()``
    convention).

    A no-op for the overwhelming majority of net names, which contain
    neither a comma nor a leading ``$``.
    """
    escaped = name.replace(",", "|")
    if escaped.startswith("$"):
        escaped = "\\" + escaped
    return escaped


def _net_identity_name(net: kdb.Net) -> str:
    """The comma -> ``|`` (issue #696) rewrite of ``net.expanded_name()``
    *without* :func:`spice_safe_net_name`'s leading-``$`` backslash escape
    (issue #1162) -- used only where the resulting string becomes (part of)
    the *real* name of a ``kdb.Net``/``kdb.Device`` this module creates in
    the working circuit (``_compute_parasitics``'s internal coupling-pair
    keys and ground-net list, and everything derived from them inside
    ``_inject_parasitics``: ``_unique_net_name``'s collision-avoidance set,
    the actual leg/hub nets ``circuit.create_net`` mints, and
    ``_sanitize_instance_name``'s input).

    Baking the *already-escaped* (``\\$2``-style) spelling into a real net's
    name would double-escape it: ``NetlistSpiceWriter`` applies its own
    leading-``$``/backslash escaping when it writes a net as a node
    reference, so a net whose actual name already starts with a literal
    backslash comes out with *two* backslashes in the written netlist
    (confirmed directly against a live ``NetlistSpiceWriter`` run). Net
    identity, instance-name sanitization, and CLI net-name matching
    (``--critical-net``, ``--mom-rlc-net``) all stay in this *unescaped*
    namespace -- exactly the pre-#1162 ``spice_safe_net_name`` behavior --
    so a leading ``$`` continues to compare/collide the same way it always
    has. Only the JSON *report* value derived from a name in this namespace
    (built by re-running it through :func:`spice_safe_net_name` at the point
    it enters a response field, e.g. ``parasitics.nets[].net``/``hub_net``/
    ``terminals[].leg_net``) picks up the escape, matching the netlist's own
    spelling without touching what the underlying net is actually called.
    """
    return net.expanded_name().replace(",", "|")


def _unique_net_name(base: str, existing: set[str], suffix: str = "__par") -> str:
    """A SPICE-safe internal parasitic-node name derived from ``base`` that
    does not collide with any already-present net name (an underscore suffix,
    not a dot, so ngspice never mistakes it for a hierarchy separator).

    ``suffix`` defaults to the original ``__par`` shunt-node suffix (issue
    #216/#283); the star topology (issue #592) also derives per-terminal
    "leg" net names from this same collision-avoidance logic with a
    ``__t<i>``-style suffix."""
    candidate = f"{base}{suffix}"
    if candidate not in existing:
        return candidate
    counter = 2
    while f"{base}{suffix}{counter}" in existing:
        counter += 1
    return f"{base}{suffix}{counter}"
