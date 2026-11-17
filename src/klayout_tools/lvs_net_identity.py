"""Per-report stable net ids for ``klt lvs`` (issue #5833).

``mismatches[].net`` and ``net_correspondence[]`` name nets only by their
display string (``_name_or_none``, i.e. ``Net.expanded_name()`` through
``spice_safe_net_name``). Anonymous net names restart per circuit -- every
subcircuit's own internal ``"$0"`` -- so thousands of *distinct* layout
nets render under one shared display string in a hierarchical report, and
a consumer holding only the committed report cannot tell how many distinct
nets a ``net.split`` list actually accuses.

This module gives every net reference a stable id that disambiguates them:

* The identity is ``(side, compare scope, raw expanded name)``. Within one
  circuit net names are unique, and the compare logger's ``scope`` counter
  is one value per compared circuit pair -- so the triple pins one net of
  one side of one circuit, no matter how its display string collides with
  another circuit's.
* The reported id is a 0-based ordinal assigned per side in
  :func:`record_circuit_nets` -- nets of the circuit pair each
  ``begin_circuit`` hands the logger, in ``each_net()`` enumeration order,
  numbered from where the previous circuit pair left off. Same inputs ->
  same compare visit order -> same ids, so repeated runs against the same
  inputs stay diff-clean (the same determinism guarantee ``mismatches[]``
  and ``net_correspondence[]`` already make).
* Ids are unique **within one report and side only** -- they are not
  stable across re-extractions or runs (cross-extraction structural net
  identity is issue #5735's scope, deliberately not this one).

Split out of :mod:`klayout_tools.lvs_mismatch` for the same reason as the
earlier ``lvs_mismatch_tolerance``/``lvs_supply_universe`` moves: a
cohesive, low-coupling subsystem kept out of a file that sits at its
per-file size ceiling. ``lvs_mismatch`` imports :func:`name_key`,
:func:`new_registry`, :func:`pair`, :func:`lookup` and
:func:`record_circuit_nets` from here; this module depends on nothing from
``lvs`` at import time (``pair`` resolves ``_name_or_none`` lazily, the
same deferred-import discipline that module uses).
"""

from __future__ import annotations

from typing import Any

#: The id registry a compare logger carries: ``(side, scope, raw expanded
#: name)`` -> 0-based per-report id, plus its per-side next-id counters.
#: ``side`` is ``"layout"`` or ``"reference"``.
NetIdRegistry = dict[tuple[str, int, str], int]


def new_registry() -> tuple[NetIdRegistry, dict[str, int]]:
    """An empty id registry and its per-side counters -- one pair per
    compare logger (see ``_make_compare_logger``)."""
    return {}, {"layout": 0, "reference": 0}


def record_circuit_nets(
    registry: NetIdRegistry,
    counters: dict[str, int],
    side: str,
    scope: int,
    circuit: Any,
) -> None:
    """Assign a stable id to every net of ``circuit`` (one side of one
    compared circuit pair, at compare ``scope``), numbered from where this
    side's previous circuit pair left off.

    Called from the logger's ``begin_circuit`` -- before any compare event
    of that scope can fire, so every net an event names already has its
    id. Re-entrant per (side, scope, name): a circuit pair handed to
    ``begin_circuit`` twice keeps its first numbering. Defensive against
    stand-in objects (test doubles) that expose neither ``each_net`` nor
    ``expanded_name``: they simply contribute no ids, and every lookup for
    their nets reports ``None``.
    """
    each_net = getattr(circuit, "each_net", None)
    if each_net is None:
        return
    next_id = counters[side]
    for net in each_net():
        expanded = getattr(net, "expanded_name", None)
        if expanded is None:
            continue
        name = expanded()
        if name is None:
            continue
        key = (side, scope, name)
        if key not in registry:
            registry[key] = next_id
            next_id += 1
    counters[side] = next_id


def lookup(
    registry: NetIdRegistry | None, side: str, key: tuple[int, str] | None
) -> int | None:
    """The stable id for one side's ``_NetKey`` (compare scope + raw
    expanded name), or ``None`` when any part is missing (no registry --
    the fake-logger classification tests -- a one-sided event, or a net the
    registry never recorded). ``None`` is the documented "no id on this
    side" spelling, matching the null-not-omitted convention of
    ``net.layout``/``net.reference``."""
    if registry is None or key is None:
        return None
    return registry.get((side, key[0], key[1]))


def name_key(scope: int | None, net: Any) -> tuple[int, str] | None:
    """``net``'s ``_NetKey`` at compare ``scope`` -- the registry's lookup
    key for one side (raw ``expanded_name``, *not* the display string
    ``_name_or_none`` renders). ``None`` when the event carried no net
    object (one-sided), no scope, or the object is a stand-in without
    ``expanded_name``."""
    if scope is None or net is None:
        return None
    expanded = getattr(net, "expanded_name", None)
    if expanded is None:
        return None
    name = expanded()
    return None if name is None else (scope, name)


def pair(
    registry: NetIdRegistry | None,
    a: Any,
    b: Any,
    keys: tuple[tuple[int, str] | None, tuple[int, str] | None],
) -> dict[str, Any]:
    """One ``mismatches[].net``/``net_correspondence[]`` object for an
    event's net pair (issue #5833): the display names ``_name_or_none``
    already produced for the two sides, plus each side's stable id (or
    ``None`` where that side has no net or no recorded id).

    Every producer of those two shapes builds them through this one
    function, so the two new id fields can never drift apart from the
    names they disambiguate -- and every entry carries the keys, never
    omits them (the same null-not-omitted convention ``_mismatch``'s own
    docstring pins for ``net`` itself).
    """
    from .lvs import _name_or_none

    return {
        "layout": _name_or_none(a),
        "layout_id": lookup(registry, "layout", keys[0]),
        "reference": _name_or_none(b),
        "reference_id": lookup(registry, "reference", keys[1]),
    }
