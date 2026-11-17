"""Report-file output for ``klt sim`` (issues #2975 and #5776).

Split out of ``cli/sim_cmd.py`` to keep the verb module under its per-file
size ceiling (the same move issue #4746 made with ``sim_slug.py``). The
implicit path resolves to the same per-request filename the ``--batch-set``
members write (``sim_set.REPORT_FILENAME``), so a completed single-request
run never exits 0 having put its evidence only on stdout.
"""

from __future__ import annotations

import argparse
import os

from .output import emit_error, write_json_file


def _implicit_report_out(report: dict, args: argparse.Namespace) -> str | None:
    """The implicit ``<outdir>/report.json`` path (issue #5776), or ``None``.

    An explicit ``-o`` on a single-request sweep now also receives the
    report envelope -- the same filename ``--batch-set`` members write
    (``sim_set.py``) -- so a completed run can no longer exit 0 having put
    its evidence only on stdout. A run that only validated or only
    submitted (``--dry-run``/``--submit-only``) has no completed envelope
    to save and writes nothing.
    """
    if not getattr(args, "outdir", None):
        return None
    if report.get("dry_run") or report.get("status") == "submitted":
        return None
    return os.path.join(args.outdir, "report.json")


def _write_report_out(
    report: dict, args: argparse.Namespace, *, implicit_outdir: bool = False
) -> int | None:
    """Write the ``--report-out`` file copy; return an exit code only on failure.

    Issue #2975: an additive file copy of the envelope; stdout is unchanged.
    Written before emitting so an unwritable path fails cleanly with the error
    envelope rather than after a full report.

    Issue #5776: with ``implicit_outdir`` (the single-request sweep only --
    ``--batch-set`` members already write their own ``report.json``), an
    explicit ``-o`` without ``--report-out`` writes ``<outdir>/report.json``
    as well, creating the directory if missing. A write failure there is the
    same error envelope a ``--report-out`` failure is, never a silent skip.
    """
    explicit = getattr(args, "report_out", None)
    report_out = explicit or (
        _implicit_report_out(report, args) if implicit_outdir else None
    )
    if not report_out:
        return None
    try:
        if implicit_outdir and not explicit:
            os.makedirs(args.outdir, exist_ok=True)
        write_json_file(report, report_out)
    except OSError as exc:
        return emit_error(
            "sim",
            f"could not write report to '{report_out}': {exc}",
            args.format,
        )
    return None
