"""``klt yield-samples`` command: derive a ``klt yield`` sample-set document
from two ``klt sim`` Monte Carlo reports -- a nominal campaign and a seeded
known-bad variant that becomes each measurement's ``negative_control``
(issue #2563).

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``. Under
``--format json`` the flat success payload **is** the sample-set document
(plus ``schema_version`` and an additive ``derivation`` block), so it can be
redirected straight to a file and handed to ``klt yield``::

    klt yield-samples nominal.json --negative-control known-bad.json \\
        --description 'seeded defect' --format json > samples.json
    klt yield samples.json --format json > yield.json

This command neither runs simulations nor grades yield; it needs no native
extension.

Exit codes:
    0 - the sample-set document was derived and written to stdout
    1 - failed to derive (missing/unreadable/malformed input, no Monte Carlo
        corners, an unmatched/duplicate/unit-mismatched measurement, an
        existing negative_control) -- returned by ``emit_error`` as
        ``output.ERROR_EXIT_CODE``, with stdout left empty
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand -- e.g. a missing ``--negative-control``.)
"""

from __future__ import annotations

import argparse

from ..yield_analysis import YieldError, derive_sample_set
from .output import emit_error, emit_success

COMMAND = "yield-samples"


def run(args: argparse.Namespace) -> int:
    measurements = None
    if args.measurement:
        measurements = [name for spec in args.measurement for name in spec.split(",")]
        measurements = [name.strip() for name in measurements if name.strip()]
        if not measurements:
            return emit_error(COMMAND, "--measurement given but empty", args.format)

    try:
        payload = derive_sample_set(
            args.nominal,
            args.negative_control,
            description=args.description,
            measurements=measurements,
        )
    except YieldError as exc:
        return emit_error(COMMAND, str(exc), args.format)

    emit_success(payload, args.format, _print_text)
    return 0


def _print_text(payload: dict) -> None:
    derivation = payload["derivation"]
    nominal = derivation["nominal"]
    control = derivation["negative_control"]
    print(f"nominal: {nominal['path']}  {nominal['content_hash']}")
    print(f"negative control: {control['path']}  {control['content_hash']}")
    if control.get("description"):
        print(f"description: {control['description']}")
    print(f"measurements: {len(payload['measurements'])}")

    disclosures = {d["name"]: d for d in derivation["measurements"]}
    for m in payload["measurements"]:
        unit = f" [{m['unit']}]" if m.get("unit") else ""
        nc = m["negative_control"]
        disclosed = disclosures[m["name"]]["negative_control"]
        print()
        print(f"{m['name']}{unit}")
        print(
            f"  nominal: N={len(m['samples'])} errored={m['errored']} "
            f"inconclusive={m['inconclusive']} "
            f"failed_unmeasurable={m['failed_unmeasurable']} "
            f"censored={m['censored']}"
        )
        print(
            f"  negative control: N={len(nc['samples'])} errored={nc['errored']} "
            f"(= {disclosed['errored']} errored + {disclosed['inconclusive']} "
            f"inconclusive) failed_unmeasurable={nc['failed_unmeasurable']} "
            f"censored={nc['censored']}"
        )

    print()
    print(
        "the sample-set document is the --format json output; pass it to "
        "`klt yield` (see docs/cli/yield.md)"
    )
