"""Diagnostic payloads that make a failed ``--check`` / signoff comparison legible.

Issue #4795. These build *additional, optional* keys only; verdicts and the
``expected``/``actual``/``match`` triple of every comparison are unchanged.
Kept out of :mod:`klayout_tools._report_verify` (and ``lvs``/``signoff``)
because those files are under the per-file size ratchet.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from ._report_verify import hash_check


def input_not_found_entry(
    path: str, report_path: str, found_relative_to_report: str | None
) -> dict[str, Any]:
    """The ``input_not_found`` diagnostic for an echoed input path that does
    not name an existing file (issues #2595, #4795).

    - ``path`` -- the path verbatim, as the report recorded it.
    - ``resolved`` -- the absolute path that was actually looked for.
    - ``found_relative_to_report`` -- where the input *does* live relative to
      the committed report's directory, or ``None``.
    - ``resolved_against`` -- the directory a relative ``path`` was anchored
      to (the cwd); ``None`` for an absolute path, which has no anchor.
    - ``hint`` -- when the input sits next to the report, how to reach it;
      ``None`` otherwise.
    """
    report_dir = os.path.dirname(os.path.abspath(report_path))
    hint = (
        None
        if found_relative_to_report is None
        else "run --check from the report's directory, or pass --layout/"
        "an absolute path; request-file-relative paths resolve against "
        f"{report_dir}"
    )
    return {
        "path": path,
        "resolved": os.path.abspath(path),
        "found_relative_to_report": found_relative_to_report,
        "resolved_against": None if os.path.isabs(path) else os.getcwd(),
        "hint": hint,
    }


def stale_pin(spec: Mapping[str, Any], resolution: Mapping[str, Any]) -> dict[str, Any]:
    """``detail`` for a ``stale_evidence`` verdict: names the failed
    comparison (field / expected / actual, the entry shape ``--check`` emits)
    so the author can tell which input to regenerate or re-pin (issue #4795).
    """
    return {
        "content_hash_pin": "stale",
        "hash_check": hash_check(
            "provenance.input.content_hash",
            spec.get("content_hash"),
            resolution["content_hash"],
        ),
    }
