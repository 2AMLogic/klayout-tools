"""Registry of DRC rule decks and extraction (connectivity/device) decks.

A DRC "deck" is our own declarative rule table (see :class:`DrcRule`) that
drives ``klayout.db.Region``'s native check primitives (``width_check``,
``space_check``, ``separation_check``, ``enclosing_check``, etc.) — the same
C++ polygon-processing engine that backs KLayout's higher-level DRC-DSL
scripts, invoked directly instead of through the script runner. This keeps
``klt drc`` headless with zero new runtime dependency (see
``docs/cli/drc.md`` for the engine-choice rationale).

An *extraction* deck (see :class:`ExtractionDeck`) is the same idea for the
other half of physical verification: a declarative description of which drawn
layers exist, which derived layers are computed from them, which device
extractors run over those derived layers, and what connects to what. It drives
``klayout.db.LayoutToNetlist`` for ``klt extract`` (see
``docs/cli/extract.md``, and ``docs/design/lvs-extraction-spike.md`` for the
engine-choice rationale). Both open PDKs route their real LVS through
magic+netgen rather than a KLayout-native LVS deck, so — exactly as that spike
predicted under "Where the connectivity/extraction rules live" — these decks
are ours to curate, like the DRC decks.

Deck data lives in per-PDK sibling modules (``sky130.py``, ``gf180mcu.py``);
this module only aggregates them into name -> deck registries.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DrcRule:
    """One rule in a DRC deck.

    ``layer`` and ``other_layer`` are ``(layer, datatype)`` pairs. ``check``
    selects which ``klayout.db.Region`` check primitive to run:
    ``"width"`` / ``"space"`` / ``"notch"`` are single-layer checks;
    ``"separation"`` / ``"enclosing"`` / ``"enclosed"`` / ``"overlap"`` are
    two-layer checks and require ``other_layer``. ``threshold_dbu`` is the
    rule's distance threshold in database units (matching the layout's own
    ``dbu`` — sky130 streams use ``dbu_um = 0.001``, i.e. 1 nm per unit).

    Rule ``id`` values are a stable, public contract once shipped — never
    renumber or repurpose one (see ``docs/cli/drc.md``).
    """

    id: str
    description: str
    layer: tuple[int, int]
    check: str
    threshold_dbu: int
    other_layer: tuple[int, int] | None = None


class UnknownDeckError(Exception):
    """Raised by :func:`get_deck` / :func:`get_layer_names` /
    :func:`get_extraction_deck` for an unknown deck name."""


#: Name of the synthetic, always-empty region an extraction deck may reference
#: as a device's bulk/well terminal layer. It carries no drawn geometry — it is
#: the substrate placeholder KLayout's LVS flow uses so a MOS4 extractor has a
#: fourth (body) terminal even where the PDK draws no explicit p-well layer.
#: A deck ties it to its substrate-tap layer via ``connect`` and names the
#: resulting net via ``global_connect``.
BULK_LAYER = "bulk"


@dataclass(frozen=True)
class DerivedLayer:
    """One derived layer in an extraction deck, computed from two earlier ones.

    ``op`` is ``"and"`` (region intersection) or ``"not"`` (region
    difference); ``left``/``right`` name either a drawn layer (a key of
    :attr:`ExtractionDeck.layers`) or an earlier :class:`DerivedLayer`, so
    derivations chain in declaration order.

    Only these two operators exist on purpose: every connectivity/device
    recipe both curated decks need is expressible as a chain of intersections
    and differences, and a two-operator table stays reviewable against the
    PDK documentation in a way an expression mini-language would not.
    """

    name: str
    op: str
    left: str
    right: str


@dataclass(frozen=True)
class DeviceSpec:
    """One device extractor in an extraction deck.

    ``name`` is the extracted device *class* name — it appears verbatim in the
    emitted SPICE (``M$1 ... <name> L=... W=...``) and in the JSON report's
    ``device_counts``/``devices[].class``, so it is a stable public contract
    once shipped, exactly like a DRC rule id.

    ``kind`` selects the KLayout device extractor; ``"mos4"`` (a four-terminal
    MOS with an explicit bulk/body terminal, ``klayout.db.
    DeviceExtractorMOS4Transistor``) is the only kind these curated decks use
    today. ``gate``/``source_drain``/``gate_conductor``/``well`` name the
    layers bound to the extractor's ``G``/``SD``/``P``/``W`` inputs.
    """

    name: str
    kind: str
    gate: str
    source_drain: str
    gate_conductor: str
    well: str


@dataclass(frozen=True)
class ExtractionDeck:
    """A curated connectivity + device-extraction recipe for one PDK.

    Fields are applied by :mod:`klayout_tools.extract` in this order: drawn
    ``layers`` are bound (a layer absent from the stream becomes an empty
    region rather than an error, mirroring ``klt drc``'s "rule's layer is
    absent -> no violations" posture), ``derived`` layers are computed,
    ``devices`` are extracted, then ``connect``/``global_connect``/``labels``
    build the connectivity model.

    - ``layers`` — drawn-layer key -> ``(layer, datatype)``.
    - ``texts`` — text-layer key -> ``(layer, datatype)``; label layers whose
      strings name nets.
    - ``derived`` — ordered derivations (see :class:`DerivedLayer`).
    - ``devices`` — device extractors (see :class:`DeviceSpec`).
    - ``intra_connect`` — layer keys that conduct within themselves.
    - ``inter_connect`` — ``(a, b)`` layer-key pairs that conduct where they
      overlap.
    - ``global_connect`` — ``(layer key, global net name)``; the deck's
      substrate/well global nets.
    - ``labels`` — ``(region layer key, text layer key)``; a text landing on
      that region names the net it lands on.
    """

    name: str
    layers: dict[str, tuple[int, int]]
    texts: dict[str, tuple[int, int]]
    derived: tuple[DerivedLayer, ...]
    devices: tuple[DeviceSpec, ...]
    intra_connect: tuple[str, ...]
    inter_connect: tuple[tuple[str, str], ...]
    global_connect: tuple[tuple[str, str], ...]
    labels: tuple[tuple[str, str], ...]


def _registry() -> dict[str, list[DrcRule]]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.DECK, "gf180mcu": gf180mcu.DECK}


def _layer_name_registry() -> dict[str, dict[tuple[int, int], str]]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.LAYER_NAMES, "gf180mcu": gf180mcu.LAYER_NAMES}


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


def _extraction_registry() -> dict[str, ExtractionDeck]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.EXTRACTION, "gf180mcu": gf180mcu.EXTRACTION}


def extraction_deck_names() -> list[str]:
    """Return the sorted names of every registered extraction deck."""
    return sorted(_extraction_registry())


def get_extraction_deck(name: str) -> ExtractionDeck:
    """Return the extraction deck registered under ``name``.

    Raises :class:`UnknownDeckError` (which the caller in ``extract.py`` turns
    into a :class:`~klayout_tools.extract.ExtractError`) if ``name`` is not a
    registered extraction deck.
    """
    decks = _extraction_registry()
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
