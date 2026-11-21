"""Error text for ``klt sim`` axis-length and worker-echo failures (issue #4885).

Pure string builders, split out of ``sim.py`` so the size ratchet stays flat.
"""

from __future__ import annotations

from typing import Any

from .sim_errors import SimError


def zipped_length_message(field: str, axes: dict[str, Any]) -> str:
    """Text for a ``corners.<field>`` axis-length mismatch: the arrays are
    zipped by index (row i takes element i of every array), not crossed, so
    they must be equally long."""
    lengths = ", ".join(f"{name}={len(values)}" for name, values in axes.items())
    return (
        f"{field} arrays must all be the same length -- they are zipped by "
        f"index (row i uses element i of every array), not crossed; lengths "
        f"found: {lengths}. To sweep a cross product, expand the pairs by "
        "hand. See docs/cli/sim.md"
    )


def require_corners_object(corners_spec: Any) -> None:
    """Raise ``SimError`` when ``corners`` is not an object (e.g. a JSON array)."""
    if not isinstance(corners_spec, dict):
        raise SimError(
            f"corners must be an object (got {type(corners_spec).__name__}); "
            "use corners.units for an explicit list of points. See docs/cli/sim.md"
        )


def echo_identity(remote_environment: dict[str, Any] | None) -> str:
    """``job <id>[, shard <n>]`` for the batch job(s) behind a result, so a
    stale worker image can be told from a result paired with the wrong
    request/shard. Empty when nothing is known."""
    if not remote_environment:
        return ""
    entries = remote_environment.get("fleet")
    if not isinstance(entries, list) or not entries:
        entries = [remote_environment]
    parts = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not entry.get("job_id"):
            continue
        shard = f", shard {index}" if len(entries) > 1 else ""
        parts.append(f"job {entry['job_id']}{shard}")
    return "; ".join(parts)


#: Per-axis wording: (axis noun, introducing issue, what a stale worker simulates).
_AXES = {
    "params": ("parameter", 3035, "the netlist default for every row"),
    "options": ("options", 3125, "the default solver tolerances for every rung"),
}


def echo_unsupported_message(
    field: str, problem: str, remote_environment: dict[str, Any] | None
) -> str:
    """Text for a worker that did not honor ``corners.<field>``: names both
    candidate causes (an old runner image, or a result paired with the wrong
    request/shard) and the job/shard identity so they can be told apart."""
    axis, issue, effect = _AXES[field]
    identity = echo_identity(remote_environment)
    where = f" [{identity}]" if identity else ""
    return (
        f"the remote/batch worker did not honor corners.{field} -- "
        f"{problem}{where}. Either the worker was built before the {axis} "
        f"axis (issue #{issue}) and ignores it -- it would simulate {effect}; "
        "update the runner image to this klt release, or use a local "
        "backend -- or the result was paired with the wrong request/shard "
        "(compare the job id with the one submitted). No result was reported"
    )
