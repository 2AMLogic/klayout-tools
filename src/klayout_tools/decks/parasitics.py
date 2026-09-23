"""Parasitics domain for the ``decks`` package: the curated lumped-RC
coefficient tables for ``klt extract --parasitics``.

Split out of ``decks/__init__.py`` (issue #1821) as a self-contained
domain: :class:`LayerRC` (per-conductor-role sheet-resistance/capacitance
coefficients) and :class:`ParasiticsDeck` (the per-PDK-family collection of
them), plus the two lookup-failure exceptions
:func:`~klayout_tools.decks.get_extraction_deck`/
:func:`~klayout_tools.decks.get_parasitics_deck` raise --
:class:`UnknownExtractionDeckError` and :class:`InvalidDeckOptionError`.
Neither type here references the DRC rule domain (``rules.py``) or the
extraction device domain (``extraction.py``).

The registry/lookup functions that use these types
(:func:`get_extraction_deck`, :func:`get_parasitics_deck`, etc.) stay in
``decks/__init__.py``, which re-exports every public name below so existing
``from klayout_tools.decks import X`` call sites are unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LayerRC:
    """First-order lumped-RC parasitic coefficients for one conductor role.

    A curated, per-PDK numeric table for ``klt extract --parasitics`` -- the
    parasitics analogue of :class:`DrcRule`'s curated width/space table and
    the SPICE model-binding table (``klayout_tools.pdk_models``), sourced
    from each PDK's *public* process/DRM data (never NDA'd, matching this
    repo's open-PDK-only rule). Three coefficients per conductor role:

    - ``sheet_res_ohm_sq`` -- sheet resistance, ohms per square. Combined
      with a per-net square count (summed over the role's connected
      conductor fragments, each estimated from its own area and perimeter
      and, where its terminals imply a current direction, along that
      direction -- see ``klayout_tools.extract_parasitics._region_n_squares``)
      to give one lumped series resistance per net.
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
