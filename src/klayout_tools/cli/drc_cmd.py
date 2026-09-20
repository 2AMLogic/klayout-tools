"""``klt drc`` command: serialise the DRC report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand — see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/drc.md`` for the full table):
    0 - ran clean, no violations (or, under --check, the report still holds).
        Also ``status: "clean_partial"`` — every check that ran passed and
        some requested rule did not (issue #2110): a real result, but not
        this verb's unconditional success.
    1 - failed to run (bad file, unknown deck, engine error) — returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ran successfully, violations found (or, under --check, drifted)
    4 - reached no usable verdict: ``status: "not_checked"`` (known zero
        checked work) or ``"coverage_unknown"`` (unmeasurable execution)
(2 is reserved for argparse usage errors, as with every other ``klt`` subcommand.)

Every one of those codes below ``1`` is assigned by the common rollup table in
:mod:`klayout_tools.coverage`, via :func:`~klayout_tools.drc.drc_exit_code` —
never by a status comparison in this module (see ``docs/coverage-contract.md``).

``--engine`` (issue #565) selects between the default curated engine
(``run_drc``, klt's own pip-only ``Region``-primitive deck) and the opt-in
``klayout`` engine (``run_drc_klayout_engine``, a subprocess wrapper around
the standalone ``klayout`` application binary running a PDK-native DRC-DSL
script) -- see ``docs/cli/drc.md``, "Engine". It is a CLI flag first (it
predates this command's request-document form); the request document's own
``engine`` field mirrors it exactly, the same way ``klt lvs``'s request-body
``engine`` field does.

The positional input slot also accepts a **request document** (issue #1867) --
a path to a JSON file, ``-`` for stdin, or an inline JSON object string --
carrying every flag below as a field, so the whole stage can be committed as
one diffable, content-hashable file the way ``klt lvs``/``klt sta``/``klt
synthesize``/``klt place-and-route`` already are. The two forms are **mutually
exclusive**: a request document plus any of this command's own input flags is
a clean application error (exit 1), never a silent override -- see
``cli/_request_document.py`` for why, and ``docs/cli/drc.md``'s "Request
document" section for the schema.

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
from .._paths import looks_like_request_document
from ..drc import (
    REQUEST_SCHEMA,
    DrcError,
    check_drc_report,
    drc_exit_code,
    load_request_arg,
    rerun_drc_report,
    run_drc,
    run_drc_klayout_engine,
)
from . import _request_document as reqdoc
from .output import emit_error, emit_success, render_rerun_drift

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
        # Issue #1867: the positional slot carries either a layout path or a
        # request document; `_apply_request` normalizes the latter into the
        # same namespace fields the argv form produces, so `_run` below sees
        # exactly one shape and the two forms cannot drift.
        if args.file is not None and looks_like_request_document(args.file):
            args = _apply_request(args)
        report = _run(args)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    # Issue #2110: the common rollup table decides this, not a local
    # status->code mapping -- `clean_partial` is a real, exit-0 result that
    # is not this verb's unconditional success, and adding it to a local
    # `status == "clean"` test is exactly the per-verb divergence
    # `docs/coverage-contract.md` exists to prevent.
    return drc_exit_code(report)


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

    text_renderer = render_rerun_drift if args.rerun else _print_check_text
    emit_success(result, args.format, text_renderer)

    return EXIT_MATCH if result["status"] == "match" else EXIT_DRIFTED


#: Every top-level field a ``klt drc`` request document may carry (issue
#: #1867), mapped to the argparse ``dest`` it stands in for -- one entry per
#: flag this command accepts, plus the positional ``file``. Only ``deck_vars``
#: differs from its ``dest`` (the flag itself, ``--deck-var``, is singular
#: because it is repeatable). ``tests/test_drc.py`` asserts this covers the
#: subparser's whole flag surface, so a flag added later cannot silently
#: become unreachable from the request form.
_REQUEST_FIELD_DESTS = {
    "file": "file",
    "deck": "deck",
    "top": "top",
    "engine": "engine",
    "deck_file": "deck_file",
    "deck_vars": "deck_var",
    "timeout_s": "timeout_s",
    "allow_deck_errors": "allow_deck_errors",
    "pdk": "pdk",
    "pdk_root": "pdk_root",
}


def _apply_request(args: argparse.Namespace) -> argparse.Namespace:
    """Load the request document in ``args.file`` and return a namespace with
    its fields in place of the argv flags they mirror (issue #1867).

    Relative paths inside the document (``file``, ``deck_file``, ``pdk_root``)
    resolve against the document's own directory for the file form, or the
    current working directory for the stdin/inline forms -- ``klt lvs``'s
    convention, implemented by the same shared helper.
    """
    request, base_dir = load_request_arg(args.file)
    reqdoc.check_schema(
        request, expected=REQUEST_SCHEMA, verb="drc", error_cls=DrcError
    )
    reqdoc.check_known_fields(
        request, tuple(_REQUEST_FIELD_DESTS), verb="drc", error_cls=DrcError
    )
    reqdoc.reject_argv_flags(args, verb="drc", error_cls=DrcError)

    def _path(key: str) -> str | None:
        return reqdoc.get_path(
            request, key, base_dir=base_dir, verb="drc", error_cls=DrcError
        )

    def _str(key: str) -> str | None:
        return reqdoc.get_str(request, key, verb="drc", error_cls=DrcError)

    timeout_s = reqdoc.get_number(request, "timeout_s", verb="drc", error_cls=DrcError)
    engine = _str("engine")
    if engine is not None and engine not in ("curated", "klayout"):
        raise DrcError(
            f"`klt drc` request field 'engine' must be 'curated' or "
            f"'klayout' (got {engine!r})"
        )

    return argparse.Namespace(
        **{
            **vars(args),
            "file": _path("file"),
            "deck": _str("deck"),
            "top": _str("top"),
            # `args.engine`, not a literal, for an omitted field: every flag
            # is provably still at its parser default here (`reject_argv_flags`
            # raised otherwise), so reusing it keeps the two forms' defaults
            # from drifting apart.
            "engine": engine if engine is not None else args.engine,
            "deck_file": _path("deck_file"),
            "deck_var": reqdoc.get_str_map_as_pairs(
                request, "deck_vars", verb="drc", flag="--deck-var", error_cls=DrcError
            ),
            "timeout_s": args.timeout_s if timeout_s is None else timeout_s,
            "allow_deck_errors": reqdoc.get_bool(
                request, "allow_deck_errors", verb="drc", error_cls=DrcError
            ),
            "pdk": _str("pdk"),
            "pdk_root": _path("pdk_root"),
        }
    )


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
            pdk_variant=args.pdk,
            pdk_root=args.pdk_root,
            allow_deck_errors=args.allow_deck_errors,
        )

    if not args.deck:
        raise DrcError("argument --deck is required for --engine curated")
    return run_drc(
        args.file,
        args.deck,
        top=args.top,
        pdk_variant=args.pdk,
        pdk_root=args.pdk_root,
    )


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
    skipped_requested = coverage.get("skipped") or []
    if skipped_requested:
        print(f"skipped requested rules: {len(skipped_requested)}")
        for record in skipped_requested:
            print(f"  {record['id']}: {record['reason']}")

    unchecked_layers = coverage["layers_in_stream_without_rules"]
    if unchecked_layers:
        print(f"unchecked layers in stream: {len(unchecked_layers)}")


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
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


def _print_check_text(result: dict) -> None:
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")
