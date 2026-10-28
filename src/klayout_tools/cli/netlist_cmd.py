"""``klt netlist`` command: export an xschem schematic to SPICE, or verify a
committed netlist is still what the schematics produce.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/netlist.md`` for the full table):
    0 - the netlist was exported (``status: "generated"``), or ``--check``
        found the committed netlist still matches (``status: "match"``)
    1 - failed to run: unreadable schematic, xschem not found, xschem hung
        (timeout, after SIGTERM->SIGKILL escalation), or no fresh output file
        -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``. Under
        ``--backend batch`` every batch failure is also exit 1, never a
        local fallback (``klayout_tools.netlist_batch``)
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
import sys

from ..netlist import NetlistError, run_netlist
from ..netlist_batch import resolve_backend, run_netlist_batch
from .output import emit_error, emit_success

EXIT_OK = 0
EXIT_DRIFTED = 3


def _warn(message: str) -> None:
    print(f"klt netlist: warning: {message}", file=sys.stderr)


def run(args: argparse.Namespace) -> int:
    try:
        # `--backend` > $KLT_NETLIST_BACKEND > local. `batch` never falls
        # back to a local xschem run: every batch failure is exit 1.
        backend = resolve_backend(getattr(args, "backend", None))
        allow_expr = bool(getattr(args, "allow_unevaluated_expr", False))
        if backend == "batch":
            if allow_expr:
                raise NetlistError(
                    "--allow-unevaluated-expr is not supported with "
                    "--backend batch (the runner applies the expr() gate "
                    "itself); use --backend local"
                )
            report = run_netlist_batch(
                args.schematic,
                args.output,
                check=args.check,
                timeout_s=args.timeout_s,
                rcfile=args.rcfile,
                pdk=args.pdk,
                pdk_root=args.pdk_root,
                block=args.block,
                batch_root=getattr(args, "batch_root", None),
                batch_include=getattr(args, "batch_include", None) or (),
                batch_exclude=getattr(args, "batch_exclude", None) or (),
                xschem_binary=args.xschem_binary,
                warn=_warn,
            )
        else:
            report = run_netlist(
                args.schematic,
                args.output,
                check=args.check,
                timeout_s=args.timeout_s,
                xschem_binary=args.xschem_binary,
                rcfile=args.rcfile,
                pdk=args.pdk,
                pdk_root=args.pdk_root,
                block=args.block,
                allow_unevaluated_expr=allow_expr,
            )
    except NetlistError as exc:
        return emit_error(
            "netlist", str(exc), args.format, code=exc.code, details=exc.details
        )

    for entry in report.get("warnings") or ():
        _warn(
            f"unevaluated expr(...) at line {entry['line']}: {entry['snippet']!r} "
            f"(xschem version: {entry['xschem_version'] or 'unknown'}); "
            "written anyway because --allow-unevaluated-expr was given"
        )
    emit_success(report, args.format, _print_text)
    return EXIT_DRIFTED if report["status"] == "drifted" else EXIT_OK


def _print_remote(report: dict) -> None:
    """The batch job line, only for a ``--backend batch`` run."""
    remote = (report.get("environment") or {}).get("remote")
    if remote is None:
        return
    print(
        f"batch: job {remote.get('job_id')} state={remote.get('state')} "
        f"instance={remote.get('instance_type') or '-'} "
        f"runner_klt={remote.get('runner_klt_version') or '-'} "
        f"({remote.get('runner_compatibility')})"
    )


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
        f"xschem: {xschem['binary']}  version={xschem.get('version') or 'unknown'}"
        f"  exit_status={xschem['exit_status']} "
        "(not the verdict -- xschem exits non-zero on a successful batch "
        f"netlist)  duration={xschem['duration_s']}s"
    )
    print(f"  flags: {' '.join(xschem['forced_flags'])} (always forced)")
    _print_remote(report)

    netlist = report["netlist"]
    print(
        f"netlist: {netlist['lines']} lines, {netlist['bytes']} bytes, "
        f"{netlist['content_hash']}"
    )

    pdk_block = report.get("pdk")
    if pdk_block is not None:
        print(
            f"pdk: {pdk_block['variant']} {pdk_block['version'] or '-'} "
            f"(via {pdk_block['resolved_via']})"
        )
        if pdk_block["warning"]:
            print(f"pdk: WARNING: {pdk_block['warning']}")
        if pdk_block["ambiguity_warning"]:
            print(f"pdk: {pdk_block['ambiguity_warning']}")

    block_block = report.get("block")
    if block_block is not None:
        print(
            "block: subckt_uncommented="
            f"{block_block['subckt_uncommented']} "
            f"ends_uncommented={block_block['ends_uncommented']} "
            f"trailing_end_removed={block_block['trailing_end_removed']}"
        )
        if block_block["warning"]:
            print(f"block: WARNING: {block_block['warning']}")

    drift = report.get("drift")
    if drift is None:
        return

    if drift["reason"] is not None:
        print(f"drift: {drift['reason']}")
    for entry in drift.get("subckt_port_order", []):
        print(
            f"drift: .subckt {entry['subckt']} ports "
            f"{' '.join(entry['committed'])} -> {' '.join(entry['regenerated'])}"
        )
    for line in drift["diff"]:
        print(f"  {line}")
    if drift["diff_truncated"]:
        print("  ... diff truncated")
