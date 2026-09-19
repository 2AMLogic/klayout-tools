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

The domain dataclasses/exceptions themselves live in three sibling modules
split out of this one (issue #1821), grouped by independent domain -- none
of the three references either of the others, except that ``extraction.py``
reuses ``rules.py``'s :class:`RuleProvenance` for its own devices'
provenance fields:

- :mod:`klayout_tools.decks.rules` -- the DRC rule domain
  (:class:`DerivedLayer`, :class:`RuleProvenance`, :class:`DrcRule`,
  :class:`UnknownDeckError`).
- :mod:`klayout_tools.decks.extraction` -- the extraction device domain
  (:class:`ResistorDevice`, :class:`ResistorFlavour`, :class:`MOSFlavour`,
  :class:`ExtractionDeck`, :class:`BipolarDevice`, :class:`CapacitorDevice`,
  :class:`CapacitorFlavour`, :class:`MomCapacitorDevice`,
  :class:`DiodeDevice`).
- :mod:`klayout_tools.decks.parasitics` -- the parasitics domain
  (:class:`LayerRC`, :class:`ParasiticsDeck`,
  :class:`UnknownExtractionDeckError`, :class:`InvalidDeckOptionError`).

Every public name from those three modules is re-exported below, so
existing ``from klayout_tools.decks import X`` call sites are unaffected --
no caller in this repo imports from a submodule path directly.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import Any

from .extraction import (
    BipolarDevice,
    CapacitorDevice,
    CapacitorFlavour,
    DiodeDevice,
    ExtractionDeck,
    MomCapacitorDevice,
    MOSFlavour,
    ResistorDevice,
    ResistorFlavour,
)
from .parasitics import (
    InvalidDeckOptionError,
    LayerRC,
    ParasiticsDeck,
    UnknownExtractionDeckError,
)
from .rules import DerivedLayer, DrcRule, RuleProvenance, UnknownDeckError

__all__ = [
    "BipolarDevice",
    "CapacitorDevice",
    "CapacitorFlavour",
    "DerivedLayer",
    "DiodeDevice",
    "DrcRule",
    "ExtractionDeck",
    "InvalidDeckOptionError",
    "LayerRC",
    "MOSFlavour",
    "MomCapacitorDevice",
    "ParasiticsDeck",
    "ResistorDevice",
    "ResistorFlavour",
    "RuleProvenance",
    "UnknownDeckError",
    "UnknownExtractionDeckError",
    "deck_names",
    "deck_source_path",
    "get_deck",
    "get_extraction_deck",
    "get_layer_names",
    "get_nominal_dbu",
    "get_parasitics_deck",
    "get_unmodeled_voltage_markers",
    "known_extraction_deck_names",
]


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


def deck_names() -> list[str]:
    """Every built-in deck name *this build* ships, sorted.

    The union of the DRC registry (:func:`get_deck`) and the extraction
    registry (:func:`get_extraction_deck`) -- deliberately separate rule
    tables that happen to share PDK-family names, so a name registered in
    either is a real deck a run can name. Used to validate a deck name before
    :func:`deck_source_path` imports it (issue #1202): every deck name maps to
    a sibling module, but not every sibling module is a deck, so an unchecked
    name would happily hash ``history.py``.

    Note this answers "what does this build ship", which is a different
    question from :func:`klayout_tools.decks.history.known_deck_names`'s "what
    have past releases shipped".
    """
    return sorted(set(_registry()) | set(_extraction_registry()))


def _registry() -> dict[str, list[DrcRule]]:
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.DECK,
        "gf180mcu": gf180mcu.DECK,
        "sg13g2": sg13g2.DECK,
        "sg13cmos5l": sg13cmos5l.DECK,
    }


def _layer_name_registry() -> dict[str, dict[tuple[int, int], str]]:
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.LAYER_NAMES,
        "gf180mcu": gf180mcu.LAYER_NAMES,
        "sg13g2": sg13g2.LAYER_NAMES,
        "sg13cmos5l": sg13cmos5l.LAYER_NAMES,
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
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.UNMODELED_VOLTAGE_MARKERS,
        "gf180mcu": gf180mcu.UNMODELED_VOLTAGE_MARKERS,
        "sg13g2": sg13g2.UNMODELED_VOLTAGE_MARKERS,
        "sg13cmos5l": sg13cmos5l.UNMODELED_VOLTAGE_MARKERS,
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
    registering a marker here does not itself change any rule threshold or
    extracted model.

    **Registration is per marker, but the DRC warning is now gated per rule
    (issue #1110).** A marker registered here can be partially modelled: as
    of #1110 gf180mcu's ``Dualgate`` *is* read by the ``DF.1a``/``DF.3a``
    ``_LV``/``_MV`` rule pairs (via :class:`DerivedLayer`'s
    ``"overlapping"``/``"not_interacting"`` modes), while the rest of that
    deck -- other DRM rules with a 5V/6V column, and ``EXTRACTION_DECK``'s
    MOS model binding -- still ignores it. ``drc.py`` therefore excludes any
    rule that actually reads the marker layer from the warning's
    "interacting with checked geometry" gate, so a layout whose only
    marker-touching geometry is already correctly scoped produces **no**
    warning, while the same marker keeps warning as soon as an unscoped rule
    checks geometry it touches. Keep a marker registered here for as long as
    *any* consumer of that deck still ignores it; the registry entry's
    description should name what remains unmodelled, not what has since been
    modelled.

    Mirrors :func:`get_layer_names`'s shape and unrecognised-deck fallback:
    an unregistered deck name returns an empty map rather than raising.
    """
    return _unmodeled_voltage_marker_registry().get(name, {})


def _nominal_dbu_registry() -> dict[str, float]:
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.NOMINAL_DBU_UM,
        "gf180mcu": gf180mcu.NOMINAL_DBU_UM,
        "sg13g2": sg13g2.NOMINAL_DBU_UM,
        "sg13cmos5l": sg13cmos5l.NOMINAL_DBU_UM,
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
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.EXTRACTION_DECK,
        "gf180mcu": gf180mcu.EXTRACTION_DECK,
        "sg13g2": sg13g2.EXTRACTION_DECK,
        "sg13cmos5l": sg13cmos5l.EXTRACTION_DECK,
    }


def known_extraction_deck_names() -> tuple[str, ...]:
    """Every registered extraction deck name, sorted.

    Distinct from :func:`klayout_tools.decks.history.known_deck_names`
    (which only lists decks that have shipped in at least one tagged
    release) -- this is the *currently installed* registry, so it also
    includes a deck added since the last release (e.g. ``sg13g2`` before its
    first tagged release). Used as the default deck set for ``klt deck
    info`` (issue #1209) when no ``--deck`` is given.
    """
    return tuple(sorted(_extraction_registry()))


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
    :class:`ResistorDevice` or :class:`CapacitorDevice` whose
    ``flavour_option`` matches that key is wired for this call -- see
    :class:`ResistorDevice`'s own ``flavour_option``/``flavours`` docstring
    (issue #595) and :class:`CapacitorDevice`'s (issue #1151) for why this
    exists (a shared-geometry device family the upstream PDK LVS deck
    selects with a build-time variable, e.g. gf180mcu's ``POLY_RES`` sheet-
    rho variable or its ``MIM_CAP`` density variable). ``None`` or an empty
    mapping (the default) returns the registered deck completely unchanged --
    byte-identical to every call site that predates this parameter. A
    non-empty mapping naming a key no resistor or capacitor entry declares,
    or a value not among a matched entry's declared
    :class:`ResistorFlavour.value`/:class:`CapacitorFlavour.value` set,
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
    return _resolve_device_flavours(name, deck, deck_options)


def _resolve_device_flavours(
    name: str, deck: ExtractionDeck, deck_options: Mapping[str, str]
) -> ExtractionDeck:
    """Apply ``deck_options`` to ``deck``'s ``resistors`` (issue #595) and
    ``capacitors`` (issue #1151) -- see :func:`get_extraction_deck` for the
    contract.

    A single combined validation pass runs first, over every
    ``flavour_option`` declared across *both* device families, so a key
    that is only valid for a capacitor (say) is never misreported as
    "unknown" while resolving the unrelated resistor family, or vice versa
    -- :func:`_resolve_resistor_flavours` (issue #595's original,
    single-family resolver this generalises) validated only against
    ``deck.resistors``, which was correct back when capacitors had no
    ``flavour_option`` field to collide with.
    """
    selectable_keys = {
        device.flavour_option
        for device in (*deck.resistors, *deck.capacitors)
        if device.flavour_option is not None
    }
    unknown_keys = sorted(set(deck_options) - selectable_keys)
    if unknown_keys:
        available = ", ".join(sorted(selectable_keys)) or "none"
        raise InvalidDeckOptionError(
            f"deck '{name}' has no selectable option(s) named "
            f"{', '.join(unknown_keys)} (available: {available})"
        )

    resolved_resistors = tuple(
        _resolve_flavour(
            name,
            resistor,
            deck_options,
            lambda flavour: {
                "name": flavour.name,
                "sheet_rho_ohm_sq": flavour.sheet_rho_ohm_sq,
            },
        )
        for resistor in deck.resistors
    )
    resolved_capacitors = tuple(
        _resolve_flavour(
            name,
            capacitor,
            deck_options,
            lambda flavour: {
                "name": flavour.name,
                "area_cap_f_um2": flavour.area_cap_f_um2,
                "perim_cap_f_um": flavour.perim_cap_f_um,
            },
        )
        for capacitor in deck.capacitors
    )
    return dataclasses.replace(
        deck, resistors=resolved_resistors, capacitors=resolved_capacitors
    )


def _resolve_flavour(
    name: str,
    device: ResistorDevice | CapacitorDevice,
    deck_options: Mapping[str, str],
    replace_fields: Callable[[Any], dict[str, Any]],
) -> Any:
    """Resolve one flavour-capable ``device`` (a :class:`ResistorDevice` or
    :class:`CapacitorDevice`) against ``deck_options``, or return it
    unchanged when this entry has no ``flavour_option`` or its key was not
    given (issues #595, #1151).

    ``replace_fields`` maps the matched flavour object
    (:class:`ResistorFlavour`/:class:`CapacitorFlavour`) to the
    ``dataclasses.replace`` kwargs for ``device``'s own dataclass shape --
    the one piece the two device families do not share, so this stays a
    small per-call closure rather than a third dataclass field both families
    would need to carry. The raised :class:`InvalidDeckOptionError` still
    names the *matched entry itself* (``device.name``), not a generic
    "device", preserving the per-device-kind clarity
    :func:`_resolve_resistor_flavours` (issue #595's original single-family
    version of this function) had before this generalisation.
    """
    option_key = device.flavour_option
    if option_key is None or option_key not in deck_options:
        return device
    value = deck_options[option_key]
    flavour = next((f for f in device.flavours if f.value == value), None)
    if flavour is None:
        available = ", ".join(f.value for f in device.flavours) or "none"
        raise InvalidDeckOptionError(
            f"deck '{name}' option '{option_key}={value}' is not one of "
            f"this deck's declared flavours for '{device.name}' "
            f"(available: {available})"
        )
    return dataclasses.replace(device, **replace_fields(flavour))


def _parasitics_registry() -> dict[str, ParasiticsDeck]:
    from . import gf180mcu, sg13cmos5l, sg13g2, sky130

    return {
        "sky130": sky130.PARASITICS,
        "gf180mcu": gf180mcu.PARASITICS,
        "sg13g2": sg13g2.PARASITICS,
        "sg13cmos5l": sg13cmos5l.PARASITICS,
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
