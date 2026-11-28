"""Settling ``options.osdi_preload`` against the backend that actually runs.

The backend an OSDI list is settled against and the backend that executes
the corners must not be able to disagree. Two steps keep them in step:
:func:`finalize_osdi_preload` settles the list once, as soon as the backend
is first known (issue #3819), and :func:`resettle_osdi_preload_for_local`
re-settles it when a *late* host-default yield steps an off-host default
back to ``local`` (issue #5492). :func:`osdi_runner_environment_fields` is
how a report names the entries the executing host resolved for itself --
each with its declared index and an explicit ``sha256`` slot the ``batch``
backend fills from the runner's own report (issue #5562).

Both halves of that one invariant live here, next to each other, rather than
a thousand lines apart inside ``sim``. ``sim`` owns the backend vocabulary
(``OFFHOST_BACKENDS``) and passes the classification in as ``offhost``, the
same way it hands :func:`sim_op_check.require_echo` the tuple.
"""

from __future__ import annotations

from typing import Any

from .sim_osdi_pin import resolve_osdi_preload, resolve_request_osdi


def _settle_on_host(
    options: dict[str, Any],
    request_dir: str,
    pdk_root: str | None,
    pdk_variant: str | None,
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """Settle for a run that executes here: every entry is expanded and
    existence-checked now (see :func:`sim_osdi_pin.resolve_osdi_preload`) and
    nothing is deferred to a runner, so no entry can reach a deck
    unresolved."""
    return resolve_osdi_preload(
        options, request_dir, pdk_root=pdk_root, pdk_variant=pdk_variant
    ), []


def finalize_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    offhost: bool,
    osdi_preload: tuple[str, ...],
    osdi_runner_resolved: dict[int, str],
    pdk_root: str | None = None,
    pdk_variant: str | None = None,
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """Settle ``osdi_preload`` once the backend is known (issue #3819).

    Off-host, the runner-resolved entries are reported by name in
    ``environment.osdi_preload_runner_resolved`` and the already-resolved
    host-local ones stand. A run that stays on this host reads every entry
    here, so the full existence check and expansion apply, exactly as before.

    Each off-host entry carries its ``index`` in the request's *declared*
    ``options.osdi_preload`` (issue #5562). That index is the only sound join
    key for the digest the runner's own report computed
    (:func:`klayout_tools.sim_batch_features.attach_runner_osdi_digests`): a
    declared list may name the same entry twice, which makes a match on the
    entry string ambiguous where the index never is. It is reported, not
    merely passed, so a consumer can relate an entry back to
    ``options.osdi_preload[index]`` for itself.
    """
    if not offhost:
        return _settle_on_host(options, request_dir, pdk_root, pdk_variant)
    return osdi_preload, [
        {
            "entry": options["osdi_preload"][index],
            "index": index,
            "reason": reason,
        }
        for index, reason in sorted(osdi_runner_resolved.items())
    ]


def resettle_osdi_preload_for_local(
    request: dict[str, Any],
    request_dir: str,
    backend: str,
    engine: str,
    engine_binary: str,
) -> tuple[tuple[str, ...], list[dict[str, Any]]]:
    """Resolve the whole list after an off-host default yields to local execution.

    Both staged files and runner-resolved references now execute on this host.
    Check their existence, declared order, and compatibility with local ngspice
    before dispatching any corner.
    """
    osdi_preload = resolve_request_osdi(request, request_dir)
    # Deferred import: ``sim`` imports this module at import time, and the
    # ABI pre-check lives there because it needs ``sim``'s engine-version
    # probe. Reaching it through the module object also keeps it patchable
    # as ``sim._check_local_osdi_abi``, which is how it is tested.
    from . import sim

    sim._check_local_osdi_abi(osdi_preload, backend, engine, engine_binary)
    return osdi_preload, []


def osdi_runner_environment_fields(
    osdi_runner_environment: list[dict[str, Any]],
) -> dict[str, Any]:
    """The ``environment`` fragment for runner-resolved ``.osdi`` entries.

    Every entry carries an explicit ``sha256`` (issue #5562), ``None`` here:
    the digest is the runner's to report, and on ``batch`` it is joined on
    afterwards from the runner's own inner report
    (:func:`klayout_tools.sim_batch_features.attach_runner_osdi_digests`).
    A *present* ``null`` is the point -- a missing field invites a consumer to
    substitute the submitting host's hash of a file the runner never read,
    which would describe the wrong bytes; ``null`` says "unknown" and cannot
    be mistaken for evidence. Entries are copied rather than annotated in
    place, so the settled list stays the record of what was settled.
    """
    if not osdi_runner_environment:
        return {}
    return {
        "osdi_preload_runner_resolved": [
            {**entry, "sha256": None} for entry in osdi_runner_environment
        ]
    }
