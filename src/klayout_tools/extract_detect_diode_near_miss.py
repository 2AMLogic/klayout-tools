"""``klt extract`` diode near-miss geometry diagnostic (issue #5736).

Split out of `extract.py` to keep it under its file-size ceiling. A move, not
a rewrite: the code below is byte-for-byte what lived inline there. It is the
layout-geometry counterpart of `extract_detect_block.py` -- it reads
``layout`` / ``top_cell`` / ``deck`` geometry after the extraction pass and
only appends to ``warnings[]``; it never changes a net, a device, a bound
model or a rule threshold. `extract.py` re-exports ``_diode_required_terms``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._layout import region as _region
from ._layout import region_expr as _region_expr
from .decks import (
    DiodeDevice,
    ExtractionDeck,
    UnknownExtractionDeckError,
    UnmodelledDeviceFamily,
    get_extraction_deck,
)

if TYPE_CHECKING:
    import klayout.db as kdb


def _extraction_deck_name(deck: ExtractionDeck) -> str | None:
    """The registered name of ``deck``, or ``None`` when it is not one of the
    built-in singletons (a deck resolved with ``--deck-option`` overrides, or
    a test-local deck built inline).

    Used only to spell a runnable ``klt deck devices --deck <name>`` hint
    into a diagnostic message -- :func:`_extract_netlist` is handed the
    resolved :class:`ExtractionDeck` object, not the name the caller asked
    for, and threading the name through every call site just to build one
    string is not worth the churn. Identity comparison, not equality: the
    registry hands out module-level singletons.
    """
    from .decks import known_extraction_deck_names

    for name in known_extraction_deck_names():
        try:
            if get_extraction_deck(name) is deck:
                return name
        except UnknownExtractionDeckError:  # pragma: no cover - defensive
            continue
    return None


#: The :func:`_diode_required_terms` roles that name a terminal's own drawn
#: conductor/diffusion layer, as opposed to a narrowing predicate over it.
#: :func:`_diode_near_miss` never reports one of these as "missing" -- see
#: its own note at the call site.
_DIODE_TERMINAL_ROLES = frozenset({"anode", "cathode"})


def _diode_required_terms(
    diode: DiodeDevice,
) -> list[tuple[str, tuple[int, int]]]:
    """Every drawn-layer predicate a :class:`DiodeDevice` entry narrows its
    recognised junction by, as ``(role, (layer, datatype))`` pairs, in the
    order :func:`_diode_near_miss` tests them -- the deck-side counterpart of
    the ``required_layers`` list ``klt deck devices`` reports.

    ``excludes`` layers are deliberately **not** in this list: a region an
    ``excludes`` layer removes was disqualified on purpose, which is not a
    near miss. A terminal declared ``None`` (formed by the substrate, not by
    a drawn mask) contributes no term of its own -- there is no layer to be
    missing.
    """
    terms: list[tuple[str, tuple[int, int]]] = [("marker", diode.marker)]
    if diode.anode is not None:
        terms.append(("anode", diode.anode))
    terms.extend(("anode_requires", pair) for pair in diode.anode_requires)
    if diode.cathode is not None:
        terms.append(("cathode", diode.cathode))
    terms.extend(("cathode_requires", pair) for pair in diode.cathode_requires)
    return terms


def _diode_near_miss(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    diode: DiodeDevice,
    claimed: kdb.Region,
    dummy: kdb.Region,
) -> tuple[list[tuple[str, tuple[int, int]]], int, kdb.Region]:
    """Which member(s) of ``diode``'s drawn-layer predicate set a layout is
    missing, when that entry recognised **no** junction at all (issue #2365).

    ``klt extract`` reporting ``device_count: 0`` with an empty ``warnings[]``
    is indistinguishable from "this layout legitimately contains no device of
    that class" -- and a curated diode entry is recognised only where *every*
    member of ``marker`` + both terminals' declared layer + both terminals'
    ``requires`` covers the same geometry, so a layout one layer short fails
    exactly as silently as a layout with nothing drawn at all. Twice in
    practice (2AMLogic/gf180-drone-fc #24 and #37) the only way to answer
    "which of the N layers am I missing?" was to read the deck's
    ``DiodeDevice`` entry field by field.

    The search is a single greedy pass over :func:`_diode_required_terms`:
    intersect the candidate region with each predicate in turn, and whenever
    a predicate would empty it, record that predicate as *missing* and carry
    the un-narrowed region forward instead. What survives at the end is
    geometry that satisfies every predicate except the recorded ones -- and
    because the real junction came out empty, each recorded predicate is by
    construction a layer that is either not drawn at all or does not cover
    this geometry. Multiple missing members are found in one pass (the
    reported failure took two rounds precisely because fixing one exposed
    the next).

    A terminal's **own** declared layer is exempt from that treatment: if
    intersecting it empties the candidate the search stops and reports
    nothing at all. Without the anode/cathode conductor there is no
    junction-shaped region left to be "one layer short of a diode" -- only a
    bare implant or marker patch, which a layout draws for all sorts of
    unrelated reasons.

    Both terminals' ``excludes`` (and the deck's ``dummy`` marker) are
    applied at every step and are never dropped: geometry an ``excludes``
    layer removes was disqualified deliberately, and a dummy-marked device
    is suppressed deliberately, so neither is a near miss.

    ``claimed`` is geometry another recognised structure already explains --
    :func:`_collect_diode_near_miss_warnings` passes the MOS active islands
    (every component of the deck's ``active`` layer a gate crosses) plus the
    deck's resolved well/substrate-tap geometry. Candidates touching it are
    dropped, which is
    what keeps this quiet on ordinary layouts: a 6V PMOS's p+-in-Nwell
    source/drain satisfies every ``diode_pd2nw_06v0`` predicate but the
    marker, and an n+ substrate tie satisfies every ``diode_nd2ps_06v0``
    predicate but the marker, so without this filter every such device would
    be reported as a missing-marker near miss on every layout.

    Returns ``(missing, candidates, region)`` -- the missing predicates as
    ``(role, (layer, datatype))`` pairs in declaration order, how many
    connected candidate regions survived, and those regions' geometry (so
    :func:`_collect_diode_near_miss_warnings` can test it against the deck's
    ``unmodelled_families``). ``([], 0, <empty>)`` when nothing survives
    (the ordinary "this layout really has no diode there" case), which is
    also what a layout drawing none of the predicate layers returns.
    """
    import klayout.db as kdb

    excludes = tuple(diode.anode_excludes) + tuple(diode.cathode_excludes)
    exclude_region = kdb.Region()
    for pair in excludes:
        exclude_region += _region(layout, top_cell, pair)
    if not dummy.is_empty():
        exclude_region += dummy

    def survivors(region: kdb.Region) -> kdb.Region:
        remaining = (region - exclude_region).merged()
        if remaining.is_empty() or claimed.is_empty():
            return remaining
        return remaining.not_interacting(claimed)

    # `None` means "not yet narrowed by anything" -- an unconstrained
    # candidate, which cannot be tested on its own (it is the whole plane),
    # so the first predicate that is actually drawn establishes the region.
    region: kdb.Region | None = None
    missing: list[tuple[str, tuple[int, int]]] = []
    for role, pair in _diode_required_terms(diode):
        term = _region(layout, top_cell, pair)
        trial = term if region is None else (region & term)
        if not survivors(trial).is_empty():
            region = trial
            continue
        if role in _DIODE_TERMINAL_ROLES:
            # A terminal's own declared layer is never treated as "missing":
            # without it there is no junction-shaped region at all, only a
            # bare implant/marker patch, and calling that a near miss is
            # noise (a gf180mcu poly resistor's oversized `Pplus` halo alone
            # would otherwise be reported as a near-miss `diode_pd2nw_06v0`).
            # A near miss is geometry that *is* device-shaped and is one
            # narrowing predicate short, which is exactly the shape the
            # reported failures took.
            return [], 0, kdb.Region()
        missing.append((role, pair))

    if region is None or not missing:
        return [], 0, kdb.Region()
    candidate_region = survivors(region)
    return missing, candidate_region.count(), candidate_region


#: How each :func:`_diode_required_terms` role reads in a ``warnings[]``
#: entry -- the deck field a caller would otherwise have to open the deck
#: module to find.
_DIODE_TERM_ROLE_TEXT = {
    "marker": "device-recognition marker",
    "anode": "anode layer",
    "cathode": "cathode layer",
    "anode_requires": "anode_requires layer",
    "cathode_requires": "cathode_requires layer",
}


def _diode_near_miss_warning(
    deck: ExtractionDeck,
    diode: DiodeDevice,
    missing: list[tuple[str, tuple[int, int]]],
    candidates: int,
    unmodelled: UnmodelledDeviceFamily | None = None,
) -> str:
    """The ``warnings[]`` line for one :func:`_diode_near_miss` result
    (issue #2365).

    One aggregate line per device class with the candidate count baked in --
    the same shape ``unbiased_pmos_body_nets``/``single_terminal_nets`` use
    (issue #599) rather than one line per candidate region, which would blow
    up ``warnings[]`` on a layout that drew the wrong implant across a whole
    array.

    The message names every missing layer by the deck's published layer name
    *and* its raw ``layer/datatype`` pair -- the pair is what a caller
    actually draws against -- and points at ``klt deck devices`` for the
    class's complete predicate set, so the answer scales past the one layer
    this particular layout happened to be missing.

    ``unmodelled`` is a deck-declared family the candidate geometry also
    overlaps (issue #3137); when given, one sentence is appended saying the
    geometry is really that family, which the deck does not model, so the
    advice to draw the missing layer does not mislead.
    """
    from .decks import get_layer_names

    deck_name = _extraction_deck_name(deck)
    layer_names = get_layer_names(deck_name) if deck_name is not None else {}

    def describe(role: str, pair: tuple[int, int]) -> str:
        name = layer_names.get(pair)
        spec = f"{pair[0]}/{pair[1]}"
        role_text = _DIODE_TERM_ROLE_TEXT.get(role, role)
        return f"{role_text} {name} ({spec})" if name else f"{role_text} {spec}"

    missing_str = ", ".join(describe(role, pair) for role, pair in missing)
    region_word = "region" if candidates == 1 else "regions"
    match_word = "matches" if candidates == 1 else "match"
    is_word = "is" if candidates == 1 else "are"
    hint = (
        f"`klt deck devices --deck {deck_name} --class {diode.name}`"
        if deck_name is not None
        else f"`klt deck devices --class {diode.name}`"
    )
    note = (
        f" Note: this geometry also matches {unmodelled.name}, a device "
        f"family this deck does not model, so it is probably not a diode "
        f"at all -- see {unmodelled.reference}."
        if unmodelled is not None
        else ""
    )
    return (
        f"{candidates} candidate '{diode.name}' {region_word} {match_word} "
        f"every other drawn-layer predicate of that device class but "
        f"{is_word} missing its {missing_str} -- so this geometry extracted "
        "as no device at "
        "all rather than as a diode, and the zero device count for this "
        "class is not evidence that the layout contains none. Draw the "
        f"missing layer(s), or run {hint} for the class's complete "
        "marker/requires/excludes set. See docs/cli/extract.md's "
        "'Marker-gated and predicate-gated device classes' section."
        f"{note}"
    )


def _collect_diode_near_miss_warnings(
    layout: kdb.Layout,
    top_cell: kdb.Cell,
    deck: ExtractionDeck,
    diode_terminals: list[tuple[DiodeDevice, kdb.Region, kdb.Region]],
    poly: kdb.Region,
    tap: kdb.Region,
    dummy: kdb.Region,
) -> list[str]:
    """One ``warnings[]`` line per :class:`DiodeDevice` entry that recognised
    **no** junction on this layout while geometry that satisfies every
    predicate but one (or a few) is drawn (issue #2365).

    ``diode_terminals`` is every declared entry's final ``(diode, anode,
    cathode)`` regions, as built by :func:`_extract_netlist`'s diode block --
    including the entries it skips, which are exactly the interesting ones
    here. An entry whose two terminals *do* overlap recognised a device and
    is never examined.

    ``poly``/``tap``/``dummy`` are the deck's already-resolved gate,
    well/substrate-tap and dummy-marker regions. The first two build the
    "already explained by another recognised structure" filter
    :func:`_diode_near_miss` applies: an `active` island a gate crosses is a
    real MOS device's diffusion (the p+-in-Nwell source/drain that satisfies
    every ``diode_pd2nw_06v0`` predicate except the marker), and tap geometry
    is a well/substrate tie (which does the same for ``diode_nd2ps_06v0``).
    Without it, every such structure on every layout would be reported.

    Returns ``[]`` for a deck with no ``diodes`` entries, for a layout whose
    every entry recognised a device, and for a layout that simply draws
    nothing diode-shaped -- the overwhelmingly common cases, none of which
    pays for the ``active`` region this otherwise builds once.
    """
    unrecognised = [
        (diode, anode, cathode)
        for diode, anode, cathode in diode_terminals
        if (anode & cathode).is_empty()
    ]
    if not unrecognised:
        return []

    claimed = (_region(layout, top_cell, deck.active).merged().interacting(poly)) + tap

    warnings: list[str] = []
    for diode, _anode, _cathode in unrecognised:
        missing, candidates, candidate_region = _diode_near_miss(
            layout, top_cell, diode, claimed, dummy
        )
        if missing:
            unmodelled = next(
                (
                    family
                    for family in deck.unmodelled_families
                    if candidate_region.interacting(
                        _region_expr(layout, top_cell, family.signature)
                    ).count()
                ),
                None,
            )
            warnings.append(
                _diode_near_miss_warning(deck, diode, missing, candidates, unmodelled)
            )
    return warnings
