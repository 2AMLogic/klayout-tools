"""Off-host corner-echo checks for ``klt sim`` (issues #3035, #5870).

Split out of ``sim.py`` so the per-file size ratchet stays flat. Each check
compares the corner reports an off-host worker returned against the points
the client dispatched, per dispatched index, and refuses the request when a
legacy runner image answered under ``corner_id``s (or ``params``) the client
never sent -- the failure mode that once silently attributed one simulation
to several corners.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .sim_corner_point import _supply_value_needs_full_precision
from .sim_echo_messages import echo_unsupported_message
from .sim_errors import SimError

if TYPE_CHECKING:
    from .sim_corner_point import CornerPoint

#: Backends whose corner reports this process does **not** construct: the
#: corners come back from an independently-run box (``remote``) or job
#: instance (``batch``) as a JSON report, so there is no local
#: :class:`CornerPoint`-keyed dispatch loop here to gate on a checkpoint, a
#: deadline, or a calibration probe of *this* host's hardware. Both
#: ``options.resume`` and the fail-fast probe are refused/skipped for these.
_OFFHOST_BACKENDS = ("remote", "batch")

PARAMS_UNSUPPORTED_WORKER_CODE = "params_unsupported_worker"


def _require_params_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
    env: dict[str, Any] | None = None,
) -> None:
    """Fail the request when an off-host worker did not honor a parameter
    axis (issue #3035).

    A worker that predates ``corners.params`` ignores the field -- it would
    run every row at the netlist's default and report ``pass`` under the
    legacy corner ID. A worker that supports it echoes each point's
    normalized ``params`` and its parameter-suffixed ``corner_id``, so the
    caller compares both against what it dispatched. This is independent of
    any version check (which compares only major.minor), so it also catches
    a same-release runner image built before the feature landed.
    """
    if backend not in _OFFHOST_BACKENDS or not any(p.params for p in points):
        return
    problem: str | None = None
    if len(corners) != len(points):
        problem = (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    else:
        for point, corner in zip(points, corners, strict=True):
            if corner.get("corner_id") != point.corner_id or (
                corner.get("params") or {}
            ) != (point.params or {}):
                problem = (
                    f"corner {point.corner_id!r} came back as "
                    f"{corner.get('corner_id')!r} with params "
                    f"{corner.get('params')!r}"
                )
                break
    if problem is not None:
        raise SimError(
            echo_unsupported_message("params", problem, env),
            code=PARAMS_UNSUPPORTED_WORKER_CODE,
        )


SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE = "supply_precision_unsupported_worker"


def _require_supply_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
    env: dict[str, Any] | None = None,
) -> None:
    """Fail the request when an off-host worker echoes legacy ``.3f`` corner
    IDs for a grid whose supply values need the lossless spelling
    (issue #5870).

    A runner image built before the lossless supply spelling formats every
    rail value to three decimals, so each sub-milli rung comes back under a
    ``corner_id`` the client never dispatched -- and, with colliding IDs, one
    simulation can be mis-attributed to several points. Like
    :func:`_require_params_echo`, this compares per dispatched index and is
    independent of any version check, so it also catches a same-release
    runner image built before the change landed. Gated on the grid actually
    containing a value finer than three decimals: for anything else a legacy
    image and an updated one spell identical IDs, and a gate would refuse
    every older worker for no observable difference.
    """
    if backend not in _OFFHOST_BACKENDS or not any(
        _supply_value_needs_full_precision(value)
        for point in points
        for value in point.supply_v.values()
    ):
        return
    problem: str | None = None
    if len(corners) != len(points):
        problem = (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    else:
        for point, corner in zip(points, corners, strict=True):
            if corner.get("corner_id") != point.corner_id:
                problem = (
                    f"corner {point.corner_id!r} came back as "
                    f"{corner.get('corner_id')!r}"
                )
                break
    if problem is not None:
        raise SimError(
            echo_unsupported_message("supply_v", problem, env),
            code=SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE,
        )
