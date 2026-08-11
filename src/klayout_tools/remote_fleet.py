"""Fleet launch lifecycle for the ``remote`` `klt sim` backend's K-host
scheduler -- Epic #375 ("fleet scheduler for the remote sim backend"),
Phase 1B (issue #377).

Reuses :class:`klayout_tools.remote_launcher.RemoteLauncher` **unchanged**,
one instance per fleet member, per the epic's decision to keep
"same-code-different-box" all the way down: this module adds *only* the
K-host orchestration, guardrails, and teardown-all guarantee around it --
:class:`RemoteLauncher` itself is never subclassed or modified.

What this module owns (Phase 1B's scope; see Epic #375's Phase 1 split):

- **Launch** K identical instances (:func:`FleetLauncher.provision_fleet`),
  sized once from the *largest* shard's unit count via the existing
  :func:`~klayout_tools.remote_launcher.select_instance_type` (so every
  member -- including a later shard-retry replacement -- has enough
  headroom for whichever shard it ends up running).
- **Fleet-level cost gate** (epic decision 5, :func:`require_fleet_cost_gate`):
  ``max_hourly_cost_usd`` bounds ``hosts * estimated hourly rate``, not one
  instance's rate -- the bug the epic names ("the current per-instance
  interpretation is a bug once K > 1"). Each fleet member's own
  ``RemoteLauncher`` is still given a *per-instance* ceiling
  (``max_hourly_cost_usd / hosts``) as defense in depth, so a shard retry's
  replacement instance re-validates its own fair share through the exact
  same unmodified gate (:func:`~klayout_tools.remote_launcher.require_cost_config`)
  -- this is how "retries respect the cost gate" is satisfied without
  inventing a second gate implementation.
- **vCPU quota pre-check** (epic decision 3, :func:`check_vcpu_quota`):
  compares ``hosts * instance_vcpu_count(instance_type)`` against the
  account's on-demand/spot "Standard" vCPU quota via ``service-quotas``,
  before any billable call, raising a named :class:`FleetQuotaError`
  (quota name, quota code, the need, and a console request-increase URL)
  instead of letting a plain ``run-instances`` call fail with AWS's own
  opaque capacity/quota error.
- **One automatic shard retry** (epic decision 2,
  :meth:`FleetLauncher.run_shards`): a shard whose caller-supplied
  :data:`ShardRunner` raises (SSH timeout, spot reclaim, transport error --
  any exception) has its instance torn down and relaunched exactly once;
  a second failure is reported back as that shard's own
  ``ShardOutcome(status="error", ...)`` -- **never** raised, and never
  aborts any other shard. Turning that per-shard error into the merged
  report's per-unit ``status: "error"`` entries is Phase 1A's job
  (issue #376); this module's contract ends at handing back one
  :class:`ShardOutcome` per shard.
- **Teardown-all, guaranteed**, on every exit path: normal completion, a
  raised exception (including the "partial launch" hard path below), or a
  caught SIGINT/SIGTERM -- :class:`FleetLauncher` is a context manager
  whose ``__exit__`` always calls :meth:`FleetLauncher.terminate_all`,
  mirroring :class:`~klayout_tools.remote_launcher.RemoteLauncher`'s own
  guarantee. The client-death path (process killed uncatchably) is
  backstopped the same way a single instance's is -- issue #264's
  ``InstanceInitiatedShutdownBehavior=terminate`` plus the AMI-baked
  idle-shutdown guard -- unchanged and per-member, since every fleet
  member is launched via
  :func:`~klayout_tools.remote_launcher.build_run_instances_args`, reused
  verbatim.

**Partial-launch failure** (K-1 instances up, one launch fails) is the new
hard path the epic calls out: :meth:`FleetLauncher.provision_fleet` treats
any single member's provisioning failure as fleet-fatal -- it terminates
every already-launched member and raises **one** named
:class:`FleetLaunchError` describing which shard(s) failed and why. This is
deliberately different from a **shard-run** failure (after every member
successfully reached ``running``): that gets the one-retry treatment above
and never aborts the fleet, per epic decision 2 ("never fail the whole
fleet for one shard").

**Integration point for Phase 1A** (issue #376, independent of this issue):
the actual corner/unit slicing, the per-shard request document, and the
merged-report assembly are Phase 1A's job. This module hands 1A (or
whatever future caller) a plain callable contract instead --
:data:`ShardRunner`, ``(shard_index, launcher, public_ip) -> Any`` -- so
Phase 1A's own shard-run implementation (built from
:mod:`klayout_tools.remote_transport`'s push/run/pull, exactly like
today's single-host ``sim._run_remote``) can be handed to
:meth:`FleetLauncher.run_shards`/:func:`run_fleet` unchanged once it
exists. Nothing in this module knows what a "shard" or a "unit" is beyond
an integer sizing count.
"""

from __future__ import annotations

import signal
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from .remote_launcher import (
    ASSUMED_THREADS_PER_CORNER,
    AwsRunner,
    RemoteLauncher,
    RemoteLaunchError,
    _run_aws_cli,
    _signal_handlers_install,
    _signal_handlers_restore,
    build_security_group_egress_lockdown_args,
    build_security_group_ingress_args,
    instance_vcpu_count,
    require_cost_config,
    select_instance_type,
)


class FleetLaunchError(RemoteLaunchError):
    """Raised when the fleet as a whole cannot be provisioned: the fleet
    cost gate refuses the estimate, the vCPU quota pre-check fails, or a
    partial-launch failure occurs (one member's ``run-instances`` fails
    after others already succeeded).

    Subclasses :class:`~klayout_tools.remote_launcher.RemoteLaunchError` so
    any existing caller that already catches that type (e.g. `klt sim`'s
    ``remote`` backend, ``sim._run_remote``) keeps working unchanged once a
    future issue wires fleet dispatch into it -- see this module's
    docstring, "Integration point for Phase 1A".
    """


class FleetQuotaError(FleetLaunchError):
    """Named error for a failed vCPU quota pre-check (epic decision 3):
    always names the quota, the fleet's vCPU need, the account's current
    quota value, and a console URL to request an increase -- never AWS's
    own opaque ``run-instances`` capacity/quota failure."""


class FleetLauncherInterrupted(Exception):
    """Raised from :class:`FleetLauncher`'s installed SIGINT/SIGTERM
    handler while a fleet operation is in flight, so ``__exit__`` runs the
    same guaranteed :meth:`FleetLauncher.terminate_all` path a normal
    exception would -- mirrors
    :class:`~klayout_tools.remote_launcher.RemoteLauncherInterrupted`."""


# --------------------------------------------------------------------------- #
# Fleet-level cost gate (epic decision 5)
# --------------------------------------------------------------------------- #


def require_fleet_cost_gate(
    *,
    region: str | None,
    instance_type: str,
    spot: bool,
    hosts: int,
    max_hourly_cost_usd: float | None,
) -> tuple[float, float | None]:
    """The fleet-level cost gate: resolve ``instance_type``'s estimated
    hourly cost and enforce ``max_hourly_cost_usd`` against **``hosts`` x
    that rate** -- fixing the bug the epic names ("the current
    per-instance interpretation is a bug once K > 1"). Enforced before any
    billable AWS API call, exactly like the single-instance gate it wraps
    (:func:`~klayout_tools.remote_launcher.require_cost_config`, reused
    here for its region-required check and its cost-table lookup, called
    with ``max_hourly_cost_usd=None`` so *this* function owns the K-aware
    comparison).

    Returns ``(per_instance_hourly, per_instance_ceiling)``:
    ``per_instance_ceiling`` is ``max_hourly_cost_usd / hosts`` (``None``
    when no ceiling was given) -- meant to be threaded into every fleet
    member's own ``RemoteLauncher(max_hourly_cost_usd=...)``, so a later
    shard retry's fresh instance re-validates its own fair share of the
    *same* ceiling through the unmodified per-instance gate (see this
    module's docstring on "retries respect the cost gate").

    Raises :class:`FleetLaunchError` (naming the K x rate arithmetic) when
    the fleet estimate exceeds the ceiling, or when ``hosts`` is not a
    positive integer.
    """
    if hosts < 1:
        raise FleetLaunchError(f"hosts must be a positive integer, got {hosts!r}")

    # max_hourly_cost_usd=None here: this function owns the K-aware
    # comparison below, not require_cost_config's single-instance one.
    per_instance_hourly = require_cost_config(
        region=region,
        instance_type=instance_type,
        spot=spot,
        max_hourly_cost_usd=None,
    )

    if max_hourly_cost_usd is None:
        return per_instance_hourly, None

    fleet_hourly = round(per_instance_hourly * hosts, 4)
    if fleet_hourly > max_hourly_cost_usd:
        raise FleetLaunchError(
            f"estimated fleet hourly cost ${fleet_hourly:.4f} ({hosts} x "
            f"{instance_type} @ ~${per_instance_hourly:.4f}/hr "
            f"{'spot' if spot else 'on-demand'} each) exceeds "
            f"remote.max_hourly_cost_usd=${max_hourly_cost_usd:.4f} -- "
            "refusing to provision any fleet instance"
        )
    return per_instance_hourly, round(max_hourly_cost_usd / hosts, 4)


# --------------------------------------------------------------------------- #
# vCPU quota pre-check (epic decision 3)
# --------------------------------------------------------------------------- #

#: ``service-quotas`` quota codes for the ``ec2`` service, keyed by whether
#: the fleet requests spot or on-demand -- both cover the ``c7i`` family's
#: "Standard" instance class (the only family this module's launcher sizes
#: from; see ``remote_launcher._C7I_LADDER``). Values are AWS's own
#: published quota codes/names at time of writing.
_QUOTA_CODE_BY_MARKET: dict[bool, tuple[str, str]] = {
    True: ("L-34B43A08", "All Standard Spot Instance Requests"),
    False: (
        "L-1216C47A",
        "Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances",
    ),
}


def check_vcpu_quota(
    *,
    region: str,
    instance_type: str,
    hosts: int,
    spot: bool,
    aws_runner: AwsRunner,
) -> float:
    """The vCPU quota pre-check: compare ``hosts *
    instance_vcpu_count(instance_type)`` against the account's on-demand
    (or spot) "Standard" vCPU quota, via ``aws service-quotas
    get-service-quota``, before any billable call.

    Raises :class:`FleetQuotaError` naming the quota (name + code), the
    fleet's vCPU need, the account's current quota value, and a console
    URL to request an increase, when the need exceeds the quota -- never
    AWS's own opaque ``run-instances`` capacity/quota failure. Returns the
    account's current quota value (vCPUs) on success.
    """
    quota_code, quota_name = _QUOTA_CODE_BY_MARKET[bool(spot)]
    needed = hosts * instance_vcpu_count(instance_type)

    raw = aws_runner(
        [
            "service-quotas",
            "get-service-quota",
            "--region",
            region,
            "--service-code",
            "ec2",
            "--quota-code",
            quota_code,
            "--query",
            "Quota.Value",
            "--output",
            "text",
        ]
    )
    try:
        quota_value = float(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise FleetLaunchError(
            f"could not parse service-quotas response for {quota_name!r} "
            f"({quota_code}) in {region}: {raw!r}"
        ) from exc

    if needed > quota_value:
        request_url = (
            f"https://{region}.console.aws.amazon.com/servicequotas/home/"
            f"services/ec2/quotas/{quota_code}"
        )
        raise FleetQuotaError(
            f"fleet needs {needed} vCPU ({hosts} x {instance_type}) but the "
            f"account's {quota_name!r} quota ({quota_code}) is only "
            f"{quota_value:g} vCPU in {region} -- request an increase at "
            f"{request_url}"
        )
    return quota_value


# --------------------------------------------------------------------------- #
# Shard-run callable contract (integration point for Phase 1A / issue #376)
# --------------------------------------------------------------------------- #

#: ``(shard_index, launcher, public_ip) -> result``. ``launcher`` is the
#: already-``provision()``ed :class:`~klayout_tools.remote_launcher.RemoteLauncher`
#: for this shard's current instance (its ``instance_id``/``instance_type``/
#: ``estimated_hourly_cost_usd``/etc. are all populated); ``public_ip`` is
#: that instance's reachable address (already past
#: :meth:`~klayout_tools.remote_launcher.RemoteLauncher.get_public_ip`'s
#: own ``running``-state wait). ``result`` is opaque to this module -- for
#: `klt sim`'s eventual fleet wiring, it will be whatever Phase 1A's
#: shard-run implementation returns (a per-shard report fragment); this
#: module never inspects it. Any exception raised means "this shard's run
#: failed" and triggers the one-retry policy (see this module's docstring).
ShardRunner = Callable[[int, RemoteLauncher, str], Any]


@dataclass
class ShardOutcome:
    """The result of running one shard through :meth:`FleetLauncher.run_shards`.

    ``status`` is ``"ok"`` (``result`` holds the :data:`ShardRunner`'s
    return value) or ``"error"`` (``error`` holds a diagnostic string,
    after the one automatic retry was exhausted -- epic decision 2).
    ``environment`` is the last successfully-provisioned instance's
    :meth:`~klayout_tools.remote_launcher.RemoteLauncher.provision` info
    dict for this shard (``None`` only if every launch attempt for this
    shard failed outright, which :meth:`FleetLauncher.run_shards` never
    reaches -- shards are only run once every member's initial
    provisioning succeeded; see :meth:`FleetLauncher.provision_fleet`).
    ``attempts`` is how many instances this shard actually ran on (1, or 2
    if the retry fired).
    """

    shard_index: int
    status: str
    result: Any | None = None
    error: str | None = None
    attempts: int = 0
    environment: dict[str, Any] | None = None


@dataclass
class FleetLaunchResult:
    """The overall result :func:`run_fleet` returns: one
    :class:`ShardOutcome` per shard, plus fleet-wide bookkeeping a future
    caller's ``environment.remote.fleet[]`` assembly (Phase 1A) can use
    directly."""

    shards: list[ShardOutcome]
    hosts_launched: int
    terminated_instance_ids: list[str] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        """``True`` iff every shard's outcome is ``"ok"`` -- never fails
        the whole fleet for one lost shard, so this is a convenience
        summary, not something :func:`run_fleet` itself raises on."""
        return all(shard.status == "ok" for shard in self.shards)


# --------------------------------------------------------------------------- #
# FleetLauncher: provision-all -> run-shards-with-retry -> teardown-all
# --------------------------------------------------------------------------- #


class FleetLauncher:
    """Provision K identical, cold, per-job instances (reusing
    :class:`~klayout_tools.remote_launcher.RemoteLauncher` unchanged, one
    per fleet member) and run a caller-supplied :data:`ShardRunner`
    concurrently against each, guaranteeing every launched instance
    terminates on every exit path.

    Usage::

        with FleetLauncher(
            region="us-east-1", pdk="sky130A",
            shard_unit_counts=[5, 5, 5, 4], launcher_cidr="203.0.113.4/32",
        ) as fleet:
            fleet.provision_fleet()
            outcomes = fleet.run_shards(my_shard_runner)
        # __exit__ has already called terminate_all() here, on ANY exit
        # reason: normal completion, an exception (including a
        # partial-launch failure raised by provision_fleet), or a caught
        # SIGINT/SIGTERM.

    Or, for the common case, :func:`run_fleet` does exactly the above in
    one call.
    """

    def __init__(
        self,
        *,
        region: str,
        pdk: str,
        shard_unit_counts: Sequence[int],
        job_id_prefix: str | None = None,
        threads_per_corner: int = ASSUMED_THREADS_PER_CORNER,
        spot: bool = True,
        max_hourly_cost_usd: float | None = None,
        launcher_cidr: str | None = None,
        security_group_id: str | None = None,
        key_name: str | None = None,
        subnet_id: str | None = None,
        manifest_path: str | Path | None = None,
        aws_runner: AwsRunner | None = None,
        max_retries_per_shard: int = 1,
        skip_quota_check: bool = False,
    ) -> None:
        self.region = region
        self.pdk = pdk
        self.shard_unit_counts = list(shard_unit_counts)
        self.hosts = len(self.shard_unit_counts)
        if self.hosts < 1:
            raise FleetLaunchError(
                "shard_unit_counts must have at least one entry (hosts must "
                "be a positive integer)"
            )

        self.job_id_prefix = job_id_prefix or f"klt-fleet-{uuid.uuid4().hex[:8]}"
        self.threads_per_corner = threads_per_corner
        self.spot = spot
        self.max_hourly_cost_usd = max_hourly_cost_usd
        self.launcher_cidr = launcher_cidr
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.subnet_id = subnet_id
        self.manifest_path = manifest_path
        self._aws: AwsRunner = aws_runner if aws_runner is not None else _run_aws_cli
        self.max_retries_per_shard = max_retries_per_shard
        self.skip_quota_check = skip_quota_check

        # Identical sizing across the fleet (epic: "Launch K identical
        # instances"), driven by the *largest* shard's unit count so every
        # member -- including a shard retry's replacement, which may land a
        # different shard's unit count on a fresh instance in a future
        # reassignment scheme -- has enough headroom. In practice 1A's
        # contiguous slicing keeps shards within one unit of each other, so
        # this is exact, not just an upper bound.
        self.instance_type = select_instance_type(
            max(self.shard_unit_counts), threads_per_corner
        )

        self.per_instance_hourly: float | None = None
        self.per_instance_ceiling: float | None = None
        self._quota_checked = False

        self._created_security_group = False
        self._launchers: list[RemoteLauncher] = []  # every launcher ever made
        self._active_launcher: dict[int, RemoteLauncher] = {}
        self._provision_info: dict[int, dict[str, Any]] = {}
        self.terminated_instance_ids: list[str] = []
        self._prev_handlers: dict[int, Any] = {}

    # -- context manager: guaranteed teardown-all ---------------------------

    def __enter__(self) -> FleetLauncher:
        self._install_signal_handlers()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.terminate_all()
        finally:
            self._restore_signal_handlers()
        return False  # never swallow an exception raised in the block

    def _install_signal_handlers(self) -> None:
        _signal_handlers_install(self._prev_handlers, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        _signal_handlers_restore(self._prev_handlers)

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        del frame
        raise FleetLauncherInterrupted(
            f"received signal {signal.Signals(signum).name}, tearing down "
            f"{len(self._launchers)} fleet instance(s)"
        )

    # -- guardrails (before any billable call) ------------------------------

    def _apply_guardrails(self) -> None:
        self.per_instance_hourly, self.per_instance_ceiling = require_fleet_cost_gate(
            region=self.region,
            instance_type=self.instance_type,
            spot=self.spot,
            hosts=self.hosts,
            max_hourly_cost_usd=self.max_hourly_cost_usd,
        )
        if not self.skip_quota_check:
            check_vcpu_quota(
                region=self.region,
                instance_type=self.instance_type,
                hosts=self.hosts,
                spot=self.spot,
                aws_runner=self._aws,
            )
        self._quota_checked = True

    # -- shared security group (one per fleet, not one per instance) -------

    def _resolve_security_group(self) -> str:
        if self.security_group_id:
            return self.security_group_id
        if not self.launcher_cidr:
            raise FleetLaunchError(
                "either security_group_id or launcher_cidr is required to "
                "provision a fleet -- same requirement as a single "
                "RemoteLauncher"
            )
        group_id = self._aws(
            [
                "ec2",
                "create-security-group",
                "--region",
                self.region,
                "--group-name",
                f"{self.job_id_prefix}-fleet",
                "--description",
                f"klt remote-sim fleet {self.job_id_prefix} (ephemeral)",
                "--query",
                "GroupId",
                "--output",
                "text",
            ]
        )
        self._created_security_group = True
        self._aws(build_security_group_ingress_args(self.launcher_cidr, group_id))
        self._aws(build_security_group_egress_lockdown_args(group_id))
        self.security_group_id = group_id
        return group_id

    def _new_launcher(self, shard_index: int) -> RemoteLauncher:
        job_id = f"{self.job_id_prefix}-shard{shard_index}-{uuid.uuid4().hex[:6]}"
        return RemoteLauncher(
            region=self.region,
            pdk=self.pdk,
            corner_count=self.shard_unit_counts[shard_index],
            job_id=job_id,
            threads_per_corner=self.threads_per_corner,
            spot=self.spot,
            max_hourly_cost_usd=self.per_instance_ceiling,
            security_group_id=self.security_group_id,
            key_name=self.key_name,
            subnet_id=self.subnet_id,
            manifest_path=self.manifest_path,
            aws_runner=self._aws,
        )

    # -- phase 1: launch K instances -----------------------------------------

    def provision_fleet(self) -> list[dict[str, Any]]:
        """Enforce the fleet cost gate and vCPU quota pre-check, then
        launch :attr:`hosts` identical instances concurrently.

        On any single member's provisioning failure, terminates every
        already-launched member and raises **one** :class:`FleetLaunchError`
        naming every failed shard and reason -- the "partial launch" hard
        path (K-1 up, one launch fails). Never partially returns: either
        every instance is running, or none are (after this call's own
        teardown) and an exception was raised.
        """
        self._apply_guardrails()
        self._resolve_security_group()

        errors: dict[int, str] = {}
        results: list[dict[str, Any] | None] = [None] * self.hosts

        with ThreadPoolExecutor(max_workers=self.hosts) as pool:
            futures = {
                pool.submit(self._provision_one, i): i for i in range(self.hosts)
            }
            for future in as_completed(futures):
                shard_index = futures[future]
                try:
                    results[shard_index] = future.result()
                except RemoteLaunchError as exc:
                    errors[shard_index] = str(exc)

        if errors:
            self.terminate_all()
            failed = ", ".join(f"shard {i}: {errors[i]}" for i in sorted(errors))
            raise FleetLaunchError(
                f"fleet launch failed for {len(errors)}/{self.hosts} "
                f"instance(s) ({failed}) -- terminated the "
                f"{self.hosts - len(errors)} already-launched instance(s)"
            )

        return [info for info in results if info is not None]

    def _provision_one(self, shard_index: int) -> dict[str, Any]:
        launcher = self._new_launcher(shard_index)
        info = launcher.provision()
        self._launchers.append(launcher)
        self._active_launcher[shard_index] = launcher
        self._provision_info[shard_index] = info
        return info

    # -- phase 2: run shards concurrently, one retry each -------------------

    def run_shards(self, shard_runner: ShardRunner) -> list[ShardOutcome]:
        """Run ``shard_runner`` concurrently against every fleet member
        provisioned by :meth:`provision_fleet` (call it first). A shard
        whose runner raises is retried exactly once on a freshly launched
        replacement instance; a second failure is recorded in the returned
        :class:`ShardOutcome` (``status="error"``) rather than raised --
        one lost shard never fails the whole fleet (epic decision 2).
        """
        if len(self._active_launcher) != self.hosts:
            raise FleetLaunchError(
                "run_shards() called before provision_fleet() succeeded for "
                "every fleet member"
            )

        outcomes: list[ShardOutcome | None] = [None] * self.hosts
        with ThreadPoolExecutor(max_workers=self.hosts) as pool:
            futures = {
                pool.submit(self._run_shard_with_retry, i, shard_runner): i
                for i in range(self.hosts)
            }
            for future in as_completed(futures):
                shard_index = futures[future]
                outcomes[shard_index] = future.result()

        assert all(o is not None for o in outcomes)
        return outcomes  # type: ignore[return-value]

    def _run_shard_with_retry(
        self, shard_index: int, shard_runner: ShardRunner
    ) -> ShardOutcome:
        attempts = 0
        last_error: str | None = None
        launcher = self._active_launcher[shard_index]
        info = self._provision_info[shard_index]

        for attempt in range(self.max_retries_per_shard + 1):
            attempts += 1
            try:
                public_ip = launcher.get_public_ip()
                result = shard_runner(shard_index, launcher, public_ip)
                return ShardOutcome(
                    shard_index=shard_index,
                    status="ok",
                    result=result,
                    attempts=attempts,
                    environment=info,
                )
            except Exception as exc:  # noqa: BLE001 -- any shard-run failure retries once
                last_error = str(exc)
                launcher.terminate()  # drop the bad instance immediately

                if attempt >= self.max_retries_per_shard:
                    break
                try:
                    launcher = self._relaunch_shard(shard_index)
                    info = self._provision_info[shard_index]
                except RemoteLaunchError as relaunch_exc:
                    last_error = f"{last_error}; retry relaunch failed: {relaunch_exc}"
                    break

        return ShardOutcome(
            shard_index=shard_index,
            status="error",
            error=last_error,
            attempts=attempts,
            environment=info,
        )

    def _relaunch_shard(self, shard_index: int) -> RemoteLauncher:
        """Launch a fresh replacement instance for ``shard_index`` (the
        one-automatic-retry path). Reuses the *same*
        ``per_instance_ceiling`` computed once by :meth:`_apply_guardrails`
        -- this is how a retry "respects the cost gate" without a second
        gate implementation: the replacement's own ``RemoteLauncher``
        re-validates through the unmodified
        :func:`~klayout_tools.remote_launcher.require_cost_config`.
        """
        launcher = self._new_launcher(shard_index)
        info = launcher.provision()
        self._launchers.append(launcher)
        self._active_launcher[shard_index] = launcher
        self._provision_info[shard_index] = info
        return launcher

    # -- teardown-all (guaranteed) -------------------------------------------

    def terminate_all(self) -> list[str]:
        """Best-effort terminate of every instance this fleet has ever
        launched (including retry replacements still tracked in
        ``_launchers`` after their own early termination -- ``terminate()``
        is idempotent, see
        :meth:`~klayout_tools.remote_launcher.RemoteLauncher.terminate`).

        Never raises: one member's ``terminate-instances`` failure does not
        stop the rest from being attempted -- every instance gets its own
        best-effort teardown call regardless of a sibling's outcome. The
        host-side backstop (``InstanceInitiatedShutdownBehavior=terminate``
        plus the AMI-baked idle-shutdown guard, unchanged per member) still
        applies to any instance this loop fails to tear down. Returns a
        list of ``"<job_id>: <error>"`` strings for any member whose
        explicit teardown call failed (empty on full success).
        """
        errors: list[str] = []
        for launcher in self._launchers:
            instance_id = launcher.instance_id
            try:
                launcher.terminate()
                if instance_id is not None:
                    self.terminated_instance_ids.append(instance_id)
            except RemoteLaunchError as exc:
                errors.append(f"{launcher.job_id}: {exc}")

        if self._created_security_group and self.security_group_id:
            try:
                self._aws(
                    [
                        "ec2",
                        "delete-security-group",
                        "--region",
                        self.region,
                        "--group-id",
                        self.security_group_id,
                    ]
                )
            except RemoteLaunchError:
                pass  # best-effort; see docstring
            self._created_security_group = False

        return errors


# --------------------------------------------------------------------------- #
# Convenience one-shot entry point
# --------------------------------------------------------------------------- #


def run_fleet(
    *,
    region: str,
    pdk: str,
    shard_unit_counts: Sequence[int],
    shard_runner: ShardRunner,
    job_id_prefix: str | None = None,
    threads_per_corner: int = ASSUMED_THREADS_PER_CORNER,
    spot: bool = True,
    max_hourly_cost_usd: float | None = None,
    launcher_cidr: str | None = None,
    security_group_id: str | None = None,
    key_name: str | None = None,
    subnet_id: str | None = None,
    manifest_path: str | Path | None = None,
    aws_runner: AwsRunner | None = None,
    max_retries_per_shard: int = 1,
    skip_quota_check: bool = False,
) -> FleetLaunchResult:
    """Provision a K-instance fleet, run ``shard_runner`` concurrently
    against every member (with one automatic retry each), and guarantee
    every launched instance terminates -- the single-call form of
    :class:`FleetLauncher`'s ``provision_fleet()`` + ``run_shards()``.

    ``len(shard_unit_counts)`` is ``K`` (``hosts``); each entry is that
    shard's unit count, used only for identical-fleet sizing (see
    :class:`FleetLauncher`'s docstring). Raises :class:`FleetLaunchError`
    (or the more specific :class:`FleetQuotaError`) before any billable
    call if the fleet cost gate or vCPU quota pre-check fails, or after a
    partial-launch failure (every already-launched instance is torn down
    first). Never raises for an individual shard's run failure after
    successful provisioning -- see ``ShardOutcome.status`` on the returned
    :class:`FleetLaunchResult`.
    """
    with FleetLauncher(
        region=region,
        pdk=pdk,
        shard_unit_counts=shard_unit_counts,
        job_id_prefix=job_id_prefix,
        threads_per_corner=threads_per_corner,
        spot=spot,
        max_hourly_cost_usd=max_hourly_cost_usd,
        launcher_cidr=launcher_cidr,
        security_group_id=security_group_id,
        key_name=key_name,
        subnet_id=subnet_id,
        manifest_path=manifest_path,
        aws_runner=aws_runner,
        max_retries_per_shard=max_retries_per_shard,
        skip_quota_check=skip_quota_check,
    ) as fleet:
        fleet.provision_fleet()
        outcomes = fleet.run_shards(shard_runner)
        hosts_launched = len(fleet._launchers)

    return FleetLaunchResult(
        shards=outcomes,
        hosts_launched=hosts_launched,
        terminated_instance_ids=list(fleet.terminated_instance_ids),
    )
