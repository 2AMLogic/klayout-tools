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
    rule's distance threshold in the deck's *nominal* database unit — the
    dbu each deck's threshold values were transcribed/authored against (see
    each deck module's ``NOMINAL_DBU_UM`` constant), **not** necessarily the
    dbu of the layout actually being checked. ``run_drc()`` converts
    ``threshold_dbu`` to the layout's own ``dbu`` at run time by scaling with
    the ratio ``NOMINAL_DBU_UM / layout.dbu`` before passing it to the
    ``Region.*_check()`` primitives — see ``docs/cli/drc.md`` "Database
    units" for the full rationale (a threshold interpreted at the wrong dbu
    silently changes which geometry passes or fails).

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


def _nominal_dbu_registry() -> dict[str, float]:
    from . import gf180mcu, sky130

    return {"sky130": sky130.NOMINAL_DBU_UM, "gf180mcu": gf180mcu.NOMINAL_DBU_UM}


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


def get_nominal_dbu_um(name: str) -> float:
    """Return the database unit (µm/dbu) that ``name``'s ``threshold_dbu``
    values were authored against.

    ``run_drc()`` uses this to convert each rule's ``threshold_dbu`` to the
    layout's actual ``dbu`` before it is passed to a ``Region.*_check()``
    primitive, so a deck's thresholds are portable across streams written at
    any dbu (see ``docs/cli/drc.md`` "Database units"). Callers already
    validated the deck name via :func:`get_deck` before reaching this point,
    so an unknown name is a programming error, not a user-facing one.
    """
    return _nominal_dbu_registry()[name]
