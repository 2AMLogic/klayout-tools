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
    """Raised by :func:`get_deck` / :func:`get_layer_names` for an unknown deck name."""


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


def get_layer_names(name: str) -> dict[tuple[int, int], str]:
    """Return the ``(layer, datatype) -> "name.purpose"`` map for a deck.

    Used only for human-readable JSON output; unrecognised decks return an
    empty map rather than raising (callers already validated the deck name
    via :func:`get_deck` before reaching this point).
    """
    return _layer_name_registry().get(name, {})
