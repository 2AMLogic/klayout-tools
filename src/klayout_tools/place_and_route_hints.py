"""Small place-and-route response helpers split out of ``place_and_route.py``
to keep that file's size ceiling from growing (issue #4914)."""

from __future__ import annotations

import os
from typing import Any

from .place_and_route_reports import parse_global_route_congestion


def outside_cwd_note(path: str) -> str:
    """Issue #4191: the docker wrapper mounts only `$PWD`, `$PDK_ROOT` and
    `$TMPDIR`, so a readable path outside the invocation directory is the
    commonest trigger -- say the rule and the fix, not just the symptom."""
    cwd = os.path.realpath(os.getcwd())
    real = os.path.realpath(path)
    if real == cwd or real.startswith(cwd.rstrip(os.sep) + os.sep):
        return ""
    return (
        f"; '{path}' is outside the invocation directory '{cwd}', and the "
        "container wrapper mounts only $PWD, $PDK_ROOT and $TMPDIR -- run "
        "from a common ancestor of the request and its inputs"
    )


def null_unconstrained_fmax(entry: dict[str, Any]) -> None:
    """Null ``fmax_mhz`` in place when the design is unconstrained (#4914)."""
    if entry["timing_status"] == "unconstrained" and "fmax_mhz" in entry:
        entry["fmax_mhz"] = None


def make_tracks_lines(floorplan: dict[str, Any]) -> list[str]:
    tracks = floorplan.get("tracks")
    if not tracks:
        return ["make_tracks"]
    return [
        f"make_tracks {t['layer']} -x_offset {t['x_offset_um']} "
        f"-x_pitch {t['x_pitch_um']} -y_offset {t['y_offset_um']} "
        f"-y_pitch {t['y_pitch_um']}"
        for t in tracks
    ]


def grt_hint(stage: str, streams: str, stdout: str) -> str | None:
    """``None``, or a congestion summary for a ``GRT-0116`` route failure
    (issue #5122).

    Names the worst-overflow layer and the total overflow from the
    ``GRT-0096`` table when it is in the captured stdout; otherwise points
    at ``route_allow_congestion`` so the caller can get the per-layer table.
    """
    if stage != "route" or "GRT-0116" not in streams:
        return None
    congestion = parse_global_route_congestion(stdout)
    if congestion is None:
        return (
            "no per-layer congestion table was captured; retry with "
            "`route_allow_congestion: true` to get the per-layer overflow table"
        )
    layers = congestion["overflow_per_layer"]
    worst = max(layers, key=lambda name: layers[name]["total_overflow"])
    return (
        f"global-route congestion: total overflow {congestion['total_overflow']} "
        f"(max H {congestion['max_horizontal_overflow']} / "
        f"max V {congestion['max_vertical_overflow']}); worst layer {worst} "
        f"with overflow {layers[worst]['total_overflow']}"
    )
