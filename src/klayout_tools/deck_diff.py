"""Rule-by-rule comparison of two ``klt deck rules`` reports -- the engine
behind ``klt deck diff`` (issue #5680).

A project that pins a deck revision (``provenance.deck.content_hash``) and
commits DRC reports as evidence must, on every pin bump, answer one question:
*which rules changed, and in which direction -- added, removed, or
re-thresholded?* That answer decides whether a new violation means "the
layout was always wrong and the old deck did not check it" or "the deck
changed its mind" -- two findings with opposite responses. Nothing in the CLI
answered it: comparing two saved reports meant two throwaway installs plus a
bespoke comparison script, re-derived by every user, and easy to get subtly
wrong (comparing only rule *ids* misses a rule whose ``value_um`` moved while
its id stayed).

This module is that diff. It is **pure** -- two ``deck rules`` report dicts
in, one JSON-serialisable ``dict`` out -- with no network access and no git
history required, so it can compare a report saved from an old release
against this build's own table (the literal ``installed`` side is resolved by
the CLI layer, not here). It is modelled on
:mod:`klayout_tools.liberty_compare`, the established cross-artifact
comparison precedent in this repo, and follows the same discipline:
**nothing is silently omitted** -- every category key is always present (an
empty list when empty), so "no differences" can be told apart from "never
compared".

What is compared, keyed by rule ``id`` (never by array position --
``rules`` is in deck declaration order, not sorted, so a positional or
textual diff is order-sensitive across revisions while the deck's meaning is
not):

- ``added`` / ``removed`` -- rule ids present on exactly one side (of either
  side's rules *or* ``unmodeled_rules`` table).
- ``changed`` -- ids enforced on both sides, with **only the fields that
  actually moved**, each with its ``from`` and ``to`` value. Every per-rule
  field ``deck_rules()`` emits is compared: ``description``, ``check``,
  ``layers``, ``layer_refs``, ``scope``, ``value_um``, ``value_dbu``,
  ``limits``, ``provenance``.
- ``enforcement_changed`` -- ids that moved between ``rules`` and
  ``unmodeled_rules`` in either direction, as its own category. This is the
  coverage-change signal the issue is about: a rule the deck *stopped
  enforcing* (enforced -> unmodeled) must not read as ``removed``, which
  would be indistinguishable from a deletion, and a rule it *started
  enforcing* must not read as ``added``.
- ``deck_level_changed`` -- envelope-level fields: ``nominal_dbu_um`` (it
  rescales every ``value_dbu``), ``marker_blind_layers``, ``layer_table``,
  ``ambiguous_names``, ``unresolved`` -- plus any unknown top-level field a
  future ``schema_version`` adds to both sides.
- ``not_comparable`` -- fields present on only one side (the expected hazard
  for a cross-revision tool: a newer release may carry envelope or per-rule
  fields an older report predates). They are listed, never read as rule
  changes.

Identity guards: the two sides' ``deck`` names must match (comparing
``sky130`` to ``gf180mcu`` raises :class:`DeckDiffError` -- an error, not a
diff reporting every rule as changed); identical ``content_hash`` on both
sides is reported explicitly as ``same_revision`` in ``identity`` rather than
left to be inferred from an empty diff.
"""

from __future__ import annotations

from typing import Any

#: Version of the diff report's own shape (independent of either input
#: report's ``schema_version``).
SCHEMA_VERSION = 1

#: Every per-rule field compared, in report order -- the full set
#: :func:`klayout_tools.decks.history.deck_rules` projects per rule. A field
#: present on only one side lands in ``not_comparable`` instead of being read
#: as a change.
RULE_FIELDS = (
    "description",
    "check",
    "layers",
    "layer_refs",
    "scope",
    "value_um",
    "value_dbu",
    "limits",
    "provenance",
)

#: The envelope-level fields compared under ``deck_level_changed`` (the
#: ``deck rules`` fields that are not the rule table itself). ``deck``,
#: ``content_hash`` and ``schema_version`` are identity, reported under
#: ``identity``; ``rules``/``unmodeled_rules`` are the rule table.
DECK_FIELDS = (
    "nominal_dbu_um",
    "marker_blind_layers",
    "layer_table",
    "ambiguous_names",
    "unresolved",
)

#: The literal that names this build's own rule table as one side, resolved
#: by the CLI layer via :func:`~klayout_tools.decks.history.deck_rules`.
INSTALLED = "installed"


class DeckDiffError(Exception):
    """Raised when a comparison cannot be made at all: a side is not a
    readable ``deck rules`` report, or the two sides name different decks."""


def compare_deck_rules(
    from_report: dict[str, Any], to_report: dict[str, Any]
) -> dict[str, Any]:
    """Compare two :func:`~klayout_tools.decks.history.deck_rules` reports.

    ``from_report`` is the older side, ``to_report`` the newer one (the
    naming matches ``klt deck diff --from/--to``). Both must carry ``deck``
    (a str), ``rules`` (a list of dicts, each with an ``id``) and
    ``unmodeled_rules`` (a list of ids); everything else is optional and its
    absence on one side is reported, not assumed.

    Raises :class:`DeckDiffError` when either input is not shaped like a
    ``deck rules`` report or the two sides name different decks. Returns a
    JSON-serialisable dict whose top-level ``status`` verdict (``"identical"``
    / ``"different"``) mirrors the CLI's exit code: ``identical`` only when
    every category -- including ``not_comparable`` -- is empty.
    """
    _validate_report(from_report, "--from")
    _validate_report(to_report, "--to")

    from_deck = from_report["deck"]
    to_deck = to_report["deck"]
    if from_deck != to_deck:
        raise DeckDiffError(
            "cannot compare two different decks: --from is "
            f"{from_deck!r} and --to is {to_deck!r} "
            "(deck diff compares one deck across revisions, not two decks)"
        )

    from_enforced = {rule["id"]: rule for rule in from_report["rules"]}
    to_enforced = {rule["id"]: rule for rule in to_report["rules"]}
    from_unmodeled = set(from_report["unmodeled_rules"])
    to_unmodeled = set(to_report["unmodeled_rules"])

    added, removed, enforcement_changed, changed, not_comparable_rules = _compare_rules(
        from_enforced, from_unmodeled, to_enforced, to_unmodeled
    )
    deck_level_changed, not_comparable_envelope = _compare_envelope(
        from_report, to_report
    )

    from_hash = from_report.get("content_hash")
    to_hash = to_report.get("content_hash")
    same_revision = (
        from_hash is not None and to_hash is not None and from_hash == to_hash
    )
    identical = not (
        added
        or removed
        or changed
        or enforcement_changed
        or deck_level_changed
        or not_comparable_envelope
        or not_comparable_rules
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "identical" if identical else "different",
        "identity": {
            "from": {
                "deck": from_deck,
                "content_hash": from_hash,
                "schema_version": from_report.get("schema_version"),
            },
            "to": {
                "deck": to_deck,
                "content_hash": to_hash,
                "schema_version": to_report.get("schema_version"),
            },
            "same_revision": same_revision,
        },
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "enforcement_changed": len(enforcement_changed),
            "deck_level_changed": len(deck_level_changed),
            "not_comparable": len(not_comparable_envelope) + len(not_comparable_rules),
            "rules_enforced": {"from": len(from_enforced), "to": len(to_enforced)},
            "unmodeled_rules": {"from": len(from_unmodeled), "to": len(to_unmodeled)},
            "identical": identical,
        },
        "added": added,
        "removed": removed,
        "changed": changed,
        "enforcement_changed": enforcement_changed,
        "deck_level_changed": deck_level_changed,
        "not_comparable": {
            "envelope_fields": not_comparable_envelope,
            "rule_fields": not_comparable_rules,
        },
    }


def _validate_report(report: Any, side: str) -> None:
    """Reject, with the offending side named, an input that is not a
    ``deck rules`` report. Only the fields the comparison keys on are
    required; everything else may legitimately be absent from an older
    release's report."""
    if not isinstance(report, dict):
        raise DeckDiffError(
            f"{side} report is not a `klt deck rules --format json` object "
            f"(got {type(report).__name__})"
        )
    deck = report.get("deck")
    if not isinstance(deck, str) or not deck:
        raise DeckDiffError(
            f"{side} report carries no deck name (missing or non-string "
            "'deck' field) -- is it a `klt deck rules --format json` report?"
        )
    rules = report.get("rules")
    if not isinstance(rules, list):
        raise DeckDiffError(
            f"{side} report ({deck!r}) carries no rule table (missing or "
            "non-list 'rules' field) -- is it a `klt deck rules --format "
            "json` report?"
        )
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str):
            raise DeckDiffError(
                f"{side} report ({deck!r}) has a rule entry without a string "
                "'id'; the comparison keys rules by id"
            )
    unmodeled = report.get("unmodeled_rules")
    if not isinstance(unmodeled, list) or not all(
        isinstance(rule_id, str) for rule_id in unmodeled
    ):
        raise DeckDiffError(
            f"{side} report ({deck!r}) carries no 'unmodeled_rules' id list "
            "(missing or non-list-of-strings) -- is it a `klt deck rules "
            "--format json` report?"
        )


def _compare_rules(
    from_enforced: dict[str, dict],
    from_unmodeled: set[str],
    to_enforced: dict[str, dict],
    to_unmodeled: set[str],
) -> tuple[list[str], list[str], list[dict], list[dict], list[dict]]:
    """The rule-table categories: ``added``, ``removed``,
    ``enforcement_changed``, ``changed`` and the per-rule ``not_comparable``
    entries, each sorted by rule id."""
    from_universe = from_enforced.keys() | from_unmodeled
    to_universe = to_enforced.keys() | to_unmodeled

    added = sorted(to_universe - from_universe)
    removed = sorted(from_universe - to_universe)

    # Ids both sides know about whose enforcement status moved: precisely the
    # "deck changed its mind" vs "was never checked" signal, kept out of
    # added/removed (issue #5680's Verified corrections, entry 2).
    enforcement_changed = []
    for rule_id in sorted(from_universe & to_universe):
        was = "enforced" if rule_id in from_enforced else "unmodeled"
        now = "enforced" if rule_id in to_enforced else "unmodeled"
        if was != now:
            enforcement_changed.append({"id": rule_id, "from": was, "to": now})

    changed: list[dict] = []
    not_comparable: list[dict] = []
    for rule_id in sorted(from_enforced.keys() & to_enforced.keys()):
        from_rule = from_enforced[rule_id]
        to_rule = to_enforced[rule_id]
        fields: dict[str, dict[str, Any]] = {}
        for field in sorted((set(from_rule) | set(to_rule)) - {"id"}):
            if field not in from_rule or field not in to_rule:
                not_comparable.append({"id": rule_id, "field": field})
            elif from_rule[field] != to_rule[field]:
                fields[field] = {"from": from_rule[field], "to": to_rule[field]}
        if fields:
            changed.append({"id": rule_id, "fields": fields})

    return added, removed, enforcement_changed, changed, not_comparable


def _compare_envelope(
    from_report: dict[str, Any], to_report: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """The deck-level category and the envelope-level ``not_comparable``
    entries. ``deck``, ``content_hash`` and ``schema_version`` are identity
    (reported under ``identity``); ``rules``/``unmodeled_rules`` are the rule
    table; ``id``-keyed per-rule fields are handled by
    :func:`_compare_rules`."""
    handled = {"deck", "content_hash", "schema_version", "rules", "unmodeled_rules"}
    from_keys = set(from_report) - handled
    to_keys = set(to_report) - handled

    deck_level_changed: dict[str, dict[str, Any]] = {}
    for field in sorted(from_keys & to_keys):
        if from_report[field] != to_report[field]:
            deck_level_changed[field] = {
                "from": from_report[field],
                "to": to_report[field],
            }

    not_comparable = sorted(from_keys ^ to_keys)
    return deck_level_changed, not_comparable
