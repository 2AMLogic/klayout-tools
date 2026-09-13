"""Fleet shard/merge engine and remote (EC2) dispatch subsystem for ``klt sim``.

Split out of ``sim.py`` (issue #1764) as a self-contained subsystem: the
pure-logic fleet shard/merge engine (Epic #375 Phase 1A, #376 --
:data:`ShardRunner`, :func:`_shard_corner_points`,
:func:`_unrun_corner_report`, :func:`_lost_shard_corner`,
:func:`_run_sharded`, :func:`_run_remote`) and remote fleet dispatch (Epic
#375 Phase 1B, #377; wired into `klt sim` by issue #906, Phase 2 of the
statistical/yield epic #710 -- :func:`_run_remote_dispatch`,
:func:`_build_remote_request`, :func:`_run_remote_fleet`,
:func:`_build_remote_job_description`, :func:`_default_remote_run_timeout_s`,
:func:`_rewrite_remote_artifact_paths`). This mirrors the shape of the
earlier ``lvs.py``/``lvs_mismatch.py`` (#1721),
``gen_compose.py``/``gen_compose_routing.py`` (#1708), and
``gen.py``/``gen_pcells`` (#1698/#1633) splits: a cohesive, low-coupling
subsystem relocated verbatim out of a file that had grown past the point one
module should hold request/response orchestration *and* fleet
scheduling/remote dispatch.

``run_sim`` (top-level request orchestration) and the ``local``/
``local-parallel`` backends are *not* part of this split -- they stay in
``sim.py``, calling into this module's entry points (:func:`_run_sharded`,
:func:`_run_remote`, :func:`_run_remote_fleet`) exactly as they called the
same functions when this was one file. The ``_BACKENDS`` dispatch registry
also stays in ``sim.py`` since it maps ``"local"``/``"local-parallel"`` to
functions that stay there too -- only ``"remote"`` resolves to a name
re-exported from this module.

Dependency surface is intentionally narrow: this module calls out to a
handful of names defined in ``sim.py`` (``SimError``,
``_corner_points_to_wire``, ``_shard_corner_points``, ``RemoteLauncher``),
imported *inside* the handful of functions that use them (the same
deferred-import discipline ``lvs_mismatch.py``/``gen_compose_routing.py``
use to depend on their own parent module) rather than at module scope, so
this module never has a load-time dependency back on ``sim.py`` -- only
``sim.py`` depends on this module at import time. ``_shard_corner_points``
and ``RemoteLauncher`` are *also* looked up this way even though this module
defines/imports them itself (the same "own same-name function" case
``gen_compose_routing.py``'s docstring calls out for
``_endpoint_stub_widen_um``) -- the test suite monkeypatches both by their
``klayout_tools.sim`` re-export (predating this split), and only a call
routed back through ``sim`` at call time still observes that patch.
``CornerPoint``, ``_Checkpoint``, and ``RemoteLauncher`` (the last also
defined in ``sim.py``, re-exported by it) appear only in type annotations
here -- ``from __future__ import annotations`` defers them to strings, so
none needs a *runtime* import, but they are still imported under
``TYPE_CHECKING`` below so static analysis (ruff's F821, mypy) can resolve
them without creating a load-time cycle. ``sim.py`` in turn imports this
module's entry points back (module scope, no cycle -- this module never
imports ``sim`` at its own module scope) to preserve
``klayout_tools.sim.<name>`` as a working import path for every name the
test suite/callers used before this split.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from . import remote_fleet, remote_transport
from .remote_launcher import RemoteLaunchError

if TYPE_CHECKING:
    from .sim import CornerPoint, RemoteLauncher, _Checkpoint

# --------------------------------------------------------------------------- #
# Fleet shard/merge engine (Epic #375 Phase 1A, #376) -- pure logic, no AWS
# --------------------------------------------------------------------------- #

#: The callable a shard's slice of the expanded unit list is handed to,
#: returning that shard's own ``(corners, engine_version,
#: remote_environment)`` triple -- exactly the shape a :data:`_BACKENDS`
#: entry returns for the *whole* matrix, just scoped to one shard. This is
#: the seam Epic #375 Phase 1B (#377) plugs a real fleet member into (a
#: callable that provisions one host per shard and runs it there); this
#: module's own caller (:func:`run_sim`) passes one that simply re-invokes
#: the already-selected backend on the shard, which is exactly correct for
#: `local`/`local-parallel` (they consume ``corner_points`` directly, with
#: no separate "request document" to slice) and is why `hosts > 1` already
#: works end to end for those two backends without any new AWS-facing code.
ShardRunner = Callable[
    [list["CornerPoint"]],
    tuple[list[dict[str, Any]], str | None, dict[str, Any] | None],
]


def _shard_corner_points(
    corner_points: list[CornerPoint], hosts: int
) -> list[list[CornerPoint]]:
    """Slice the expanded unit list into ``hosts`` contiguous shards.

    Shards are balanced as evenly as possible -- ``len(corner_points) //
    hosts`` units each, with the first ``len(corner_points) % hosts`` shards
    getting one extra -- and are always **contiguous slices** of the
    original list in its original order, so concatenating every shard back
    together reproduces ``corner_points`` exactly for any ``hosts`` from
    ``1`` to ``len(corner_points)`` (and beyond -- see below). Per-sample
    Monte Carlo seeds are already absolute (``sample_index``-derived, see
    :func:`_expand_monte_carlo` and #348), so slicing never changes any
    point's own value, only which shard runs it.

    ``hosts`` exceeding ``len(corner_points)`` is not an error -- the
    trailing shards are simply empty lists; :func:`_run_sharded` never calls
    the shard runner for an empty shard.
    """
    from .sim import SimError

    if hosts < 1:
        raise SimError("remote.hosts must be a positive integer")
    total = len(corner_points)
    base, remainder = divmod(total, hosts)
    shards: list[list[CornerPoint]] = []
    start = 0
    for index in range(hosts):
        size = base + (1 if index < remainder else 0)
        shards.append(corner_points[start : start + size])
        start += size
    return shards


#: Human-readable message for each :func:`_unrun_corner_report` diagnostic
#: code -- shared between the ``local``/``local-parallel`` dispatch loops
#: (issue #473) so both backends report a skipped corner identically.
_UNRUN_MESSAGES: dict[str, str] = {
    "budget_exceeded": (
        "sweep options.wall_clock_budget_s was exceeded before this corner "
        "started -- see environment.budget"
    ),
    "orphaned": (
        "the launching process exited before this corner started; the "
        "sweep stopped rather than continue as an orphan -- see "
        "environment.orphaned"
    ),
}


def _unrun_corner_report(
    point: CornerPoint, measurements_spec: list[dict[str, Any]], code: str
) -> dict[str, Any]:
    """Synthesize an ``error`` corner report for a unit that never got the
    chance to run: the sweep's own wall-clock budget was exceeded before its
    turn (``code="budget_exceeded"``), the launching process died first
    (``code="orphaned"``), or its shard never returned at all
    (``code="lost_shard"``, via :func:`_lost_shard_corner`).

    Mirrors :func:`_run_corner`'s own ``error``-status shape field-for-field,
    so an unrun unit is indistinguishable, downstream, from a corner that
    ran and errored on its own -- ``_rollup_measurements``, the CLI's
    text/JSON renderers, and the ``errored`` count all need no special case
    for any of these codes.
    """
    message = _UNRUN_MESSAGES.get(code, code)
    measurements = [
        {
            "name": spec["name"],
            "value": None,
            "unit": spec.get("unit"),
            "status": "error",
            "margin": None,
        }
        for spec in measurements_spec
    ]
    monte_carlo: dict[str, Any] | None = None
    if point.sample_index is not None:
        assert point.mc_seed is not None  # every sampled point carries one
        monte_carlo = {
            "sample_index": point.sample_index,
            "seed": point.mc_seed["rndseed"],
            "process_seed": point.mc_seed["process_seed"],
            "mismatch_seed": point.mc_seed["mismatch_seed"],
        }
    return {
        "corner_id": point.corner_id,
        "process": point.process,
        "supply_v": point.supply_v,
        "temperature_c": point.temperature_c,
        "status": "error",
        "runtime_s": 0.0,
        "measurements": measurements,
        "diagnostics": [{"severity": "error", "code": code, "message": message}],
        "artifacts": {"log": None, "raw": None, "waveform": None, "deck": None},
        "monte_carlo": monte_carlo,
    }


def _lost_shard_corner(
    point: CornerPoint, measurements_spec: list[dict[str, Any]], reason: str
) -> dict[str, Any]:
    """Synthesize an ``error`` corner report for one unit of a shard that
    never returned (Epic #375 decision 2's *merge* half -- the automatic
    retry that avoids this in the common case is Phase 1B, #377).

    A thin wrapper over :func:`_unrun_corner_report` that keeps its own
    ``lost_shard`` diagnostic message (which, unlike the other codes there,
    carries the caller-supplied ``reason`` -- the shard's own exception
    text -- rather than a fixed message).
    """
    report = _unrun_corner_report(point, measurements_spec, "lost_shard")
    report["diagnostics"] = [
        {"severity": "error", "code": "lost_shard", "message": reason}
    ]
    return report


def _run_sharded(
    *,
    shard_runner: ShardRunner,
    corner_points: list[CornerPoint],
    hosts: int,
    measurements_spec: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The fleet shard/merge engine: slice ``corner_points`` into ``hosts``
    contiguous shards (:func:`_shard_corner_points`), run each shard through
    ``shard_runner`` -- concurrently, one shard meant to be one host -- and
    deterministically merge the per-shard reports back into **global unit
    order**, independent of which shard finishes first. This is the same
    ordering contract ``local-parallel`` already honors for individual
    corners (:func:`_run_local_parallel`), one level up: shards are
    dispatched to a thread pool and reassembled by shard index once every
    future completes, never by completion order.

    A shard whose ``shard_runner`` call raises is a **lost shard**: every
    unit in that shard is reported with ``status: "error"`` and a
    ``lost_shard`` diagnostic carrying the exception text
    (:func:`_lost_shard_corner`), and the run continues -- a lost shard
    never aborts a sibling shard, mirroring `local-parallel`'s per-corner
    failure isolation one level up. An empty shard (``hosts`` exceeds the
    unit count) is never handed to ``shard_runner`` at all.

    ``engine_version`` is derived the same "last non-``None`` wins, in
    corner-list order" way every backend already computes it -- shards are
    visited in shard order (itself global-unit order, since shards are
    contiguous), so the result is deterministic given a deterministic unit
    list, independent of completion order.

    ``environment.remote`` becomes a ``fleet[]`` array -- one entry per
    host, ``None`` for a lost shard -- only when at least one shard produced
    a non-``None`` ``remote_environment``; otherwise it stays ``None`` (the
    pre-fleet single-block shape), so a fleet of `local`/`local-parallel`
    shards (which never populate ``remote_environment``) reports no
    ``environment.remote`` at all -- exactly like an unsharded `local`/
    `local-parallel` run today.
    """
    from .sim import _shard_corner_points

    shards = _shard_corner_points(corner_points, hosts)

    def _run_one(
        shard_points: list[CornerPoint],
    ) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
        if not shard_points:
            return [], None, None
        return shard_runner(shard_points)

    results: list[
        tuple[list[dict[str, Any]], str | None, dict[str, Any] | None] | None
    ] = [None] * len(shards)
    lost_reasons: list[str | None] = [None] * len(shards)

    with ThreadPoolExecutor(max_workers=max(1, hosts)) as pool:
        future_to_index = {
            pool.submit(_run_one, shard): index for index, shard in enumerate(shards)
        }
        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 - "shard never returned"
                lost_reasons[index] = str(exc)

    corners: list[dict[str, Any]] = []
    engine_version: str | None = None
    fleet: list[dict[str, Any] | None] = []
    any_remote = False
    for shard_points, result, lost_reason in zip(
        shards, results, lost_reasons, strict=True
    ):
        if lost_reason is not None:
            corners.extend(
                _lost_shard_corner(
                    point, measurements_spec, f"shard lost: {lost_reason}"
                )
                for point in shard_points
            )
            fleet.append(None)
            continue
        assert result is not None  # every non-lost shard was submitted+awaited
        shard_corners, shard_engine_version, shard_remote_environment = result
        corners.extend(shard_corners)
        if shard_engine_version is not None:
            engine_version = shard_engine_version
        fleet.append(shard_remote_environment)
        if shard_remote_environment is not None:
            any_remote = True

    remote_environment = {"fleet": fleet} if any_remote else None
    return corners, engine_version, remote_environment


#: Default idle-poll interval for :func:`remote_transport.wait_for_ssh` when
#: called from :func:`_run_remote` -- kept short so tests exercising a
#: timeout don't stall, while still reasonable against real EC2 boot times.
_REMOTE_SSH_POLL_INTERVAL_S = 5.0

#: Default overall SSH-readiness budget. Originally 240s per the design
#: note's documented 1-3 minute spin-up estimate plus slack (decision 3);
#: raised to 600s after the first live run observed cold boots (AMI
#: first-boot cloud-init, not just instance ``running``) taking longer than
#: that budget on some instance types/regions -- see
#: docs/design/remote-sim-backend-spike.md. Still overridable per-request via
#: ``remote.ssh_ready_timeout_s``.
_REMOTE_SSH_READY_TIMEOUT_S = 600.0


def _run_remote(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    models_lib: str | None,
    analysis: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    max_workers: int | None,
    request: dict[str, Any],
    deadline: float | None = None,
    initial_ppid: int | None = None,
    checkpoint: _Checkpoint | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``remote`` backend: provision one EC2 instance sized for the whole
    corner matrix, push the netlist + a request-specific copy of ``request``
    to it over SSH/SCP, and run the *same* ``local-parallel`` worker-pool
    code (unmodified, on the provisioned box) via ``klt sim ... --backend
    local-parallel`` -- not a reimplementation of corner expansion, ordering,
    measurement extraction, or pass/fail classification (see
    ``docs/design/remote-sim-backend-spike.md`` decisions 2 and 5, and
    :mod:`klayout_tools.remote_transport`'s :func:`remote_transport.run_remote_job`).
    The generic push/run/collect job description built here
    (:func:`_build_remote_job_description`) is `klt sim`'s own instance of
    the contract ``docs/design/remote-job-description.md`` documents (issue
    #278, Epic #253 Phase 3).

    ``analysis``/``measurements_spec``/``models_lib`` are accepted for
    signature parity with the other backends but unused directly here --
    they are already embedded in ``request`` (the remote request document is
    built from ``request`` itself, see :func:`_build_remote_request`), and
    ``models_lib`` is a *local* path resolution only used for this run's
    ``environment.models_lib``/``models_lib_sha256`` provenance (computed by
    ``run_sim`` regardless of backend); the remote host resolves its own
    baked model library from ``request.models`` and its own ``$PDK_ROOT``
    (see decision 4).

    ``max_workers`` (the caller's *local* worker-pool override) is ignored --
    the provisioned box was already sized to fit
    ``corner_count * threads_per_corner`` with headroom
    (``remote_launcher.select_instance_type``), so its own
    ``local-parallel`` default (``_default_max_workers``, derived from that
    box's own CPU count) is already right-sized without an override.

    Teardown is guaranteed on every exit path -- normal completion, any
    exception raised in this function's body (including a transport
    failure), or a caught SIGINT/SIGTERM -- by ``RemoteLauncher``'s own
    context-manager guarantee (guardrail mechanics SS3(a)). A provisioning or
    transport failure is re-raised as :class:`SimError`: no corner ever ran,
    the same "the sweep never started" class as an unresolvable netlist or
    model library.

    ``deadline``/``initial_ppid``/``checkpoint`` (issue #473) are accepted
    for signature parity with ``local``/``local-parallel`` but not yet
    honored here: the provisioned box's own dispatch loop runs its
    corners via a *nested* ``local-parallel`` invocation over SSH, outside
    this process, so this function has no dispatch loop of its own to gate.
    Wiring the remote backend into the same budget/orphan/resume machinery
    is tracked as follow-up work, not silently promised by this signature.
    """
    from .sim import RemoteLauncher, SimError

    del analysis, measurements_spec, models_lib, max_workers  # see docstring
    del deadline, initial_ppid, checkpoint  # not yet honored -- see docstring

    remote_spec = request.get("remote") or {}
    region = remote_spec.get("region")
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        raise SimError(
            "backend 'remote' requires request.models.pdk (selects which "
            "baked-AMI PDK to provision -- see "
            "remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
        )
    ssh_key_path = remote_spec.get("ssh_key_path")
    if not ssh_key_path:
        raise SimError(
            "backend 'remote' requires request.remote.ssh_key_path (local "
            "private key matching request.remote.key_name, used to SSH/SCP "
            "into the provisioned instance)"
        )
    ssh_user = remote_spec.get("ssh_user") or remote_transport.DEFAULT_SSH_USER

    job_id = f"klt-sim-{uuid.uuid4().hex[:12]}"
    launcher = RemoteLauncher(
        region=region,
        pdk=pdk,
        corner_count=len(corner_points),
        job_id=job_id,
        spot=remote_spec.get("spot", True),
        max_hourly_cost_usd=remote_spec.get("max_hourly_cost_usd"),
        launcher_cidr=remote_spec.get("launcher_cidr"),
        launcher_cidrs=remote_spec.get("launcher_cidrs"),
        security_group_id=remote_spec.get("security_group_id"),
        key_name=remote_spec.get("key_name"),
        subnet_id=remote_spec.get("subnet_id"),
        # request.remote.ami_manifest is the explicit-override tier of
        # remote_launcher's 4-step AMI manifest resolution order (issue
        # #370); when absent (None), RemoteLauncher/load_ami_manifest fall
        # through to $KLT_AMI_MANIFEST, then the user-scope manifest
        # (~/.config/klt/remote-sim-ami-manifest.json -- written by
        # scripts/aws/build-remote-sim-ami.sh alongside its repo-checkout
        # copy), then the packaged default -- see
        # remote_launcher._candidate_manifest_paths.
        manifest_path=remote_spec.get("ami_manifest"),
    )

    started = time.monotonic()
    info: dict[str, Any] | None = None
    corners: list[dict[str, Any]] | None = None
    engine_version: str | None = None
    try:
        with launcher:
            info = launcher.provision()
            public_ip = launcher.get_public_ip()
            remote_transport.wait_for_ssh(
                public_ip,
                user=ssh_user,
                identity_file=ssh_key_path,
                timeout_s=remote_spec.get(
                    "ssh_ready_timeout_s", _REMOTE_SSH_READY_TIMEOUT_S
                ),
                poll_interval_s=_REMOTE_SSH_POLL_INTERVAL_S,
            )
            spin_up_s = round(time.monotonic() - started, 3)

            corners, engine_version = _run_remote_dispatch(
                launcher=launcher,
                public_ip=public_ip,
                corner_points=corner_points,
                netlist_path=netlist_path,
                timeout_s=timeout_s,
                keep_artifacts=keep_artifacts,
                want_waveforms=want_waveforms,
                artifacts_dir=artifacts_dir,
                request=request,
                ssh_user=ssh_user,
                ssh_key_path=ssh_key_path,
                ssh_run_timeout_s=remote_spec.get(
                    "ssh_timeout_s",
                    _default_remote_run_timeout_s(len(corner_points), timeout_s),
                ),
            )
    except (RemoteLaunchError, remote_transport.RemoteTransportError) as exc:
        raise SimError(f"remote backend failed: {exc}") from exc

    assert info is not None and corners is not None  # provision()/run succeeded

    remote_environment = {
        "provider": info["provider"],
        "region": info["region"],
        "instance_type": info["instance_type"],
        "instance_id": info["instance_id"],
        "spot": info["spot"],
        "estimated_hourly_cost_usd": info["estimated_hourly_cost_usd"],
        "ami_id": info["ami_id"],
        "pdk_snapshot": info["pdk_snapshot"],
        "spin_up_s": spin_up_s,
    }
    return corners, engine_version, remote_environment


def _run_remote_dispatch(
    *,
    launcher: RemoteLauncher,
    public_ip: str,
    corner_points: list[CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    ssh_user: str,
    ssh_key_path: str,
    ssh_run_timeout_s: float,
    explicit_points: list[CornerPoint] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Push, run, and (when ``keep_artifacts``) pull one job's worth of
    corners against an already-provisioned, SSH-reachable remote host --
    the shared body of the single-host ``remote`` backend
    (:func:`_run_remote`, which provisions its own ``launcher`` first) and
    a real fleet's per-shard dispatch (:func:`_run_remote_fleet`, whose
    ``launcher``/``public_ip`` come from :func:`remote_fleet.run_fleet`'s
    already-provisioned fleet member).

    ``explicit_points`` -- when given -- is threaded through to
    :func:`_build_remote_request` so the pushed request carries exactly
    this call's own ``corner_points`` (with their already-derived Monte
    Carlo seeds) rather than re-expanding from the caller's ``corners``/
    ``monte_carlo`` request fields; see that parameter's own docstring.
    ``_run_remote`` never passes it (a single host always runs the whole
    matrix); ``_run_remote_fleet`` always does (one shard's own slice).
    """
    remote_job_dir = remote_transport.job_dir(ssh_user, launcher.job_id)
    remote_request = _build_remote_request(
        request,
        timeout_s=timeout_s,
        keep_artifacts=keep_artifacts,
        want_waveforms=want_waveforms,
        explicit_points=explicit_points,
    )
    job = _build_remote_job_description(remote_request, netlist_path)
    remote_transport.push_job(
        host=public_ip,
        user=ssh_user,
        identity_file=ssh_key_path,
        remote_job_dir=remote_job_dir,
        job=job,
    )
    remote_report = remote_transport.run_remote_job(
        host=public_ip,
        user=ssh_user,
        identity_file=ssh_key_path,
        remote_job_dir=remote_job_dir,
        job=job,
        timeout_s=ssh_run_timeout_s,
    )
    if keep_artifacts:
        remote_transport.pull_artifacts(
            host=public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            remote_job_dir=remote_job_dir,
            local_artifacts_dir=artifacts_dir,
            job=job,
        )
    remote_transport.cleanup_job(
        host=public_ip,
        user=ssh_user,
        identity_file=ssh_key_path,
        remote_job_dir=remote_job_dir,
    )

    corners = remote_report.get("corners", [])
    if keep_artifacts:
        _rewrite_remote_artifact_paths(
            corners,
            remote_root=remote_transport.artifacts_root(
                remote_job_dir, job.artifacts_relative_dir
            ),
            local_root=artifacts_dir,
        )
    engine_version = (remote_report.get("environment") or {}).get("engine_version")
    return corners, engine_version


def _build_remote_request(
    request: dict[str, Any],
    *,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    explicit_points: list[CornerPoint] | None = None,
) -> dict[str, Any]:
    """Build the request document pushed to the remote host: a copy of the
    caller's own request with ``backend`` forced to ``local-parallel`` (per
    decision 2 -- the box runs #255's worker pool directly, never ``remote``
    recursively) and ``netlist`` repointed at the pushed file's job-relative
    path.

    ``models`` is forwarded unchanged -- per decision 4, the remote host
    resolves its own baked model library the same way ``_resolve_models_lib``
    resolves a local one (``models.pdk`` plus the AMI's own ``$PDK_ROOT``, so
    long as ``models.pdk_root`` is not itself an operator-local absolute path
    that only exists on the caller's own machine -- see docs/cli/sim.md's
    "Remote backend" section for this constraint). ``options.max_workers`` is
    dropped so the remote run resolves its own default from that box's own
    CPU count (see ``_run_remote``'s docstring).

    ``explicit_points`` (issue #906's fleet-shard wiring) -- when given --
    replaces ``corners``/``monte_carlo``/``exclude`` with the internal
    ``_explicit_points`` wire field (:func:`_corner_points_to_wire`) instead
    of forwarding them verbatim, so the remote box's own ``run_sim`` runs
    *exactly* this caller's ``corner_points`` (see ``run_sim``'s handling of
    ``request["_explicit_points"]``) rather than re-expanding the whole
    matrix from ranges -- correct only when the whole matrix is meant to run
    on one host, which a fleet shard is not. ``None`` (the default,
    ``_run_remote``'s own case) forwards ``corners``/``monte_carlo``
    unchanged, exactly as before this parameter existed.
    """
    from .sim import _corner_points_to_wire

    remote_request = dict(request)
    remote_request.pop("remote", None)
    remote_request["backend"] = "local-parallel"
    remote_request["netlist"] = remote_transport.REMOTE_NETLIST_FILENAME

    if explicit_points is not None:
        remote_request.pop("corners", None)
        remote_request.pop("monte_carlo", None)
        remote_request.pop("exclude", None)
        remote_request["_explicit_points"] = _corner_points_to_wire(explicit_points)

    options = dict(request.get("options") or {})
    options["timeout_s"] = timeout_s
    options["keep_artifacts"] = keep_artifacts
    options["waveforms"] = want_waveforms
    options.pop("max_workers", None)
    remote_request["options"] = options
    return remote_request


# --------------------------------------------------------------------------- #
# Remote fleet dispatch (Epic #375 Phase 1B, #377; wired into `klt sim` by
# issue #906, Phase 2 of the statistical/yield epic #710)
# --------------------------------------------------------------------------- #


def _run_remote_fleet(
    *,
    corner_points: list[CornerPoint],
    netlist_path: str,
    timeout_s: float,
    keep_artifacts: bool,
    want_waveforms: bool,
    artifacts_dir: str,
    request: dict[str, Any],
    hosts: int,
    measurements_spec: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    """The ``remote`` backend's ``hosts > 1`` dispatch: provision ``hosts``
    real EC2 instances through :func:`remote_fleet.run_fleet` -- the K-host
    launch, fleet-level cost gate, vCPU quota pre-check, one-shard retry, and
    guaranteed teardown-all Epic #375 Phase 1B (#377) already ships and
    already tests, reused here unmodified rather than reimplemented. This is
    that lifecycle's first real caller (issue #906, Phase 2 of the
    statistical/yield epic #710 -- driving an MC campaign's corner x
    Monte-Carlo-sample grid across the fleet).

    Slices ``corner_points`` into ``hosts`` contiguous shards
    (:func:`_shard_corner_points`, #376) and gives each shard its own
    ``ShardRunner`` closure that -- once :class:`remote_fleet.FleetLauncher`
    hands it an already-provisioned, SSH-reachable instance -- pushes,
    runs, and (when ``keep_artifacts``) pulls exactly that shard's slice
    via :func:`_run_remote_dispatch`'s ``explicit_points`` path (never the
    whole matrix -- see that function's docstring on why a fleet shard
    cannot reuse the single-host ``remote`` backend's own re-expand-on-the-
    box behavior). A lost shard (both the initial attempt and its one
    automatic retry failed) is reported the same way :func:`_run_sharded`
    reports one for a local backend: every one of its units gets a
    ``status: "error"``/``lost_shard`` corner report
    (:func:`_lost_shard_corner`) rather than aborting the fleet.

    ``hosts`` exceeding ``len(corner_points)`` is rejected up front (unlike
    the local shard/merge engine, which just runs an empty shard's worth of
    nothing) -- an idle fleet member is still billed, so there is no benign
    reading of "more hosts than units" here.
    """
    from .sim import SimError, _shard_corner_points

    if hosts > len(corner_points):
        raise SimError(
            f"remote.hosts ({hosts}) exceeds the number of units to "
            f"dispatch ({len(corner_points)}) -- an idle fleet member would "
            "still be billed; use hosts <= corner/sample count"
        )

    remote_spec = request.get("remote") or {}
    region = remote_spec.get("region")
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        raise SimError(
            "backend 'remote' requires request.models.pdk (selects which "
            "baked-AMI PDK to provision -- see "
            "remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
        )
    if not region:
        raise SimError("backend 'remote' requires request.remote.region")
    ssh_key_path = remote_spec.get("ssh_key_path")
    if not ssh_key_path:
        raise SimError(
            "backend 'remote' requires request.remote.ssh_key_path (local "
            "private key matching request.remote.key_name, used to SSH/SCP "
            "into every provisioned instance)"
        )
    ssh_user = remote_spec.get("ssh_user") or remote_transport.DEFAULT_SSH_USER

    shards = _shard_corner_points(corner_points, hosts)
    job_id_prefix = f"klt-sim-fleet-{uuid.uuid4().hex[:8]}"

    def _shard_runner(
        shard_index: int, launcher: RemoteLauncher, public_ip: str
    ) -> tuple[list[dict[str, Any]], str | None]:
        shard_points = shards[shard_index]
        remote_transport.wait_for_ssh(
            public_ip,
            user=ssh_user,
            identity_file=ssh_key_path,
            timeout_s=remote_spec.get(
                "ssh_ready_timeout_s", _REMOTE_SSH_READY_TIMEOUT_S
            ),
            poll_interval_s=_REMOTE_SSH_POLL_INTERVAL_S,
        )
        shard_artifacts_dir = (
            os.path.join(artifacts_dir, f"shard{shard_index}")
            if keep_artifacts
            else artifacts_dir
        )
        return _run_remote_dispatch(
            launcher=launcher,
            public_ip=public_ip,
            corner_points=shard_points,
            netlist_path=netlist_path,
            timeout_s=timeout_s,
            keep_artifacts=keep_artifacts,
            want_waveforms=want_waveforms,
            artifacts_dir=shard_artifacts_dir,
            request=request,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            ssh_run_timeout_s=remote_spec.get(
                "ssh_timeout_s",
                _default_remote_run_timeout_s(len(shard_points), timeout_s),
            ),
            explicit_points=shard_points,
        )

    try:
        fleet_result = remote_fleet.run_fleet(
            region=region,
            pdk=pdk,
            shard_unit_counts=[len(shard) for shard in shards],
            shard_runner=_shard_runner,
            job_id_prefix=job_id_prefix,
            spot=remote_spec.get("spot", True),
            max_hourly_cost_usd=remote_spec.get("max_hourly_cost_usd"),
            launcher_cidr=remote_spec.get("launcher_cidr"),
            security_group_id=remote_spec.get("security_group_id"),
            key_name=remote_spec.get("key_name"),
            subnet_id=remote_spec.get("subnet_id"),
            manifest_path=remote_spec.get("ami_manifest"),
        )
    except (RemoteLaunchError, remote_transport.RemoteTransportError) as exc:
        raise SimError(f"remote fleet backend failed: {exc}") from exc

    corners: list[dict[str, Any]] = []
    engine_version: str | None = None
    fleet: list[dict[str, Any] | None] = []
    for shard_points, outcome in zip(shards, fleet_result.shards, strict=True):
        if outcome.status != "ok":
            corners.extend(
                _lost_shard_corner(
                    point, measurements_spec, f"shard lost: {outcome.error}"
                )
                for point in shard_points
            )
            fleet.append(None)
            continue
        shard_corners, shard_engine_version = outcome.result
        corners.extend(shard_corners)
        if shard_engine_version is not None:
            engine_version = shard_engine_version
        info = outcome.environment or {}
        fleet.append(
            {
                "provider": info.get("provider"),
                "region": info.get("region"),
                "instance_type": info.get("instance_type"),
                "instance_id": info.get("instance_id"),
                "spot": info.get("spot"),
                "estimated_hourly_cost_usd": info.get("estimated_hourly_cost_usd"),
                "ami_id": info.get("ami_id"),
                "pdk_snapshot": info.get("pdk_snapshot"),
                "attempts": outcome.attempts,
            }
        )

    return corners, engine_version, {"fleet": fleet}


#: `klt sim`'s own remote-command exit codes that still mean "the sweep ran
#: and produced a report": 0 (pass)/3 (measurement failure)/4 (corner error)
#: -- see ``docs/cli/sim.md``'s exit-code table and
#: ``remote_transport.run_remote_job``'s docstring.
_REMOTE_SIM_SUCCESS_EXIT_CODES: tuple[int, ...] = (0, 3, 4)


def _build_remote_job_description(
    remote_request: dict[str, Any], netlist_path: str
) -> remote_transport.JobDescription:
    """Build the `klt sim` corner-fan-out job as a generic
    :class:`remote_transport.JobDescription` (issue #278, Epic #253 Phase
    3): the netlist and the generated ``remote_request`` document are the
    pushed inputs, ``klt sim ... --backend local-parallel --format json`` is
    the remote command, and ``.klt/sim`` (``sim.run_sim``'s own
    ``keep_artifacts`` default, since the remote invocation is never given
    an explicit ``--outdir``) is the collected artifacts directory.

    This is the one and only place `klt sim`'s remote job shape is
    constructed -- :mod:`klayout_tools.remote_transport`'s push/run/collect
    functions accept it as data and hard-code none of it, so a future
    `extract`/`lvs`/DRC remote backend builds its own
    :class:`remote_transport.JobDescription` here instead (see
    ``docs/design/remote-job-description.md``).
    """
    return remote_transport.JobDescription(
        label="klt sim",
        inputs=(
            remote_transport.JobInput(
                remote_name=remote_transport.REMOTE_NETLIST_FILENAME,
                label="netlist",
                local_path=netlist_path,
            ),
            remote_transport.JobInput(
                remote_name=remote_transport.REMOTE_REQUEST_FILENAME,
                label="request",
                content=json.dumps(remote_request),
            ),
        ),
        command=(
            f"klt sim {remote_transport.REMOTE_REQUEST_FILENAME} "
            "--backend local-parallel --format json"
        ),
        success_exit_codes=_REMOTE_SIM_SUCCESS_EXIT_CODES,
        artifacts_relative_dir=remote_transport.DEFAULT_ARTIFACTS_RELATIVE_DIR,
    )


def _default_remote_run_timeout_s(
    corner_count: int, per_corner_timeout_s: float
) -> float:
    """Conservative SSH-command timeout for the remote ``klt sim`` invocation:
    the fully-serial worst case (every corner hits its own timeout, one
    after another) plus slack for SSH/``klt`` startup.

    The provisioned box is right-sized to run every corner concurrently
    (``remote_launcher.select_instance_type``), so real runs are expected to
    finish far faster than this bound -- it exists only so a genuinely
    wedged remote run doesn't hang the SSH channel forever. Overridable via
    ``request.remote.ssh_timeout_s``.
    """
    return per_corner_timeout_s * corner_count + 120.0


def _rewrite_remote_artifact_paths(
    corners: list[dict[str, Any]], *, remote_root: str, local_root: str
) -> None:
    """Rewrite each corner's ``artifacts.*`` paths from the remote host's
    filesystem (where the pulled report JSON was generated) to the local
    path :func:`remote_transport.pull_artifacts` just copied them to.

    The response's ``artifacts`` block always describes paths on the machine
    the caller is running on -- ``local``/``local-parallel``/``remote``
    alike -- so a raw remote path would be meaningless (and unreadable) to a
    caller inspecting the returned report.
    """
    for corner in corners:
        artifacts = corner.get("artifacts") or {}
        for key, value in list(artifacts.items()):
            if not value:
                continue
            relative = os.path.relpath(value, remote_root)
            artifacts[key] = os.path.join(local_root, relative)
