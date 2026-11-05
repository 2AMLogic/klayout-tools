"""Small place-and-route response helpers split out of ``place_and_route.py``
to keep that file's size ceiling from growing (issue #4914)."""

from __future__ import annotations

import os
from typing import Any


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
