"""``klt equiv`` command: serialise the combinational or sequential
equivalence report as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/equiv.md`` for the full table):
    0 - proven equivalent (``status: "equivalent"``)
    1 - failed to run at all (bad request, unresolvable/unreadable RTL
        source, unsupported engine, a sequential design in this
        combinational-only MVP's scope, a Yosys elaboration/miter error, or
        a missing ``yosys`` binary) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - ran successfully, proven non-equivalent (``status: "counterexample"``)
    4 - ran but the proof is inconclusive -- solver or process timeout
        (``status: "inconclusive"``); never ``0``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

This is ``klt sim``'s 0/1/2/3/4 precedent: a formal-equivalence proof has the
same third "ran but the result isn't trustworthy" outcome a PVT corner sweep
has (a timeout), which a plain netlist comparison does not have from its own
compare engine. ``klt lvs`` reuses this same ``4`` for the one case where it
too cannot reach a verdict (issue #1370) -- see docs/cli/equiv.md's "Exit
codes" section for the full reasoning.

``--check <report>``/``--rerun`` (issues #2224 + #2280) switch this command
into the shared committed-evidence verification mode, whose exit codes
reuse the same numeric vocabulary the run mode already established -- the
same deliberate reuse ``klt drc --check``/``klt synthesize --check`` make
(#2224): ``0`` still means "nothing to report" (``match``) and ``3`` still
means "a finding to report" (``drifted``), matching the docstring comment
on ``drc_cmd.EXIT_DRIFTED`` -- the modes are mutually exclusive, so the
same code never carries two meanings in one invocation.
"""

import argparse

from ..equiv import (
    EquivError,
    check_equiv_report,
    rerun_equiv_report,
    run_equiv,
)
from .output import emit_error, emit_success, render_rerun_drift

EXIT_EQUIVALENT = 0
EXIT_COUNTEREXAMPLE = 3
EXIT_INCONCLUSIVE = 4

#: Aliases for the ``--check``/``--rerun`` outcome (issues #2224 + #2280) --
#: the same 0/3 split `klt drc --check` uses (`drc_cmd.EXIT_MATCH`/
#: `EXIT_DRIFTED`), named for what it means in verification mode: 0 = the
#: committed evidence still reproduces, 3 = "a finding to report" (the
#: evidence drifted). The modes are mutually exclusive with the run path,
#: so reusing this verb's existing 3 ("counterexample") for "drifted"
#: matches how `klt drc`'s 3 ("violations found") doubles as its
#: "drifted" -- and `klt synthesize`'s 3 likewise.
EXIT_MATCH = 0
EXIT_DRIFTED = 3

_EXIT_BY_STATUS = {
    "equivalent": EXIT_EQUIVALENT,
    "counterexample": EXIT_COUNTEREXAMPLE,
    "inconclusive": EXIT_INCONCLUSIVE,
}


def run(args: argparse.Namespace) -> int:
    if args.check is not None:
        return _run_check(args)

    if args.rerun:
        # Same shape as every other flow verb's --rerun-without---check
        # refusal (issue #2224): a clean error envelope, not a traceback.
        return emit_error("equiv", "--rerun requires --check <report>", args.format)

    try:
        report = run_equiv(
            args.request,
            timeout_s=args.timeout_s,
            sim_backend=args.sim_backend,
            resume=args.resume,
        )
    except EquivError as exc:
        return emit_error("equiv", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return _EXIT_BY_STATUS[report["status"]]


def _run_check(args: argparse.Namespace) -> int:
    """``--check <report>`` (and ``--rerun``): verify committed evidence
    (issues #2224 + #2280) instead of running a fresh proof. Cheap mode
    re-hashes the request's sources against the report's recorded
    ``provenance.input.content_hash`` with no engine run; full mode
    (``--rerun``) re-runs the proof and diffs verdict-bearing fields. The
    same wiring ``synthesize_cmd._run_check`` established, mirrored rather
    than reinvented."""
    try:
        if args.rerun:
            result = rerun_equiv_report(
                args.check,
                args.request,
                timeout_s=args.timeout_s,
                sim_backend=args.sim_backend,
            )
        else:
            result = check_equiv_report(args.check, args.request)
    except EquivError as exc:
        return emit_error("equiv", str(exc), args.format)

    emit_success(
        result, args.format, render_rerun_drift if args.rerun else _print_check_text
    )

    return EXIT_DRIFTED if result["status"] == "drifted" else EXIT_MATCH


def _print_check_text(result: dict) -> None:
    """``--format text`` rendering of a cheap-mode ``--check`` result -- the
    same shape ``synthesize_cmd._print_check_text`` renders, so every
    verb's verification output reads the same way."""
    print(f"report: {result['report']}")
    print(f"status: {result['status']}")
    for check in result["checks"]:
        mark = "OK" if check["match"] else "DRIFTED"
        print(f"  [{mark}] {check['field']}")
        if not check["match"]:
            print(f"      expected: {check['expected']}")
            print(f"      actual:   {check['actual']}")


def _print_resume_line(resume: dict | None) -> None:
    """Issue #2280: present only when --resume was requested. Text is a
    courtesy rendering (docs/json-contract.md), so this line mirrors
    sim_cmd's resume line rather than inventing a second vocabulary."""
    if resume is None:
        return
    print(
        f"resume: reused stage {resume['resumed_stage']} commit record  "
        f"record_path={resume['record_path']}"
    )


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} {report['engine_version'] or '-'}")
    print(f"sim_backend: {report['sim_backend']}")
    print(f"gold: top={report['gold']['top']}  sources={report['gold']['sources']}")
    print(f"gate: top={report['gate']['top']}  sources={report['gate']['sources']}")
    if report.get("port_map"):
        print(f"port_map: {report['port_map']}")
    print(f"status: {report['status']}")
    print(f"timeout_s: {report['timeout_s']}  elapsed_s: {report['elapsed_s']}")
    _print_resume_line(report.get("resume"))

    for diag in report["diagnostics"]:
        print(f"  diagnostic: {diag['code']} - {diag['message']}")

    counterexample = report["counterexample"]
    if counterexample is not None and "cycles" in counterexample:
        # "yosys-sequential" engine's own multi-cycle shape (issue #1313) --
        # see docs/cli/equiv.md's "Sequential equivalence" section.
        print()
        first_diverging = counterexample["first_diverging_cycle"]
        print(f"counterexample (first diverging cycle: {first_diverging}):")
        for cycle in counterexample["cycles"]:
            for name, entry in cycle["inputs"].items():
                print(
                    f"  t={cycle['time']} in  {name} = {entry['bin']}b "
                    f"({entry['value']})"
                )
            for name in cycle["diverging_outputs"]:
                gold_entry = cycle["gold_outputs"][name]
                gate_entry = cycle["gate_outputs"][name]
                print(
                    f"  t={cycle['time']} out {name}: gold={gold_entry['bin']}b "
                    f"({gold_entry['value']})  gate={gate_entry['bin']}b "
                    f"({gate_entry['value']})"
                )
        confirmed = counterexample["confirmed_by_simulation"]
        print(f"  confirmed_by_simulation: {confirmed}")
        _print_cross_check(counterexample)
    elif counterexample is not None:
        print()
        print("counterexample:")
        for name, entry in counterexample["inputs"].items():
            print(f"  in  {name} = {entry['bin']}b ({entry['value']})")
        for name in counterexample["diverging_outputs"]:
            gold_entry = counterexample["gold_outputs"][name]
            gate_entry = counterexample["gate_outputs"][name]
            print(
                f"  out {name}: gold={gold_entry['bin']}b "
                f"({gold_entry['value']})  gate={gate_entry['bin']}b "
                f"({gate_entry['value']})"
            )
        confirmed = counterexample["confirmed_by_simulation"]
        print(f"  confirmed_by_simulation: {confirmed}")
        _print_cross_check(counterexample)

    artifacts = report["artifacts"]
    print()
    print(f"script: {artifacts['script_path']}")
    if artifacts["netlist_path"]:
        print(f"netlist: {artifacts['netlist_path']}")
    if artifacts["log_path"]:
        print(f"log: {artifacts['log_path']}")
    if artifacts.get("stage2_script_path"):
        print(f"stage2 script: {artifacts['stage2_script_path']}")
    if artifacts.get("stage2_log_path"):
        print(f"stage2 log: {artifacts['stage2_log_path']}")


def _print_cross_check(counterexample: dict) -> None:
    """Report the second replay backend's own outcome under
    `--sim-backend both` (issue #2223) -- silent when no cross-check ran,
    so the default single-backend output is unchanged."""
    cross_check = counterexample.get("simulation_cross_check")
    if not cross_check:
        return
    print(
        f"  cross_check ({cross_check['engine']} "
        f"{cross_check['engine_version'] or '-'}): "
        f"agreement={cross_check['agreement']} "
        f"confirmed_by_simulation={cross_check['confirmed_by_simulation']}"
    )
    for entry in cross_check["output_mismatches"]:
        cycle = f"t={entry['cycle']} " if "cycle" in entry else ""
        print(
            f"    mismatch: {cycle}{entry['side']}.{entry['name']} "
            f"canonical={entry['canonical']} "
            f"cross_check={entry['cross_check']} "
            f"explained_by={entry['explained_by']}"
        )
