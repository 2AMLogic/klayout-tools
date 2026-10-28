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
reproduces -- see ``docs/cli/drc.md``, "--check". The input path is read from
the committed report itself unless the positional ``file`` is also given
(issue #2699), which then overrides the recorded input unconditionally.
Cheap mode (default): re-hash the input/deck named in the report and compare
against its own recorded ``content_hash`` values, no DRC engine re-run. Full mode
(``--check <report> --rerun``): actually
re-run the deck the report names and diff verdict-bearing fields. Both reuse
``status: "match"`` / ``"drifted"`` and the same 0/3 exit-code split as a
normal run (see ``klayout_tools._report_verify``).
"""

import argparse

from .. import pdk as pdk_module
from .._paths import looks_like_request_document
from ..drc import (
    REQUEST_SCHEMA,
    SKIP_REASON_NOT_SELECTED,
    DrcError,
    check_drc_report,
    drc_exit_code,
    list_deck_rules,
    load_request_arg,
    load_rule_category_manifest,
    parse_rule_selectors,
    rerun_drc_report,
    run_drc,
    run_drc_klayout_engine,
)
from ..drc_crosscheck import crosscheck_exit_code, run_drc_crosscheck
from ..env_provenance import render_path_field
from . import _request_document as reqdoc
from .output import (
    emit_error,
    emit_success,
    render_check_text,
    render_rerun_drift,
    write_json_file,
)

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 3
#: Aliases for the --check/--rerun outcome (issue #1106) -- same numeric
#: values as a normal run's 0/3 split (see this module's docstring), just
#: named for the "match"/"drifted" vocabulary those modes report under.
EXIT_MATCH = EXIT_CLEAN
EXIT_DRIFTED = EXIT_VIOLATIONS


def run(args: argparse.Namespace) -> int:
    # `file`/`--check`/`--list-rules`: at least one is required and
    # `--list-rules` stands alone -- both usage errors (exit 2) enforced by
    # `parser._require_one_input` before `run()` is ever called. `file` may
    # accompany `--check` (issue #2699) as an explicit input override.
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        return emit_error("drc", "--rerun requires --check <report>", args.format)

    if args.list_rules:
        return _run_list_rules(args)

    try:
        # Issue #1867: the positional slot carries either a layout path or a
        # request document; `_apply_request` normalizes the latter into the
        # same namespace fields the argv form produces, so `_run` below sees
        # exactly one shape and the two forms cannot drift.
        if args.file is not None and looks_like_request_document(args.file):
            args = _apply_request(args)
        report = _run(args)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format, code=exc.code)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    write_failure = _write_output(report, args)
    if write_failure is not None:
        return write_failure

    if report.get("mode") == "crosscheck":
        emit_success(report, args.format, _print_crosscheck_text)
        return crosscheck_exit_code(report)

    emit_success(report, args.format, _print_text)

    # Issue #2110: the common rollup table decides this, not a local
    # status->code mapping -- `clean_partial` is a real, exit-0 result that
    # is not this verb's unconditional success, and adding it to a local
    # `status == "clean"` test is exactly the per-verb divergence
    # `docs/coverage-contract.md` exists to prevent.
    return drc_exit_code(report)


def _write_output(report: dict, args: argparse.Namespace) -> int | None:
    """Write the ``-o/--output`` file copy; return an exit code only on failure.

    Issue #4393: an additive copy of the envelope, written before emitting so
    it lands regardless of the verdict's exit code and an unwritable path
    fails with the error envelope. Stdout is unchanged.
    """
    if not args.output:
        return None
    try:
        write_json_file(report, args.output)
    except OSError as exc:
        return emit_error(
            "drc",
            f"could not write report to '{args.output}': {exc}",
            args.format,
        )
    return None


def _run_list_rules(args: argparse.Namespace) -> int:
    """``--list-rules`` (issue #2768): enumerate a curated deck's rules.

    Listing reads no layout and runs no check, so the only inputs it takes
    are ``--deck`` (required) and the default ``--engine curated``; any other
    input flag is rejected rather than silently ignored -- a caller passing
    ``--rules`` or ``--top`` here almost certainly meant to run DRC. Exit 0
    on success: a listing has no verdict, so it never borrows a DRC status.
    """
    try:
        if args.engine == "klayout":
            raise DrcError(
                "--list-rules is not supported with --engine klayout: a "
                "PDK-native runset does not share the curated rule registry "
                "(see docs/cli/drc.md, 'Rule listing and selection')"
            )
        extra = _non_default_flags(args, allowed=("deck", "engine"))
        if extra:
            raise DrcError(
                f"--list-rules takes only --deck; also given: {', '.join(extra)}"
            )
        if not args.deck:
            raise DrcError("--list-rules requires --deck <name>")
        listing = list_deck_rules(args.deck)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)

    emit_success(listing, args.format, _print_rule_list)
    return EXIT_CLEAN


def _non_default_flags(
    args: argparse.Namespace, *, allowed: tuple[str, ...]
) -> list[str]:
    """Option strings of this command's input flags set away from their
    parser default, other than ``allowed`` dests -- the same table
    ``reject_argv_flags`` reads for request-document conflicts."""
    return [
        option
        for dest, (option, default) in sorted(args.request_argv_flags.items())
        if dest not in allowed and getattr(args, dest, default) != default
    ]


def _run_check(args: argparse.Namespace) -> int:
    if args.output is not None:
        # Issue #4393: --check emits a verification result, not a run report.
        return emit_error(
            "drc",
            "-o/--output cannot be combined with --check: --check emits no "
            "run report to write",
            args.format,
        )
    if args.waivers is not None:
        # Issue #3483: like --rules, the waivers a report was run with are
        # recorded in its provenance and replayed by `--check --rerun`.
        return emit_error(
            "drc",
            "--waivers cannot be combined with --check: --check --rerun "
            "replays the committed report's own recorded waivers file",
            args.format,
        )
    if args.tile is not None:
        # Issue #4226: the abutment context a report was run with is
        # recorded in its `tile` echo and replayed by `--check --rerun`.
        return emit_error(
            "drc",
            "--tile cannot be combined with --check: --check --rerun replays "
            "the committed report's own recorded tile",
            args.format,
        )
    if args.rules is not None:
        # Issue #2768: `--check` verifies a committed report as it was run;
        # `--rerun` replays that report's own recorded `rule_selection`, so a
        # second, argv-supplied selection would be ambiguous.
        return emit_error(
            "drc",
            "--rules cannot be combined with --check: --check --rerun replays "
            "the committed report's own recorded rule selection",
            args.format,
        )
    try:
        if args.rerun:
            result = rerun_drc_report(args.check, input_path=args.file)
        else:
            result = check_drc_report(args.check, input_path=args.file)
    except DrcError as exc:
        return emit_error("drc", str(exc), args.format)
    except pdk_module.PdkNotFoundError as exc:
        return emit_error("drc", str(exc), args.format)

    text_renderer = render_rerun_drift if args.rerun else render_check_text
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
    "allow_missing_host_tools": "allow_missing_host_tools",
    "expect_rule_categories": "expect_rule_categories",
    "expect_rule_categories_file": "expect_rule_categories_file",
    "min_klayout_version": "min_klayout_version",
    "negative_control": "negative_control",
    "negative_control_categories": "negative_control_categories",
    "pdk": "pdk",
    "pdk_root": "pdk_root",
    "rules": "rules",
    "waivers_file": "waivers",
    "output": "output",
    "tile": "tile",
}


def _apply_request(args: argparse.Namespace) -> argparse.Namespace:
    """Load the request document in ``args.file`` and return a namespace with
    its fields in place of the argv flags they mirror (issue #1867).

    Relative paths inside the document (``file``, ``deck_file``, ``pdk_root``,
    ``expect_rule_categories_file``, ``waivers_file``) resolve against the
    document's own directory for the file form, or the current working directory for the
    stdin/inline forms -- ``klt lvs``'s convention, implemented by the same
    shared helper.
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
    if engine is not None and engine not in ("curated", "klayout", "both"):
        raise DrcError(
            f"`klt drc` request field 'engine' must be 'curated', "
            f"'klayout' or 'both' (got {engine!r})"
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
            "allow_missing_host_tools": reqdoc.get_bool(
                request, "allow_missing_host_tools", verb="drc", error_cls=DrcError
            ),
            "expect_rule_categories": _expect_rule_categories(request),
            # Issue #2806: resolved against the request document's own
            # directory, like every other path field.
            "expect_rule_categories_file": _path("expect_rule_categories_file"),
            "min_klayout_version": _str("min_klayout_version"),
            # Issue #3076: resolved against the request document's own
            # directory, like every other path field.
            "negative_control": _path("negative_control"),
            "negative_control_categories": _str("negative_control_categories"),
            "pdk": _str("pdk"),
            "pdk_root": _path("pdk_root"),
            "rules": _request_rules(request),
            # Issue #3483: resolved against the request document's directory.
            "waivers": _path("waivers_file"),
            # Issue #4393: resolved against the request document's directory.
            "output": _path("output"),
            "tile": _str("tile"),
        }
    )


def _request_rules(request: dict) -> str | list[str] | None:
    """``request["rules"]`` (issue #2768): the ``--rules`` selection, as one
    comma-separated string (the argv spelling) or a list of strings. Any
    other type -- a number, an object, a list holding a non-string -- is a
    clean error; token-level checks (empty/unmatched) are shared with the
    argv form downstream in :func:`~klayout_tools.drc.parse_rule_selectors`
    and :func:`~klayout_tools.drc.resolve_rule_selection`."""
    value = request.get("rules")
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise DrcError(
        "`klt drc` request field 'rules' must be a string of comma-separated "
        f"rule ids/globs or a list of strings (got {value!r})"
    )


def _expect_rule_categories(request: dict) -> int | None:
    """``request["expect_rule_categories"]`` as a positive int, or ``None``
    (issue #2697). Booleans, floats and strings are rejected."""
    value = request.get("expect_rule_categories")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise DrcError(
            "`klt drc` request field 'expect_rule_categories' must be a "
            f"positive integer (got {value!r})"
        )
    return value


def _run_crosscheck(args: argparse.Namespace) -> dict:
    """``--engine both`` (issue #3973): both engines, one comparison report."""
    for flag, value in (
        ("--rules", args.rules),
        ("--waivers", args.waivers),
        ("--tile", args.tile),
    ):
        if value is not None:
            raise DrcError(
                f"{flag} is not supported with --engine both: the cross-check "
                "compares each engine's whole-deck report unchanged (see "
                "docs/cli/drc.md, 'Cross-check')"
            )
    if not args.deck:
        raise DrcError("argument --deck is required for --engine both")
    deck_file = args.deck_file
    if deck_file is None:
        deck_file = pdk_module.drc_deck_file(variant=args.pdk, root=args.pdk_root)
    return run_drc_crosscheck(
        args.file,
        args.deck,
        deck_file,
        top=args.top,
        pdk_variant=args.pdk,
        pdk_root=args.pdk_root,
        native_kwargs={
            "timeout_s": args.timeout_s,
            "deck_vars": _parse_deck_vars(args.deck_var),
            "allow_deck_errors": args.allow_deck_errors,
            "allow_missing_host_tools": args.allow_missing_host_tools,
            "expected_rule_categories": args.expect_rule_categories,
            "min_klayout_version": args.min_klayout_version,
            "expected_rule_category_names": _expected_rule_category_names(args),
            "negative_control": args.negative_control,
            "negative_control_categories": _negative_control_categories(args),
        },
    )


def _run(args: argparse.Namespace) -> dict:
    if args.engine == "both":
        return _run_crosscheck(args)
    if args.engine == "klayout":
        if args.tile is not None:
            raise DrcError(
                "--tile is not supported with --engine klayout: the abutment "
                "context is built by the curated engine (see docs/cli/drc.md, "
                "'Abutment context')"
            )
        if args.waivers is not None:
            raise DrcError(
                "--waivers is not supported with --engine klayout: waivers "
                "match a marker's source_cell, which only the curated engine "
                "attributes (see docs/cli/drc.md, 'Waivers')"
            )
        if args.rules is not None:
            # Issue #2768: a native runset's rules are not the curated
            # registry `--rules` resolves against (#2806 tracks native
            # category discovery), so the selection is refused, not ignored.
            raise DrcError(
                "--rules is not supported with --engine klayout: a PDK-native "
                "runset does not share the curated rule registry (see "
                "docs/cli/drc.md, 'Rule listing and selection')"
            )
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
            deck_name=args.deck or None,
            allow_deck_errors=args.allow_deck_errors,
            allow_missing_host_tools=args.allow_missing_host_tools,
            expected_rule_categories=args.expect_rule_categories,
            min_klayout_version=args.min_klayout_version,
            expected_rule_category_names=_expected_rule_category_names(args),
            negative_control=args.negative_control,
            negative_control_categories=_negative_control_categories(args),
        )

    if not args.deck:
        raise DrcError("argument --deck is required for --engine curated")
    # Token syntax (empty tokens) is checked here, before any file is read;
    # matching against the deck happens in `run_drc`, still before the
    # layout is loaded.
    selectors = parse_rule_selectors(args.rules)
    return run_drc(
        args.file,
        args.deck,
        top=args.top,
        pdk_variant=args.pdk,
        pdk_root=args.pdk_root,
        rules=selectors,
        waivers_file=args.waivers,
        tile=args.tile,
        negative_control=args.negative_control,
        negative_control_categories=_negative_control_categories(args),
    )


def _negative_control_categories(args: argparse.Namespace) -> list[str] | None:
    """Split ``--negative-control-categories`` (issue #3076) into names;
    refused without ``--negative-control`` rather than silently ignored."""
    raw = args.negative_control_categories
    if raw is None:
        return None
    if args.negative_control is None:
        raise DrcError(
            "--negative-control-categories requires --negative-control <layout>"
        )
    names = sorted({name.strip() for name in raw.split(",") if name.strip()})
    if not names:
        raise DrcError("--negative-control-categories names no category")
    return names


def _expected_rule_category_names(args: argparse.Namespace) -> list[str] | None:
    """Resolve ``--expect-rule-categories-file`` (issue #2806) into the
    sorted, deduplicated names :func:`run_drc_klayout_engine` compares
    against, once, at the CLI/request boundary -- before KLayout is launched.
    Combining it with ``--expect-rule-categories`` is rejected first, so a
    conflicting invocation never even reads the manifest."""
    manifest = args.expect_rule_categories_file
    if manifest is None:
        return None
    if args.expect_rule_categories is not None:
        raise DrcError(
            "--expect-rule-categories and --expect-rule-categories-file are "
            "mutually exclusive -- pass one rule-category assertion"
        )
    return load_rule_category_manifest(manifest)


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
