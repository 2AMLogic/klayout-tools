"""``klt place-and-route --backend batch`` for a single-unit flow run
(issue #5799).

``place_and_route_batch`` dispatches one fleet job per ``pdk.rebuild_corners``
unit. A real ~12k-instance route is a ~78-minute single-threaded OpenROAD
process, and with one corner there was no way to move it off the host. This
module submits **the whole flow** -- honoring ``target_stage`` -- as **one**
fleet job when the backend is selected *explicitly* (``--backend batch`` or
``request.backend``):

* A host-default ``$KLT_SIM_BACKEND=batch`` still keeps a single unit local
  (:func:`place_and_route_batch.resolve_backend`): it is a preference, and a
  smoke/debug run must not go to the fleet.
* The request is shipped unchanged (``backend`` dropped; a one-element
  ``pdk.rebuild_corners`` list becomes ``pdk.corner`` exactly as in a
  multi-corner unit), so the runner executes the same flow a local run would.
* The runner's ``.klt/place-and-route/`` is copied back into this host's
  ``.klt/place-and-route/`` (no ``<corner>/`` subdirectory), so
  ``--check``/``--rerun`` and every report path read as for a local run.
* The returned report is the runner's own single-run report with
  ``environment = {"backend": "batch", "remote": {..., "job_id": ...}}``.
* **No fallback, ever.** A failed upload, launch, poll or run raises
  :class:`~klayout_tools.place_and_route.PlaceAndRouteError` carrying the
  existing ``batch_*`` ``error.code``; OpenROAD is never launched here.

Transport, packaging, the job spec, the preflight and the failure translation
are :mod:`klayout_tools.place_and_route_batch`'s, called as they are.
"""

from __future__ import annotations

import contextlib
import copy
import os
import tempfile
import time
from collections.abc import Callable
from typing import Any

from . import place_and_route_batch as _par_batch
from . import remote_transport, sim_batch
from .place_and_route import PlaceAndRouteError


def _single_unit_request(request: dict[str, Any], corner: str | None) -> dict[str, Any]:
    """``request`` as the one job's request: ``backend`` dropped, and a
    one-element ``pdk.rebuild_corners`` replaced by ``pdk.corner``."""
    if corner is not None and "rebuild_corners" in request["pdk"]:
        return _par_batch.unit_request(request, corner)
    unit = copy.deepcopy(request)
    unit.pop("backend", None)
    return unit


def _check_deck(
    report: dict[str, Any],
    job_id: str,
    cell_library: str,
    corner: str | None,
    pdk_variant: str | None,
) -> None:
    """Fail the job when this host resolves a different liberty deck than the
    runner used (a fleet image whose PDK differs from this host's)."""
    runner_deck = _par_batch._runner_deck_hash(report)
    local_deck = _par_batch._local_deck_hash(cell_library, corner, pdk_variant)
    if local_deck is not None and local_deck != runner_deck:
        raise _par_batch._UnitFailed(
            f"batch job {job_id}: the runner's liberty deck ({runner_deck}) "
            f"differs from this host's ({local_deck}); the fleet image's PDK "
            f"does not match {_par_batch._NO_FALLBACK}",
            code=_par_batch.BATCH_DECK_MISMATCH_CODE,
            job_id=job_id,
        )


def run_single_batch(
    request_path: str,
    request: dict[str, Any],
    corner: str | None,
    *,
    pdk_variant: str | None,
    runner: remote_transport.CommandRunner | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run the whole flow of ``request`` as one fleet job and return the
    runner's single-run report.

    ``corner`` is the one ``pdk.rebuild_corners`` name when the request has a
    one-element list, else ``None`` (the request's own ``pdk.corner`` or the
    library default applies). Request-level problems raise
    :class:`PlaceAndRouteError` before any upload; so does every failure
    after it.
    """
    request_dir = os.path.dirname(os.path.abspath(request_path))
    _, cell_library, files, timeout_s, config = _par_batch._preflight(
        request, request_dir, pdk_variant
    )
    unit_dir = os.path.join(request_dir, ".klt", "place-and-route")
    job_id = _par_batch.new_job_id()
    with tempfile.TemporaryDirectory(prefix="klt-par-batch-") as scratch:
        tarball = os.path.join(scratch, _par_batch.BATCH_TREE_FILENAME)
        _par_batch._build_tarball(files, tarball)
        spec = _par_batch.build_job_spec(
            request_name=os.path.basename(request_path),
            unit=_single_unit_request(request, corner),
            tarball_path=tarball,
            pdk_variant=pdk_variant,
            timeout_s=timeout_s,
            version_check=config.runner_version_check,
        )
        try:
            try:
                sim_batch.submit_job(config, job_id, spec, runner=runner)
                sim_batch.launch_job(config, job_id, runner=runner, sleep=sleep)
            except sim_batch.BatchError as exc:
                raise _par_batch._transport_failure(job_id, exc) from exc
            report, remote = _par_batch._finish_unit(
                config,
                job_id,
                os.path.join(scratch, "collected"),
                unit_dir,
                runner=runner,
                sleep=sleep,
            )
            _check_deck(
                report,
                job_id,
                cell_library,
                corner or request["pdk"].get("corner"),
                pdk_variant,
            )
        except _par_batch._UnitFailed as exc:
            raise PlaceAndRouteError(
                str(exc), code=exc.code or _par_batch.BATCH_JOB_FAILED_CODE
            ) from exc
    report["environment"] = {"backend": "batch", "remote": remote}
    return report


def run_batch(
    request_path: str,
    request: dict[str, Any],
    rebuild_corners: list[str] | None,
    *,
    resume: bool,
    pdk_variant: str | None,
    ownership: contextlib.ExitStack,
) -> dict[str, Any]:
    """The ``batch`` branch of ``run_place_and_route``: refuse what the fleet
    cannot honor, then dispatch one job per ``pdk.rebuild_corners`` unit, or
    the whole flow as one job when there is at most one unit."""
    if "dir" in request.get("outputs", {}):
        raise PlaceAndRouteError(
            "request.outputs.dir is not supported with the batch backend "
            "(the fleet copies artifacts back into .klt/place-and-route/)"
        )
    if resume:
        raise PlaceAndRouteError(
            "--resume is not supported with the batch backend (the fleet job "
            "runs the whole flow and copies its artifacts back)"
        )
    if rebuild_corners is not None and len(rebuild_corners) > 1:
        return _par_batch.run_rebuild_corners_batch(
            request_path,
            request,
            rebuild_corners,
            pdk_variant=pdk_variant,
            ownership=ownership,
        )
    return run_single_batch(
        request_path,
        request,
        rebuild_corners[0] if rebuild_corners else None,
        pdk_variant=pdk_variant,
    )


__all__ = ["run_batch", "run_single_batch"]
