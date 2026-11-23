"""The ``klt sim --cancel`` mode, split out of ``cli/sim_cmd.py`` in the
``sim_job_status.py`` pattern (issue #4351) to respect that module's
per-file size ceiling.

Exit ``0`` when the fleet accepted the cancel. A job with nothing live to
terminate fails with ``error.code: "batch_cancel_nothing_to_cancel"``;
any other script failure surfaces its bucket-redacted stderr.
"""

from __future__ import annotations

import argparse

from ..sim import SimError, load_request
from ..sim_batch_cancel import cancel_job
from .output import EXIT_USAGE_ERROR, emit_error, emit_success

EXIT_PASS = 0

#: Job-by-id flags ``--cancel`` cannot ride with. ``--batch-set`` is refused
#: centrally in ``cli/sim_cmd.py``'s ``_run_batch_set`` conflict list.
_CONFLICTS = (
    ("submit_only", "--submit-only"),
    ("wait_for_capacity", "--wait-for-capacity: it launches nothing"),
    ("attach_job_id", "--attach"),
    ("job_status", "--job-status"),
)


def _run_job_cancel(args: argparse.Namespace) -> int:
    """``klt sim [REQUEST] --cancel JOB_ID``: cancel one fleet job (#4351).

    The request file is optional (as ``--job-status`` since issue #5810):
    without one, the bucket/region/profile resolve from the ``KLT_BATCH_*``
    environment and the fleet config.
    """
    for flag, tail in _CONFLICTS:
        value = getattr(args, flag, None)
        if value is not None and value is not False:
            return emit_error(
                "sim",
                f"--cancel cannot be combined with {tail}",
                args.format,
                exit_code=EXIT_USAGE_ERROR,
            )
    if args.backend not in (None, "batch"):
        return emit_error(
            "sim",
            f"--cancel drives the batch fleet (got --backend {args.backend})",
            args.format,
            exit_code=EXIT_USAGE_ERROR,
        )
    try:
        request = load_request(args.request) if args.request is not None else {}
        result = cancel_job(request, args.cancel)
    except SimError as exc:
        return emit_error("sim", str(exc), args.format, code=exc.code)
    emit_success(result, args.format, _print_job_cancel_text)
    return EXIT_PASS


def _print_job_cancel_text(payload: dict) -> None:
    print(f"cancelled: {payload['job_id']}")
    print(f"job: {payload['job_uri']}  (s3 prefix left in place)")
    for line in payload.get("warnings") or []:
        print(f"warning: {line}")
