"""``klt netlist`` command: export an xschem schematic to SPICE, or verify a
committed netlist is still what the schematics produce.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/netlist.md`` for the full table):
    0 - the netlist was exported (``status: "generated"``), or ``--check``
        found the committed netlist still matches (``status: "match"``)
    1 - failed to run: unreadable schematic, xschem not found, xschem hung
        (timeout, after SIGTERM->SIGKILL escalation), or no fresh output file
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - ``--check`` found the committed netlist drifted (or is absent)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

The 0/3 split under ``--check`` is deliberately the same one ``klt drc
--check`` / ``klt lvs --check`` use for ``"match"``/``"drifted"``, so a CI
gate treats a stale committed netlist exactly as it already treats a stale
committed report. ``--check`` differs from those verbs' in one respect worth
stating: theirs take a committed ``--format json`` *report* path as the flag's
value, whereas the artifact being re-verified here is the netlist itself,
which is already named by ``--output`` -- so ``--check`` is a boolean switch
rather than taking a second path.
"""

import argparse

from ..netlist import NetlistError, run_netlist
from .output import emit_error, emit_success

EXIT_OK = 0
EXIT_DRIFTED = 3


def run(args: argparse.Namespace) -> int:
    try:
        report = run_netlist(
            args.schematic,
            args.output,
            check=args.check,
            timeout_s=args.timeout_s,
            xschem_binary=args.xschem_binary,
            rcfile=args.rcfile,
        )
    except NetlistError as exc:
        return emit_error("netlist", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return EXIT_DRIFTED if report["status"] == "drifted" else EXIT_OK


def _print_text(report: dict) -> None:
    print(f"schematic: {report['schematic']}")
    print(f"output: {report['output']}")
    print(f"mode: {report['mode']}")
    print(f"status: {report['status']}")

    xschem = report["xschem"]
    # The exit status is printed *with* its disclaimer, never bare: a reader
    # who sees "exit_status: 1" next to "status: generated" and no explanation
    # reasonably concludes something went wrong (issue #55, edge #1).
    print(
        f"xschem: {xschem['binary']}  exit_status={xschem['exit_status']} "
        "(not the verdict -- xschem exits non-zero on a successful batch "
        f"netlist)  duration={xschem['duration_s']}s"
    )
    print(f"  flags: {' '.join(xschem['forced_flags'])} (always forced)")

    netlist = report["netlist"]
    print(
        f"netlist: {netlist['lines']} lines, {netlist['bytes']} bytes, "
        f"{netlist['content_hash']}"
    )

    drift = report.get("drift")
    if drift is None:
        return

    if drift["reason"] is not None:
        print(f"drift: {drift['reason']}")
    for line in drift["diff"]:
        print(f"  {line}")
    if drift["diff_truncated"]:
        print("  ... diff truncated")
