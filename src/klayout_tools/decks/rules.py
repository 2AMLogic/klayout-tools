"""DRC rule domain for the ``decks`` package: :class:`DrcRule` and its
supporting types.

Split out of ``decks/__init__.py`` (issue #1821) as a self-contained
domain: the curated DRC rule-table row type (:class:`DrcRule`) plus the two
types it composes -- :class:`DerivedLayer` (a sized/boolean combination of
two drawn layers used when a rule's official DRM scope isn't a single drawn
``(layer, datatype)``) and :class:`RuleProvenance` (structured citation
back to the DRM section a rule was transcribed from) -- and
:class:`UnknownDeckError`, the lookup-failure exception :func:`get_deck`/
:func:`get_layer_names` raise. None of these types reference the
extraction (``extraction.py``) or parasitics (``parasitics.py``) domains;
:class:`RuleProvenance` is reused by several extraction dataclasses'
``provenance``/``nfet_provenance``/``pfet_provenance`` fields, which is why
``extraction.py`` imports it from here rather than the reverse.

The registry/lookup functions that use these types (:func:`get_deck`,
:func:`get_layer_names`, etc.) stay in ``decks/__init__.py``, which
re-exports every public name below so existing
``from klayout_tools.decks import X`` call sites are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedLayer:
    """A "virtual" derived layer computed from two drawn layers, for a
    :class:`DrcRule` whose official DRM scope is a sized/boolean layer
    expression rather than a single drawn ``(layer, datatype)`` (issue #345).

    Some DRM rules are defined against a derived geometry rather than a
    literal drawn layer -- e.g. gf180mcu's ``MIMTM.2`` ("min. MiM bottom-plate
    overlap of ``Via4``") scopes to the MiM stack's "virtual bottom plate":
    the purpose-drawn top-plate layer (``FuseTop``) oversized by a fixed
    margin, restricted to wherever the bottom-plate conductor (``Metal4``)
    already comes near it. Checking this rule against raw ``Metal4`` (the way
    ``mim.space.1`` approximates ``MIMTM.1``) would be actively wrong, not
    merely conservative: ordinary ``Metal4``-``Via4``-``Metal5`` routing
    anywhere in the layout would falsely trip it, since an unscoped
    "``Metal4`` must enclose every ``Via4`` by the MiM margin" check has
    nothing to do with routing vias at all.

    ``mode`` selects *which* derivation is applied (see the three values
    below). The default, ``"sized_intersection"``, is the original
    (issue #345) one:
    ``intersect_with_region.interacting(base_region) & base_region.sized(sized_by_um)``
    -- i.e. only shapes of ``intersect_with`` that already touch the *unsized*
    ``base`` region somewhere, clipped to ``base``'s oversized outline. This
    mirrors :class:`CapacitorDevice`'s own ``bottom_plate_oversize_um``
    "virtual bottom plate" derivation in ``extract.py`` (issue #314) --
    the DRC-deck analogue of that same official two-step PDK derivation,
    reused here as a general escape hatch for any future rule (in this or
    another deck) whose official scope needs a sized/derived layer that the
    plain single-/two-layer :class:`DrcRule` check primitives can't express
    on their own.

    ``base`` is sized (oversized) by ``sized_by_um`` micrometres (a real
    physical distance, rescaled against the *layout's own* ``dbu`` at run
    time -- unlike ``DrcRule.threshold_dbu``, which is expressed in the
    deck's nominal dbu and rescaled by :func:`~klayout_tools.drc.run_drc`'s
    ``dbu_scale`` instead). ``intersect_with`` is the second drawn layer
    restricted against it. Both fields are independent of
    :attr:`DrcRule.layer`, which continues to serve only as the rule's
    reporting identity (the layer name shown in ``violations[].layer`` and
    tracked in ``coverage.deck_layers``) -- typically set to whichever of the
    two derived-layer inputs best matches the sibling non-derived rules that
    check the same physical structure (e.g. gf180mcu's ``mim.space.1``/
    ``mim.enclosing.fusetop.1`` both report ``"Metal4"``, so
    ``mim.enclosing.via4.1``'s ``DrcRule.layer`` does too, even though its
    ``base`` is ``FuseTop``).

    **Marker-scoped modes (issue #1110).** A second, different need the
    ``"sized_intersection"`` derivation above cannot express: a DRM rule
    whose threshold *depends on whether the geometry is marked*, published
    as a pair of columns rather than one number -- gf180mcu's ``DF.1a_LV``
    (0.22 um min COMP width) vs. ``DF.1a_MV`` (0.30 um), selected by whether
    the ``Comp`` shape is drawn inside ``Dualgate`` (55/0), its 5V/6V
    thick-oxide marker. Modelling that pair needs *both* halves of a
    whole-polygon partition of one drawn layer against a marker layer, which
    the two remaining modes provide:

    - ``"overlapping"``: ``base_region.overlapping(intersect_with_region)``
      -- the whole ``base`` polygons that share **area** with
      ``intersect_with`` (the marked half, e.g. ``comp_56v``).
    - ``"not_interacting"``:
      ``base_region.not_interacting(intersect_with_region)`` -- the whole
      ``base`` polygons that do not touch ``intersect_with`` at all (the
      unmarked half, e.g. ``comp_3p3v``).

    Both are **whole-polygon selections**, not boolean clips: a ``Comp``
    polygon partly inside ``Dualgate`` is selected (or rejected) in its
    entirety, never cut at the marker's edge. This is deliberate and
    matches the PDK's own executable deck verbatim -- gf180mcu's
    ``rule_decks/comp.drc`` derives ``comp_56v = comp.overlapping(dualgate)``
    and ``comp_3p3v = comp.not_interacting(v5_xtor).not_interacting(dualgate)``
    -- and it is what keeps the derivation artefact-free: an ``and``/``not``
    boolean clip would leave a cut edge at the marker boundary that a
    ``width``/``space`` check would then measure as a narrow sliver that
    exists nowhere in the drawn layout.

    ``sized_by_um`` still applies in these two modes, but to
    ``intersect_with`` (the marker) rather than to ``base``: the selection
    is made against ``intersect_with_region.sized(sized_by_um)``, i.e. a
    guard band around the marker. ``0.0`` (the value both gf180mcu
    ``_LV``/``_MV`` pairs use, matching the PDK deck's own unsized marker)
    makes it a plain, unsized selection.

    In ``"not_interacting"`` mode -- and only that mode --
    :func:`~klayout_tools.drc.run_drc` treats an ``intersect_with`` layer
    that is **absent** from the stream as an empty region rather than
    skipping the rule: "``base`` polygons not touching a marker nobody
    drew" is every ``base`` polygon, so a layout with no marker geometry at
    all must still be fully checked against the unmarked column (this is the
    overwhelmingly common case -- a thin-oxide-only design draws no
    ``Dualgate`` at all). The other two modes derive an empty region from an
    absent input layer, so they skip as any other missing-layer rule does.

    **``"holes"`` mode (issue #1976).** A third, unrelated need: a DRM rule
    scoped not to a boolean/sized combination of two drawn layers, but to
    the *interior voids* of one merged drawn layer -- sky130's ``m1.7``-
    ``m5.7`` (each spelled ``m{N}.holes.with_area(0..threshold)`` in the
    source DSL) require a minimum area on the holes enclosed by a slotted
    metal plate or fill pattern, not on the metal polygons themselves.
    ``"holes"`` derives ``base_region.merged().holes()`` --
    ``klayout.db.Region.holes()``, which returns each enclosed void as its
    own filled polygon (merged first, since a hole formed by several
    abutting drawn rectangles -- the common GDS idiom for a slotted plate --
    is only visible once those fragments are merged into one polygon with a
    void; ``Region.holes()`` already applies this "merged semantics"
    internally, but the explicit ``.merged()`` here documents the
    requirement rather than relying on the default silently). ``base`` is
    the only input this mode reads; ``intersect_with`` is unused (leave it
    ``None``, the default) and ``sized_by_um`` is unused (conventionally
    ``0.0``) -- there is no second layer or sizing margin in this
    derivation, unlike every mode above. A ``base`` region with no holes at
    all (an ordinary unslotted plate) derives an *empty* region, which is
    not a violation for any check kind applied to it -- see
    :attr:`DrcRule.area_min_dbu2`'s own note that an empty input region
    simply has nothing to report, the same "nothing to check" behaviour
    every check kind already has for a missing/empty layer.

    Pair this mode with ``check="area"`` (:attr:`DrcRule.area_min_dbu2`) to
    express a holes-area rule like ``m1.7``: the checked "region" becomes
    each hole polygon, and the area threshold applies to each one
    individually, exactly matching ``with_area``'s official semantics.
    """

    base: tuple[int, int]
    sized_by_um: float
    intersect_with: tuple[int, int] | None = None
    mode: str = "sized_intersection"


@dataclass(frozen=True)
class RuleProvenance:
    """Machine-readable citation of the exact upstream PDK source a
    :class:`DrcRule` was transcribed from (issue #747, the deck-compiler
    proposal's `§5 item 2 <../../docs/design/deck-compiler-proposal.md>`_).

    Every rule in ``sky130.py``/``gf180mcu.py`` already carries a *prose*
    citation in its own inline comment (a source repo, file, and official
    rule id -- e.g. ``# sky130.lydrc rule "poly.1a"``); that prose is real,
    specific, and re-verifiable by a human, but not queryable. ``provenance``
    turns the same information into a structured field so a caller (or a
    coverage-audit script) can ask "which rules trace to open-pdks commit X"
    without grepping Python source comments by hand.

    ``source_repo`` is the upstream repository the value was transcribed
    from, as ``"owner/repo"`` (e.g. ``"fossi-foundation/open-pdks"``,
    ``"google/gf180mcu-pdk"``). ``source_path`` is the path *within* that
    repo to the specific file the rule's threshold/geometry was read from --
    a live ``.lydrc``/``.drc`` DRC-DSL script line for sky130, or a
    published DRM section (``.rst`` + its numeric ``tables_clear/*.csv``)
    for gf180mcu, per each deck module's own top-of-file provenance note.
    ``rule_id`` is the *official* upstream rule id the value came from (e.g.
    ``"poly.1a"``, ``"DF.1a"``) -- distinct from this deck's own
    :attr:`DrcRule.id`, which follows this repo's own
    ``"<layer>.<check>.<n>"`` dotted convention, not the PDK's. ``commit`` is
    the upstream commit/tag the value was verified against, when the deck
    module's own docstring pins one (empty string, the default, when it
    does not).

    **Distinct from the existing** :attr:`DrcRule.scope` **field (issue
    #566)**: ``scope`` is coarser and deck-level-aggregated -- a DRM section
    number (``"7.5 Comp"``) or a rule-id-family prefix (``"li"``, ``"m1"``)
    *shared by every rule* transcribed from that section/family, rolled up
    into ``coverage.deck_scope`` so a caller can diff "what sections does
    this deck claim" against the DRM's own table of contents.
    ``provenance`` is **per-rule and exact**: the one specific file and
    official rule id *this individual rule* -- not its whole family --
    was transcribed from, with no deck-wide aggregation. A caller wanting
    "does this deck claim to cover the Nwell chapter" reads ``scope``/
    ``coverage.deck_scope``; a caller wanting "what upstream source line did
    ``nwell.space.1`` itself come from" reads ``provenance``. The two are
    complementary, not redundant: many rules sharing one ``scope`` value
    each carry a different ``provenance.rule_id``.

    As of issue #747, populated only for the 37 width/space rules piloted
    in that issue (sky130: 11, gf180mcu: 26) -- see
    ``tests/golden_deck/README.md`` for the golden-pair manifest that
    cross-checks those same 37 rules against the real PDK-native deck.
    Unpopulated rules simply omit this field (``None``, the default),
    exactly as an unset ``scope`` (``""``) does today.

    **Reused for LVS device-extraction rules (issue #868, Epic #711 Phase
    2a).** Everything above was written for :class:`DrcRule`, but the type
    itself is check-kind-agnostic -- ``source_repo``/``source_path``/
    ``rule_id``/``commit`` describe "which upstream source line does this
    deck value come from" regardless of whether the deck value is a DRC
    threshold or a device-recognition coefficient. :class:`ResistorDevice`,
    :class:`CapacitorDevice`, :class:`BipolarDevice`, and :class:`DiodeDevice`
    each carry their own ``provenance`` field of this same type (their
    per-device-class geometry/coefficient citation), and
    :class:`ExtractionDeck` carries ``nfet_provenance``/``pfet_provenance``
    (MOS has no per-entry list the way resistor/capacitor/bipolar/diode do --
    a deck declares exactly one NMOS and one PMOS recognition rule via its
    own ``active``/``poly``/``nwell`` fields, so the two provenance citations
    live directly on the deck rather than on a nested per-entry dataclass).
    For these, ``source_path``/``rule_id`` typically cite the PDK's
    **KLayout LVS deck** (e.g. sky130's ``sky130.lvs``, a different upstream
    file than the DRC-side ``.lydrc``/``.drc`` script :class:`DrcRule`
    entries cite) and its official device-class name (e.g.
    ``"sky130_fd_pr__nfet_01v8"``, ``"sky130_fd_pr__res_generic_po"``) as
    ``rule_id`` -- the LVS analogue of a DRC rule id, naming *which specific
    device* this entry's geometry/coefficients were transcribed from rather
    than a numbered check. See each deck module's own device-declaration
    comments (``sky130.py``'s ``EXTRACTION_DECK``) for the concrete
    citations, and ``docs/cli/extract.md``'s "Device rule provenance"
    section for the JSON-visibility caveat this shares with ``DrcRule``'s
    own (not yet surfaced in ``klt extract``'s JSON output; queryable only
    by a caller that imports ``klayout_tools.decks`` directly).
    """

    source_repo: str
    source_path: str
    rule_id: str
    commit: str = ""


@dataclass(frozen=True)
class DrcRule:
    """One rule in a DRC deck.

    ``layer`` and ``other_layer`` are ``(layer, datatype)`` pairs. ``check``
    selects which ``klayout.db.Region`` check primitive to run:
    ``"width"`` / ``"space"`` / ``"notch"`` / ``"isolated"`` are single-layer
    checks; ``"separation"`` / ``"enclosing"`` / ``"enclosed"`` / ``"overlap"``
    are two-layer checks and require ``other_layer``; ``"area"``, ``"density"``,
    and ``"antenna"`` (issue #812) are a third shape entirely -- see their own
    fields below and ``drc.py``'s ``_run_area_check``/``_run_density_check``/
    ``_run_antenna_check`` for why they cannot reuse ``threshold_dbu`` or the
    ``EdgePairs``-shaped result the checks above return.
    ``"ongrid"`` and ``"angle"`` (issue #2642) are single-layer *vertex*
    checks rather than distance checks -- they measure where a polygon's
    corners sit, not how far apart its edges are -- and use
    ``grid_um``/``angle_limit_deg`` below instead of ``threshold_dbu``; see
    ``drc.py``'s ``_run_grid_check``/``_run_angle_check``.
    ``"isolated"`` (``Region.isolated_check``, issue #1654) measures spacing
    between *different* polygons of a merged region only -- unlike
    ``"space"`` (``Region.space_check``), it never flags a concave notch
    carved into a single polygon. Use it for a rule transcribed from a
    source rule whose real semantics is "isolated"/"distinct polygon"
    spacing (as opposed to "space", which also bounds intra-polygon
    notches) -- see ``sky130.py``'s ``nwell.space.1`` for a worked example.
    ``threshold_dbu`` is the
    rule's distance threshold expressed in database units of the deck's own
    *nominal* dbu (see each deck module's ``NOMINAL_DBU_UM`` constant — e.g.
    sky130 and gf180mcu are both authored against ``dbu_um = 0.001``, i.e.
    1 nm per unit).

    For ``"enclosing"`` (``layer`` encloses ``other_layer``) and ``"enclosed"``
    (``layer`` is enclosed by ``other_layer``), ``run_drc()`` reports more than
    the raw ``Region.enclosing_check``/``enclosed_check`` edge-pair violations:
    it additionally flags any part of an interacting enclosed shape that
    escapes the enclosing layer entirely (zero overlap, not just insufficient
    margin) under this same rule ``id`` -- see ``docs/cli/drc.md``'s
    "``\"enclosing\"``/``\"enclosed\"`` also catch zero-overlap escapes"
    section and ``drc.py``'s ``_run_check`` (#318).

    ``require_containment`` (issue #2726) opts an ``"enclosing"``/``"enclosed"``
    rule into *required full containment* of its **cut** side -- the
    ``other_layer`` of an ``"enclosing"`` rule, the ``layer`` of an
    ``"enclosed"`` rule. Every merged cut shape, anywhere in the checked cell
    (hierarchy included), must lie entirely inside the merged conductor on the
    other side; whatever escapes is reported under this rule's ``id`` as a
    polygon violation (``cut - conductor``), with no ``interacting`` pre-filter.
    A cut that touches the conductor only by an edge or a sliver, or lies
    wholly outside it (or inside a hole of it), therefore fails. Exact
    boundary coincidence passes. ``threshold_dbu`` still applies as an
    ordinary margin; ``0`` means pure geometric containment. Use it only for
    a *single-population* cut layer (a ``ViaN`` that must always land on both
    adjacent metals); leave it ``False`` (the default) for a cut layer shared
    by disjoint populations (e.g. gf180mcu Contact vs Poly2/Comp), which keeps
    the interaction-scoped #318 behaviour. When cuts exist but the conductor
    layer is absent from the stream, the rule is *checked* against an empty
    conductor (every cut is reported); when no cut shape exists the rule is
    skipped/inapplicable as usual. Valid only on ``"enclosing"``/``"enclosed"``
    rules with an ``other_layer`` and no ``derived_layer``; ``run_drc()`` raises
    :class:`~klayout_tools.drc.DrcError` otherwise.

    ``derived_layer``, when set (issue #345), replaces the *region actually
    checked* on the ``layer``/enclosing side of the rule with a computed
    :class:`DerivedLayer` (a sized/boolean combination of two drawn layers)
    instead of ``layer``'s own raw drawn shapes -- see :class:`DerivedLayer`
    for the derivation and why this exists. ``layer`` remains required and is
    still used for reporting (``violations[].layer``, ``coverage``); it is
    independent of ``derived_layer.base``/``intersect_with``, which name the
    two real drawn layers actually read to compute the checked region.
    ``None`` (the default) means ``layer``'s own raw shapes are checked
    directly, exactly as before this field existed. Which derivation is
    applied is :attr:`DerivedLayer.mode`'s job -- including the
    ``"overlapping"``/``"not_interacting"`` marker-scoped modes (issue
    #1110) that let a ``_LV``/``_MV``-style rule *pair* split one drawn
    layer's polygons by a voltage-domain marker instead of applying one
    column's threshold to all of them.

    ``threshold_dbu`` is **not** used directly against a layout's shapes:
    ``run_drc()`` scales it by the ratio of the deck's ``NOMINAL_DBU_UM`` to
    the layout's actual ``dbu`` before passing it to the ``Region.*_check()``
    primitives, so a deck's rules give identical results regardless of the
    database unit the input stream happens to be written at (see
    ``docs/cli/drc.md``).

    Rule ``id`` values are a stable, public contract once shipped — never
    renumber or repurpose one (see ``docs/cli/drc.md``).

    ``scope`` (issue #566) is a machine-readable identifier for the *official*
    DRM section or rule-id family this rule implements/approximates -- the
    coverage-reporting analogue of the prose citation each rule's own comment
    already carries (see each deck module's per-rule ``# DRM ...`` / official
    rule-id comments). ``run_drc()`` aggregates every non-empty ``scope``
    across a deck's rules into ``coverage.deck_scope`` (deduplicated, sorted),
    so a caller can diff "what this curated deck claims to implement" against
    the source DRM's own table of contents -- a coarser, section-level
    question ``coverage.layers_in_stream_without_rules``'s per-layer view
    cannot answer (see ``docs/cli/drc.md``'s "Coverage" section). For a deck
    that cites numbered DRM sections in its rules' comments (e.g. gf180mcu's
    "7.5 Comp", "7.13 Metaln"), ``scope`` is that section string, shared by
    every rule transcribed from it. For a deck whose source has no such
    section numbering, only a flat official rule-id namespace (e.g. sky130's
    ``sky130.lydrc``/``sky130A_mr.drc``, whose ids are dotted like
    ``"li.1"``/``"m1.2"``), ``scope`` is that namespace's own dotted prefix
    (e.g. ``"li"``, ``"m1"``) shared by every rule in the same family --
    the "rule-id prefix" alternative the issue's own proposal names. Defaults
    to ``""`` (unscoped), which contributes nothing to ``coverage.deck_scope``
    -- a deck rule that predates this field, or that intentionally declines to
    claim a specific DRM scope, extracts exactly as it did before the field
    existed.

    ``provenance`` (issue #747) is a machine-readable, **per-rule** citation
    of the exact upstream source line/table row this rule was transcribed
    from -- see :class:`RuleProvenance` for its fields and how it differs
    from the deck-level-aggregated ``scope`` above (short version: ``scope``
    answers "what DRM section/rule-id family does this deck claim," shared
    by many rules; ``provenance`` answers "what exact upstream file and
    official rule id did *this* rule come from," unique per rule).
    ``None`` (the default) means no structured provenance has been
    backfilled for this rule yet -- the prose citation in its own inline
    comment remains the only record, exactly as for every rule before this
    field existed.

    ``area_min_dbu2`` / ``area_max_dbu2`` (issue #812) are the fields
    ``check="area"`` uses instead of ``threshold_dbu``: a minimum and/or
    maximum polygon area, in **square** database units of the deck's own
    nominal dbu -- ``run_drc()`` rescales each by ``dbu_scale ** 2`` (the
    *squared* ratio of the deck's nominal dbu to the layout's actual one),
    not the plain ``dbu_scale`` every linear-distance check above uses,
    since an area scales with the square of a linear rescaling. Both
    default to ``None``; at least one must be set for an ``"area"`` rule
    (``run_drc()`` raises :class:`~klayout_tools.drc.DrcError` for an
    ``"area"`` rule with neither set) -- a rule needs only a floor (a
    minimum-area rule, e.g. sky130's ``met2.area.1``, transcribed from
    ``m2.6``), only a ceiling, or both. Driven by
    ``klayout.db.Region.with_area(min_area, max_area, inverse=True)``,
    which returns exactly the *violating*
    polygons (area below the minimum or at/above the maximum) directly --
    unlike every check above, this returns a ``Region`` (polygons), not an
    ``EdgePairs`` collection, so ``run_drc()`` reports each returned polygon
    as its own violation the same way it already does for the
    ``"enclosing"``/``"enclosed"`` zero-overlap-escape term (see
    ``_run_area_check`` in ``drc.py``). ``other_layer``/``derived_layer`` are
    unused for this check kind.

    ``density_window_um`` / ``density_min`` / ``density_max`` (issue #812)
    are the fields ``check="density"`` uses: no native ``Region`` primitive
    computes windowed area density, so ``run_drc()`` tiles the checked
    layer's own drawn extent (``region.bbox()``, *not* a chip-boundary
    layer this engine has no concept of) into non-overlapping
    ``density_window_um`` x ``density_window_um`` squares and flags any
    window whose covered-area fraction falls outside ``[density_min,
    density_max]`` (either bound may be ``None`` for "no floor"/"no
    ceiling", but at least one must be set). ``density_window_um`` is a real
    physical window size in micrometres, rescaled against the *layout's
    own* ``dbu`` directly at run time -- like ``DerivedLayer.sized_by_um``,
    **not** like ``threshold_dbu``, which is expressed in the deck's
    nominal dbu and rescaled by ``dbu_scale`` instead (there is no natural
    "nominal window size" the way there is a nominal distance threshold).
    A remainder narrower than one whole window at the checked extent's
    right/top edge is not tiled and so not checked -- a documented
    approximation of this first cut, not a defect (see ``docs/cli/drc.md``).
    All three default to ``None``; ``other_layer``/``derived_layer`` are
    unused for this check kind.

    ``threshold_max_dbu`` (issue #2370) is the *upper* bound half of a
    fixed-size rule, in database units of the deck's own nominal dbu exactly
    like ``threshold_dbu`` (and rescaled by the same ``dbu_scale``). It is
    valid only alongside ``check="width"``, where it turns the rule from a
    minimum-width lower bound into a full min **and** max size check: a
    published rule such as gf180mcu's ``CO.1`` ("min/max contact size ->
    0.22um", i.e. contacts are a *fixed* 0.22 x 0.22 um square) or ``Vn.1``
    (a fixed 0.26 x 0.26 um via) bounds the feature from both sides, and
    ``Region.width_check`` -- like every other ``Region`` check primitive --
    only ever reports the lower-bound half. ``None`` (the default) leaves a
    ``"width"`` rule exactly as it behaved before this field existed:
    minimum-width only.

    The max half is **not** an inverted ``width_check``. A "width" in the
    ``width_check`` sense is the distance between two *facing* edges, so
    an over-long contact bar (0.22 x 2 um) has a perfectly legal 0.22 um
    facing-edge width and would never be reported by any width-based upper
    bound -- yet it is exactly the geometry ``CO.1`` forbids. ``run_drc()``
    therefore measures the max half as a **bounding-box size** bound:
    ``klayout.db.Region.with_bbox_max(0, max + 1, inverse=True)``, which
    returns the merged polygons whose *larger* bounding-box dimension
    exceeds ``threshold_max_dbu`` -- flagging an oversized square, an
    elongated bar, and an L-shaped cut alike, while leaving a
    correctly-sized square (whose bbox is exactly the threshold) clean. Like
    ``"area"``'s ``with_area``, this returns violating *polygons* (a
    ``Region``), not an ``EdgePairs`` collection, so each is reported as its
    own violation under the same rule ``id`` and the same ``check: "width"``
    string -- additive to the minimum-width edge pairs, the same way the
    ``"enclosing"``/``"enclosed"`` zero-overlap escape term already is (see
    ``_run_width_max_check`` in ``drc.py``). Because the bound is measured
    on the bounding box, a *non-axis-aligned* cut (e.g. a 45-degree-rotated
    square) is measured by its axis-aligned envelope and so is flagged
    somewhat conservatively -- acceptable for the fixed-size cut/via rules
    this exists for, whose official geometry is an axis-aligned square.

    ``antenna_ratio_max`` (issue #812) is the field ``check="antenna"``
    uses: the maximum allowed ratio of ``layer``'s total merged area to
    ``other_layer``'s (required, like every other two-layer check kind).
    **This is a flat, connectivity-free geometric approximation of a real
    antenna/process-antenna-area-ratio (PAAR) check, not a net-aware one** --
    a true antenna check accumulates conductor area *per net*, reset at
    each via level, which this purely-geometric engine cannot compute
    without net extraction (a different code path, ``extract.py``, not
    wired into ``drc.py``). Instead, ``run_drc()`` sums ``layer``'s and
    ``other_layer``'s merged area across the *whole checked cell* (no
    per-net split) and reports a single flat violation when their ratio
    exceeds ``antenna_ratio_max`` -- or when ``layer`` has nonzero area but
    ``other_layer`` has none at all (an undefined/infinite ratio, always a
    violation when ``layer`` is present). Mirrors how ``"enclosing"``/
    ``"enclosed"`` already document their own approximation of the official
    rule they check (see this class's docstring above and
    ``docs/cli/drc.md``); a future golden-pair author must not assume more
    precision than this primitive actually has. Defaults to ``None``, and
    ``run_drc()`` raises :class:`~klayout_tools.drc.DrcError` for an
    ``"antenna"`` rule that leaves it unset or omits ``other_layer``.

    ``grid_um`` (issue #2642) is the field ``check="ongrid"`` uses: the
    manufacturing grid, in micrometres, every vertex of ``layer``'s merged
    polygons must land on. It is a **real physical distance rescaled
    against the layout's own ``dbu``** at run time -- like
    ``density_window_um``/:attr:`DerivedLayer.sized_by_um`, and *unlike*
    ``threshold_dbu``, which is expressed in the deck's nominal dbu and
    rescaled by ``dbu_scale``: a manufacturing grid is a property of the
    process, not of the database unit a stream happens to be written at, so
    0.005um must stay 0.005um whatever ``layout.dbu`` is. Driven by
    ``klayout.db.Region.grid_check(gx, gy)`` -- the exact primitive the PDK
    decks' own DRC-DSL ``layer.ongrid(g)`` compiles to -- which returns an
    ``EdgePairs`` collection holding one degenerate (single-point) edge pair
    per off-grid vertex, so each off-grid vertex is reported as its own
    violation through the same edge-pair path every distance check already
    uses. ``other_layer``/``derived_layer``/``threshold_dbu`` are unused for
    this check kind (``run_drc()`` raises
    :class:`~klayout_tools.drc.DrcError` for an ``"ongrid"`` rule that
    leaves ``grid_um`` unset or sets it non-positive).

    ``angle_limit_deg`` (issue #2642) is the field ``check="angle"`` uses:
    the minimum *interior* corner angle, in degrees, ``layer``'s merged
    polygons may have. A corner whose interior angle is **strictly less
    than** this value is a violation; a corner exactly at it is legal (so
    ``angle_limit_deg=45.0`` permits a 45-degree chamfer and flags anything
    sharper, and ``90.0`` permits only right-angle-or-blunter corners, i.e.
    rejects every acute corner). Driven by
    ``klayout.db.Region.with_angle(0.0, angle_limit_deg, False)``, which is
    what the PDK decks' own ``layer.with_angle(0 .. limit)`` on a polygon
    layer evaluates: a half-open ``[0, limit)`` interior-angle interval,
    returning an ``EdgePairs`` collection holding the two edges forming each
    offending corner. Note what this is *not*: it is a minimum-angle bound,
    not an "every edge must lie on a multiple of 45 degrees" test -- the
    upstream decks' own rule descriptions say "non 45 degree angle", but the
    primitive they invoke only rejects corners sharper than the limit (a
    30-degree-off-axis edge meeting another at 100 degrees passes). Deck
    authors transcribing such a rule should mirror the source's primitive
    and value, and document the gap rather than "fixing" it here, so the
    curated verdict stays comparable to the PDK deck's own. Unused for every
    other check kind; ``run_drc()`` raises
    :class:`~klayout_tools.drc.DrcError` for an ``"angle"`` rule that leaves
    it unset or outside ``(0, 180]``.

    ``requires_any_layer`` (issue #2634) skips this rule entirely -- the
    same ``coverage.rules_skipped`` path a missing ``layer``/``other_layer``
    already uses -- unless at least one of the listed ``(layer, datatype)``
    pairs has **any** shapes anywhere in the layout stream being checked
    (tested the same way ``layer``'s own presence is: ``Layout.find_layer``
    resolving to a real index, not merely declared-but-empty). It exists for
    a rule whose official DRM scope is *context-gated* by a layer the rule's
    own checked region never reads geometrically -- unlike
    ``derived_layer.intersect_with``, which both reads a second layer's
    shapes *and* feeds them into the checked region, ``requires_any_layer``
    only gates whether the rule applies at all; it contributes nothing to
    ``coverage.deck_layers``/``layers_checked``, since no shape of these
    layers is ever measured.

    The motivating case is gf180mcu's ``pplus.enclosing.comp.1`` (DRM "7.9
    Pplus" rule ``PP.5b``). The official ``rule_decks/pplus.drc`` does not
    apply one flat 0.16um extension requirement to every ``PCOMP`` shape:
    ``PP.5b`` itself only covers ``Pplus`` edges "(1) Inside NWELL (2)
    outside LVPWELL but inside DNWELL" (``pp5b_pplus =
    pp5b_pplus_slct.and(nwell).join(pp5b_pplus_slct.not(lvpwell).and(dnwell))``);
    a ``PCOMP`` shape with **no** ``Nwell``/``Dnwell`` context anywhere falls
    instead under ``PP.5di``/``PP.5dii`` ("outside DNWELL"), whose own
    threshold is only 0.02um unless the shape sits within 0.429um of a pure
    (non-``Dnwell``-overlapping) ``Nwell`` -- the common case for an
    ordinary poly-resistor's P+ substrate tap, which this curated deck has
    no :class:`DerivedLayer` mode to express (it would need a three-input,
    proximity-sized derivation, not a sized/boolean combination of two
    layers). Verified directly: a synthetic ``PCOMP`` tap extended by
    exactly 0.11um of ``Pplus`` (below the flat 0.16um this deck checked
    pre-#2634), with no ``Nwell``/``Dnwell``/``Lvpwell`` drawn anywhere,
    reports **zero** findings against a real, locally fetched
    ``rule_decks/pplus.drc`` (the identical geometry mirrored onto
    ``Nplus``/``NCOMP`` *does* report 4 ``NP.5b`` findings, confirming the
    asymmetry is real: ``NP.5b``'s own two sub-cases -- "(1) inside LVPWELL
    (2) outside Nwell and DNWELL" -- both resolve to the *same* 0.16um
    value, so ``nplus.enclosing.comp.1`` needs no equivalent gate). Setting
    ``requires_any_layer=((21, 0), (12, 0))`` (``Nwell``, ``Dnwell``) on
    ``pplus.enclosing.comp.1`` matches this exactly: a layout that draws
    neither anywhere has no shape this rule's 0.16um threshold can
    correctly apply to, so the whole rule is skipped (not silently
    evaluated at the wrong, stricter threshold) rather than reported
    against an empty-but-still-flat-0.16um checked region. A layout that
    draws *either* layer anywhere keeps today's unchanged (and still
    approximate -- butting edges, ``euclidian`` vs. ``projection``, see the
    rule's own comment in ``decks/gf180mcu.py``) flat 0.16um behaviour,
    since this gate cannot further narrow *which* ``PCOMP`` shapes within
    that layout are in which DRM sub-case without the same unsupported
    three-input derivation.

    Defaults to ``None`` (every pre-existing rule), under which this gate
    never applies and a rule behaves exactly as it did before this field
    existed -- skipped only by the pre-existing missing-``layer``/
    ``other_layer``/``derived_layer`` input checks.

    ``voltage_independent`` (issue #2369) opts a rule out of
    ``run_drc()``'s ``coverage.voltage_domain_warnings`` gate (issue #552/
    #1110, see ``drc.py``). That gate warns when geometry inside an
    unmodelled voltage marker (gf180mcu's ``Dualgate``, 55/0) was checked by
    a rule that ignores the marker -- i.e. one that may have applied the
    *wrong column* of a multi-column DRM table. Set this only for a rule
    whose source table publishes **one** column, so no other column exists
    to have been read by mistake: gf180mcu's ``7.8 Nplus``/``7.9 Pplus``
    implant tables are the first such case (their CSVs carry a single
    ``LAYOUT RULE`` value per row, unlike ``7.5 Comp``'s ``3.3V``/``5V/6V``
    or ``7.7 Poly2``'s ``3.3V``/``5V``/``6V`` pairs), and the PDK's own
    executable ``rule_decks/{nplus,pplus}.drc`` conditions none of those
    rules on ``dualgate``/``v5_xtor`` either. Defaults to ``False`` -- the
    conservative value every pre-existing rule keeps, so the gate's
    behaviour is unchanged for them.
    """

    id: str
    description: str
    layer: tuple[int, int]
    check: str
    threshold_dbu: int
    other_layer: tuple[int, int] | None = None
    derived_layer: DerivedLayer | None = None
    scope: str = ""
    provenance: RuleProvenance | None = None
    area_min_dbu2: int | None = None
    area_max_dbu2: int | None = None
    density_window_um: float | None = None
    density_min: float | None = None
    density_max: float | None = None
    antenna_ratio_max: float | None = None
    grid_um: float | None = None
    angle_limit_deg: float | None = None
    threshold_max_dbu: int | None = None
    voltage_independent: bool = False
    requires_any_layer: tuple[tuple[int, int], ...] | None = None
    require_containment: bool = False


class UnknownDeckError(Exception):
    """Raised by :func:`get_deck` / :func:`get_layer_names` for an unknown deck name."""
