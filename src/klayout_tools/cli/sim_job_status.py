"""The ``klt sim --job-status`` mode, split out of ``cli/sim_cmd.py`` to
keep that module under its per-file size ceiling -- the same move
``sim_report_out.py`` made (issue #4746).

Exit ``0`` whenever the read succeeded -- whatever state the *job* is in
(``failed`` included): the exit code reports this read, the ``state``
field reports the job. ``1`` for an unknown job id or a transport error.
"""

from __future__ import annotations

import argparse

from ..sim import SimError, load_request
from ..sim_batch_async import read_job_status
from .output import EXIT_USAGE_ERROR, emit_error, emit_success
from .sim_job_text import _print_job_status_text

EXIT_PASS = 0


def _run_job_status(args: argparse.Namespace) -> int:
    """``klt sim [REQUEST] --job-status JOB_ID``: one state read (issue #3073).

    The request file is optional here (issue #5810): without one, the
    bucket/region/profile resolve from the ``KLT_BATCH_*`` environment and
    the fleet config, exactly as ``read_job_status`` resolves them.
    """
    if getattr(args, "submit_only", False):
        return emit_error(
            "sim",
            "--job-status cannot be combined with --submit-only",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if getattr(args, "wait_for_capacity", None) is not None:
        return emit_error(
            "sim",
            "--job-status cannot be combined with --wait-for-capacity: it "
            "launches nothing",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    if args.backend not in (None, "batch"):
        return emit_error(
            "sim",
            f"--job-status reads a batch job (got --backend {args.backend})",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        request = load_request(args.request) if args.request is not None else {}
        status = read_job_status(request, args.job_status)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    emit_success(status, args.format, _print_job_status_text)
    return EXIT_PASS
