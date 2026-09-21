"""``klt erc`` command: serialise the layer-by-layer connectivity model and
per-gate antenna-ratio verdict as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/erc.md`` for the full table):
    0 - "clean" (at least one antenna level graded, no antenna/connectivity
        finding) or "clean_partial" (the common rollup rule, issue #2109 --
        every executed check passed, but some requested antenna work was
        skipped, e.g. a full sky130 stack whose met3-5 roles have no
        antenna-ratio limit)
    1 - failed to run (bad file/spec, malformed stackup/via declaration,
        unknown --pdk, ambiguous top cell, or no net carries any gate-role
        geometry at all) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``
    3 - an actual finding/limit failure
    4 - no relevant checks (not_checked)
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

The exit code answers the *antenna* question, because ``status`` does (see
``_EXIT_CODE_BY_STATUS`` below). On a PDK with no antenna-ratio table --
every PDK but sky130 today, and any run that omits ``--pdk`` -- that answer
is permanently ``4``, however clean the design is. A caller that wants only
the connectivity/structural read reads the payload's own ``erc_status``
(issue #2179) instead of the exit code; it is ``"clean"``/``"violations"``
regardless of whether an antenna table exists.
"""

import argparse

from ..erc import ErcError, run_erc
from .output import emit_error, emit_success

#: `status` -> exit code (issue #2115, applying the common rollup rule's
#: exit codes, #2109). `"clean_partial"` reports the same exit `0` as
#: `"clean"`: a partial result is a real, successful run, just not this
#: verb's *unconditional* success -- `klt signoff` is where that
#: distinction actually gates a citation (`docs/coverage-contract.md`'s
#: "Signoff qualification").
_EXIT_CODE_BY_STATUS = {
    "clean": 0,
    "clean_partial": 0,
    "violations": 3,
    "not_checked": 4,
}


def run(args: argparse.Namespace) -> int:
    try:
        report = run_erc(args.file, args.spec, top=args.top, pdk=args.pdk)
    except ErcError as exc:
        return emit_error("erc", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return _EXIT_CODE_BY_STATUS[report["status"]]


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"spec: {report['spec']}")
    print(f"pdk: {report['pdk']}")
    print(f"status: {report['status']}")
    print(f"gate_role: {report['gate_role']}")
    print(f"gates: {report['gate_count']}")

    for gate in report["gates"]:
        print()
        label = gate["net"] if gate["net"] else gate["gate_id"]
        print(
            f"{gate['gate_id']} ({label}): gate_area={gate['gate_area_um2']} um^2 "
            f"antenna_verdict={gate['antenna_verdict']}"
        )
        for level in gate["levels"]:
            print(
                f"  {level['layer']}: step={level['step_area_um2']} um^2  "
                f"cumulative={level['cumulative_area_um2']} um^2  "
                f"antenna_ratio={level['antenna_ratio']} "
                f"(max={level['antenna_ratio_max']}) "
                f"verdict={level['verdict']}"
            )
            remedy = level["remedy"]
            if remedy is not None:
                target = remedy["target_layer"]
                suffix = f" -> {target}" if target else ""
                print(f"    remedy: {remedy['type']}{suffix}")

    print()
    print(f"erc_status: {report['erc_status']}")
    print(f"erc_findings: {report['erc_finding_count']}")
    for finding in report["erc_findings"]:
        subject = finding["net"] or finding["gate_id"] or finding["layer"] or "?"
        print(f"  [{finding['rule']}] {subject}: {finding['description']}")
        # Per-island locations for a multi-island `erc.unconnected_net`
        # (issue #2194) -- the count alone says nothing about where to
        # look. Raw-database-unit boxes, same `(left,bottom)-(right,top)`
        # rendering `klt drc`'s own text violations use.
        for i, island in enumerate(finding["islands"] or []):
            bbox = island["bbox"]
            where = (
                f"({bbox['left']},{bbox['bottom']})-({bbox['right']},{bbox['top']})"
                if bbox is not None
                else "?"
            )
            print(
                f"    island {i + 1}: {where}  layer={island['layer']}  "
                f"shapes={island['shape_count']}"
            )
