"""Settling the ``engine_version`` and ``models.pdk_version`` pins against
the backend that actually runs.

Both pins are backend-dependent: ``engine_version`` is probed here for a
local backend and read off the runner's report for an off-host one
(:func:`check_local_engine_version_pin` /
:func:`check_offhost_engine_version_pin`, issue #3041), and
``models.pdk_version`` can only be compared where the PDK resolves, so an
off-host backend refuses it outright (:func:`check_pdk_version_pin`, issue
#4046, with the worker-side comparison tracked in #3462).

That makes *which* backend they are evaluated against part of their meaning,
and ``sim`` resolves the backend in stages: two host-default yields run
*after* the pre-checks' natural place (the resume yield in
``sim._prepare_sim_request`` and the single-unit yield in
``sim._run_sim_corners``, where the unit count is first known). Evaluated
against the pre-yield backend, as they were before issue #5500, an
env-sourced off-host default that steps back to ``local`` left
``engine_version`` checked by *neither* half -- a declared pin silently
unenforced -- and turned a valid single-corner ``models.pdk_version``
request into the ``#3462`` refusal, which is exactly the "a host default
must not turn a valid request into a refusal" rule the yields exist to
honour.

:func:`check_pdk_version_pin_early` and :func:`settle_version_pins` are the
two halves of the fix, and they live here together, next to the checks they
schedule, rather than a few hundred lines apart inside ``sim``. This is the
same split :mod:`klayout_tools.sim_osdi_settle` makes for
``options.osdi_preload`` (issue #5492), whose ``resettle_osdi_preload_for_local``
is called from the same two yield sites.

``sim`` keeps the backend vocabulary (``_OFFHOST_BACKENDS``), the shared
version comparison and the local engine probe, so those are reached through
a deferred ``from . import sim`` -- ``sim`` imports this module at import
time, and the attribute access also keeps ``sim._probe_engine_version``
patchable, which is how it is tested.
"""

from __future__ import annotations

from typing import Any

from .pdk import PdkNotFoundError, find_pdk
from .sim_errors import SimError


def check_local_engine_version_pin(
    pin: tuple[int, ...] | None, backend: str, engine: str, binary: str
) -> None:
    """Enforce the pin before any corner runs, where this process can see the
    engine (local backends); a no-op when unset or for off-host backends."""
    from . import sim

    if pin is None or backend in sim._OFFHOST_BACKENDS:
        return
    sim._check_engine_version_pin(
        pin,
        sim._probe_engine_version(engine, binary),
        engine,
        where="the local engine probe",
    )


def check_offhost_engine_version_pin(
    pin: tuple[int, ...] | None,
    backend: str,
    engine: str,
    engine_version: str | None,
) -> None:
    """Enforce the pin for off-host backends. No pre-submit probe exists for a
    remote/batch runner, so it is checked against the version its reports
    carried (after the run)."""
    from . import sim

    if pin is None or backend not in sim._OFFHOST_BACKENDS:
        return
    sim._check_engine_version_pin(
        pin, engine_version, engine, where=f"the {backend} runner's report"
    )


def validate_pdk_version_pin(models: Any) -> str | None:
    """The request's ``models.pdk_version`` pin (issue #4046), ``None`` when
    unset. Same value forms as ``klt pdk check --expect-version``."""
    if not isinstance(models, dict):
        return None
    pin = models.get("pdk_version")
    if pin is None:
        return None
    if not isinstance(pin, str) or not pin.strip():
        raise SimError(
            f"request.models.pdk_version must be a non-empty string (got {pin!r})"
        )
    return pin.strip()


def check_pdk_version_pin(pin: str | None, models: Any, backend: str) -> None:
    """Enforce ``models.pdk_version`` before any corner runs (issue #4046).

    Local backends resolve the PDK the way ``provenance.pdk`` does and fail
    closed (``pdk_version_unmet``) on ``mismatch``/``unknown`` or when no PDK
    resolves. Off-host backends reject the field: the worker-side comparison
    is tracked in #3462, and silently passing would hide drift."""
    from . import sim

    if pin is None:
        return
    if backend in sim._OFFHOST_BACKENDS:
        raise SimError(
            f"models.pdk_version is not supported for backend {backend!r}: "
            "the PDK is resolved on the runner, and the worker-side pin check "
            "is not implemented yet (see issue #3462) -- use backend 'local' "
            "or 'local-parallel', or drop the pin"
        )
    variant = models.get("pdk")
    root = models.get("pdk_root")
    if not (variant or root):
        raise SimError(
            f"models.pdk_version pins {pin!r} but no PDK is named: set "
            "models.pdk and/or models.pdk_root so the installed revision can "
            "be checked",
            code=sim.PDK_VERSION_UNMET_CODE,
        )
    try:
        found = find_pdk(variant=variant, root=root, expect_version=pin)
    except PdkNotFoundError as exc:
        raise SimError(
            f"models.pdk_version pins {pin!r} but the PDK could not be resolved: {exc}",
            code=sim.PDK_VERSION_UNMET_CODE,
        ) from exc
    expectation = found.get("version_expectation") or {}
    status = expectation.get("status")
    if status == "match":
        return
    installed = expectation.get("installed")
    if status == "mismatch":
        detail = f"installed PDK is {installed!r}"
    elif installed:
        detail = f"installed PDK {installed!r} could not be compared"
    else:
        detail = "installed PDK identity could not be determined"
    raise SimError(
        f"pinned PDK unavailable: request requires {pin!r}; {detail}",
        code=sim.PDK_VERSION_UNMET_CODE,
    )


def check_pdk_version_pin_early(
    models: Any, backend: str, backend_from_host_default: bool
) -> None:
    """Run the ``models.pdk_version`` pre-check at its natural, early place --
    unless the backend is an off-host *host default* a later yield may still
    step back to ``local`` (issue #5500).

    The pin's value is always validated here, so a malformed
    ``models.pdk_version`` is still refused as early as it ever was. What is
    deferred is only the comparison, and only for the one backend state whose
    answer is not final yet: refusing a run off-host (the ``#3462`` message)
    while a yield could still bring it on-host is how a host default turned a
    valid single-corner request into a refusal. :func:`settle_version_pins`
    picks it up after the last yield, where ``batch`` means ``batch``.

    An explicitly named backend -- ``--backend``, ``request.backend``, or the
    ``--batch-set`` preflight's ``backend="batch"`` -- is never a host
    default, so it is checked here and fails exactly as early as before.
    """
    from . import sim

    pin = validate_pdk_version_pin(models)
    if backend_from_host_default and backend in sim._OFFHOST_BACKENDS:
        return
    check_pdk_version_pin(pin, models, backend)


def settle_version_pins(
    request: dict[str, Any],
    backend: str,
    engine: str,
    engine_binary: str,
    engine_version_pin: tuple[int, ...] | None,
    *,
    settle: bool,
) -> None:
    """Evaluate both pins against ``backend`` at a *late* host-default yield
    site (issue #5500); a no-op unless ``settle``.

    Callers pass ``settle=True`` for exactly the two states the early
    pre-checks could not answer:

    * the yield just moved an off-host run on-host (``yielded_on_host``), so
      ``engine_version`` must be probed here -- otherwise neither
      :func:`check_local_engine_version_pin` (skipped, the backend looked
      off-host) nor :func:`check_offhost_engine_version_pin` (skipped, the
      backend is now local) ever enforces it -- and ``models.pdk_version``
      must be compared against the PDK this host resolves;
    * the *last* yield did not fire and the run really is leaving this host
      under a host default, so the deferred ``models.pdk_version`` refusal
      (``#3462``) is due now, before anything is submitted.

    ``engine_version`` is checked through the same local helper the early
    site uses, so it stays a no-op when the run stays off-host and the
    runner's report is the thing checked instead. Both are evaluated at most
    once per request: the early site skips what it defers, and only one of
    the two yields can move a run on-host.
    """
    if not settle:
        return
    check_local_engine_version_pin(engine_version_pin, backend, engine, engine_binary)
    models = request.get("models") or {}
    check_pdk_version_pin(validate_pdk_version_pin(models), models, backend)
