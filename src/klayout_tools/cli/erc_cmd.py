"""``klt erc`` command: serialise the layer-by-layer connectivity model as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/erc.md`` for the full table):
    0 - extraction succeeded, at least one gate net was found
    1 - failed to run (bad file/spec, malformed stackup/via declaration,
        ambiguous top cell, or no net carries any gate-role geometry at
        all) -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..erc import ErcError, run_erc
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_erc(args.file, args.spec, top=args.top)
    except ErcError as exc:
        return emit_error("erc", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"spec: {report['spec']}")
    print(f"gate_role: {report['gate_role']}")
    print(f"gates: {report['gate_count']}")

    for gate in report["gates"]:
        print()
        label = gate["net"] if gate["net"] else gate["gate_id"]
        print(f"{gate['gate_id']} ({label}): gate_area={gate['gate_area_um2']} um^2")
        for level in gate["levels"]:
            print(
                f"  {level['layer']}: step={level['step_area_um2']} um^2  "
                f"cumulative={level['cumulative_area_um2']} um^2"
            )
