"""Bounded previews and ngspice classification evidence from collected logs."""

from __future__ import annotations

import mmap
import os
import re

from . import sim_cause


def _late_evidence(log: mmap.mmap, limit: int) -> str:
    """Keep the first occurrence of each diagnostic marker beyond the preview.

    Use the runner's patterns on a read-only mapping. A waveform table or a
    single very long line therefore never becomes a Python string in memory.
    """
    from .sim import _BIN_RANGE_SUPERSEDERS, _DIAGNOSTIC_PATTERNS, _NGSPICE_FATAL_CODES

    fatal_codes = _NGSPICE_FATAL_CODES | _BIN_RANGE_SUPERSEDERS
    patterns = [
        pattern for code, pattern in _DIAGNOSTIC_PATTERNS if code in fatal_codes
    ]
    patterns.extend(
        (
            sim_cause._NETLIST_LINE_ERROR_RE,
            sim_cause._FATAL_ABORT_ALTERNATIVES_RE,
            sim_cause._ANALYSIS_STARTED_RE,
            sim_cause._RUN_FINISHED_RE,
        )
    )
    excerpts: dict[int, bytes] = {}
    for pattern in patterns:
        binary = re.compile(
            pattern.pattern.encode("ascii"), pattern.flags & ~re.UNICODE
        )
        match = binary.search(log)
        if match is None or match.end() <= limit:
            continue
        # Retain the marker's own line and its immediate cause line. Bound
        # both sides even if a corrupt or raw waveform row has no newline.
        end = match.end()
        earliest = max(0, end - 512)
        start = max(earliest, log.rfind(b"\n", earliest, end) + 1)
        excerpts[start] = b"\n".join(log[start : end + 512].splitlines()[:2])
    return "\n".join(
        excerpts[pos].decode("utf-8", "replace") for pos in sorted(excerpts)
    )


def read_collected_log(path: str, limit: int) -> tuple[str, str]:
    """Return the original bounded preview and text for the fatal classifier."""
    head = ""
    try:
        with open(path, "rb") as handle:
            head = handle.read(limit).decode("utf-8", "replace")
            if os.fstat(handle.fileno()).st_size <= limit:
                return head, head
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as log:
                return head, head + "\n" + _late_evidence(log, limit)
    except (OSError, ValueError):
        return head, head
