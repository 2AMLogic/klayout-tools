"""``klt drc`` command: serialise the DRC report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/drc.md`` for the full table):
    0 - ran clean, no violations (or, under --check, the report still holds)
    1 - failed to run (bad file, unknown deck, engine error) — returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, violations found (or, under --check, drifted)
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)

``--engine`` (issue #565) selects between the default curated engine
(``run_drc``, klt's own pip-only ``Region``-primitive deck) and the opt-in
``klayout`` engine (``run_drc_klayout_engine``, a subprocess wrapper around
the standalone ``klayout`` application binary running a PDK-native DRC-DSL
script) -- see ``docs/cli/drc.md``, "Engine". Unlike ``klt lvs``'s
request-body ``engine`` field, ``klt drc`` has no request-document
precedent (its flags are argv-only), so this is a CLI flag instead.

``--check <report>`` (issue #1106) switches ``klt drc`` from running a fresh
DRC into *verifying a previously committed* ``--format json`` report still
reproduces -- see ``docs/cli/drc.md``, "--check" -- and is mutually exclusive
with the positional ``file`` argument (the input path is read from the
committed report itself). Cheap mode (default): re-hash the input/deck named
in the report and compare against its own recorded ``content_hash`` values,
no DRC engine re-run. Full mode (``--check <report> --rerun``): actually
re-run the deck the report names and diff verdict-bearing fields. Both reuse
``status: "match"`` / ``"drifted"`` and the same 0/3 exit-code split as a
normal run (see ``klayout_tools._report_verify``).
"""

import argparse

from .. import pdk as pdk_module
from ..drc import (
    DrcError,
    check_drc_report,
    rerun_drc_report,
    run_drc,
    run_drc_klayout_engine,
)
from .output import emit_error, emit_success

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 3
#: Aliases for the --check/--rerun outcome (issue #1106) -- same numeric
#: values as a normal run's 0/3 split (see this module's docstring), just
#: named for the "match"/"drifted" vocabulary those modes report under.
EXIT_MATCH = EXIT_CLEAN
EXIT_DRIFTED = EXIT_VIOLATIONS


def run(args: argparse.Namespace) -> int:
    # `file` and `--check` are a required, mutually exclusive argparse group
    # (parser.py) -- omitting both, or giving both, is already a usage error
    # (exit 2) by the time `run()` is ever called.
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error("drc", "--rerun requires --check <report>", args.format)

    try:
        report = _run(args)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return EXIT_VIOLATIONS if report["status"] == "violations" else EXIT_CLEAN


def _run_check(args: argparse.Namespace) -> int:
    try:
        if args.rerun:
            result = rerun_drc_report(args.check)
        else:
            result = check_drc_report(args.check)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    text_renderer = _print_rerun_text if args.rerun else _print_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


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
            args.file,
            deck_file,
            top=args.top,
            timeout_s=args.timeout_s,
            deck_vars=_parse_deck_vars(args.deck_var),
        )

    if not args.deck:
        raise DrcError("argument --deck is required for --engine curated")
    return run_drc(args.file, args.deck, top=args.top)


def _parse_deck_vars(raw: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--deck-var name=value`` values (issue #1302) into
    the ``deck_vars`` mapping :func:`run_drc_klayout_engine` threads through
    as extra ``-rd`` script globals. Silently ignored (returns ``{}``) when
    ``--engine curated`` is selected -- ``--deck-var`` is a klayout-engine-
    only flag, the same "ignored, not rejected" treatment ``--deck-file``
    and ``--timeout-s`` already get under the curated engine.

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


def _print_check_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")


def _print_rerun_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    drift = result["drift"]
    if not drift:
        return
    print()
    print("drift:")
    for entry in drift:
        print(f"  {entry['field']}:")
        print(f"      committed: {entry['committed']!r}")
        print(f"      fresh:     {entry['fresh']!r}")
