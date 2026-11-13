"""Settling ``options.osdi_preload`` against the backend that actually runs.

The backend an OSDI list is settled against and the backend that executes
the corners must not be able to disagree. Two steps keep them in step:
:func:`finalize_osdi_preload` settles the list once, as soon as the backend
is first known (issue #3819), and :func:`resettle_osdi_preload_for_local`
re-settles it when a *late* host-default yield steps an off-host default
back to ``local`` (issue #5492). :func:`osdi_runner_environment_fields` is
how a report names the entries the executing host resolved for itself.

Both halves of that one invariant live here, next to each other, rather than
a thousand lines apart inside ``sim``. ``sim`` owns the backend vocabulary
(``_OFFHOST_BACKENDS``) and passes the classification in as ``offhost``, the
same way it hands :func:`sim_op_check.require_echo` the tuple.
"""

from __future__ import annotations

from typing import Any

from .sim_osdi_pin import osdi_pdk_root_pin, resolve_osdi_preload


def _settle_on_host(
    options: dict[str, Any], request_dir: str, pdk_root: str | None
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Settle for a run that executes here: every entry is expanded and
    existence-checked now (see :func:`sim_osdi_pin.resolve_osdi_preload`) and
    nothing is deferred to a runner, so no entry can reach a deck
    unresolved."""
    return resolve_osdi_preload(options, request_dir, pdk_root=pdk_root), []


def finalize_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    offhost: bool,
    osdi_preload: tuple[str, ...],
    osdi_runner_resolved: dict[int, str],
    pdk_root: str | None = None,
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Settle ``osdi_preload`` once the backend is known (issue #3819).

    Off-host, the runner-resolved entries are reported by name in
    ``environment.osdi_preload_runner_resolved`` and the already-resolved
    host-local ones stand. A run that stays on this host reads every entry
    here, so the full existence check and expansion apply, exactly as before.
    """
    if not offhost:
        return _settle_on_host(options, request_dir, pdk_root)
    return osdi_preload, [
        {"entry": options["osdi_preload"][index], "reason": reason}
        for index, reason in sorted(osdi_runner_resolved.items())
    ]


def resettle_osdi_preload_for_local(
    request: dict[str, Any],
    request_dir: str,
    backend: str,
    engine: str,
    engine_binary: str,
) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    """Re-settle the OSDI list after a *late* host-default yield landed the
    run on this host (issue #5492).

    :func:`finalize_osdi_preload` answers once, early, against the backend as
    it stands at that point. Two yields run after it -- the resume yield in
    ``sim._prepare_sim_request`` and the single-unit yield in
    ``sim._run_sim_corners`` (the unit count is only known after shard/resume
    scoping) -- and either can step an env-sourced off-host default back to
    ``local``. Without this, the deck that actually runs here carries no
    ``pre_osdi`` line for the runner-resolved entries (issue #3819): the
    report says ``backend.resolved: "local"`` while still listing them under
    ``environment.osdi_preload_runner_resolved``, and the only symptom is an
    ngspice ``Unknown model type`` error deep in a per-corner log.

    The caller applies this exactly when the yield moved an off-host run
    on-host **and** the request deferred at least one entry to the runner (a
    non-empty ``osdi_runner_environment``, which is the off-host settling's
    own record of that);
    the on-host settling then applies in full, so a missing image-baked
    ``.osdi`` raises the same ``SimError`` a ``--backend local`` run raises,
    before any corner is dispatched. The OSDI-ABI pre-check (issue #4438) is
    re-applied over the re-settled entries for the same reason: it was a
    no-op while the backend still looked off-host.
    """
    osdi_preload, osdi_runner_environment = _settle_on_host(
        request.get("options") or {}, request_dir, osdi_pdk_root_pin(request)
    )
    # Deferred import: ``sim`` imports this module at import time, and the
    # ABI pre-check lives there because it needs ``sim``'s engine-version
    # probe. Reaching it through the module object also keeps it patchable
    # as ``sim._check_local_osdi_abi``, which is how it is tested.
    from . import sim

    sim._check_local_osdi_abi(osdi_preload, backend, engine, engine_binary)
    return osdi_preload, osdi_runner_environment


def osdi_runner_environment_fields(
    osdi_runner_environment: list[dict[str, str]],
) -> dict[str, Any]:
    """The ``environment`` fragment for runner-resolved ``.osdi`` entries."""
    if not osdi_runner_environment:
        return {}
    return {"osdi_preload_runner_resolved": osdi_runner_environment}
