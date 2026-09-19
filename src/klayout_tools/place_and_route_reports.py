"""Detailed-route pass artifacts and authoritative final DRC metrics."""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any

# Supports both TritonRoute's inline ``on Layer Metal2`` and its multiline
# ``Layer: met1`` spelling. Retain all four identity fields: two violations
# at the same coordinates can still differ in type, source, or layer.
_DRC_RECORD = re.compile(
    r"([^\n]+)\n\s*srcs:\s*(.*?)\s*bbox\s*=\s*(.*?)"
    r"\s*(?:on Layer\s+|Layer:\s*)(\S+)",
    re.DOTALL,
)


def detailed_route_lines(
    output_dir: str, top: str, seed: int, pass_index: int, final_pass: int
) -> list[str]:
    """Keep the historical paths for the final pass, isolate earlier passes.

    Delete this pass's artifacts first: rerunning the same request must not
    append yesterday's violations to today's report. Nonfinal metrics use
    OpenROAD's metrics-stage formatter so their counters cannot collide with
    the final pass's unqualified keys (including the entire iteration series).
    API: OpenROAD src/utl/include/utl/Logger.h, log_metric / metrics_stages_.
    """
    suffix = "" if pass_index == final_pass else f"_pass_{pass_index}"
    drc_report = os.path.join(output_dir, f"{top}_route{suffix}_drc.rpt")
    maze_log = os.path.join(output_dir, f"{top}_route{suffix}_maze.log")
    lines = [f"file delete -force {drc_report} {maze_log}"]
    if pass_index != final_pass:
        lines.append(f'utl::push_metrics_stage "route_pass:{pass_index}__{{}}"')
    lines.append(
        f"detailed_route -output_drc {drc_report} -output_maze {maze_log} "
        f"-or_seed {seed}"
    )
    if pass_index != final_pass:
        lines.append("utl::pop_metrics_stage")
    return lines


def count_route_drc_violations(drc_report_path: str) -> int | None:
    """Count distinct complete (type, sources, bbox, layer) records.

    OpenROAD can repeat a violation even within a single detailed-route pass
    (#2089). Header counts therefore overstate distinct sites. Incomplete
    records are counted individually, never collapsed on insufficient evidence.
    An empty report means zero; an unreadable report means unknown (None).
    Earlier passes must be isolated by the producer: deduplication alone cannot
    discard a distinct violation that was fixed before the final pass.
    """
    try:
        with open(drc_report_path, encoding="utf-8") as handle:
            content = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    records = re.split(r"(?m)^violation type:[ \t]*", content)[1:]
    distinct = set()
    incomplete = 0
    for record in records:
        match = _DRC_RECORD.match(record)
        if match is None:
            incomplete += 1
        else:
            distinct.add(tuple(" ".join(field.split()) for field in match.groups()))
    return len(distinct) + incomplete


def route_metrics_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Resolve repeated engine diagnostics explicitly and retain their history.

    Pass scoping prevents cross-pass collisions in newly generated scripts.
    Engine versions may still repeat keys within a pass. Expose the last emitted
    diagnostic while retaining *every* value for repeated keys; final DRC count
    is replaced separately from the authoritative report, never from this rule.
    """
    data: dict[str, Any] = {}
    repeated: dict[str, list[Any]] = {}
    for key, value in pairs:
        if key in data:
            repeated.setdefault(key, [data[key]]).append(value)
        data[key] = value
    if repeated:
        data["klt__repeated_metrics"] = repeated
    return data


def write_route_metrics(
    metrics_path: str,
    metrics: dict[str, Any],
    drc_count: int | None,
    *,
    error_cls: type[Exception],
) -> None:
    """Publish one authoritative DRC summary in the saved metrics artifact.

    None deliberately replaces an engine counter when the final report is
    unavailable: the saved metric and response must agree that count is unknown.
    Replace the inode atomically: a Docker engine may own the readable
    original file even though its containing directory belongs to this user.
    """
    metrics["route__drc_errors"] = drc_count
    try:
        with tempfile.TemporaryDirectory(
            prefix=".klt-route-metrics-", dir=os.path.dirname(metrics_path) or "."
        ) as temporary:
            replacement = os.path.join(temporary, "metrics.json")
            with open(replacement, "w", encoding="utf-8") as handle:
                json.dump(metrics, handle, indent=2)
                handle.write("\n")
            os.replace(replacement, metrics_path)
    except OSError as exc:
        raise error_cls(
            f"could not write final route metrics '{metrics_path}': {exc}"
        ) from exc
