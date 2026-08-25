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
"""

import argparse

from ..equiv import EquivError, run_equiv
from .output import emit_error, emit_success

EXIT_EQUIVALENT = 0
EXIT_COUNTEREXAMPLE = 3
EXIT_INCONCLUSIVE = 4

_EXIT_BY_STATUS = {
    "equivalent": EXIT_EQUIVALENT,
    "counterexample": EXIT_COUNTEREXAMPLE,
    "inconclusive": EXIT_INCONCLUSIVE,
}


def run(args: argparse.Namespace) -> int:
    try:
        report = run_equiv(args.request, timeout_s=args.timeout_s)
    except EquivError as exc:
        return emit_error("equiv", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    return _EXIT_BY_STATUS[report["status"]]


def _print_text(report: dict) -> None:
    print(f"engine: {report['engine']} {report['engine_version'] or '-'}")
    print(f"gold: top={report['gold']['top']}  sources={report['gold']['sources']}")
    print(f"gate: top={report['gate']['top']}  sources={report['gate']['sources']}")
    if report.get("port_map"):
        print(f"port_map: {report['port_map']}")
    print(f"status: {report['status']}")
    print(f"timeout_s: {report['timeout_s']}  elapsed_s: {report['elapsed_s']}")

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
