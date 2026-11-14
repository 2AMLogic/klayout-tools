"""``klt lvs`` command: serialise the netlist-compare report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/lvs.md`` for the full table):
    0 - LVS clean, layout matches reference (``status: "match"``), or under
        --check, the committed report still holds (``status: "match"``)
    1 - failed to run (bad request, unresolvable layout/reference input,
        unknown deck, unsupported engine, engine error) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, mismatches found (``status: "mismatch"``), or
        under --check, drifted (``status: "drifted"``)
    4 - ran, but the compare the request asked for could not be performed, so
        no verdict about the design was reached (``status: "inconclusive"``,
        issue #1370 -- ``options.combine_devices`` exhausted its retry budget
        and both sides were rolled back to their uncombined state); never
        ``0``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand. The 0/1/2/3 split is `klt drc`'s -- LVS is a clean/dirty verdict
like DRC -- and `4` is `klt equiv`'s ``EXIT_INCONCLUSIVE`` reused verbatim for
the one case where LVS genuinely cannot reach a verdict; see docs/cli/lvs.md's
"Exit codes" section.)

``--check <report>`` (issue #1106) switches ``klt lvs`` from running a fresh
compare into *verifying a previously committed* ``--format json`` report
still reproduces -- see ``docs/cli/lvs.md``, "--check" -- and is mutually
exclusive with the positional ``request`` argument (inputs are read from the
committed report itself). ``--layout PATH`` (issue #2699) supplies the layout
explicitly, overriding the recorded one unconditionally. Cheap mode (default):
re-hash the layout/reference netlists (and extraction deck, when one was used)
named in the report and compare against its own recorded hash values, no compare engine
re-run. Full mode (``--check <report> --rerun``): best-effort re-run of the
compare the report describes and diff verdict-bearing fields. Both reuse
``status: "match"`` / ``"drifted"`` and the same 0/3 exit-code split as a
normal run (see ``klayout_tools._report_verify``).
"""

import argparse
import sys

from ..env_provenance import render_path_field
from ..lvs import LvsError, check_lvs_report, rerun_lvs_report, run_lvs
from .output import (
    emit_error,
    emit_success,
    render_check_text,
    render_rerun_drift,
    write_json_file,
)

EXIT_MATCH = 0
EXIT_MISMATCH = 3
#: Issue #1370: "ran, but could not reach a verdict" -- deliberately the same
#: numeric value as ``cli/equiv_cmd.py``'s own ``EXIT_INCONCLUSIVE``, so the
#: two `klt` verbs that can report an unreachable verdict agree on the code a
#: caller gates on. Never `0`, and never folded into `3`: an automation gate
#: must be able to tell "the design differs" from "the comparison could not be
#: performed".
EXIT_INCONCLUSIVE = 4
#: Aliases for the --check/--rerun outcome (issue #1106) -- same numeric
#: values as a normal run's 0/3 split (see this module's docstring), just
#: named for the "match"/"drifted" vocabulary those modes report under.
EXIT_DRIFTED = EXIT_MISMATCH

_EXIT_BY_STATUS = {
    "match": EXIT_MATCH,
    "mismatch": EXIT_MISMATCH,
    "inconclusive": EXIT_INCONCLUSIVE,
}


def configure(parser: argparse.ArgumentParser) -> None:
    """Add ``-o/--output`` (issue #4984) and bind ``run`` as the handler."""
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        metavar="PATH",
        help=(
            "also write the JSON envelope to PATH (same as --format json "
            "stdout; written atomically, '.gz' gzip-compressed; stdout is "
            "unchanged). An unwritable PATH is a clean error (exit 1). "
            "Rejected with --check"
        ),
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    # `request` and `--check` are a required, mutually exclusive argparse
    # group (parser.py) -- omitting both, or giving both, is already a usage
    # error (exit 2) by the time `run()` is ever called.
    if args.check is not None:
        if args.deck_file is not None or args.deck_var:
            return emit_error(
                "lvs",
                "--deck-file/--deck-var apply to a fresh run; --check reads "
                "the deck from the committed report",
                args.format,
            )
        return _run_check(args)

    if args.rerun:
        return emit_error("lvs", "--rerun requires --check <report>", args.format)

    if args.layout is not None:
        return emit_error("lvs", "--layout requires --check <report>", args.format)

    try:
        report = run_lvs(
            args.request,
            deck_file=args.deck_file,
            deck_vars=_parse_deck_vars(args.deck_var),
            # `--progress` (issue #4347), same default as `klt extract`:
            # heartbeat only, and only when stderr is not a TTY.
            progress=(
                args.progress
                if args.progress is not None
                else ("heartbeat" if not sys.stderr.isatty() else False)
            ),
        )
    except LvsError as exc:
        return emit_error("lvs", str(exc), args.format)

    write_failure = _write_output(report, args)
    if write_failure is not None:
        return write_failure

    emit_success(report, args.format, _print_text)

    # Unknown/unexpected status can only mean a future additive value this
    # build predates -- never silently report it as a clean `0`.
    return _EXIT_BY_STATUS.get(report["status"], EXIT_MISMATCH)


def _write_output(report: dict, args: argparse.Namespace) -> int | None:
    """Write the ``-o/--output`` file copy; return an exit code only on failure.

    Issue #4984, mirroring ``klt drc -o`` (#4393): an additive copy of the
    envelope, written before emitting so it lands regardless of the verdict's
    exit code and an unwritable path fails with the error envelope.
    """
    if not args.output:
        return None
    try:
        write_json_file(report, args.output)
    except OSError as exc:
        return emit_error(
            "lvs",
            f"could not write report to '{args.output}': {exc}",
            args.format,
        )
    return None


def _parse_deck_vars(raw: list[str] | None) -> dict[str, str]:
    """Parse repeatable ``--deck-var NAME=VALUE`` (issue #3206), same
    spelling and last-one-wins rule as ``klt drc --deck-var``."""
    deck_vars: dict[str, str] = {}
    for item in raw or ():
        name, sep, value = item.partition("=")
        if not sep or not name:
            raise LvsError(
                f"invalid --deck-var {item!r}: expected 'name=value' "
                "(e.g. --deck-var feol=true)"
            )
        deck_vars[name] = value
    return deck_vars


def _run_check(args: argparse.Namespace) -> int:
    if args.output is not None:
        return emit_error(
            "lvs",
            "-o/--output cannot be combined with --check: --check emits no "
            "run report to write",
            args.format,
        )
    try:
        if args.rerun:
            result = rerun_lvs_report(args.check, layout_path=args.layout)
        else:
            result = check_lvs_report(args.check, layout_path=args.layout)
    except LvsError as exc:
        return emit_error("lvs", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


def _print_excluded_pins(power: dict) -> None:
    """Issue #4363: name each well-layer LEF pin the power check skipped."""
    for excluded in power.get("excluded_pins") or []:
        print(
            f"  excluded_pin: {excluded['pin']} "
            f"({excluded['instance_count']} instance(s)) -- {excluded['reason']}"
        )


def _print_text(report: dict) -> None:
    # `layout` is the `{path, scope}` object
    # `env_provenance.repo_relative_path` defines (issue #2659) -- rendered
    # through the same courtesy helper `klt sim`/`klt pex` use, so the
    # absolute path is never printed even in text mode. `reference` is still
    # the request's own verbatim echo (a plain string) and prints as-is.
    print(f"layout: {render_path_field(report['layout'])}")
    # Issue #4984: an object since schema_version 3; a pre-v3 or runset
    # report still carries the bare request string.
    reference = report["reference"]
    shown = reference if isinstance(reference, str) else render_path_field(reference)
    print(f"reference: {shown}")
    print(f"top: {report['top']}")
    # Issue #1205: only printed when the two sides resolved to different top
    # circuits -- the ordinary compare's output is unchanged, but a negative
    # control (a `<cell>_shorted` layout compared against the intact
    # `<cell>`'s reference netlist) says which cell each side actually used.
    if report.get("reference_top") not in (None, report["top"]):
        print(f"reference top: {report['reference_top']}")
    print(f"engine: {report['engine']}")
    # Issue #589: only printed when opted in, so the default human output is
    # unchanged -- but a verdict reached under a caller-supplied design
    # tolerance always says so, right next to `status`.
    if report.get("parameter_tolerance") is not None:
        print(f"parameter_tolerance: {report['parameter_tolerance']}")
    print(f"status: {report['status']}")
    print(f"mismatches: {report['mismatch_count']}")
    # Issue #1132: printed unconditionally (like `mismatches` above), even
    # when 0, so a caller skimming text output can tell "0 mismatches" and
    # "N mismatches, 0 of them errors" apart from "no report at all".
    print(f"errors: {report['error_count']}")

    counts = report["counts"]
    for kind in ("nets", "devices", "pins"):
        c = counts[kind]
        print(
            f"  {kind}: layout={c['layout']}  reference={c['reference']}  "
            f"matched={c['matched']}"
        )

    # Issue #1952: the power/ground half of a `gate-level-verilog` compare,
    # printed as its own verdict right under the signal-connectivity one --
    # a text reader who sees only `status: match` would otherwise have no
    # way to tell a signal-only compare from a full one. Printed
    # unconditionally (including `unchecked`, with its reason) for exactly
    # that reason: silence would read as "nothing to report".
    power = report.get("power_connectivity")
    if isinstance(power, dict):
        print(f"power_connectivity: {power['status']}")
        if power.get("reason"):
            print(f"  ({power['reason']})")
        # Issue #1978: `expected_nets` naming a pin nothing was observed on
        # matches zero findings and would otherwise print identically to "no
        # such pin exists to worry about" -- name it explicitly so a text
        # reader can tell the two cases apart.
        unchecked_expected = power.get("unchecked_expected_pins") or []
        if unchecked_expected:
            print(
                "  unchecked_expected_pins: "
                f"{', '.join(unchecked_expected)} (named in "
                "options.power_connectivity.expected_nets but never "
                "observed on any layout-side instance)"
            )
        _print_excluded_pins(power)
        for finding in power["findings"]:
            print(
                f"  [{finding['severity']}] {finding['rule']}  {finding['description']}"
            )

    category_counts = report["category_counts"]
    if category_counts:
        print()
        print("category_counts:")
        for category in sorted(category_counts):
            print(f"  {category}: {category_counts[category]}")

    mismatches = report["mismatches"]
    if not mismatches:
        return

    print()
    for entry in mismatches:
        print(
            f"[{entry['severity']}] {entry['category']}  ({entry['side']})  "
            f"{entry['description']}"
        )
