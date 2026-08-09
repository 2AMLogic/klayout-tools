"""``klt drc`` command: serialise the DRC report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/drc.md`` for the full table):
    0 - ran clean, no violations
    1 - failed to run (bad file, unknown deck, engine error) — returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, violations found
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)

``--engine`` (issue #565) selects between the default curated engine
(``run_drc``, klt's own pip-only ``Region``-primitive deck) and the opt-in
``klayout`` engine (``run_drc_klayout_engine``, a subprocess wrapper around
the standalone ``klayout`` application binary running a PDK-native DRC-DSL
script) -- see ``docs/cli/drc.md``, "Engine". Unlike ``klt lvs``'s
request-body ``engine`` field, ``klt drc`` has no request-document
precedent (its flags are argv-only), so this is a CLI flag instead.
"""

import argparse

from .. import pdk as pdk_module
from ..drc import DrcError, run_drc, run_drc_klayout_engine
from .output import emit_error, emit_success

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = _run(args)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_VIOLATIONS if report["status"] == "violations" else EXIT_CLEAN


def _run(args: argparse.Namespace) -> dict:
    if args.engine == "klayout":
        deck_file = args.deck_file
        if deck_file is None:
            deck_file = pdk_module.drc_deck_file(variant=args.pdk, root=args.pdk_root)
        if deck_file is None:
            raise DrcError(
                "no PDK-native klayout DRC deck script found for this PDK "
                "variant -- pass --deck-file to point at one directly (see "
                "docs/cli/drc.md, 'Engine' -> 'klayout')"
            )
        return run_drc_klayout_engine(
            args.file, deck_file, top=args.top, timeout_s=args.timeout_s
        )

    if not args.deck:
        raise DrcError("argument --deck is required for --engine curated")
    return run_drc(args.file, args.deck, top=args.top)


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"deck: {report['deck']}")
    if "engine" in report:
        print(f"engine: {report['engine']}")
    print(f"dbu_um: {report['dbu_um']}")
    print(f"status: {report['status']}")
    print(f"violations: {report['violation_count']}")

    unchecked_layers = report["coverage"]["layers_in_stream_without_rules"]
    if unchecked_layers:
        print(f"unchecked layers in stream: {len(unchecked_layers)}")

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
