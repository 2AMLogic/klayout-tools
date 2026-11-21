"""``klt deck`` argument parser (moved out of ``parser.py`` as a pure move:
``parser.py`` is at its file-size ratchet ceiling and may not grow, and the
``deck diff`` subcommand (issue #5680) is what would have grown it). Parser
registration lives here; the ``run_*`` handlers stay in :mod:`.deck_cmd`.
Parser-wide helpers are callables the caller passes in, so this module never
imports :mod:`parser` back. See docs/cli/deck.md.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from .. import deck_cmd


def add_deck_parser(
    subparsers: argparse._SubParsersAction,
    _no_subcommand_handler: Callable[..., Any],
    _add_format_arg: Callable[..., Any],
    _deck_names_str: Callable[[], str],
    _drc_deck_names_str: Callable[[], str],
) -> None:
    """Register the ``deck`` verb with nested ``resolve`` (issue #623),
    ``hash`` (issue #1202), ``info`` (issue #1209), and ``rules`` (issue
    #2308) subcommands, mirroring ``pdk``/``kb``'s grouped-verb pattern (see
    ``_add_pdk_parser``) so future deck-identity/deck-history operations
    (e.g. listing the full table) have a natural home alongside them.
    """
    deck_parser = subparsers.add_parser(
        "deck",
        help="identify a built-in rule deck, resolve one to its release, "
        "report the installed build's own deck coverage, list the rule "
        "values a deck enforces, or diff two deck revisions",
        description=(
            "Identify klt's built-in DRC/LVS rule decks (sky130, gf180mcu, "
            "sg13g2). `hash` reports the `provenance.deck.content_hash` this "
            "build resolves for a deck, with no layout file and no check run "
            "-- the cheap way to gate on the deck revision a build will use "
            "before doing any work. `resolve` goes the other way: it turns a "
            "pinned `provenance.deck.content_hash` (docs/json-contract.md) "
            "or a `deck name + version` can be turned back into the "
            "klayout-tools git tag/commit and PyPI version that shipped it "
            "-- without hand-bisecting this repo's git history to reproduce "
            "against an older deck revision (`resolve`), or report this "
            "install's own deck content hash, structural device-class "
            "coverage, and release status directly, with no input layout "
            "needed (`info`). `rules` reports what the deck actually "
            "requires -- every rule's id, description, and threshold in "
            "micrometres, with the deck's content hash -- so pre-layout "
            "arithmetic can read a constant from the installed deck instead "
            "of transcribing it out of a deck comment. `diff` compares two "
            "saved `deck rules` reports rule by rule, so a pin bump can be "
            "answered (added / removed / re-thresholded / enforcement "
            "changed) offline instead of by hand-diffing two installs. "
            "Resolve-only: this does not fetch, check out, or build a "
            "historical revision -- install the reported version yourself "
            "to reproduce against it."
        ),
    )
    deck_sub = deck_parser.add_subparsers(dest="deck_command", metavar="<subcommand>")

    deck_parser.set_defaults(func=_no_subcommand_handler(deck_parser))

    resolve_parser = deck_sub.add_parser(
        "resolve",
        help="resolve a deck's content hash or (name, version) to a release",
        description=(
            "Look up a deck query against the generated hash/version -> "
            "release history table. Give either --content-hash (optionally "
            "narrowed with --deck), or both --deck and --version together."
        ),
    )
    resolve_parser.add_argument(
        "--content-hash",
        dest="content_hash",
        default=None,
        help=(
            "sha256:<hex> deck content hash to resolve, e.g. as reported by "
            "`klt drc --deck sky130`'s provenance.deck.content_hash"
        ),
    )
    resolve_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "deck name (e.g. sky130, gf180mcu); narrows a --content-hash "
            "query, or combines with --version for an exact lookup"
        ),
    )
    resolve_parser.add_argument(
        "--version",
        default=None,
        help="klayout-tools package version to look up (requires --deck)",
    )
    resolve_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    resolve_parser.set_defaults(func=deck_cmd.run_resolve)

    hash_parser = deck_sub.add_parser(
        "hash",
        help="report the content hash this build resolves for a deck",
        description=(
            "Report the `provenance.deck.content_hash` this build would "
            "record for a built-in deck -- with no layout file and no check "
            "run (issue #1202). The hash, not the package version, is what "
            "pins a run's rule set, so gating on it used to mean running a "
            "throwaway `klt drc` on whatever layout happened to be around "
            "just to read the hash back out of the report. This answers the "
            "same question directly, so a CI preflight or container smoke "
            "test can check the deck it is about to use before doing any "
            "work. Pair with `klt deck resolve --content-hash <hash>` to "
            "turn the answer into the release that shipped it."
        ),
    )
    hash_parser.add_argument(
        "--deck",
        required=True,
        help=f"built-in deck name to identify (e.g. {_deck_names_str()})",
    )
    _add_format_arg(hash_parser)
    hash_parser.set_defaults(func=deck_cmd.run_hash)

    path_parser = deck_sub.add_parser(
        "path",
        help="print the installed path of a deck's standalone DRC script",
        description=(
            "Print the absolute path of a built-in deck's standalone KLayout "
            "DRC-DSL script (issue #3789) -- a file usable as `klt drc "
            "--engine klayout --deck-file <path>` from a wheel / `uv tool` "
            "install, where the repo-relative src/ path does not exist. Only "
            "asap7 ships such a script; the Python rule-table decks exit 1."
        ),
    )
    path_parser.add_argument(
        "--deck",
        required=True,
        help="built-in deck name whose script path to print (asap7)",
    )
    _add_format_arg(path_parser)
    path_parser.set_defaults(func=deck_cmd.run_path)

    info_parser = deck_sub.add_parser(
        "info",
        help="report the installed build's own deck content hash, device "
        "coverage, and release status",
        description=(
            "Report this klt install's own deck content hash, structural "
            "device-class coverage (ExtractionDeck.device_classes -- e.g. "
            "whether diode recognition is wired up at all), and release "
            "status -- with no input layout needed. Complements `klt deck "
            "resolve`: `resolve` turns a hash you already have into a "
            "release; `info` tells you your own currently-running hash and "
            "coverage without first running an extraction (issue #1209 -- "
            "two installs can report the identical `klt --version` string "
            "while shipping different deck content)."
        ),
    )
    info_parser.add_argument(
        "--deck",
        default=None,
        help="deck name (e.g. sky130, gf180mcu) to report; omit to report "
        "every registered deck",
    )
    info_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="output format (default: text)",
    )
    info_parser.set_defaults(func=deck_cmd.run_info)

    rules_parser = deck_sub.add_parser(
        "rules",
        help="list the rule values a built-in deck enforces (id, "
        "description, threshold in um), with no layout needed",
        description=(
            "Report the *numbers* a built-in deck enforces -- every "
            "registered rule's id, description, check kind, drawn layers, "
            "and distance threshold in micrometres -- alongside the deck's "
            "own content hash, with no layout file and no check run (issue "
            "#2308). `klt drc` reports violations and therefore needs a "
            "stream; this answers the pre-layout question (area budgeting, "
            "device pitch, whether a proposed segmentation is drawable at "
            "all) that has to be settled before any GDS exists. Rule ids "
            "are this deck's own (`poly.width.1`), not the upstream PDK's "
            "numbering -- that is reported per rule under "
            "`provenance.rule_id`. Pair the reported `content_hash` with a "
            "cited constant to make the citation re-checkable against the "
            "installed deck on the next PDK bump."
        ),
    )
    rules_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "built-in curated-DRC deck name whose rules to list "
            f"(e.g. {_drc_deck_names_str()})"
        ),
    )
    rules_parser.add_argument(
        "--rule",
        default=None,
        help=(
            "narrow to a single rule by exact id (e.g. poly.width.1); an id "
            "the deck does not carry is a clean error, not an empty list"
        ),
    )
    _add_format_arg(rules_parser)
    rules_parser.set_defaults(func=deck_cmd.run_rules)

    devices_parser = deck_sub.add_parser(
        "devices",
        help="list the drawn-layer predicate set (marker + per-terminal "
        "requires/excludes) each of a deck's device classes is recognised by",
        description=(
            "Report what has to be **drawn** for each of an extraction "
            "deck's device classes to be recognised -- its device-"
            "recognition marker layer plus every per-terminal "
            "requires/excludes predicate, with layer/datatype numbers -- "
            "alongside the deck's own content hash, with no layout file and "
            "no extraction run (issue #2365). `klt deck info` says which "
            "device classes a deck *can* recognise; this says what a layout "
            "must draw for one to be found. Most classes are driven off "
            "drawn conductor/diffusion geometry alone, but a minority are "
            "gated on a marker layer and/or implant predicates (filter on "
            "`marker_gated`/`predicate_gated`) -- and a layout missing any "
            "single member of that set extracts as `device_count: 0`, which "
            "reads exactly like 'this layout contains none of that device'. "
            "`klt extract` names the missing layer in `warnings[]` for a "
            "junction-diode near miss; this answers the same question "
            "ahead of time, for every class."
        ),
    )
    devices_parser.add_argument(
        "--deck",
        required=True,
        help=(
            "built-in extraction deck name whose device classes to list "
            f"(e.g. {_deck_names_str()})"
        ),
    )
    devices_parser.add_argument(
        "--class",
        dest="device_class",
        default=None,
        help=(
            "narrow to a single device class by exact name (e.g. "
            "diode_pd2nw_06v0); a name the deck does not declare is a clean "
            "error, not an empty list"
        ),
    )
    _add_format_arg(devices_parser)
    devices_parser.set_defaults(func=deck_cmd.run_devices)

    diff_parser = deck_sub.add_parser(
        "diff",
        help="compare two saved `deck rules` reports (or one against the "
        "installed build) rule by rule: added / removed / changed rules, "
        "enforcement moves, deck-level changes",
        description=(
            "Compare two saved `klt deck rules --format json` reports rule "
            "by rule -- the offline answer to 'which rules changed, and in "
            "which direction?' on every pin bump of "
            "provenance.deck.content_hash (issue #5680). Each side is a "
            "path to a saved report, or the literal `installed` for this "
            "build's own table, so the common check needs one install: "
            "`uvx --from klayout-tools==<old> klt deck rules --deck sky130 "
            "--format json > old.json` then `klt deck diff --from old.json "
            "--to installed`. Rules are matched by id, never by array "
            "position (the rule table is in deck declaration order, not "
            "sorted, so reordering is not a change). Report categories: "
            "`added`/`removed` (ids on exactly one side), `changed` (only "
            "the fields that moved, each with from/to), "
            "`enforcement_changed` (a rule moving between `rules` and "
            "`unmodeled_rules` in either direction -- the deck changed its "
            "mind vs was never checked, never reported as added/removed), "
            "`deck_level_changed` (nominal_dbu_um -- it rescales every "
            "value_dbu -- marker_blind_layers, layer_table, "
            "ambiguous_names, unresolved) and `not_comparable` (fields "
            "only one side carries -- the expected cross-schema_version "
            "hazard, listed rather than read as a change). Resolve-only "
            "like every deck verb: this never fetches, checks out, or "
            "installs the other revision -- save its report with uvx "
            "yourself. Exit 0 when the sides are identical, 3 when any "
            "difference is reported (report still on stdout, the `klt drc` "
            "verdict-exit precedent -- gate a pin bump on the bare exit "
            "code), 1 for an unreadable report or two different decks."
        ),
    )
    diff_parser.add_argument(
        "--from",
        dest="from_side",
        required=True,
        help=(
            "path to a saved `klt deck rules --format json` report (the "
            "older side), or the literal 'installed' for this build's own "
            "table"
        ),
    )
    diff_parser.add_argument(
        "--to",
        dest="to_side",
        required=True,
        help=(
            "path to a saved `klt deck rules --format json` report (the "
            "newer side), or the literal 'installed' for this build's own "
            "table"
        ),
    )
    diff_parser.add_argument(
        "--deck",
        default=None,
        help=(
            "deck name to resolve an 'installed' side for; defaults to the "
            "other side's own deck field (only needed when both sides are "
            "'installed')"
        ),
    )
    _add_format_arg(diff_parser)
    diff_parser.set_defaults(func=deck_cmd.run_diff)
