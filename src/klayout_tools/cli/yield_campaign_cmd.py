"""``klt yield-campaign`` command: launch and analyse a Monte Carlo yield
campaign directly from a campaign spec -- issue #906, Phase 2a of the
statistical/yield epic #710.

A distinct top-level verb, not a `klt yield` sub-subcommand -- mirrors `klt
gen-compose`'s own reasoning for standing apart from `klt gen` (see
``docs/cli/gen-compose.md``'s "CLI shape" note): `klt yield`'s existing
single-positional-argument shape (a sample-set/`klt sim`-report path, Phase
1) stays exactly as shipped, and this command produces exactly the same
response shape -- Phase 1's own yield-report JSON, run unmodified -- from a
campaign spec instead of a pre-run sample set, with one added ``campaign``
provenance block.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes mirror `klt yield`'s own (see ``docs/cli/yield.md``):
    0 - the campaign ran and every measurement's declared target_yield was
        met (or none declared one)
    1 - the campaign could not be dispatched or analysed (bad spec, `klt
        sim` failed to start, no spec limits, native extension not built) --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - the campaign ran; at least one measurement's yield claim was not met
        at the stated confidence
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..yield_campaign import CampaignError, run_campaign
from .output import emit_error, emit_success
from .yield_cmd import _print_text as _print_yield_report

EXIT_PASS = 0
EXIT_TARGET_MISSED = 3


def run(args: argparse.Namespace) -> int:
    measurements = None
    if args.measurement:
        measurements = [name for spec in args.measurement for name in spec.split(",")]
        measurements = [name.strip() for name in measurements if name.strip()]
        if not measurements:
            return emit_error(
                "yield-campaign", "--measurement given but empty", args.format
            )

    try:
        report = run_campaign(
            args.spec,
            out_dir=args.out_dir,
            backend=args.backend,
            hosts=args.hosts,
            seed=args.seed,
            confidence=args.confidence,
            target_ci_halfwidth=args.target_ci_halfwidth,
            min_samples=args.min_samples,
            measurements=measurements,
        )
    except CampaignError as exc:
        return emit_error("yield-campaign", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "fail":
        return EXIT_TARGET_MISSED
    return EXIT_PASS


def _print_text(report: dict) -> None:
    campaign = report["campaign"]
    print(f"campaign spec: {campaign['spec']}")
    print(
        f"seed: {campaign['seed']} ({campaign['seed_source']})  "
        f"samples: {campaign['requested_samples']}  vary: {campaign['vary']}"
    )
    print(
        f"backend: {campaign['backend']}  hosts: {campaign['hosts']}  "
        f"sim status: {campaign['sim_status']}  corners: {campaign['corner_count']}"
    )
    print(f"sim report written to: {campaign['sim_report_path']}")
    print()
    _print_yield_report(report)
