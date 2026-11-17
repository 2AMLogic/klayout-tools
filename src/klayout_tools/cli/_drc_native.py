"""Native-engine deck resolution for ``klt drc`` (issue #4002).

Split out of :mod:`klayout_tools.cli.drc_cmd` to keep that module under its
per-file size ceiling.
"""

from __future__ import annotations

import argparse

from .. import pdk
from ..drc import DrcError
from ..pdk_native_drc import native_drc_main_script


def _parse_deck_vars(raw: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--deck-var name=value`` values (issue #1302) into
    the ``deck_vars`` mapping
    :func:`~klayout_tools.drc_klayout_engine.run_drc_klayout_engine` (and
    :func:`~klayout_tools.drc_klayout_list.list_klayout_deck_rule_categories`)
    thread through as extra ``-rd`` script globals. Silently ignored
    (returns ``{}``) when ``--engine curated`` is selected -- ``--deck-var``
    is a klayout-engine-only flag, the same "ignored, not rejected"
    treatment ``--deck-file`` and ``--timeout-s`` already get under the
    curated engine.

    Lives here, beside its only caller :func:`resolve_native_deck`, rather
    than in ``drc_cmd`` -- which used to force a lazy import to break the
    cycle (issue #5444).

    Raises :class:`DrcError` for a malformed value missing the required
    ``=`` separator -- a clean error (exit 1) rather than a confusing
    ``KeyError``/silent no-op.
    """
    if not raw:
        return {}
    deck_vars: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep:
            raise DrcError(
                f"invalid --deck-var {item!r}: expected 'name=value' "
                "(e.g. --deck-var feol=true)"
            )
        deck_vars[name] = value
    return deck_vars


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
    """
    deck_vars = _parse_deck_vars(args.deck_var)
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
