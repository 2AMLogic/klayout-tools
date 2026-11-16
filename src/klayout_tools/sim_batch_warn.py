"""Once-per-process stderr warnings for batch planning (issue #5010).

Split out of :mod:`klayout_tools.sim_batch` to keep that module under its
per-file size ratchet.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

SERIAL_BOUND_WARNED = threading.Event()


def warn_over_serial_bound_once(
    requested: int | None, largest: int, per_job: int, assessment: dict[str, Any]
) -> None:
    """Emit the over-the-serial-bound warning at most once per process, so a
    ``--batch-set`` of N requests prints it once, not N times. The bound is
    ``timeout_s`` x units, so lowering ``options.timeout_s`` tightens it --
    that is the actionable part."""
    if SERIAL_BOUND_WARNED.is_set():
        return
    SERIAL_BOUND_WARNED.set()
    print(
        f"klt sim: warning: request.batch.max_units_per_job {requested} "
        f"leaves jobs of {largest} units, over the {per_job}-unit "
        f"serial upper bound for the fleet's "
        f"{assessment['max_job_seconds']:g}s whole-job ceiling (bound = "
        "options.timeout_s x units, not a runtime estimate; lower "
        "options.timeout_s to tighten it); a job still running at the "
        "ceiling is killed and every unit is reported batch_job_timeout "
        "(shown once per run)",
        file=sys.stderr,
    )
