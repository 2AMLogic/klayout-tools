"""``klt deck`` commands: identify a built-in rule deck, and resolve one back
to the release that shipped it.

- ``resolve`` (issue #623) -- turn a pinned ``provenance.deck.content_hash``
  (or a ``(name, version)`` pair) into the klt release that shipped it, via
  the generated release-history table.
- ``hash`` (issue #1202) -- report the ``content_hash`` *this* build resolves
  for a deck, with no layout file and no check run. Answers "which deck
  revision will this build use?" before doing any work; ``resolve`` answers
  the complementary "which release shipped that revision?".
- ``info`` (issue #1209) -- report the currently-installed build's own deck
  content hash / structural device-class coverage / release status directly,
  for one deck or every registered deck, with no input layout needed.
  Complements ``hash``: ``hash`` is a single deck's identity, ``info`` adds
  device-class coverage and can report on every registered deck at once.
- ``rules`` (issue #2308) -- report the *numbers* a deck enforces: every
  registered ``DrcRule``'s id, description, and threshold (in micrometres),
  alongside the deck's own ``content_hash``, with no layout and no check
  run. ``info`` says which deck you have; ``rules`` says what it requires,
  so pre-layout arithmetic can read a constant from the installed deck
  rather than transcribing it out of a deck comment.
- ``devices`` (issue #2365) -- report the *drawn-layer predicate set* every
  device class an extraction deck declares is recognised by: its
  device-recognition marker layer plus each terminal's ``requires``/
  ``excludes`` narrowing, with layer/datatype numbers. ``info`` says which
  device classes a deck can recognise; this says what has to be **drawn**
  for one to be recognised, so a near-miss layout does not have to be
  diagnosed by reading the deck module's Python source.

All five verbs emit through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md``.

Exit codes:
    0 - the query succeeded
    1 - failed to run (invalid query, a query that matched no known release,
        or an unknown deck name) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

from __future__ import annotations

import argparse

from .._provenance import UnknownProvenanceDeckError, deck_identity
from ..decks.history import (
    DeckHistoryError,
    deck_devices,
    deck_info,
    deck_rules,
    resolve_deck,
)
from .output import emit_error, emit_success


def run_resolve(args: argparse.Namespace) -> int:
    try:
        report = resolve_deck(
            content_hash=args.content_hash, deck=args.deck, version=args.version
        )
    except DeckHistoryError as exc:
        return emit_error("deck resolve", str(exc), args.format)

    emit_success(report, args.format, _print_resolve_text)
    return 0


def run_hash(args: argparse.Namespace) -> int:
    try:
        block = deck_identity(args.deck)
    except UnknownProvenanceDeckError as exc:
        return emit_error("deck hash", str(exc), args.format)

    if block["content_hash"] is None:
        return emit_error(
            "deck hash",
            f"deck '{args.deck}' is registered but its source file could not "
            "be read, so no content hash can be computed",
            args.format,
        )

    # Flat payload, mirroring `deck resolve`'s shape: `deck`/`content_hash`
    # are named exactly as the provenance block names them, so a consumer
    # comparing against a committed report's `provenance.deck` is comparing
    # like with like.
    payload = {
        "schema_version": 1,
        "deck": block["name"],
        "content_hash": block["content_hash"],
        "released": block["released"],
    }
    emit_success(payload, args.format, _print_hash_text)
    return 0


def _print_hash_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"released: {_released_text(report['released'])}")


def _released_text(released: bool | None) -> str:
    """Render the tri-state ``released`` signal for humans -- ``unknown`` is
    deliberately distinct from ``no`` (see ``docs/json-contract.md``)."""
    if released is None:
        return "unknown"
    return "yes" if released else "no"


def _print_resolve_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"git_tag: {report['git_tag']}")
    print(f"git_commit: {report['git_commit']}")
    print(f"package_version: {report['package_version']}")


def run_info(args: argparse.Namespace) -> int:
    try:
        report = deck_info(name=args.deck)
    except DeckHistoryError as exc:
        return emit_error("deck info", str(exc), args.format)

    emit_success(report, args.format, _print_info_text)
    return 0


def run_rules(args: argparse.Namespace) -> int:
    try:
        report = deck_rules(name=args.deck, rule_id=args.rule)
    except DeckHistoryError as exc:
        return emit_error("deck rules", str(exc), args.format)

    emit_success(report, args.format, _print_rules_text)
    return 0


def run_devices(args: argparse.Namespace) -> int:
    try:
        report = deck_devices(name=args.deck, device_class=args.device_class)
    except DeckHistoryError as exc:
        return emit_error("deck devices", str(exc), args.format)

    emit_success(report, args.format, _print_devices_text)
    return 0


def _layer_text(entry: dict | None) -> str:
    """One layer as ``"Pplus (31/0)"`` -- the deck's published name plus the
    raw GDS pair, or just the pair when the deck publishes no name for it.
    Never drops the numbers: they are what a caller draws against."""
    if entry is None:
        return "none"
    pair = f"{entry['layer']}/{entry['datatype']}"
    name = entry.get("name")
    return f"{name} ({pair})" if name else pair


def _layers_text(entries: list) -> str:
    return ", ".join(_layer_text(entry) for entry in entries) if entries else "none"


def _print_devices_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"device_classes: {len(report['device_classes'])}")
    for entry in report["device_classes"]:
        gating = []
        if entry["marker_gated"]:
            gating.append("marker-gated")
        if entry["predicate_gated"]:
            gating.append("predicate-gated")
        print()
        print(f"{entry['name']} ({entry['kind']})")
        print(f"  gating: {', '.join(gating) if gating else 'drawn geometry only'}")
        print(f"  marker: {_layer_text(entry['marker'])}")
        for terminal in entry["terminals"]:
            print(f"  terminal {terminal['name']}:")
            print(f"    layer: {_layer_text(terminal['layer'])}")
            print(f"    requires: {_layers_text(terminal['requires'])}")
            print(f"    excludes: {_layers_text(terminal['excludes'])}")
        for flavour in entry.get("flavours", []):
            print(f"  flavour {flavour['flavour']}: {_layer_text(flavour['marker'])}")
        print(f"  required_layers: {_layers_text(entry['required_layers'])}")
        print(f"  excluded_layers: {_layers_text(entry['excluded_layers'])}")
        print(f"  provenance: {_provenance_text(entry['provenance'])}")


def _print_rules_text(report: dict) -> None:
    print(f"deck: {report['deck']}")
    print(f"content_hash: {report['content_hash']}")
    print(f"nominal_dbu_um: {report['nominal_dbu_um']}")
    print(f"rules: {len(report['rules'])}")
    for rule in report["rules"]:
        print()
        print(f"{rule['id']} ({rule['check']})")
        print(f"  description: {rule['description']}")
        if rule["value_um"] is not None:
            print(f"  value_um: {rule['value_um']}")
        for key, value in rule["limits"].items():
            print(f"  {key}: {value}")
        print(f"  layers: {', '.join(rule['layers'])}")
        if rule["scope"]:
            print(f"  scope: {rule['scope']}")
        print(f"  provenance: {_provenance_text(rule['provenance'])}")


def _provenance_text(provenance: dict | None) -> str:
    """One-line rendering of a rule's upstream citation -- ``none`` when the
    rule carries no structured provenance (its inline comment in the deck
    module remains the only record), deliberately distinct from a rule that
    cites a source but pins no commit."""
    if provenance is None:
        return "none"
    commit = provenance["commit"]
    pinned = f" @ {commit}" if commit else ""
    return (
        f"{provenance['source_repo']}:{provenance['source_path']} "
        f"rule {provenance['rule_id']}{pinned}"
    )


def _print_info_text(report: dict) -> None:
    for entry in report["decks"]:
        print(f"deck: {entry['deck']}")
        print(f"  content_hash: {entry['content_hash']}")
        print(f"  device_classes: {', '.join(entry['device_classes'])}")
        print(f"  released: {entry['released']}")
        release = entry["release"]
        if release is not None:
            print(
                f"  release: {release['package_version']} "
                f"({release['git_tag']}, {release['git_commit']})"
            )
