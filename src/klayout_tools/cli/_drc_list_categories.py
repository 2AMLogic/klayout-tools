"""``klt drc --engine klayout --list-rule-categories`` CLI handler (#5444).

A fourth input mode beside ``file`` / ``--check`` / ``--list-rules``: it
reads no layout and reaches no verdict, it derives what a PDK-native deck
*declares* so the result can be fed straight back in as
``--expect-rule-categories-file``. Kept out of :mod:`.drc_cmd` so that
module stays under its size ceiling.

The engine side -- how the set is derived and what it does and does not
establish -- is :mod:`klayout_tools.drc_klayout_list`.
"""

from __future__ import annotations

import argparse
import sys

from ..drc import DrcError
from ..drc_klayout_list import list_klayout_deck_rule_categories
from ..pdk import PdkNotFoundError
from . import _drc_native as native_deck
from .output import emit_error, emit_success

#: ``dest``s this mode accepts: the native-deck selectors and the run knobs
#: that shape the KLayout invocation itself. Everything else -- a layout
#: flag (``--top``, ``--rules``, ``--waivers``, ``--tile``), an assertion
#: (``--expect-rule-categories[-file]``), a verdict-shaping flag
#: (``--max-violations``, ``--allow-deck-errors``, ``-o``) -- is **rejected,
#: not ignored**: a caller passing one almost certainly meant to run DRC.
#:
#: ``deck`` is in the set because it is a native-deck *selector*: it
#: constrains PDK-family resolution (#5211) and `provenance.pdk` (#4474),
#: so ``--pdk`` must resolve the same deck here as it does for a run.
ACCEPTED_DESTS = (
    "allow_missing_host_tools",
    "deck",
    "deck_file",
    "deck_var",
    "engine",
    "klayout_binary",
    "min_klayout_version",
    "pdk",
    "pdk_root",
    "timeout_s",
)

#: The option strings behind :data:`ACCEPTED_DESTS`, for the refusal text.
_ACCEPTED_OPTIONS = (
    "--deck, --deck-file, --pdk, --pdk-root, --deck-var, --timeout-s, "
    "--klayout-binary, --min-klayout-version, --allow-missing-host-tools"
)


def print_rule_category_list(listing: dict) -> None:
    """``--format text``: **exactly** the category names on stdout, one per
    line, so that

        klt drc --engine klayout --deck-file D --list-rule-categories > D.categories.txt

    is directly a valid ``--expect-rule-categories-file`` manifest (that
    loader reads one name per line and has no comment syntax). Every note --
    the count, the limitations, any deck-globals warning -- goes to stderr,
    where redirecting stdout leaves it visible rather than in the manifest.
    """
    for name in listing["rule_categories"]:
        print(name)

    notes = [
        f"{listing['rule_category_count']} rule category/categories declared "
        f"by {listing['deck_file']} (basis: {listing['basis']})"
    ]
    if listing["deck_vars"]:
        pairs = ", ".join(f"{k}={v}" for k, v in sorted(listing["deck_vars"].items()))
        notes.append(f"deck vars: {pairs}")
    else:
        notes.append("deck vars: (none)")
    notes.extend(
        f"limitation [{e['category']}]: {e['text']}" for e in listing["limitations"]
    )
    notes.extend(
        f"warning [{w['category']}]: {w['text']}" for w in listing.get("warnings", [])
    )
    for note in notes:
        print(note, file=sys.stderr)


def run_list_rule_categories(
    args: argparse.Namespace, non_default_flags, exit_ok: int
) -> int:
    """Handle ``--list-rule-categories``.

    ``non_default_flags`` is :func:`.drc_cmd._non_default_flags` (passed in
    rather than imported, so this module does not import its own importer);
    ``exit_ok`` is that module's ``EXIT_CLEAN``. A listing has no verdict,
    so success is always that code -- it never borrows a DRC status.
    """
    try:
        if args.engine != "klayout":
            raise DrcError(
                "--list-rule-categories requires --engine klayout: it "
                "derives a PDK-native deck's declared rule categories by "
                "running that deck. The curated engine's rules are listed "
                "with --list-rules instead (see docs/cli/drc.md, 'Rule "
                "listing and selection')"
            )
        extra = non_default_flags(args, allowed=ACCEPTED_DESTS)
        if extra:
            raise DrcError(
                f"--list-rule-categories takes only {_ACCEPTED_OPTIONS}; "
                f"also given: {', '.join(extra)}"
            )
        deck_file, deck_vars, min_version, native = native_deck.resolve_native_deck(
            args
        )
        if deck_file is None:
            raise DrcError(native_deck.no_deck_message(args))
        listing = native_deck.attach_native_deck(
            list_klayout_deck_rule_categories(
                deck_file,
                timeout_s=args.timeout_s,
                deck_vars=deck_vars,
                pdk_variant=args.pdk,
                pdk_root=args.pdk_root,
                deck_name=args.deck or None,
                allow_missing_host_tools=args.allow_missing_host_tools,
                min_klayout_version=min_version,
                klayout_binary=args.klayout_binary,
            ),
            native,
        )
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format, **vars(exc))
    except PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    emit_success(listing, args.format, print_rule_category_list)
    return exit_ok
