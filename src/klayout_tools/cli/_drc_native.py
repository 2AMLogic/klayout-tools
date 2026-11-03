"""Native-engine deck resolution for ``klt drc`` (issue #4002).

Split out of :mod:`klayout_tools.cli.drc_cmd` to keep that module under its
per-file size ceiling.
"""

from __future__ import annotations

import argparse

from .. import pdk
from ..pdk_native_drc import native_drc_main_script


def resolve_native_deck(
    args: argparse.Namespace,
) -> tuple[str | None, dict[str, str], str | None, dict | None]:
    """Resolve ``(deck_file, deck_vars, min_klayout_version, native_deck)``
    for the native engine (issue #4002).

    ``--deck-file`` wins untouched. Otherwise the variant-named deck
    (:func:`pdk.drc_deck_file`, unchanged for sky130/IHP) is tried, then the
    PDK's family main script (gf180mcu), whose default ``variant`` global
    and version floor apply unless the caller set them (``--deck-var`` and
    ``--min-klayout-version`` override). ``native_deck`` is the additive
    report block, ``None`` when the family script was not used.
    """
    from .drc_cmd import _parse_deck_vars  # lazy: drc_cmd imports this module

    deck_vars = _parse_deck_vars(args.deck_var)
    min_version = args.min_klayout_version
    if args.deck_file is not None:
        return args.deck_file, deck_vars, min_version, None
    deck_file = pdk.drc_deck_file(variant=args.pdk, root=args.pdk_root)
    if deck_file is not None:
        return deck_file, deck_vars, min_version, None
    found = native_drc_main_script(variant=args.pdk, root=args.pdk_root)
    if found is None:
        return None, deck_vars, min_version, None
    defaulted = {k: v for k, v in found["deck_vars"].items() if k not in deck_vars}
    deck_vars = {**defaulted, **deck_vars}
    floor_defaulted = min_version is None
    if floor_defaulted:
        min_version = found["min_klayout_version"]
    native = {
        "kind": found["kind"],
        "variant": found["variant"],
        "deck_file": found["deck_file"],
        "deck_vars_defaulted": defaulted,
        "min_klayout_version": min_version,
        "min_klayout_version_defaulted": floor_defaulted,
    }
    return found["deck_file"], deck_vars, min_version, native


def attach_native_deck(result: dict, native: dict | None) -> dict:
    """Add the additive ``native_deck`` report block when one was resolved."""
    if native is not None and isinstance(result, dict):
        result["native_deck"] = native
    return result
