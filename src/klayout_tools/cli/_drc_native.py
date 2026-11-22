"""Native-engine deck resolution for ``klt drc`` (issue #4002).

Split out of :mod:`klayout_tools.cli.drc_cmd` to keep that module under its
per-file size ceiling.
"""

from __future__ import annotations

import argparse

from .. import pdk
from ..drc import DrcError
from ..pdk_native_drc import native_drc_main_script
from ._parsing import parse_deck_options


def _family_variant(args: argparse.Namespace) -> tuple[str | None, str | None]:
    """``(variant, error)``: the PDK variant constrained to ``--deck``'s family
    (issue #5211). ``error`` carries the :class:`pdk.PdkNotFoundError` message
    when no install of the deck's family exists (or ``--pdk`` is another
    family); a deck-less or unknown-family request is unconstrained."""
    deck_name = getattr(args, "deck", None)
    if not deck_name:
        return args.pdk, None
    try:
        info = pdk.find_pdk_for_deck(deck_name, variant=args.pdk, root=args.pdk_root)
    except pdk.PdkNotFoundError as exc:
        return None, str(exc)
    return info.get("variant", args.pdk), None


def native_family_error(args: argparse.Namespace) -> str | None:
    """Why the native engine cannot run for ``--deck`` (issue #5211), or
    ``None``. ``--deck-file`` bypasses the constraint."""
    if args.deck_file is not None:
        return None
    return _family_variant(args)[1]


def no_deck_message(args: argparse.Namespace) -> str:
    """The ``DrcError`` text for ``--engine klayout`` finding no deck script."""
    family_error = native_family_error(args)
    if family_error is not None:
        return f"no PDK-native klayout DRC deck: {family_error}"
    return (
        "no PDK-native klayout DRC deck script found for this PDK "
        "variant -- pass --deck-file to point at one directly (see "
        "docs/cli/drc.md, 'Engine' -> 'klayout')"
    )


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

    Repeatable ``--deck-var NAME=VALUE`` (issue #1302) becomes the
    ``deck_vars`` mapping the engine threads through as extra ``-rd`` script
    globals. It is parsed by the shared
    :func:`._parsing.parse_deck_options` (issue #6738), so ``klt drc``,
    ``klt lvs`` and ``klt extract`` accept one spelling and raise
    :class:`DrcError` (a clean exit 1) for a malformed entry. Only the
    klayout engine reaches this resolver, so under ``--engine curated`` the
    flag is ignored, not rejected -- the treatment ``--deck-file`` and
    ``--timeout-s`` get too.
    """
    deck_vars = parse_deck_options(args.deck_var, DrcError, flag="--deck-var") or {}
    min_version = args.min_klayout_version
    if args.deck_file is not None:
        return args.deck_file, deck_vars, min_version, None
    variant, error = _family_variant(args)
    if error is not None:
        return None, deck_vars, min_version, None
    deck_file = pdk.drc_deck_file(variant=variant, root=args.pdk_root)
    if deck_file is not None:
        return deck_file, deck_vars, min_version, None
    found = native_drc_main_script(variant=variant, root=args.pdk_root)
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
