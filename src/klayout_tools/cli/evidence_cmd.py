"""``klt evidence record``: write one ``evidence-record/1`` file around a
finished ``klt sim`` / ``klt pex`` report (issue #4044, gap G2).

Emits through the shared envelope helpers in :mod:`.output` -- see
``docs/json-contract.md`` and ``docs/cli/evidence.md``.

Exit codes:
    0 - record written
    1 - not written: unreadable/invalid input, non-git ``--root``, an
        existing record (never overwritten), or a provenance leak --
        ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse

from .. import evidence_record
from .output import emit_error, emit_success


def run_record(args: argparse.Namespace) -> int:
    try:
        summary = evidence_record.write_record(
            args.report,
            args.request,
            block=args.block,
            scope=args.scope,
            root=args.root,
            subset_reason=args.subset_reason,
            supersedes=args.supersedes or [],
            extensions_path=args.extensions,
            extra_inputs=args.inputs or [],
        )
    except evidence_record.EvidenceRecordError as exc:
        return emit_error("evidence record", str(exc), args.format, code=exc.code)

    payload = {"schema_version": 1, "status": "recorded", **summary}
    emit_success(payload, args.format, _print_record_text)
    return 0


def _print_record_text(payload: dict) -> None:
    print(f"recorded {payload['record_path']}")
    print(f"  record id:        {payload['record_id']}")
    print(f"  dirty inputs:     {payload['dirty_inputs']}")
    print(f"  netlist snapshot: {payload['netlist_snapshot']}")
