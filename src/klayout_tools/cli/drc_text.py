"""Text renderers for ``klt drc`` (split out of :mod:`.drc_cmd`, issue #4950,
to keep that module under its file-size ceiling). Pure formatting: each
function prints one section of the human-readable report."""

from ..drc import SKIP_REASON_NOT_SELECTED
from ..env_provenance import render_path_field


def _print_coverage_gaps(coverage: dict) -> None:
    """The text form's "here is what was not looked at" lines.

    Two different gaps, both of which a bare ``status:`` line would hide:

    - **Skipped requested rules** (issue #2110): the common block's
      ``skipped`` list -- rules the deck asked for that did not run and could
      have found something. This is exactly what makes a run
      ``clean_partial``, so the text form names the rules rather than letting
      a partial run read as an unqualified pass. Rules with nothing
      applicable to check land in ``inapplicable`` instead and are
      deliberately *not* printed here: they are not a gap (see
      ``docs/cli/drc.md``).
    - **Unchecked stream layers** (issue #189): geometry drawn on layers no
      active rule references at all.
    """
    # Issue #2768: rules deliberately left out of a `--rules` subset are
    # summarised by `_print_rule_selection` instead of listed one per line.
    skipped_requested = [
        record
        for record in coverage.get("skipped") or []
        if record.get("reason") != SKIP_REASON_NOT_SELECTED
    ]
    if skipped_requested:
        print(f"skipped requested rules: {len(skipped_requested)}")
        for record in skipped_requested:
            print(f"  {record['id']}: {record['reason']}")

    unchecked_layers = coverage["layers_in_stream_without_rules"]
    if unchecked_layers:
        print(f"unchecked layers in stream: {len(unchecked_layers)}")

    # Issue #3796: deck-static foundry rules this deck knowingly omits, so a
    # text-mode "clean" is not read as full runset coverage.
    unmodeled = coverage.get("unmodeled_rules") or []
    if unmodeled:
        print(
            f"warning: {len(unmodeled)} foundry rule(s) not enforced by this "
            f"deck: {_abbreviate(unmodeled)} (see coverage.unmodeled_rules)"
        )


def _print_coverage_assertion(assertion: dict | None) -> None:
    """One line summarizing a caller-vouched rule-category assertion
    (issues #2697/#2806), plus the missing/unexpected names of an
    unsatisfied set assertion (only reachable via --allow-deck-errors; a
    strict mismatch is an error, not a report)."""
    if not assertion:
        return
    verdict = "satisfied" if assertion.get("satisfied") else "not satisfied"
    if "expected_names" in assertion:
        print(
            f"rule-category set assertion: {verdict} "
            f"({len(assertion['expected_names'])} expected, "
            f"{len(assertion['observed_names'])} declared)"
        )
        for label in ("missing", "unexpected"):
            for name in assertion.get(f"{label}_names") or []:
                print(f"  {label}: {name}")
    else:
        print(
            f"rule-category count assertion: {verdict} "
            f"({assertion.get('expected')} expected, "
            f"{assertion.get('observed')} declared)"
        )


def _print_rule_selection(selection: dict | None) -> None:
    """One line stating a ``--rules`` subset (issue #2768), so the text form
    never renders a partial run as an unqualified full-deck result."""
    if not selection:
        return
    selected = selection.get("selected") or []
    omitted = selection.get("omitted") or []
    total = selection.get("deck_rule_count", len(selected) + len(omitted))
    kind = "partial run" if omitted else "full deck"
    print(
        f"rule selection: {len(selected)} of {total} rules "
        f"({len(omitted)} omitted; {kind}) -- "
        f"selectors: {_abbreviate(selection.get('selectors') or [])}"
    )


def _abbreviate(items: list[str], limit: int = 8) -> str:
    """Comma-join ``items``, eliding past ``limit`` (text form only)."""
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f", ... ({len(items) - limit} more)"


def _print_rule_list(listing: dict) -> None:
    print(f"deck: {listing['deck']}")
    print(f"engine: {listing['engine']}")
    print(f"rules: {listing['rule_count']}")
    print()
    for rule in listing["rules"]:
        layers = rule["layer"]
        if rule.get("other_layer"):
            layers += f" vs {rule['other_layer']}"
        scope = f"  [{rule['scope']}]" if rule.get("scope") else ""
        print(f"{rule['id']}  {rule['check']}  {layers}{scope}  {rule['description']}")


def _print_text(report: dict) -> None:
    # `file` is the `{path, scope}` object (issue #2659) -- rendered via the
    # same courtesy helper `klt sim`/`klt pex` use, never the absolute path.
    print(f"file: {render_path_field(report['file'])}")
    print(f"deck: {report['deck']}")
    if "engine" in report:
        print(f"engine: {report['engine']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    # Issue #1941: a run that tolerated deck errors via --allow-deck-errors
    # ran only part of its deck -- say so, so the text form never renders a
    # partial run as an unqualified "clean".
    deck_errors = report.get("engine_deck_errors")
    if deck_errors is not None:
        print(
            "deck errors tolerated (--allow-deck-errors): klayout exit "
            f"status {deck_errors['exit_status']}, "
            f"{len(deck_errors['error_lines'])} ERROR line(s)"
        )
        for line in deck_errors["error_lines"]:
            print(f"  {line}")
    print(f"violations: {report['violation_count']}")
    if "waived_count" in report:
        print(f"waived: {report['waived_count']}")
        for waiver in report["unused_waivers"]:
            print(f"  unused waiver: {waiver['cell']} / {waiver['rule']}")
    _print_rule_selection(report.get("rule_selection"))
    _print_coverage_assertion(report.get("coverage_assertion"))
    _print_coverage_gaps(report["coverage"])

    rule_counts = report["rule_counts"]
    if rule_counts:
        print()
        print("rule_counts:")
        for rule_id in sorted(rule_counts):
            print(f"  {rule_id}: {rule_counts[rule_id]}")

    violations = report["violations"]
    if not violations:
        return

    print()
    for entry in violations:
        bbox = entry["bbox"]
        print(
            f"{entry['rule']}  {entry['cell']}  {entry['layer']}  "
            f"({bbox['left']},{bbox['bottom']})-({bbox['right']},{bbox['top']})  "
            f"{entry['description']}"
        )


def _print_crosscheck_text(report: dict) -> None:
    comparison = report["comparison"]
    print(f"file: {report['file']}")
    print(f"status: {report['status']}")
    print(f"comparison: {comparison['state']}")
    error = comparison.get("error")
    if error:
        print(f"  native run did not complete: {error['message']}")
    for engine in ("curated", "native"):
        sub = report["reports"][engine]
        if sub is None:
            print(f"{engine}: not run")
        else:
            print(f"{engine}: {sub['status']} ({sub['violation_count']} violations)")
    for group in comparison["groups"]:
        print(f"  group {group['id']}: {group['state']}")
    unmapped = comparison["unmapped"]
    for engine in ("curated", "native"):
        if unmapped[engine]:
            print(f"unmapped {engine} rules: {len(unmapped[engine])}")
