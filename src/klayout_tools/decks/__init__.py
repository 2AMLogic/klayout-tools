"""Registry of DRC rule decks.

A "deck" is our own declarative rule table (see :class:`DrcRule`) that drives
``klayout.db.Region``'s native check primitives (``width_check``,
``space_check``, ``separation_check``, ``enclosing_check``, etc.) — the same
C++ polygon-processing engine that backs KLayout's higher-level DRC-DSL
scripts, invoked directly instead of through the script runner. This keeps
``klt drc`` headless with zero new runtime dependency (see
``docs/cli/drc.md`` for the engine-choice rationale).

Deck data lives in per-PDK sibling modules (``sky130.py``, ``gf180mcu.py``);
this module only aggregates them into a name -> deck registry.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
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

    The derived region is
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
    """

    base: tuple[int, int]
    sized_by_um: float
    intersect_with: tuple[int, int]


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
    ``"width"`` / ``"space"`` / ``"notch"`` are single-layer checks;
    ``"separation"`` / ``"enclosing"`` / ``"enclosed"`` / ``"overlap"`` are
    two-layer checks and require ``other_layer``; ``"area"``, ``"density"``,
    and ``"antenna"`` (issue #812) are a third shape entirely -- see their own
    fields below and ``drc.py``'s ``_run_area_check``/``_run_density_check``/
    ``_run_antenna_check`` for why they cannot reuse ``threshold_dbu`` or the
    ``EdgePairs``-shaped result the checks above return.
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

    ``derived_layer``, when set (issue #345), replaces the *region actually
    checked* on the ``layer``/enclosing side of the rule with a computed
    :class:`DerivedLayer` (a sized/boolean combination of two drawn layers)
    instead of ``layer``'s own raw drawn shapes -- see :class:`DerivedLayer`
    for the derivation and why this exists. ``layer`` remains required and is
    still used for reporting (``violations[].layer``, ``coverage``); it is
    independent of ``derived_layer.base``/``intersect_with``, which name the
    two real drawn layers actually read to compute the checked region.
    ``None`` (the default) means ``layer``'s own raw shapes are checked
    directly, exactly as before this field existed.

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
    minimum-area rule, e.g. sky130's un-transcribed ``m2.6``), only a
    ceiling, or both. Driven by ``klayout.db.Region.with_area(min_area,
    max_area, inverse=True)``, which returns exactly the *violating*
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


class UnknownDeckError(Exception):
    """Raised by :func:`get_deck` / :func:`get_layer_names` for an unknown deck name."""


@dataclass(frozen=True)
class ResistorDevice:
    """One *drawn* precision-resistor device class an :class:`ExtractionDeck`
    can recognise (issue #222).

    A drawn resistor is a deliberately-marked segment of an ordinary
    conductor (poly, diffusion, metal): the designer draws the conductor,
    then covers the resistive part of it with the PDK's resistor-ID
    ("marker") layer. Without this declaration that segment extracts as a
    plain conductor -- i.e. a **short** between the resistor's two heads --
    so a resistor drawn at the wrong length/width passes LVS silently. This
    is the device-recognition analogue of :class:`ExtractionDeck`'s
    ``nfet_class``/``pfet_class`` MOS wiring, driving KLayout's native
    ``klayout.db.DeviceExtractorResistor`` /
    ``...DeviceExtractorResistorWithBulk``.

    Geometry (all fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering):

    - ``body`` -- the drawn conductor layer the resistor lives on. **Must**
      be one of the owning deck's own conductor layers (``poly``,
      ``active``, or one of ``metals``), because the recognised resistor
      body is *subtracted* from that layer's connectivity region: leaving it
      in would short the two terminals together through the conductor and
      defeat the whole point.
    - ``marker`` -- the resistor-ID layer. The resistive segment is
      ``body & marker`` (further narrowed by ``requires``/``excludes``); the
      **terminals** are the rest of that conductor layer (``body`` minus the
      recognised segment), i.e. the contacted heads on either side. This
      mirrors both PDKs' own KLayout LVS decks, which derive the terminal
      layer the same way (sky130's ``poly_con = poly.not(poly_res)``,
      gf180mcu's ``poly2_con = poly2.not(res_mk)``).
    - ``requires`` -- additional layers that must **all** cover the segment
      for it to be this device (e.g. gf180mcu's ``Pplus`` + ``SAB``, which
      are what distinguish a 350 ohm/sq *unsalicided* p+ poly resistor from
      a 7.3 ohm/sq salicided one).
    - ``excludes`` -- layers that disqualify the segment (subtracted from
      it), e.g. sky130's ``rpm``/``urpm`` precision-resistor implant masks,
      which mark the *other*, much-higher-sheet-rho poly resistor flavours.
      A segment excluded here is left as ordinary connected conductor (it
      keeps today's short) rather than extracted with the wrong resistance
      -- a wrong value passing LVS with high confidence is worse than a
      known-unmodelled device.
    - ``terminal`` -- optional override naming a *different* deck conductor
      layer to take the terminals from (defaults to ``body``). Like
      ``body``, it must be one of the deck's own conductor layers.

    ``sheet_rho_ohm_sq`` is the device's sheet resistance in ohms per
    square; KLayout computes ``R = L / W * sheet_rho`` from the recognised
    segment's own geometry -- exactly the role ``area_cap_f_um2`` plays for
    a capacitor device (#512). Every deck that sets it must cite the
    PDK/DRM source it came from inline, the same way the DRC decks cite
    rule ids.

    ``fixed_offset_ohm`` (issue #518) is an optional second coefficient: a
    per-instance resistance added on top of ``L / W * sheet_rho_ohm_sq``,
    for a precision-resistor flavour whose real PDK model composes a
    length-scaling *body* term with a fixed-length *head*/end-effect term
    at the metal-to-resistor contact (sky130's ``res_high_po``'s SPICE
    ``.subckt`` wires an ``rhead`` sub-resistor -- a constant contact
    resistance, independent of the segment's own drawn length -- in series
    with an ``rbody`` sub-resistor that scales with drawn length). KLayout's
    ``kdb.DeviceExtractorResistor``/``...ResistorWithBulk`` constructors
    only take one sheet-rho coefficient, so -- mirroring
    ``CapacitorDevice.perim_cap_f_um``'s post-extraction correction using
    the already-computed ``A``/``P`` device parameters -- this is *not*
    threaded into that constructor call: ``extract.py``'s
    ``_apply_device_parameter_corrections`` reads the already-computed ``R``
    device parameter back and rewrites it in place to ``L / W *
    sheet_rho_ohm_sq + fixed_offset_ohm`` after extraction, before the
    netlist reaches the SPICE writer or ``klt lvs``'s comparer (issue
    #521). Defaults to ``0.0``, which
    reproduces ``R = L / W * sheet_rho_ohm_sq`` only -- today's behaviour,
    bit-for-bit -- for every deck entry that does not set it. A deck that
    transcribes (or, as for sky130's ``res_high_po``, measures via ngspice
    against) its PDK's own composite head-plus-body resistor model should
    set this rather than leave the fixed head/end term silently dropped.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and the model token on the written ``R`` card -- a
    consumer simulating the netlist supplies a matching ``.model``, exactly
    as it already must for the ``nfet``/``pfet`` ``M`` cards).

    ``bulk_to_substrate`` selects ``DeviceExtractorResistorWithBulk`` (a
    third ``W`` terminal tied to the deck's ``substrate_net`` global)
    instead of the plain two-terminal ``DeviceExtractorResistor``, for a
    device whose PDK LVS deck models a bulk terminal (e.g. gf180mcu's
    ``ppolyf_u``, extracted upstream with ``'W' => sub``). It shares the
    same ``W`` terminal region as the NMOS body (``extract.py``'s
    ``nfet_body``), so it inherits that terminal's behaviour: on a deck that
    draws a distinct ``tap`` layer (issue #490), a substrate-tie ring drawn
    outside every ``nwell`` and contacted up to a named net resolves the
    bulk terminal to that real net; only a layout with no such ring falls
    back to the deck's synthesized ``substrate_net`` global.

    ``flavour_option``/``flavours`` (issue #595) declare an optional,
    caller-selectable **sheet-rho flavour set** for a resistor family whose
    members share *identical* recognition geometry -- the same
    ``body``/``marker``/``requires``/``excludes`` region, disambiguated only
    by a build-time deck variable in the official PDK LVS deck this is
    transcribed from (e.g. gf180mcu's ``POLY_RES``, which selects one of
    ``'1k'``/``'2k'``/``'3k'`` for the *same* drawn ``ppolyf_u_h`` region --
    see #299's own note on why wiring all three as separate
    :class:`ResistorDevice` entries would instead recognise the *same* drawn
    shape three times over). This is a different problem from
    ``requires``/``excludes`` (which tell two *geometrically distinguishable*
    flavours apart): there is no drawn layer here a deck could key off, so
    the deck itself keeps recognising and wiring exactly one entry -- this
    entry's own ``name``/``sheet_rho_ohm_sq`` are simply *which* flavour that
    is -- and a caller who knows their design draws a different flavour of
    the same geometry selects it via ``get_extraction_deck``'s
    ``deck_options`` (``klt extract --deck-option <flavour_option>=<value>``).

    ``flavour_option`` is the deck-option key this entry's flavour is chosen
    by (``None``, the default, means this entry has no caller-selectable
    flavour -- today's behaviour, unaffected either way). ``flavours`` is the
    tuple of every :class:`ResistorFlavour` selectable through that key,
    typically including one entry that matches this ``ResistorDevice``'s own
    default ``name``/``sheet_rho_ohm_sq`` (the value used when no
    ``deck_options`` override is given) plus the previously-unmodelled
    siblings. :func:`get_extraction_deck` validates a given ``deck_options``
    value against this list and raises :class:`InvalidDeckOptionError` for an
    unrecognised key or value rather than silently keeping the default or
    guessing -- the same "known-unmodelled short beats a silently wrong
    value" discipline ``excludes`` already applies. A deck entry that leaves
    ``flavours`` at its empty default has no selectable flavour: passing
    ``deck_options`` naming a key no entry declares is itself an
    :class:`InvalidDeckOptionError`, not a silent no-op.

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's ``sheet_rho_ohm_sq`` (and, when
    set, ``fixed_offset_ohm``) was transcribed/measured from -- the
    device-recognition analogue of :class:`DrcRule.provenance` (see
    :class:`RuleProvenance`'s own docstring for the shared type and how it
    generalises beyond DRC). ``None`` (the default) means no structured
    provenance has been backfilled for this entry yet -- the prose citation
    in the deck module's own inline comment remains the only record, exactly
    as for every entry before this field existed.
    """

    name: str
    body: tuple[int, int]
    marker: tuple[int, int]
    sheet_rho_ohm_sq: float
    requires: tuple[tuple[int, int], ...] = ()
    excludes: tuple[tuple[int, int], ...] = ()
    terminal: tuple[int, int] | None = None
    bulk_to_substrate: bool = False
    fixed_offset_ohm: float = 0.0
    flavour_option: str | None = None
    flavours: tuple[ResistorFlavour, ...] = ()
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class ResistorFlavour:
    """One caller-selectable ``(value, name, sheet_rho_ohm_sq)`` choice for a
    :class:`ResistorDevice` whose ``flavours`` field declares more than one
    sheet-rho interpretation of the *same* recognised geometry (issue #595).

    ``value`` is the string a caller passes via ``deck_options`` (e.g.
    gf180mcu's ``"1k"``/``"2k"``/``"3k"``, matching the PDK's own upstream
    ``POLY_RES`` deck-variable spelling so a record can cite it directly).
    ``name``/``sheet_rho_ohm_sq`` are the ``ResistorDevice.name``/
    ``sheet_rho_ohm_sq`` the owning entry is rewritten to when this flavour is
    selected -- everything else about the entry (``body``, ``marker``,
    ``requires``, ``excludes``, ``terminal``, ``bulk_to_substrate``,
    ``fixed_offset_ohm``) is unchanged, since flavour selection never changes
    *which* geometry is recognised, only what device it is reported as.
    """

    value: str
    name: str
    sheet_rho_ohm_sq: float


@dataclass(frozen=True)
class ExtractionDeck:
    """Connectivity + device-extraction rule set for ``klt extract`` (see
    ``docs/design/lvs-extraction-spike.md`` and ``docs/cli/extract.md``).

    A curated, per-PDK-family layer-role table for KLayout's
    ``klayout.db.LayoutToNetlist`` -- the extraction analogue of
    :class:`DrcRule`'s curated width/space/enclosure table, covering a
    two-terminal-well CMOS stack (NMOS/PMOS via one drawn well layer,
    contact/local-interconnect up through the PDK's declared metal stack --
    an arbitrary number of ``metals`` levels joined by ``vias``, e.g.
    gf180mcu's full ``Metal1``-``Metal5``) rather than a full PDK's device
    zoo. All layer fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering.

    ``active``/``poly``/``nwell`` are the device-recognition layers: NMOS is
    ``active - nwell``, PMOS is ``active & nwell`` (KLayout's standard
    "well marks the flip side" MOS-splitting idiom). ``tap`` is an optional,
    *distinct* substrate/well-tie diffusion layer (present when a PDK draws
    taps on a separate layer from transistor active, e.g. sky130's
    ``tap.drawing``; ``None`` when taps share the active layer, e.g.
    gf180mcu's ``Comp`` -- see the family deck's own docstring for which
    case applies and why a blanket "connect the well to every contact
    inside it" rule is wrong (it shorts every transistor terminal in the
    well together; only a genuinely distinct tap region is safe to tie to
    the well directly).

    ``tap_nplus``/``tap_pplus`` (issue #1084) are optional companion fields
    for exactly the ``tap is None`` case above -- a PDK family that draws no
    dedicated tap mask at all, but *does* draw a well/substrate tie as
    ordinary ``active`` diffusion covered by an implant layer the deck
    already declares for other purposes (e.g. gf180mcu's ``Nplus``/
    ``Pplus``, used elsewhere in that deck's diode/resistor/bipolar
    recognition). When ``tap`` is ``None`` and at least one of these is set,
    ``extract.py`` *derives* an equivalent tap region rather than requiring
    a literal drawn tap layer: ``tap_nplus & active & nwell`` is a well tie
    (n+ diffusion tied to the well from *inside* it -- opposite doping from
    an ordinary PMOS source/drain, which is p+ inside the well, so the two
    never collide), ``tap_pplus & active - nwell`` is a substrate tie (p+
    diffusion tied to the substrate from *outside* every well -- opposite
    doping from an ordinary NMOS source/drain, which is n+ outside the
    well). The derived region then feeds the exact same tap/``tap_substrate``
    connectivity mechanism a directly-drawn ``tap`` layer already uses
    (issue #490) -- ties the PMOS body (via ``nwell``) and the NMOS body
    (via the ``substrate_net`` global) to whatever real, routed net the tie
    reaches, instead of a second, parallel mechanism. Either field left
    ``None`` simply contributes nothing to the derived region (a deck may
    declare only one side). Both default to ``None``: a deck that declares
    neither -- every deck as of this field's introduction, until gf180mcu's
    own module sets them -- derives no tap at all, byte-for-byte the same
    behaviour as before these fields existed. Ignored outright when ``tap``
    itself is set: a genuinely distinct drawn tap layer always wins over a
    derived one.

    ``contact`` connects ``active``/``poly``/``tap`` to the first metal
    level. ``metals`` is the ordered metal-stack layer list (index 0 is the
    one ``contact`` lands on); ``metal_labels`` is the matching list of
    optional text/label layers used to name nets/pins (``None`` where a
    level has no label layer in this curated deck); ``vias`` connects
    ``metals[i]`` to ``metals[i + 1]`` and has ``len(metals) - 1`` entries.

    ``well_label`` is an optional text/label layer read directly off
    ``nwell`` (for a PDK that labels the well/body pin on the well layer
    itself, e.g. sky130's ``VPB``) -- distinct from ``metal_labels``, which
    label metal-level pins/power straps.

    ``dummy`` is an optional marker layer declaring drawn-but-non-functional
    "dummy" devices (issue #295, extended to drawn resistors and bipolars in
    #462): matched-pair/array edge fill whose terminals are tied off to a
    rail so the device contributes nothing to the circuit, yet must be
    *drawn* on the real device layers next to the devices it protects. Any
    MOS gate, drawn-resistor body, or bipolar unit lying under a shape on
    this layer is dropped before device recognition (``extract.py``
    subtracts it from the NMOS/PMOS gate regions, each drawn resistor's
    candidate body, and each bipolar's base region alike), so the dummy
    never appears in the extracted netlist and ``klt lvs`` no longer reports
    a spurious ``device.unmatched`` for it -- while the dummy's remaining
    geometry still extracts as ordinary interconnect (it ties to the rail as
    drawn). ``None`` (the default) is fully backward-compatible: a deck that
    declares no ``dummy`` layer extracts exactly as it did before the field
    existed. Whether a given PDK draws a native dummy-marker layer is left
    to the deck author to declare.

    ``poly_label`` is an optional text/label layer read directly off ``poly``
    (mirroring ``well_label``'s "label a pin on the drawn layer itself"
    pattern) so a gate/poly node can be named without a metal landing pad on
    it -- a device gate that ``klt gen`` draws as bare poly (no contact/metal,
    see ``gen.py``'s ``_mos_unit_layout``) still has no ``metals[]`` shape for
    a ``metal_labels[]`` text to attach to, so a poly-level label layer is the
    only way ``klt gen-compose``'s ``pins[]`` can promote such a port to a
    named ``.SUBCKT`` pin (#210). ``None`` where a family's curated deck
    declares no poly-label convention.

    ``nfet_class``/``pfet_class`` name the extracted ``DeviceClassMOS4Transistor``
    device classes (``devices[].class`` in the JSON response). ``substrate_net``
    is the global net name (KLayout ``connect_global``) the NMOS body
    terminal falls back to when a deck draws a distinct ``tap`` layer but no
    tap shape ends up outside every ``nwell`` in a given layout, or when
    ``tap`` is ``None`` entirely (e.g. gf180mcu's shared ``Comp`` layer, no
    per-purpose split possible). When a deck *does* declare ``tap`` and a
    layout draws a substrate-tie ring on it (outside every ``nwell``,
    contacted up to a named net -- issue #490), ``extract.py`` wires that
    real geometry to the NMOS body terminal instead, so the body terminal
    resolves to the ring's own real net rather than this synthesized global
    -- see the family deck's docstring for the tap/nwell-containment split
    that makes this possible.

    ``bipolars`` is an optional tuple of :class:`BipolarDevice` entries (empty
    by default) declaring this deck's drawn vertical-BJT device-recognition
    layers -- the resistor/bipolar/capacitor extension point #221's own
    docstring anticipates (see :attr:`device_classes` below). Empty for a
    deck with no curated bipolar recognition; non-empty decks may declare
    more than one entry (e.g. distinct NPN and PNP device families).

    ``capacitors`` is an optional tuple of :class:`CapacitorDevice` entries
    (empty by default) declaring this deck's drawn MiM (Metal-Insulator-Metal)
    capacitor device-recognition layers (issue #225), the capacitor sibling of
    ``bipolars`` above. Empty for a deck with no curated capacitor
    recognition; non-empty decks may declare more than one entry (e.g.
    sky130's two independent MiM stacks, one per metal level it is drawn on).

    ``resistors`` declares the deck's *drawn* precision-resistor device
    classes (see :class:`ResistorDevice`), each recognised by KLayout's
    ``DeviceExtractorResistor``/``...WithBulk``. Optional and empty by
    default: a deck that declares none extracts exactly as it did before the
    field existed (#222), and a conductor that carries no declared resistor
    marker is never reclassified as a resistor.

    ``diodes`` is an optional tuple of :class:`DiodeDevice` entries (empty by
    default) declaring this deck's drawn junction-diode device-recognition
    layers (issue #542), the diode sibling of ``bipolars`` above -- what lets
    a discrete PN/ESD-clamp diode extract as a ``D`` element instead of
    vanishing from the netlist as unrecognised diffusion geometry. Empty for
    a deck with no curated diode recognition; non-empty decks may declare
    more than one entry (e.g. gf180mcu's n+/p-substrate and p+/Nwell
    junction flavours).

    ``nfet_provenance``/``pfet_provenance`` (issue #868) are machine-readable
    citations of the exact upstream PDK-LVS-deck source line each MOS
    recognition rule was transcribed from -- the device-recognition analogue
    of :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring), applied to the two MOS device classes this deck *always*
    recognises via its own ``active``/``poly``/``nwell`` fields above. Unlike
    ``bipolars``/``capacitors``/``resistors``/``diodes``, MOS recognition has
    no per-entry list to attach a ``provenance`` field to (a deck declares
    exactly one NMOS and one PMOS recognition rule, not a variable-length
    collection), so these two citations live directly on the deck instead.
    Both default to ``None`` -- no structured provenance backfilled yet, the
    same "prose comment remains the record" default every other
    ``provenance`` field in this module carries.
    """

    active: tuple[int, int]
    poly: tuple[int, int]
    nwell: tuple[int, int]
    contact: tuple[int, int]
    metals: tuple[tuple[int, int], ...]
    tap: tuple[int, int] | None = None
    tap_nplus: tuple[int, int] | None = None
    tap_pplus: tuple[int, int] | None = None
    well_label: tuple[int, int] | None = None
    poly_label: tuple[int, int] | None = None
    dummy: tuple[int, int] | None = None
    metal_labels: tuple[tuple[int, int] | None, ...] = ()
    vias: tuple[tuple[int, int], ...] = ()
    nfet_class: str = "nfet"
    pfet_class: str = "pfet"
    substrate_net: str = "vsubs"
    bipolars: tuple[BipolarDevice, ...] = ()
    capacitors: tuple[CapacitorDevice, ...] = ()
    resistors: tuple[ResistorDevice, ...] = ()
    diodes: tuple[DiodeDevice, ...] = ()
    nfet_provenance: RuleProvenance | None = None
    pfet_provenance: RuleProvenance | None = None

    @property
    def device_classes(self) -> tuple[str, ...]:
        """The device-class *roles* (not the ``devices[].class`` label
        strings ``nfet_class``/``pfet_class``/``BipolarDevice.class_name``/
        ``CapacitorDevice.name`` provide) this deck is structurally capable
        of recognising -- independent of whether a given layout actually
        contains any devices of that class (see ``device_counts`` in
        ``docs/cli/extract.md`` for the "what was found" counterpart of this
        "what can be found" declaration).

        Every registered deck extracts two-terminal-well MOS (``"nfet"``,
        ``"pfet"``); a deck that also declares one or more ``bipolars``
        entries (#223) appends each entry's ``class_name`` (in declaration
        order, deduplicated), and a deck that declares one or more
        ``capacitors`` entries (#225) likewise appends each entry's ``name``
        after that. Finally, a deck that declares one or more ``resistors``
        entries (#222) appends the ``"resistor"`` role after those -- a single
        role token regardless of how many drawn-resistor device classes the
        deck declares. Last, a deck that declares one or more ``diodes``
        entries (#542) appends each entry's ``name`` (in declaration order,
        deduplicated) after that -- one token per declared junction-diode
        flavour, matching how ``bipolars``/``capacitors`` name theirs.
        """
        classes = ["nfet", "pfet"]
        for bipolar in self.bipolars:
            if bipolar.class_name not in classes:
                classes.append(bipolar.class_name)
        for capacitor in self.capacitors:
            if capacitor.name not in classes:
                classes.append(capacitor.name)
        if self.resistors and "resistor" not in classes:
            classes.append("resistor")
        for diode in self.diodes:
            if diode.name not in classes:
                classes.append(diode.name)
        return tuple(classes)

    @property
    def merge_layers(self) -> frozenset[tuple[int, int]]:
        """The ``(layer, datatype)`` pairs whose shapes actually *merge* two
        nets together -- exactly ``metals`` plus ``vias``, this deck's
        net-connectivity stack (issue #619).

        This is a strict subset of :attr:`connectivity_layers`: a layer can
        be *read* by extraction (e.g. a ``capacitors[].bottom_plate`` device-
        recognition role) without ever being one of these merge levels -- see
        :attr:`device_recognition_only_layers` for exactly that gap, the one
        that let sky130's own met3/met4 (read as MiM-cap bottom plates, but
        not yet a ``metals`` level) hide a routing-connectivity gap from
        ``ignored_layers`` before this deck's ``metals`` stack reached them
        too.
        """
        return frozenset(self.metals) | frozenset(self.vias)

    @property
    def device_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """Every ``(layer, datatype)`` this deck reads for
        ``bipolars``/``capacitors``/``resistors``/``diodes`` device
        recognition -- base/emitter/marker/collector; plate +
        requires/excludes; body/marker/requires/excludes plus optional
        terminal; anode/cathode/marker + requires/excludes -- regardless of
        whether the same layer is also one of this deck's ``metals``/``vias``
        connectivity levels (see :attr:`merge_layers`). ``None`` entries (an
        absent optional layer) are skipped.

        A layer can legitimately serve both roles at once (sky130's met3/met4
        are simultaneously a ``metals`` connectivity level and a
        ``capacitors[].bottom_plate`` -- issue #619); this property reports
        the device-recognition role on its own, independent of connectivity
        status, so :attr:`device_recognition_only_layers` can subtract
        :attr:`merge_layers` from it to find the layers that are read but
        never merge a net.
        """
        layers: set[tuple[int, int]] = set()
        for bipolar in self.bipolars:
            layers.add(bipolar.base)
            layers.add(bipolar.emitter)
            layers.add(bipolar.marker)
            layers.update(bipolar.emitter_requires)
            layers.update(bipolar.emitter_excludes)
            if bipolar.collector is not None:
                layers.add(bipolar.collector)
        for capacitor in self.capacitors:
            layers.add(capacitor.top_plate)
            layers.add(capacitor.bottom_plate)
            layers.update(capacitor.top_plate_requires)
            layers.update(capacitor.top_plate_excludes)
            layers.update(capacitor.bottom_plate_requires)
            layers.update(capacitor.bottom_plate_excludes)
            if capacitor.top_plate_via is not None:
                layers.add(capacitor.top_plate_via)
        for resistor in self.resistors:
            layers.add(resistor.body)
            layers.add(resistor.marker)
            layers.update(resistor.requires)
            layers.update(resistor.excludes)
            if resistor.terminal is not None:
                layers.add(resistor.terminal)
        for diode in self.diodes:
            layers.add(diode.marker)
            layers.update(diode.anode_requires)
            layers.update(diode.anode_excludes)
            layers.update(diode.cathode_requires)
            layers.update(diode.cathode_excludes)
            if diode.anode is not None:
                layers.add(diode.anode)
            if diode.cathode is not None:
                layers.add(diode.cathode)
        return frozenset(layers)

    @property
    def _structural_recognition_layers(self) -> frozenset[tuple[int, int]]:
        """The MOS-core and label layers this deck reads for a role other
        than ``metals``/``vias`` connectivity or bipolar/capacitor/resistor/
        diode device recognition -- ``active``/``poly``/``nwell``/
        ``contact``, the optional ``tap``/``tap_nplus``/``tap_pplus``/
        ``well_label``/``poly_label``/``dummy``, and every ``metal_labels``
        entry. ``None`` optionals are skipped.

        Subtracted from :attr:`device_recognition_only_layers` (issue #619):
        a bipolar device can legitimately reuse the deck's own MOS-core
        layers as its recognition geometry (sky130's vertical PNP is built
        from the *same* ``nwell``/``diff`` layers as an ordinary MOS body/
        active region -- see ``sky130.py``'s ``bipolars`` comment), so
        without this exclusion, *any* routine PMOS/NMOS layout -- with no
        bipolar device drawn at all -- would trigger a spurious "device
        recognition only" report from the coincidental layer-number overlap
        alone, defeating this diagnostic's only-fire-on-a-real-gap intent.
        """
        layers: set[tuple[int, int]] = {
            self.active,
            self.poly,
            self.nwell,
            self.contact,
        }
        for optional in (
            self.tap,
            self.tap_nplus,
            self.tap_pplus,
            self.well_label,
            self.poly_label,
            self.dummy,
        ):
            if optional is not None:
                layers.add(optional)
        layers.update(label for label in self.metal_labels if label is not None)
        return frozenset(layers)

    @property
    def device_recognition_only_layers(self) -> frozenset[tuple[int, int]]:
        """:attr:`device_recognition_layers` minus :attr:`merge_layers` and
        minus :attr:`_structural_recognition_layers` -- layers this deck
        reads *only* for a bipolar/capacitor/resistor/diode device-
        recognition role that plays no other connectivity or MOS-core role
        (issue #619).

        Consumed by ``klt extract`` to compute the response's
        ``device_recognition_only_layers`` field, the "read but not merged"
        counterpart to ``ignored_layers``'s "never read at all". Without this
        distinction, a layer that is read for device recognition looks
        identical to a genuine connectivity level in
        :attr:`connectivity_layers` -- ``ignored_layers`` alone cannot tell
        "this layer is fully ignored" from "this layer is read, but shapes on
        it never merge two nets" -- exactly the gap that let sky130's met3/
        met4 (drawn as MiM-cap bottom plates) hide a routing-connectivity gap
        from ``ignored_layers`` with a clean-looking (but wrong) empty
        report, before this deck's ``metals`` stack grew to cover them too.

        The :attr:`_structural_recognition_layers` subtraction keeps this
        low-noise: a bipolar device's ``base``/``emitter`` can coincide with
        the deck's own MOS ``nwell``/``active`` layers (sky130's vertical
        PNP does exactly this), and those layers were never candidates for a
        routing-connectivity gap in the first place -- they are structural
        MOS-recognition geometry, not metal.
        """
        return (
            self.device_recognition_layers
            - self.merge_layers
            - self._structural_recognition_layers
        )

    @property
    def connectivity_layers(self) -> frozenset[tuple[int, int]]:
        """Every ``(layer, datatype)`` this deck actually reads during
        extraction -- the device-recognition, connectivity, and label layers
        ``extract.py``'s ``_extract_netlist`` loads a ``Region``/``Texts`` for.

        Consumed by ``klt extract`` to compute the response's
        ``ignored_layers`` field (issue #220): shapes drawn on a layer *not*
        in this set are invisible to the connectivity graph, so a block routed
        on such a layer silently extracts as disconnected nets. Reporting the
        set difference against what the stream actually carries turns that
        silent mis-extraction into a diagnostic (the extraction-side analogue
        of ``klt drc``'s ``coverage.layers_in_stream_without_rules``).

        Includes the MOS-recognition layers (``active``/``poly``/``nwell``/
        ``contact``, plus optional ``tap``/``tap_nplus``/``tap_pplus``), the
        ``metals``/``vias`` stack
        (:attr:`merge_layers`) and every label layer (``well_label``/
        ``poly_label``/``metal_labels``), and each ``bipolars``/
        ``capacitors``/``resistors``/``diodes`` entry's own recognition
        layers (:attr:`device_recognition_layers`). ``None`` entries (an
        absent optional layer) are skipped.

        Note this does *not* distinguish a ``metals``/``vias`` connectivity
        level from a layer read for device recognition only -- see
        :attr:`device_recognition_only_layers` for that distinction (issue
        #619).
        """
        layers: set[tuple[int, int]] = {
            self.active,
            self.poly,
            self.nwell,
            self.contact,
        }
        for optional in (
            self.tap,
            self.tap_nplus,
            self.tap_pplus,
            self.well_label,
            self.poly_label,
            self.dummy,
        ):
            if optional is not None:
                layers.add(optional)
        layers.update(self.merge_layers)
        layers.update(label for label in self.metal_labels if label is not None)
        layers.update(self.device_recognition_layers)
        return frozenset(layers)


@dataclass(frozen=True)
class BipolarDevice:
    """One drawn vertical-BJT device-recognition entry for an
    :class:`ExtractionDeck`'s optional ``bipolars`` field (issue #223),
    consumed by ``extract.py``'s ``kdb.DeviceExtractorBJT3Transistor``
    wiring -- the bipolar analogue of :class:`ExtractionDeck`'s own
    ``active``/``poly``/``nwell`` MOS-recognition layers.

    ``base``/``emitter`` reuse the *same* curated layers the deck already
    declares for MOS recognition (typically ``nwell`` and ``active``
    respectively -- a vertical PNP/NPN's base/emitter are drawn on the same
    physical well/diffusion masks an ordinary MOS transistor uses, just in a
    different geometric arrangement) rather than introducing dedicated
    bipolar-only masks this curated deck does not otherwise model.

    ``marker`` is the PDK's dedicated bipolar device-recognition mark layer
    (drawn by the bipolar device library cell over itself; not consumed for
    connectivity otherwise, e.g. sky130's ``pnp.drawing`` 82/44 or
    gf180mcu's ``DRC_BJT`` 127/5). It disambiguates "this specific patch of
    well/diffusion is a real bipolar device" from the many unrelated
    nwell/diffusion regions a layout draws for ordinary PMOS/tap purposes --
    ``extract.py`` intersects ``base`` with ``marker`` before extraction, so
    only nwell area actually inside a marked device cell becomes a base
    region, and only diffusion inside *that* scoped base region becomes an
    emitter; an ordinary PMOS-only nwell drawn elsewhere in the layout is
    never misrecognised as a bipolar base.

    ``collector`` is ``None`` when the PDK's vertical bipolar has no drawn
    collector layer of its own (collector formed by the substrate -- true
    for both curated decks this issue populates). KLayout's
    ``DeviceExtractorBJT3Transistor`` handles that case itself: an empty
    ``C`` input makes it output the base region's own footprint onto the
    collector terminal, which ``extract.py``'s wiring then ties to the
    deck's ``substrate_net`` global -- the same ``connect_global`` pattern
    :class:`ExtractionDeck`'s NMOS body/``substrate_net`` wiring already
    uses. When a future deck's bipolar has a genuinely distinct drawn
    collector layer (e.g. a lateral device), set this instead.

    ``emitter_requires``/``emitter_excludes`` narrow the recognised emitter
    region the same requires/excludes idiom :class:`ResistorDevice` (#222)
    and :class:`CapacitorDevice` (#225) already use: after ``extract.py``
    scopes the emitter to diffusion inside the marked base
    (``emitter & base & marker``), every layer in ``emitter_requires`` must
    *also* cover it (intersected in) and every layer in ``emitter_excludes``
    is subtracted. This exists to disambiguate a genuine emitter diffusion
    from a **base-contact ring** drawn on the *same* diffusion layer inside
    the *same* well and *same* device mark (issue #302): without narrowing,
    that ring is a second shape on the emitter layer inside the base, so
    ``DeviceExtractorBJT3Transistor`` recognises it as a second, artefact
    device sharing the one base net (its "emitter" is really the base tie).
    A deck that models the implant masks can positively identify the emitter
    (e.g. gf180mcu's p+ emitter has ``Pplus`` and the n+ base tie has
    ``Nplus``, so ``emitter_excludes=(Nplus,)`` drops the ring). A deck that
    models *no* implant layers (e.g. sky130's curated deck) has no such
    disambiguator for a ring drawn on the literal emitter layer -- a
    documented residual limitation, see that deck's docstring. Both default
    to ``()`` (no narrowing), so a deck that declares neither extracts
    exactly as it did before the fields existed.

    ``class_name`` names the extracted ``DeviceClassBJT3Transistor`` device
    class (``devices[].class`` in the JSON response, and one of the values
    :attr:`ExtractionDeck.device_classes` reports for a deck that declares
    this entry).

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from -- the device-recognition analogue of
    :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring). ``None`` (the default) means no structured provenance has
    been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    base: tuple[int, int]
    emitter: tuple[int, int]
    marker: tuple[int, int]
    collector: tuple[int, int] | None = None
    emitter_requires: tuple[tuple[int, int], ...] = ()
    emitter_excludes: tuple[tuple[int, int], ...] = ()
    class_name: str = "bjt"
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class CapacitorDevice:
    """One drawn MiM (Metal-Insulator-Metal) capacitor device-recognition
    entry for an :class:`ExtractionDeck`'s optional ``capacitors`` field
    (issue #225), consumed by ``extract.py``'s ``kdb.DeviceExtractorCapacitor``
    wiring -- the capacitor sibling of :class:`BipolarDevice`.

    A MiM cap is two conductor plates separated by a thin dielectric, drawn
    as *two independent layers* rather than one marked-up conductor the way
    :class:`BipolarDevice` reuses the deck's own MOS layers: ``top_plate`` is
    the PDK's purpose-drawn top-plate layer (e.g. gf180mcu's ``FuseTop``,
    sky130's ``capm``/``capm2`` "MiM cap plate" mark layers) and
    ``bottom_plate`` is the conductor the bottom plate is drawn on (e.g.
    gf180mcu's ``Metal4``, sky130's ``met3``/``met4``).

    Unlike :class:`ResistorDevice`-shaped fields elsewhere in this codebase
    (there is no such class yet -- see #222), ``bottom_plate`` does **not**
    need to be one of the owning deck's own ``metals``. When it *is* (e.g.
    gf180mcu's bottom plate is ``Metal4``, which the deck now tracks as part
    of its full Metal1-Metal5 stack since #220), ``extract.py`` ties the
    recognised bottom-plate region directly into that ``metals[]`` node
    (issue #314), so ordinary contact/via/metal routing to that metal reaches
    the capacitor's bottom terminal. When it is *not* (e.g. sky130's
    ``met3``/``met4`` bottom plates, which sit above this curated deck's
    ``metals`` stack -- ``li1``/``met1`` only), the bottom plate stays its
    own new, self-connected connectivity node, isolated from the rest of the
    deck's graph -- see "Known limitation" below.

    Geometry (all layer fields are ``(layer, datatype)`` pairs):

    - ``top_plate`` -- the purpose-drawn top-plate conductor. Narrowed by
      ``top_plate_requires`` (all of which must also cover it) and
      ``top_plate_excludes`` (subtracted), the same requires/excludes idiom
      :class:`ResistorDevice` uses (#222) -- e.g. gf180mcu's ``FuseTop``
      additionally requires both ``CAP_MK``/``MIM_L_MK`` and excludes
      ``efuse_mk``/``plfuse``, mirroring the PDK's own official derivation.
    - ``bottom_plate`` -- the conductor the bottom plate is drawn on, with
      its own optional ``bottom_plate_requires``/``bottom_plate_excludes``.
      When this layer matches one of the owning deck's own ``metals`` (by
      ``(layer, datatype)`` value), ``extract.py`` connects the recognised
      bottom-plate region into that ``metals[]`` connectivity node (#314) --
      see "Known limitation" below for when it does not match.
    - ``bottom_plate_oversize_um`` -- when nonzero, the bottom plate is not
      the raw (filtered) ``bottom_plate`` region but the PDK's derived
      "virtual bottom plate": the subset of that region already touching the
      *unsized* ``top_plate`` region, clipped to ``top_plate`` sized
      (oversized) by this many micrometres -- gf180mcu's own official
      derivation for its MiM stack (``FuseTop.sized(1.06um) &
      Metal4.interacting(FuseTop)``, the gf180mcu DRM's "10.4.2 MIM Option
      B" footnote 1). Zero (the default) means the bottom plate is simply
      the filtered ``bottom_plate`` region, unfiltered by any sizing step --
      sky130's own official derivation, where the bottom plate is "whatever
      conductor the purpose-built top-plate mark layer sits over," no
      virtual-plate derivation needed.
    - ``top_plate_via``/``top_plate_via_metal`` -- optional declaration of
      the via layer that lands directly on the top plate and the ``metals[]``
      layer it connects up to (#314). When both are set, ``extract.py`` reads
      the raw ``top_plate_via`` layer as its own region, connects it to the
      recognised top-plate region wherever the two geometrically touch, and
      connects it on to the ``metals[]`` entry matching
      ``top_plate_via_metal`` -- the top-plate analogue of ``bottom_plate``
      matching a tracked metal above. Both default to ``None`` (no top-plate
      connectivity beyond the plate's own self-merge); ``top_plate_via_metal``
      must be one of the deck's own ``metals`` when ``top_plate_via`` is set
      (``extract.py`` raises :class:`~klayout_tools.extract.ExtractError` for
      a deck that sets one without the other, or sets
      ``top_plate_via_metal`` to a layer the deck does not track -- a
      deck-authoring mistake, the same class of check
      :func:`~klayout_tools.extract._resolve_resistors` already performs for
      a resistor's ``body``/``terminal`` layers). Left unset for a PDK/deck
      combination where the top plate's real via either does not exist or
      lands on a metal this curated deck's own ``metals`` stack does not
      track (e.g. gf180mcu's MiM stack sets both -- see ``gf180mcu.py``;
      sky130's two MiM stacks likewise set both as of issue #775, once #619
      extended sky130's own ``metals``/``vias`` to the full li1-met5 stack
      their real ``via3``/``via4`` land on) -- documented, not silently
      claimed as fixed for a deck whose ``metals`` stack genuinely does not
      reach that far. When ``top_plate_via`` *is* one of the owning deck's own
      ``vias`` layers, ``extract.py`` also excludes the geometric overlap
      between the via and this capacitor's recognised ``bottom_plate``
      region from that ``vias[]`` layer before the deck's generic per-layer
      connectivity loop runs (#364) -- otherwise a via placed per the PDK's
      own minimum-overlap rule for it (which requires the bottom plate to
      enclose/overlap the via, not clear it) would be read by that generic
      loop as an ordinary via shorting the two plates together, even though
      the plate wiring above already connects the via to the top plate
      correctly.

    ``area_cap_f_um2`` is the device's capacitance per square micrometre of
    plate *overlap* area, in **Farads**. KLayout's
    ``kdb.DeviceExtractorCapacitor`` computes ``C = A * area_cap`` from the
    two plates' actual geometric overlap -- exactly the role
    ``sheet_rho_ohm_sq`` plays for a resistor device (#222). Every deck that
    sets it must cite the PDK/DRM source it came from inline.

    ``perim_cap_f_um`` (issue #512) is an optional second coefficient: the
    device's *fringe/sidewall* capacitance per micrometre of plate-overlap
    *perimeter*, in Farads, added on top of the area term so the reported
    ``c_f`` becomes ``area_cap_f_um2 * A + perim_cap_f_um * P`` -- ``A``/``P``
    are the same overlap area/perimeter KLayout's own
    ``DeviceClassCapacitor`` already computes and exposes as its ``A``/``P``
    device parameters (``extract.py``'s
    ``_apply_device_parameter_corrections`` reads both back and rewrites
    ``C`` in place after extraction, before the netlist reaches the SPICE
    writer or ``klt lvs``'s comparer -- issue #521; KLayout's
    ``DeviceExtractorCapacitor`` constructor itself takes only one
    coefficient, so this is *not* threaded into that call). Defaults to
    ``0.0``, which reproduces ``C = area_cap_f_um2 * A`` only -- today's
    behaviour, bit-for-bit -- for every deck that does not set it. A MiM
    capacitor's real model is two-term (area *and* perimeter/fringe), the
    same shape :class:`LayerRC` below already uses for
    ``--parasitics``'s substrate capacitance (``cap_area_ff_um2``/
    ``cap_perim_ff_um``); a deck that transcribes its PDK's own two-term MiM
    model card (e.g. gf180mcu's/sky130's own SPICE ``.model`` cards'
    ``c_cox``/``c_capsw`` or ``camimc``/``cpmimc`` coefficients) should set
    this rather than leave the perimeter term silently dropped.

    ``name`` is the extracted device-class name (``devices[].class`` in the
    JSON response, and one of the values :attr:`ExtractionDeck.device_classes`
    reports for a deck that declares this entry).

    Known limitation: a plate whose declared layer does not resolve to one
    of the deck's tracked ``metals[]`` entries (``bottom_plate`` that is not
    one of ``metals``, or a ``top_plate`` with no ``top_plate_via`` declared)
    is still registered as its own new, self-connected connectivity node --
    multiple plate polygons that touch each other merge into one net (e.g. a
    shared bottom plate across several caps), but that plate's net does not
    extend into whatever real routing the deck's metal-stack connectivity
    would otherwise connect it to. The *device* itself (a capacitor of the
    correct value between two correctly-shaped plates) is still correctly
    recognised in every case; only an unwired plate's *net name/connectivity*
    carries this documented approximation -- the same "curated starter
    subset, not the full metal stack" scope guard the rest of this deck
    already carries (see ``docs/cli/extract.md`` -> "Coverage").

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's ``area_cap_f_um2`` (and, when
    set, ``perim_cap_f_um``) was transcribed from -- the device-recognition
    analogue of :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s
    own docstring). ``None`` (the default) means no structured provenance
    has been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    name: str
    top_plate: tuple[int, int]
    bottom_plate: tuple[int, int]
    area_cap_f_um2: float
    top_plate_requires: tuple[tuple[int, int], ...] = ()
    top_plate_excludes: tuple[tuple[int, int], ...] = ()
    bottom_plate_requires: tuple[tuple[int, int], ...] = ()
    bottom_plate_excludes: tuple[tuple[int, int], ...] = ()
    bottom_plate_oversize_um: float = 0.0
    top_plate_via: tuple[int, int] | None = None
    top_plate_via_metal: tuple[int, int] | None = None
    perim_cap_f_um: float = 0.0
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class DiodeDevice:
    """One drawn junction-diode device-recognition entry for an
    :class:`ExtractionDeck`'s optional ``diodes`` field (issue #542),
    consumed by ``extract.py``'s ``kdb.DeviceExtractorDiode`` wiring -- the
    diode sibling of :class:`BipolarDevice`.

    A junction diode is an ordinary doped-diffusion/well overlap the PDK
    marks with a dedicated device-recognition layer, exactly like a vertical
    BJT: without this declaration that geometry is either dropped from the
    netlist entirely (nothing recognises it) or, worse, silently merged into
    surrounding interconnect -- so any diode-based ESD clamp is unverifiable
    by ``klt lvs``. This is the device-recognition analogue of
    :class:`ExtractionDeck`'s ``nfet_class``/``pfet_class`` MOS wiring,
    driving KLayout's native ``klayout.db.DeviceExtractorDiode``: the
    recognised device is the **geometric overlap** of the two terminal
    regions, and its extracted parameters are that overlap's area ``A``
    (square micrometres) and perimeter ``P`` (micrometres) -- reported as
    ``params.area_um2``/``params.perimeter_um`` in the JSON response.

    Deliberately *not* modelled (issue #542's own "Non-goals"): the device's
    saturation-current/area-scaling I-V model. A recognised diode is emitted
    as a schematic-equivalent ``D`` card whose model token is this entry's
    ``name``, the same fidelity level the MOS/BJT recognisers already
    provide -- a consumer simulating the netlist supplies a matching
    ``.model``.

    Geometry (all layer fields are ``(layer, datatype)`` pairs, matching the
    layout's own GDS numbering):

    - ``anode`` -- the p-doped side's drawn layer (e.g. gf180mcu's ``Comp``
      for a p+ diffusion diode, or its ``Nwell`` when the *cathode* is the
      diffusion). ``extract.py`` scopes it to ``anode & marker`` before
      extraction.
    - ``cathode`` -- the n-doped side's drawn layer, scoped the same way.
    - ``marker`` -- the PDK's dedicated diode device-recognition mark layer
      (e.g. gf180mcu's ``diode_mk`` 115/5), drawn by the diode device
      library cell over itself. It is what disambiguates "this patch of
      diffusion inside a well is a real diode device" from the many
      unrelated diffusion/well overlaps an ordinary MOS layout draws --
      ``extract.py`` intersects *both* terminal layers with it before
      extraction, the same guard the bipolar block applies to its base, so
      an ordinary PMOS's p+-in-Nwell source/drain is never misrecognised as
      a diode.
    - ``anode_requires``/``anode_excludes`` and
      ``cathode_requires``/``cathode_excludes`` -- narrow the corresponding
      terminal region with the same requires/excludes idiom
      :class:`ResistorDevice` (#222), :class:`CapacitorDevice` (#225) and
      :class:`BipolarDevice` (#302) already use: every layer in ``requires``
      must *also* cover the region (intersected in) and every layer in
      ``excludes`` is subtracted. This is how a deck tells one junction
      flavour apart from another drawn on the same masks -- e.g. gf180mcu's
      n+/p-substrate ``diode_nd2ps_06v0`` requires ``Nplus`` + ``Dualgate``
      and excludes ``Nwell``, versus the p+/Nwell ``diode_pd2nw_06v0``'s
      ``Pplus`` + ``Dualgate``. All four default to ``()`` (no narrowing).

    Exactly one of ``anode``/``cathode`` may be ``None``, meaning "this
    terminal is formed by the substrate rather than by a drawn layer" -- the
    n+-in-p-substrate case (gf180mcu's ``diode_nd2ps_*``), whose anode is
    the p-substrate the PDK never draws a mask for. That terminal's region
    is then the device's own ``marker`` footprint (narrowed by its
    ``requires``/``excludes`` the same way a declared layer would be, e.g.
    ``anode_excludes=(Nwell, DNWELL)`` to keep the substrate side genuinely
    *outside* every well), and ``extract.py`` ties it to the deck's
    ``substrate_net`` global with ``connect_global`` -- exactly the pattern
    :class:`BipolarDevice`'s collector-less case and
    :class:`ExtractionDeck`'s NMOS body already use. Using the marker
    footprint rather than an empty region is load-bearing:
    ``DeviceExtractorDiode`` forms the device from the *overlap* of its two
    inputs, so an empty input yields no device at all.

    Terminal connectivity is derived from the declared layers rather than
    configured separately: a terminal whose layer is the owning deck's own
    ``nwell`` joins that well's connectivity node (so a well tap/label names
    it), a substrate-formed (``None``) terminal joins the deck's
    ``substrate_net`` global, and any other terminal layer (i.e. a
    diffusion) joins the deck's ``contact`` node, so ordinary
    contact/metal routing to the diffusion names it. This mirrors the
    bipolar block's fixed base->``nwell`` / emitter->``contact`` wiring.

    ``name`` is the extracted ``DeviceClassDiode`` device class
    (``devices[].class`` in the JSON response, the model token on the
    written ``D`` card, and one of the values
    :attr:`ExtractionDeck.device_classes` reports for a deck that declares
    this entry).

    ``provenance`` (issue #868) is a machine-readable citation of the exact
    upstream PDK-LVS-deck source this entry's recognition geometry was
    transcribed from -- the device-recognition analogue of
    :class:`DrcRule.provenance` (see :class:`RuleProvenance`'s own
    docstring). ``None`` (the default) means no structured provenance has
    been backfilled for this entry yet -- the prose citation in the deck
    module's own inline comment remains the only record, exactly as for
    every entry before this field existed.
    """

    name: str
    marker: tuple[int, int]
    anode: tuple[int, int] | None = None
    cathode: tuple[int, int] | None = None
    anode_requires: tuple[tuple[int, int], ...] = ()
    anode_excludes: tuple[tuple[int, int], ...] = ()
    cathode_requires: tuple[tuple[int, int], ...] = ()
    cathode_excludes: tuple[tuple[int, int], ...] = ()
    provenance: RuleProvenance | None = None


@dataclass(frozen=True)
class LayerRC:
    """First-order lumped-RC parasitic coefficients for one conductor role.

    A curated, per-PDK numeric table for ``klt extract --parasitics`` -- the
    parasitics analogue of :class:`DrcRule`'s curated width/space table and
    the SPICE model-binding table (``klayout_tools.pdk_models``), sourced
    from each PDK's *public* process/DRM data (never NDA'd, matching this
    repo's open-PDK-only rule). Three coefficients per conductor role:

    - ``sheet_res_ohm_sq`` -- sheet resistance, ohms per square. Combined
      with a per-net square count (estimated from the net's per-layer area
      and perimeter, see ``klayout_tools.extract._n_squares``) to give one
      lumped series resistance per net.
    - ``cap_area_ff_um2`` -- parallel-plate capacitance to substrate,
      femtofarads per square micrometre of the net's area on this layer.
    - ``cap_perim_ff_um`` -- sidewall/fringe capacitance to substrate,
      femtofarads per micrometre of the net's perimeter on this layer.

    **These are representative, uncalibrated, order-of-magnitude starter
    values**, exactly like the DRC decks' "curated starter subset" scope
    (see ``docs/cli/drc.md`` -> "Coverage"). Parasitic-extraction accuracy
    tuning/calibration against silicon is an explicit non-goal of the first
    cut (issue #216's "Non-goals"); each family deck's ``PARASITICS``
    docstring cites the public source its numbers are drawn from.
    """

    sheet_res_ohm_sq: float
    cap_area_ff_um2: float
    cap_perim_ff_um: float


@dataclass(frozen=True)
class ParasiticsDeck:
    """Per-PDK-family first-order lumped-RC coefficient set for
    ``klt extract --parasitics`` (see ``docs/cli/extract.md`` -> "Parasitic
    (RC) extraction" and ``docs/design/lvs-extraction-spike.md`` -> "Addendum
    (#216)").

    One :class:`LayerRC` per conductor role the extraction pass tracks
    geometry for. ``metals`` is index-aligned with the matching
    :class:`ExtractionDeck`'s ``metals`` stack (so ``metals[0]``'s
    coefficients apply to whatever layer that deck's ``metals[0]`` is -- e.g.
    sky130's local-interconnect ``li1``, gf180mcu's ``Metal1``), which is why
    the coefficients are per-deck and per-metal-index rather than keyed by a
    universal role name. A ``None`` role (or a ``metals`` entry that is
    ``None``) contributes no parasitics for that role.

    ``metal_overlaps`` (issue #760, Extract Stage 2a) curates the *vertical*
    plate-overlap coupling coefficient for each **adjacent** pair of
    ``metals`` levels -- entry ``i`` is the coefficient between
    ``metals[i]`` (the lower plate) and ``metals[i+1]`` (the upper plate),
    transcribed from each PDK's own public magic tech file's
    ``defaultoverlap`` entries (femtofarads per square micrometre of
    overlapping area, the same aF/um^2 -> fF/um^2 convention as
    ``cap_area_ff_um2``). A fully-populated table has ``len(metals) - 1``
    entries; a shorter tuple, or an explicit ``None`` entry, means that pair
    has no curated coupling coefficient -- see
    ``klayout_tools.extract._describe_parasitics_overlap_gaps`` (the
    ``metals_without_coefficient``-style gap report for this table, issue
    #547's pattern extended to overlap coupling) for how that is surfaced
    rather than silently zeroed. Empty by default so a deck that never
    populates this field behaves exactly as before this field existed
    (ground capacitance only, `--parasitics`'s pre-#760 behaviour).

    ``metal_sidewalls``/``metal_sidewall_lookback_um`` (issue #976, Epic
    #709 Phase 2a) curate the *lateral* (same-layer, sidewall) coupling
    coefficient family -- magic's ``defaultsidewall`` -- the one named as
    the still-absent follow-on to ``metal_overlaps`` in
    ``docs/design/extract-fidelity-roadmap.md``'s "Stage 2b". Both are
    index-aligned with ``metals`` (entry ``i`` describes ``metals[i]``'s own
    same-layer coupling, unlike ``metal_overlaps``' adjacent-*pair*
    indexing). ``metal_sidewalls[i]`` is femtofarads per micrometre of
    facing-edge length between two *distinct* nets' conductors on level
    ``i`` within ``metal_sidewall_lookback_um[i]`` micrometres of each other
    -- the same aF/um -> fF/um transcription convention ``cap_perim_ff_um``
    uses, from magic's own public tech file's ``defaultsidewall`` *first*
    parameter (nominal process corner). ``metal_sidewall_lookback_um[i]`` is
    deliberately **twice this deck's own same-layer minimum-spacing DRC
    rule** for level ``i`` (e.g. sky130's ``met1.space.1``), not a
    transcription of magic's own ``defaultsidewall`` *second* parameter --
    that parameter's exact distance/scaling semantics are an open question
    flagged in the roadmap doc (open question #1) rather than guessed at
    here. Exactly the bare minimum spacing was tried first and rejected: a
    ``separation_check(d)``-style query only reports edges *closer than*
    ``d``, so a 1x lookback can only ever fire on a DRC-*illegal* layout,
    making it a near-no-op on any real, DRC-clean design -- see
    ``decks/sky130.py``'s own ``metal_sidewall_lookback_um`` comment for the
    measured ``gcd`` corpus evidence behind the 2x choice. Unlike
    ``metal_overlaps`` (unconditional across every net pair on the whole
    layout, issue #760), lateral coupling is only ever computed for a net
    pair where at least one side is named in a caller's ``critical_nets``
    request (``klt extract --critical-net``) -- see
    ``klayout_tools.extract._compute_parasitics``'s docstring for why a
    full-layout lateral pass is deliberately out of this issue's scope.
    Neither table deducts the matching charge from either net's substrate
    fringe term (unlike ``metal_overlaps``' ground-charge deduction) -- see
    that same docstring's "known simplification" note. Both empty by default,
    so a deck that never populates them (or a run that never names a
    ``critical_nets`` entry) behaves exactly as before this field existed.
    """

    diffusion: LayerRC | None = None
    poly: LayerRC | None = None
    metals: tuple[LayerRC | None, ...] = ()
    metal_overlaps: tuple[float | None, ...] = ()
    metal_sidewalls: tuple[float | None, ...] = ()
    metal_sidewall_lookback_um: tuple[float | None, ...] = ()


class UnknownExtractionDeckError(Exception):
    """Raised by :func:`get_extraction_deck` for an unknown deck name."""


class InvalidDeckOptionError(Exception):
    """Raised by :func:`get_extraction_deck` for a ``deck_options`` entry
    that names a key no declared :class:`ResistorDevice.flavour_option`
    matches, or a value not among that entry's declared
    :class:`ResistorFlavour.value` set (issue #595).

    ``extract.py`` turns this into an
    :class:`~klayout_tools.extract.ExtractError` (a clean exit-1 message, not
    a traceback), the same treatment it already gives
    :class:`UnknownExtractionDeckError`.
    """


def deck_source_path(name: str) -> str | None:
    """Absolute path to the Python module source that defines deck ``name``.

    Our decks are declarative Python tables in per-PDK sibling modules
    (``sky130.py``, ``gf180mcu.py``) rather than standalone rule-deck files,
    so the "deck file" whose content hash pins a run's rule set (see
    :func:`klayout_tools._provenance.build_provenance`) is that module's
    source. Deck names map 1:1 to those sibling module names (see the
    registries below). Returns ``None`` when the module can't be located.
    """
    from importlib import import_module

    try:
        module = import_module(f"{__package__}.{name}")
    except ImportError:
        return None
    return getattr(module, "__file__", None)


def _registry() -> dict[str, list[DrcRule]]:
    from . import gf180mcu, sg13g2, sky130

    return {"sky130": sky130.DECK, "gf180mcu": gf180mcu.DECK, "sg13g2": sg13g2.DECK}


def _layer_name_registry() -> dict[str, dict[tuple[int, int], str]]:
    from . import gf180mcu, sg13g2, sky130

    return {
        "sky130": sky130.LAYER_NAMES,
        "gf180mcu": gf180mcu.LAYER_NAMES,
        "sg13g2": sg13g2.LAYER_NAMES,
    }


def get_deck(name: str) -> list[DrcRule]:
    """Return the rule list for a registered deck name.

    Raises :class:`UnknownDeckError` (which the caller in ``drc.py`` turns
    into a :class:`~klayout_tools.drc.DrcError`) if ``name`` is not a
    registered deck.
    """
    decks = _registry()
    try:
        return decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None


def get_layer_names(name: str) -> dict[tuple[int, int], str]:
    """Return the ``(layer, datatype) -> "name.purpose"`` map for a deck.

    Used only for human-readable JSON output; unrecognised decks return an
    empty map rather than raising (callers already validated the deck name
    via :func:`get_deck` before reaching this point).
    """
    return _layer_name_registry().get(name, {})


def _unmodeled_voltage_marker_registry() -> dict[str, dict[tuple[int, int], str]]:
    from . import gf180mcu, sg13g2, sky130

    return {
        "sky130": sky130.UNMODELED_VOLTAGE_MARKERS,
        "gf180mcu": gf180mcu.UNMODELED_VOLTAGE_MARKERS,
        "sg13g2": sg13g2.UNMODELED_VOLTAGE_MARKERS,
    }


def get_unmodeled_voltage_markers(name: str) -> dict[tuple[int, int], str]:
    """Return the ``(layer, datatype) -> description`` map of voltage-domain
    marker layers ``name``'s deck draws but does not model the DRC/
    extraction *scoping* of (issue #552).

    Several open PDKs draw two gate-oxide/voltage domains on the same
    wafer, selected by a marker layer -- e.g. gf180mcu's ``Dualgate``
    (55/0) selects its 5V/6V thick-oxide domain, whose DRM publishes a
    second, materially different (30-60% larger) column of DRC thresholds
    and a distinct set of MOS models. This curated deck's rule/extraction
    tables encode only the default (thin-oxide) column and never read the
    marker, so geometry drawn *inside* it is checked against the wrong
    thresholds and extracted with the wrong model name -- silently, with a
    ``clean``/plausible-looking result.

    A deck registers such a layer here, with a description naming the
    concrete consequence, so ``drc.py``'s ``coverage.voltage_domain_warnings``
    and ``extract.py``'s ``voltage_domain_warnings`` can surface a loud
    warning whenever that marker's geometry actually interacts with checked/
    extracted geometry, rather than leave a silent false ``clean`` /
    unflagged wrong-model result. This is deliberately only a diagnostic
    signal ("fail loudly" -- see this issue's "Suggested shape" option (3)):
    it does not change any rule threshold or extracted model itself, which
    would require the larger, separately-tracked option (1)/(2) work (a
    subtraction-capable :class:`DerivedLayer` and a per-flavour MOS marker
    field, respectively).

    Mirrors :func:`get_layer_names`'s shape and unrecognised-deck fallback:
    an unregistered deck name returns an empty map rather than raising.
    """
    return _unmodeled_voltage_marker_registry().get(name, {})


def _nominal_dbu_registry() -> dict[str, float]:
    from . import gf180mcu, sg13g2, sky130

    return {
        "sky130": sky130.NOMINAL_DBU_UM,
        "gf180mcu": gf180mcu.NOMINAL_DBU_UM,
        "sg13g2": sg13g2.NOMINAL_DBU_UM,
    }


def get_nominal_dbu(name: str) -> float:
    """Return the database unit (in micrometres) that ``name``'s rule
    thresholds were authored against.

    Every ``DrcRule.threshold_dbu`` value in a deck is transcribed assuming
    this dbu; ``run_drc()`` uses it to rescale thresholds to the actual
    layout's ``dbu`` before running any ``Region.*_check()`` (see
    :class:`DrcRule`). Raises :class:`UnknownDeckError` for an unregistered
    deck name, mirroring :func:`get_deck`.
    """
    registry = _nominal_dbu_registry()
    try:
        return registry[name]
    except KeyError:
        available = ", ".join(sorted(registry))
        raise UnknownDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None


def _extraction_registry() -> dict[str, ExtractionDeck]:
    from . import gf180mcu, sg13g2, sky130

    return {
        "sky130": sky130.EXTRACTION_DECK,
        "gf180mcu": gf180mcu.EXTRACTION_DECK,
        "sg13g2": sg13g2.EXTRACTION_DECK,
    }


def get_extraction_deck(
    name: str, deck_options: Mapping[str, str] | None = None
) -> ExtractionDeck:
    """Return the registered :class:`ExtractionDeck` for ``name``, optionally
    resolved against ``deck_options`` (issue #595).

    Raises :class:`UnknownExtractionDeckError` (which ``extract.py`` turns
    into an :class:`~klayout_tools.extract.ExtractError`) if ``name`` is not
    a registered deck. Deliberately a *separate* registry/name lookup from
    :func:`get_deck` (the DRC deck registry) even though the deck names
    overlap (``"sky130"``/``"gf180mcu"``) -- DRC and extraction decks are
    different rule tables that happen to share PDK-family names, not the
    same object reused for two purposes.

    ``deck_options`` (``klt extract --deck-option <key>=<value>``, repeatable)
    selects, per key, which caller-visible flavour of a
    :class:`ResistorDevice` whose ``flavour_option`` matches that key is
    wired for this call -- see :class:`ResistorDevice`'s own
    ``flavour_option``/``flavours`` docstring for why this exists (a
    shared-geometry sheet-rho family the upstream PDK LVS deck selects with a
    build-time variable, e.g. gf180mcu's ``POLY_RES``). ``None`` or an empty
    mapping (the default) returns the registered deck completely unchanged --
    byte-identical to every call site that predates this parameter. A
    non-empty mapping naming a key no resistor entry declares, or a value not
    among a matched entry's declared :class:`ResistorFlavour.value` set,
    raises :class:`InvalidDeckOptionError` rather than silently keeping the
    default or ignoring the override.
    """
    decks = _extraction_registry()
    try:
        deck = decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownExtractionDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None
    if not deck_options:
        return deck
    return _resolve_resistor_flavours(name, deck, deck_options)


def _resolve_resistor_flavours(
    name: str, deck: ExtractionDeck, deck_options: Mapping[str, str]
) -> ExtractionDeck:
    """Apply ``deck_options`` to ``deck``'s ``resistors`` (issue #595) --
    see :func:`get_extraction_deck` for the contract.
    """
    selectable_keys = {
        resistor.flavour_option
        for resistor in deck.resistors
        if resistor.flavour_option is not None
    }
    unknown_keys = sorted(set(deck_options) - selectable_keys)
    if unknown_keys:
        available = ", ".join(sorted(selectable_keys)) or "none"
        raise InvalidDeckOptionError(
            f"deck '{name}' has no selectable option(s) named "
            f"{', '.join(unknown_keys)} (available: {available})"
        )

    resolved_resistors = []
    for resistor in deck.resistors:
        option_key = resistor.flavour_option
        if option_key is None or option_key not in deck_options:
            resolved_resistors.append(resistor)
            continue
        value = deck_options[option_key]
        flavour = next((f for f in resistor.flavours if f.value == value), None)
        if flavour is None:
            available = ", ".join(f.value for f in resistor.flavours) or "none"
            raise InvalidDeckOptionError(
                f"deck '{name}' option '{option_key}={value}' is not one of "
                f"this deck's declared flavours for '{resistor.name}' "
                f"(available: {available})"
            )
        resolved_resistors.append(
            dataclasses.replace(
                resistor, name=flavour.name, sheet_rho_ohm_sq=flavour.sheet_rho_ohm_sq
            )
        )
    return dataclasses.replace(deck, resistors=tuple(resolved_resistors))


def _parasitics_registry() -> dict[str, ParasiticsDeck]:
    from . import gf180mcu, sg13g2, sky130

    return {
        "sky130": sky130.PARASITICS,
        "gf180mcu": gf180mcu.PARASITICS,
        "sg13g2": sg13g2.PARASITICS,
    }


def get_parasitics_deck(name: str) -> ParasiticsDeck:
    """Return the registered :class:`ParasiticsDeck` for ``name``.

    Raises :class:`UnknownExtractionDeckError` (which ``extract.py`` turns
    into an :class:`~klayout_tools.extract.ExtractError`) if ``name`` is not a
    registered deck -- same name lookup and error type as
    :func:`get_extraction_deck`, since every extraction deck also carries a
    parasitics table.
    """
    decks = _parasitics_registry()
    try:
        return decks[name]
    except KeyError:
        available = ", ".join(sorted(decks))
        raise UnknownExtractionDeckError(
            f"unknown deck '{name}' (available: {available})"
        ) from None
