"""Per-pin reports from the library-only slew and capacitance sweep."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ._sta_request_diag import extract_block
from .place_and_route_base import (
    _MAX_CAPACITANCE_LIBRARY_VIOLATIONS_BEGIN as CAPACITANCE_BEGIN,
)
from .place_and_route_base import (
    _MAX_CAPACITANCE_LIBRARY_VIOLATIONS_END as CAPACITANCE_END,
)
from .place_and_route_base import (
    _MAX_TRANSITION_LIBRARY_VIOLATIONS_BEGIN as SLEW_BEGIN,
)
from .place_and_route_base import (
    _MAX_TRANSITION_LIBRARY_VIOLATIONS_END as SLEW_END,
)


def prepare_reports(output_dir: str, top: str) -> Path:
    """Clear this design's previous reports, including corners dropped on rerun."""
    directory = Path(output_dir) / f"{top}_route_library_limits"
    for previous in (directory / "aggregate.rpt", *directory.glob("corner-*.rpt")):
        try:
            previous.unlink(missing_ok=True)
        except OSError:
            # A retention failure must not invalidate the measured counts.
            pass
    return directory


def library_limit_fields(
    stdout: str, directory: Path, corner: str | None
) -> dict[str, Any]:
    """Count and retain the same unmodified OpenSTA blocks for this session."""
    slew = (
        extract_block(
            stdout,
            SLEW_BEGIN,
            SLEW_END,
        )
        or ""
    )
    cap = (
        extract_block(
            stdout,
            CAPACITANCE_BEGIN,
            CAPACITANCE_END,
        )
        or ""
    )
    slew_count = slew.count("(VIOLATED)")
    cap_count = cap.count("(VIOLATED)")
    path = None
    if slew_count or cap_count:
        name = (
            "aggregate.rpt"
            if corner is None
            else f"corner-{quote(corner, safe='')}.rpt"
        )
        target = directory / name
        try:
            directory.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(dir=directory) as temp:
                report = Path(temp) / name
                report.write_text(
                    f"max_transition{slew}\nmax_capacitance{cap}", encoding="utf-8"
                )
                report.replace(target)
            path = str(target)
        except OSError:
            pass
    return {
        "max_transition_violation_count_vs_library": slew_count,
        "max_capacitance_violation_count_vs_library": cap_count,
        "library_limit_report_path": path,
    }


def retained_artifacts(output_dir: str, stage: dict[str, Any]) -> list[str]:
    """Include the reports in checkpoint validation before reusing their paths."""
    entries = [stage, *(stage.get("corners") or [])]
    return sorted(
        {
            str(Path(path).relative_to(output_dir))
            for entry in entries
            if (path := entry.get("library_limit_report_path")) is not None
        }
    )
