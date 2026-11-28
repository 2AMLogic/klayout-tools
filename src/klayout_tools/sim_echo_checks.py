"""Off-host corner-echo checks for ``klt sim`` (issues #3035, #5870).

Split out of ``sim.py`` so the per-file size ratchet stays flat. Each check
compares the corner reports an off-host worker returned against the points
the client dispatched, per dispatched index, and refuses the request when a
legacy runner image answered under ``corner_id``s (or ``params``) the client
never sent -- the failure mode that once silently attributed one simulation
to several corners.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .sim_corner_point import _supply_value_needs_full_precision
from .sim_echo_messages import echo_unsupported_message
from .sim_errors import SimError
from .sim_transient_noise import check_transient_noise_seed_echo

if TYPE_CHECKING:
    from .sim_corner_point import CornerPoint

#: Backends whose corner reports this process does **not** construct: the
#: corners come back from an independently-run box (``remote``) or job
#: instance (``batch``) as a JSON report, so there is no local
#: :class:`CornerPoint`-keyed dispatch loop here to gate on a checkpoint, a
#: deadline, or a calibration probe of *this* host's hardware. Both
#: ``options.resume`` and the fail-fast probe are refused/skipped for these.
OFFHOST_BACKENDS = ("remote", "batch")


def _first_corner_echo_mismatch(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    mismatch: Callable[[CornerPoint, dict[str, Any]], str | None],
) -> str | None:
    """Shared scan behind every per-corner echo check: the count
    disagreement when the worker returned some other number of corner
    reports than points dispatched, else the first per-index problem
    ``mismatch`` reports (``None`` when every point echoed back cleanly)."""
    if len(corners) != len(points):
        return (
            f"it returned {len(corners)} corner report(s) for "
            f"{len(points)} dispatched point(s)"
        )
    for point, corner in zip(points, corners, strict=True):
        problem = mismatch(point, corner)
        if problem is not None:
            return problem
    return None


def _require_dict_axis_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    attr: str,
    code: str,
    backend: str = "remote",
    env: dict[str, Any] | None = None,
) -> None:
    """Shared body of the ``params`` and ``options`` echo checks: a worker
    that honors a dict-valued corner axis echoes both the axis dict and its
    axis-suffixed ``corner_id`` back in each corner report, so the first
    disagreement on either names the stale-worker problem."""

    def mismatch(point: CornerPoint, corner: dict[str, Any]) -> str | None:
        if corner.get("corner_id") != point.corner_id or (corner.get(attr) or {}) != (
            getattr(point, attr) or {}
        ):
            return (
                f"corner {point.corner_id!r} came back as "
                f"{corner.get('corner_id')!r} with {attr} "
                f"{corner.get(attr)!r}"
            )
        return None

    if backend not in OFFHOST_BACKENDS or not any(getattr(p, attr) for p in points):
        return
    problem = _first_corner_echo_mismatch(points, corners, mismatch)
    if problem is not None:
        raise SimError(echo_unsupported_message(attr, problem, env), code=code)


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
    _require_dict_axis_echo(
        points,
        corners,
        attr="params",
        code=PARAMS_UNSUPPORTED_WORKER_CODE,
        backend=backend,
        env=env,
    )


OPTIONS_UNSUPPORTED_WORKER_CODE = "options_unsupported_worker"


def _require_options_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
    env: dict[str, Any] | None = None,
) -> None:
    """Fail the request when an off-host worker did not honor a
    ``corners.options`` axis (issue #3125) -- the counterpart of
    :func:`_require_params_echo`. A worker that predates the axis ignores
    it and would run every rung at the default tolerance under the legacy
    corner ID; a supporting worker echoes each point's ``options`` and its
    option-suffixed ``corner_id``."""
    _require_dict_axis_echo(
        points,
        corners,
        attr="options",
        code=OPTIONS_UNSUPPORTED_WORKER_CODE,
        backend=backend,
        env=env,
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
    if backend not in OFFHOST_BACKENDS or not any(
        _supply_value_needs_full_precision(value)
        for point in points
        for value in point.supply_v.values()
    ):
        return

    def mismatch(point: CornerPoint, corner: dict[str, Any]) -> str | None:
        if corner.get("corner_id") != point.corner_id:
            return (
                f"corner {point.corner_id!r} came back as {corner.get('corner_id')!r}"
            )
        return None

    problem = _first_corner_echo_mismatch(points, corners, mismatch)
    if problem is not None:
        raise SimError(
            echo_unsupported_message("supply_v", problem, env),
            code=SUPPLY_PRECISION_UNSUPPORTED_WORKER_CODE,
        )


NODESET_UNSUPPORTED_WORKER_CODE = "nodeset_unsupported_worker"


def _require_nodeset_echo(
    points: list[CornerPoint],
    corners: list[dict[str, Any]],
    *,
    backend: str = "remote",
    label: str = "options.nodeset",
    attr: str = "nodeset",
    code: str = NODESET_UNSUPPORTED_WORKER_CODE,
) -> None:
    """Fail the request when an off-host worker did not honor
    ``options.nodeset`` (issue #3036) -- or, with ``label``/``attr``/``code``
    of ``options.initial_conditions``, that field (issue #3881).

    A worker built before the field ignores the hints and would report a
    passing, unhinted solve; one that supports it echoes each point's
    ``nodeset`` in its corner report, which is compared with what was
    dispatched. Independent of any version check, so it also catches a
    same-release runner image built before the feature landed.
    """
    if backend not in OFFHOST_BACKENDS or not any(getattr(p, attr) for p in points):
        return

    def mismatch(point: CornerPoint, corner: dict[str, Any]) -> str | None:
        expected = getattr(point, attr)
        if (corner.get(attr) or type(expected)()) != expected:
            return (
                f"corner {point.corner_id!r} came back with {attr} "
                f"{corner.get(attr)!r}, expected {expected!r}"
            )
        return None

    problem = _first_corner_echo_mismatch(points, corners, mismatch)
    if problem is not None:
        raise SimError(
            f"the remote/batch worker did not honor {label} -- "
            f"{problem}. A worker built before the field ignores it and "
            "would solve without the requested initial values; update the "
            "runner image to this klt release, or use a local backend. No "
            "result was reported",
            code=code,
        )


def check_ngspice_options_echo(request: dict[str, Any], report: dict[str, Any]) -> None:
    """Fail loudly when an off-host report did not honour
    ``options.ngspice_options`` (issue #3111).

    A runner that predates the field drops it silently; its report then
    omits ``environment.ngspice_options``. Comparing the echo with the
    declared map turns that into a named :class:`SimError`.
    """
    declared = (request.get("options") or {}).get("ngspice_options") or {}
    if not declared:
        return
    echoed = (report.get("environment") or {}).get("ngspice_options") or {}
    for name in declared:
        if echoed.get(name) != declared[name] or name not in echoed:
            raise SimError(
                f"off-host runner did not honour options.ngspice_options."
                f"{name}: declared {json.dumps(declared[name])}, runner "
                f"echoed {json.dumps(echoed.get(name))} (the runner is "
                "likely older than this client and dropped the option)"
            )
    if set(echoed) != set(declared):
        raise SimError(
            "off-host runner echoed options.ngspice_options "
            f"{json.dumps(echoed, sort_keys=True)}, which differs from the "
            f"declared {json.dumps(declared, sort_keys=True)}"
        )


def check_runner_option_echoes(request: dict[str, Any], report: dict[str, Any]) -> None:
    """Verify declared simulator options against the collected worker report."""
    check_ngspice_options_echo(request, report)
    check_transient_noise_seed_echo(request, report)
