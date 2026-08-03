"""Provisioning + teardown launcher for the ``remote`` `klt sim` backend.

Implements the Phase 2 decisions from
``docs/design/remote-sim-backend-spike.md`` (decisions 1-4, "Minimal-
credential host profile and IAM shape", and "Guardrail mechanics" SS2-3):

- A **direct EC2 API launcher** (``run-instances``/``terminate-instances``),
  not a call into ``repo:remote``'s ``up``/``down`` (that tool provisions a
  named, reused, persistent dev box -- the opposite shape from "one
  disposable instance per job"; see decision 1). The AWS CLI subprocess
  pattern and the cost-gate/idle-shutdown-guard *design* are reused from
  ``.claude/skills/repo/scripts/repo-remote.sh``, but no code here calls that
  script.
- **One instance sized for the whole requested corner matrix** (decision 2),
  via :func:`select_instance_type`.
- **Cold, per-job instances** (decision 3) -- :class:`RemoteLauncher` has no
  warm-pool/reuse path; every ``provision()`` call creates a fresh instance.
- **Spot by default** (``InstanceMarketOptions.MarketType=spot``), on-demand
  as an explicit opt-out -- see :func:`build_run_instances_args`.
- **Model-library transport via a maintained AMI** (decision 4) -- ngspice
  and the curated PDK decks are baked into a versioned AMI per PDK, resolved
  from a small on-disk manifest (see :func:`resolve_ami` and
  ``scripts/aws/build-remote-sim-ami.sh``, the build/refresh pipeline that
  produces that manifest). This module never fetches PDK data per job.
- **No IAM instance profile attached to the guest by default** -- see
  :func:`build_run_instances_args`, which never emits
  ``--iam-instance-profile`` unless one is explicitly passed in (the
  documented S3-fallback escape hatch, owned by a future issue).
- **Idle-shutdown baked into the AMI**, a materially shorter window than
  ``repo:remote``'s 120-minute default -- see :data:`DEFAULT_IDLE_MINUTES`
  and :func:`idle_guard_install_script` (installed at AMI-build time by
  ``scripts/aws/build-remote-sim-ami.sh``, not injected as launch-time
  user-data -- "baked into the AMI" per the design note, not
  "re-derived per launch").
- **Teardown guaranteed on all failure paths, two independent mechanisms**:
  (a) :class:`RemoteLauncher` is a context manager whose ``__exit__`` always
  calls :meth:`RemoteLauncher.terminate`, covering normal completion and any
  exception raised inside the ``with`` block, plus an explicit SIGINT/SIGTERM
  handler installed for the block's duration that turns an OS signal into a
  Python exception so the same ``__exit__`` path runs; (b)
  ``InstanceInitiatedShutdownBehavior=terminate`` is always set at launch
  (see :func:`build_run_instances_args`) as a host-side backstop independent
  of whether (a)'s explicit ``terminate-instances`` call ever arrives.

Scope note: this module provisions and tears down the instance and resolves
the AMI/sizing/cost inputs to do so, plus (:meth:`RemoteLauncher.get_public_ip`)
resolving how to *reach* what it provisioned. It does **not** implement the
SSH/SCP corner-fan-out transport itself (pushing the netlist, running the
worker pool remotely, pulling results back) -- that lives in
:mod:`klayout_tools.remote_transport` and is wired into ``sim.py``'s
``remote`` backend, per issue #265 (the follow-on issue #264's own body
anticipated: "future corner fan-out ... issues will hook into the launcher
this issue builds").
"""

from __future__ import annotations

import json
import math
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

#: <repo root>/data/remote-sim-ami-manifest.json -- this file lives at
#: <repo root>/src/klayout_tools/remote_launcher.py. Mirrors kb.py's
#: DEFAULT_KB_ROOT convention: walk up from this module's own location so the
#: module works the same from an installed wheel's source checkout or a repo
#: clone; every function also accepts an explicit path override for tests.
DEFAULT_MANIFEST_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "remote-sim-ami-manifest.json"
)

#: PDK combinations the AMI build pipeline maintains (per #264's acceptance
#: criteria). Not every historical PDK klt supports -- only the ones the
#: remote backend's baked-AMI transport is scoped to, per the design note's
#: decision 4.
SUPPORTED_PDKS: tuple[str, ...] = ("sky130A", "gf180mcu")

#: ``c7i`` instance-family sizing ladder: (name, vcpu count), ascending.
#: Compute-optimized, per the design note's sizing recipe: "ngspice is
#: CPU-bound with a modest memory footprint per process, so paying for extra
#: memory-per-vCPU buys nothing here." vCPU counts are AWS's published values
#: for the ``c7i`` family at time of writing.
_C7I_LADDER: tuple[tuple[str, int], ...] = (
    ("c7i.large", 2),
    ("c7i.xlarge", 4),
    ("c7i.2xlarge", 8),
    ("c7i.4xlarge", 16),
    ("c7i.8xlarge", 32),
    ("c7i.12xlarge", 48),
    ("c7i.16xlarge", 64),
    ("c7i.24xlarge", 96),
    ("c7i.48xlarge", 192),
)

#: Approximate on-demand USD/hour by instance type, curated the same way
#: ``repo-remote.sh``'s ``estimate_cost`` is (a hand-maintained table with an
#: approximate fallback, never a silent zero) -- extended here with the
#: ``c7i`` family entries that table lacks (see the design note's guardrail
#: mechanics SS1 and "Open questions for Phase 2" -> "Instance-type table
#: maintenance"). Values are ballpark us-east-1 on-demand list prices at
#: time of writing; a caller that needs a tighter number should treat
#: ``estimated_hourly_cost_usd``'s pairing with ``approximate`` in
#: :func:`estimate_cost`'s return value as authoritative about precision.
_ON_DEMAND_HOURLY_USD: dict[str, float] = {
    "c7i.large": 0.1071,
    "c7i.xlarge": 0.1785,
    "c7i.2xlarge": 0.357,
    "c7i.4xlarge": 0.714,
    "c7i.8xlarge": 1.428,
    "c7i.12xlarge": 2.142,
    "c7i.16xlarge": 2.856,
    "c7i.24xlarge": 4.284,
    "c7i.48xlarge": 8.568,
}

#: Spot is priced as a fraction of on-demand when no curated spot price is
#: available. ~45% mirrors typical observed ``c7i`` spot discounts and lines
#: up with the design note's own worked example (`0.97` spot against a
#: `c7i.12xlarge` whose on-demand price here is `2.142`, `0.97 / 2.142 ~=
#: 0.45`) -- always flagged ``approximate`` (see :func:`estimate_cost`), never
#: presented as a live quote. Spot Price History API integration (a tighter,
#: non-approximate estimate at the cost of an extra AWS call before every
#: provision) is an explicitly deferred "Open question for Phase 2" the
#: design note leaves unresolved.
_SPOT_FRACTION_OF_ON_DEMAND = 0.45

#: Same threads-per-``ngspice``-process assumption ``sim.py``'s
#: ``_ASSUMED_THREADS_PER_NGSPICE`` uses (kept as an independent constant
#: here rather than importing a private name across modules -- see this
#: module's docstring on why ``sim.py`` is not touched by this issue).
ASSUMED_THREADS_PER_CORNER = 8

#: ~20% headroom for OS/guard/artifact-collection overhead, per the design
#: note's sizing recipe.
SIZING_HEADROOM = 1.2

#: Guardrail mechanics SS2: "materially shorter [than repo:remote's 120-minute
#: default] ... recommend on the order of 10-15 minutes". 12 sits in that
#: range with slack on both sides.
DEFAULT_IDLE_MINUTES = 12

#: ``InstanceMarketOptions.MarketType`` spot-capacity failure codes the AWS
#: CLI surfaces on a failed spot `run-instances` call. Recognised so
#: :meth:`RemoteLauncher.provision` can implement the design note's resolved
#: open question (see this module's docstring section on spot fallback)
#: without misclassifying an unrelated failure (bad AMI id, quota, etc.) as a
#: capacity issue.
_SPOT_CAPACITY_ERROR_MARKERS: tuple[str, ...] = (
    "InsufficientInstanceCapacity",
    "SpotMaxPriceTooLow",
    "MaxSpotInstanceCountExceeded",
    "Unsupported",  # e.g. spot not offered for this type/AZ combination
)


class RemoteLaunchError(Exception):
    """Raised when a ``remote`` job cannot even be provisioned: unresolvable
    AMI/PDK, a cost estimate that can't be resolved or exceeds the caller's
    ceiling, a missing ``aws`` CLI, or a failed ``run-instances``/
    ``terminate-instances`` call.

    Mirrors ``sim.SimError``'s "fails loudly before any corner runs" role,
    but scoped to provisioning: no `RunInstances` call that costs money is
    ever made once this is raised (see :func:`require_cost_config`'s
    docstring), and this exception is never used for a *corner*-level
    failure -- that stays the fan-out issue's ``status: "error"`` reporting
    convention.
    """


# --------------------------------------------------------------------------- #
# Instance sizing (decision 2's recipe)
# --------------------------------------------------------------------------- #


def select_instance_type(
    corner_count: int,
    threads_per_corner: int = ASSUMED_THREADS_PER_CORNER,
    *,
    headroom: float = SIZING_HEADROOM,
) -> str:
    """Select the smallest ``c7i`` instance type that fits the requested
    corner matrix, per the design note's sizing recipe:
    ``vcpu_needed = corner_count * threads_per_corner``, then the smallest
    ladder entry whose vCPU count is ``>= vcpu_needed * headroom``.

    Applied to the measured 5-corner x 8-thread case: ``40 * 1.2 == 48`` ->
    ``c7i.12xlarge`` (48 vCPU), exactly the design note's own worked example.

    Raises :class:`RemoteLaunchError` for a non-positive ``corner_count``/
    ``threads_per_corner`` (nothing to size), or when the requested matrix
    exceeds the largest ``c7i`` size the ladder knows about (192 vCPU,
    ``c7i.48xlarge``) -- surfaced loudly rather than silently returning an
    undersized instance.
    """
    if corner_count < 1:
        raise RemoteLaunchError("corner_count must be a positive integer")
    if threads_per_corner < 1:
        raise RemoteLaunchError("threads_per_corner must be a positive integer")

    vcpu_needed = corner_count * threads_per_corner
    required = math.ceil(vcpu_needed * headroom)

    for name, vcpu in _C7I_LADDER:
        if vcpu >= required:
            return name

    largest_name, largest_vcpu = _C7I_LADDER[-1]
    raise RemoteLaunchError(
        f"corner matrix needs {required} vCPU (headroom-adjusted from "
        f"{corner_count} corners x {threads_per_corner} threads), which "
        f"exceeds the largest known c7i size ({largest_name}, "
        f"{largest_vcpu} vCPU) -- split the matrix across multiple "
        "requests"
    )


# --------------------------------------------------------------------------- #
# Cost gate (guardrail mechanics SS1)
# --------------------------------------------------------------------------- #


def estimate_cost(instance_type: str, spot: bool = True) -> tuple[float, bool]:
    """Return ``(estimated_hourly_cost_usd, approximate)`` for
    ``instance_type``.

    A curated on-demand price is looked up in :data:`_ON_DEMAND_HOURLY_USD`;
    an unknown instance type falls back to the largest known ``c7i`` entry's
    price scaled by its own vCPU ratio (a deliberately approximate estimate,
    flagged as such, never a silent zero -- mirroring ``repo-remote.sh``'s
    ``estimate_cost``). Spot pricing has no curated table yet (see this
    module's docstring on the deferred Spot Price History API question) --
    it is always derived as :data:`_SPOT_FRACTION_OF_ON_DEMAND` of the
    resolved on-demand price and therefore always reported ``approximate``.
    """
    on_demand = _ON_DEMAND_HOURLY_USD.get(instance_type)
    approximate = on_demand is None
    if on_demand is None:
        # Fall back to a per-vCPU rate derived from the known ladder, applied
        # to whatever vCPU count this type nominally has (best-effort parse
        # of the ladder; an entirely unrecognised type falls back further to
        # the smallest known entry's per-vCPU rate).
        known_vcpu = dict(_C7I_LADDER).get(instance_type)
        rate_per_vcpu = (
            _ON_DEMAND_HOURLY_USD["c7i.xlarge"] / dict(_C7I_LADDER)["c7i.xlarge"]
        )
        on_demand = rate_per_vcpu * (known_vcpu or 4)

    if not spot:
        return round(on_demand, 4), approximate

    return round(on_demand * _SPOT_FRACTION_OF_ON_DEMAND, 4), True


def instance_vcpu_count(instance_type: str) -> int:
    """Return the exact vCPU count for a known :data:`_C7I_LADDER` entry.

    Unlike :func:`estimate_cost`'s deliberately approximate unknown-type
    fallback, a vCPU count has no "close enough" story: it feeds a hard
    account-quota comparison (Epic #375 Phase 1B's fleet vCPU pre-check, see
    :mod:`klayout_tools.remote_fleet`), so an unrecognized ``instance_type``
    raises :class:`RemoteLaunchError` rather than guessing.
    """
    vcpu = dict(_C7I_LADDER).get(instance_type)
    if vcpu is None:
        raise RemoteLaunchError(
            f"unknown instance type {instance_type!r} -- vCPU count is only "
            "known for the c7i ladder this module sizes from (see "
            "_C7I_LADDER)"
        )
    return vcpu


def require_cost_config(
    *,
    region: str | None,
    instance_type: str,
    spot: bool,
    max_hourly_cost_usd: float | None,
) -> float:
    """The cost gate: resolve ``instance_type``'s estimated hourly cost and
    enforce ``max_hourly_cost_usd`` (if given) *before* any billable AWS API
    call. Returns the resolved estimate on success.

    Missing config fails loudly, never a silent default -- mirroring
    ``repo-remote.sh``'s ``require_cost_config`` verbatim ("Missing config
    fails loudly (exit 2), never a silent default"). Region is the one
    genuinely required, non-cost-relevant-but-mandatory field per the design
    note's request-field table ("No default -- an unset region is a usage
    error, not an inferred one").
    """
    if not region:
        raise RemoteLaunchError(
            "remote.region is required (no default -- an unset region is a "
            "usage error, not an inferred one, per the design note)"
        )

    hourly, _approximate = estimate_cost(instance_type, spot=spot)
    if max_hourly_cost_usd is not None and hourly > max_hourly_cost_usd:
        raise RemoteLaunchError(
            f"estimated hourly cost ${hourly:.4f} for {instance_type} "
            f"({'spot' if spot else 'on-demand'}) exceeds "
            f"remote.max_hourly_cost_usd=${max_hourly_cost_usd:.4f} -- "
            "refusing to provision"
        )
    return hourly


# --------------------------------------------------------------------------- #
# AMI resolution (decision 4)
# --------------------------------------------------------------------------- #


def load_ami_manifest(manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Load the versioned AMI manifest (see ``scripts/aws/build-remote-sim-ami.sh``,
    the build/refresh pipeline that produces it).

    Raises :class:`RemoteLaunchError` if the manifest is missing/unreadable/
    malformed -- resolving an AMI to run real corners against is exactly the
    kind of cost-relevant fact the design note's "no silent default"
    discipline applies to (a wrong or stale AMI silently used is worse than a
    loud failure).
    """
    path = Path(manifest_path) if manifest_path is not None else DEFAULT_MANIFEST_PATH
    if not path.is_file():
        raise RemoteLaunchError(
            f"AMI manifest not found: {path} -- run "
            "scripts/aws/build-remote-sim-ami.sh to build and publish one"
        )
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteLaunchError(f"AMI manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or "images" not in manifest:
        raise RemoteLaunchError(
            f"AMI manifest at {path} is missing a top-level 'images' array"
        )
    return manifest


def resolve_ami(
    pdk: str, region: str, manifest_path: str | Path | None = None
) -> dict[str, str]:
    """Resolve ``pdk``/``region`` to the manifest's most recently built
    published AMI, returning ``{"ami_id": ..., "pdk_snapshot": ...,
    "ngspice_version": ..., "built_at": ...}``.

    Raises :class:`RemoteLaunchError` for an unsupported ``pdk`` (not in
    :data:`SUPPORTED_PDKS`) or when the manifest has no published entry for
    the ``(pdk, region)`` pair -- never silently substitutes a different
    region's or a stale AMI.
    """
    if pdk not in SUPPORTED_PDKS:
        raise RemoteLaunchError(
            f"unsupported PDK '{pdk}' for the remote backend "
            f"(supported: {', '.join(SUPPORTED_PDKS)})"
        )

    manifest = load_ami_manifest(manifest_path)
    candidates = [
        image
        for image in manifest.get("images", [])
        if image.get("pdk") == pdk and image.get("region") == region
    ]
    if not candidates:
        raise RemoteLaunchError(
            f"no published AMI for pdk={pdk!r} region={region!r} in the "
            "manifest -- run scripts/aws/build-remote-sim-ami.sh for that "
            "region, or pick a region the manifest already publishes to"
        )

    # Most recently built wins -- `built_at` is an ISO-8601 string per the
    # manifest schema (see scripts/aws/build-remote-sim-ami.sh), so plain
    # string comparison is chronological.
    latest = max(candidates, key=lambda image: image.get("built_at", ""))
    return {
        "ami_id": latest["ami_id"],
        "pdk_snapshot": latest.get("pdk_snapshot", latest.get("built_at", "")),
        "ngspice_version": latest.get("ngspice_version", ""),
        "built_at": latest.get("built_at", ""),
    }


# --------------------------------------------------------------------------- #
# Idle-shutdown guard (guardrail mechanics SS2) -- baked into the AMI
# --------------------------------------------------------------------------- #


def idle_guard_install_script(idle_minutes: int = DEFAULT_IDLE_MINUTES) -> str:
    """Return a shell script that installs the idle-shutdown cron watchdog
    onto disk, for ``scripts/aws/build-remote-sim-ami.sh`` to run *on the AMI
    build instance* before it snapshots the image -- "baked into the AMI"
    literally, not injected as launch-time user-data (see this module's
    docstring).

    Adapted from ``repo-remote.sh``'s ``idle_guard_userdata`` (same
    who/load-average activity signal, same once-a-minute cron cadence), with
    two deliberate differences: ``idle_minutes`` defaults to
    :data:`DEFAULT_IDLE_MINUTES` (12, materially shorter than
    ``repo:remote``'s 120-minute dev-session default -- this is a job host,
    not a "come back to this later" box), and the idle-exit-marker override
    ``repo-remote.sh`` supports for a daemon-managed host is omitted -- a
    ``remote`` sim job runs no long-lived daemon to hand the guard a precise
    idle-start, so that extra complexity has no caller here.
    """
    return f"""#!/bin/bash
# klt remote-sim idle-shutdown guard (idle window: {idle_minutes} min)
# Baked into the AMI by scripts/aws/build-remote-sim-ami.sh -- see
# src/klayout_tools/remote_launcher.py's idle_guard_install_script docstring.
cat >/usr/local/bin/klt-remote-sim-idle-check <<'GUARD'
#!/bin/bash
IDLE_MIN={idle_minutes}
STAMP=/var/run/klt-remote-sim-idle.stamp
# An active SSH session or non-trivial CPU load is real activity: reset the
# idle timer and veto shutdown. No process-name check (see repo-remote.sh's
# own note on this) -- a running process alone never keeps this host alive.
if who | grep -q . || [ "$(awk '{{print ($1 > 0.2)}}' /proc/loadavg)" = "1" ]; then
  date +%s > "$STAMP"; exit 0
fi
[ -f "$STAMP" ] || {{ date +%s > "$STAMP"; exit 0; }}
NOW=$(date +%s); LAST=$(cat "$STAMP")
if [ $(( (NOW - LAST) / 60 )) -ge "$IDLE_MIN" ]; then
  /sbin/shutdown -h now "klt remote-sim: idle for ${{IDLE_MIN}}m"
fi
GUARD
chmod +x /usr/local/bin/klt-remote-sim-idle-check
echo "* * * * * root /usr/local/bin/klt-remote-sim-idle-check" \\
  >/etc/cron.d/klt-remote-sim-idle
"""


# --------------------------------------------------------------------------- #
# Network posture: SSH-inbound-only, no default egress
# --------------------------------------------------------------------------- #


def build_security_group_ingress_args(launcher_cidr: str, group_id: str) -> list[str]:
    """``aws ec2 authorize-security-group-ingress`` argv allowing SSH (22)
    from ``launcher_cidr`` only -- the design note's "No inbound rules except
    SSH from the launcher's own IP/CIDR" posture."""
    return [
        "ec2",
        "authorize-security-group-ingress",
        "--group-id",
        group_id,
        "--protocol",
        "tcp",
        "--port",
        "22",
        "--cidr",
        launcher_cidr,
    ]


def build_security_group_egress_lockdown_args(group_id: str) -> list[str]:
    """``aws ec2 revoke-security-group-egress`` argv removing the security
    group's default allow-all-outbound rule.

    Because decision 4 bakes ngspice + the PDK decks into the AMI, the guest
    never needs to call out anywhere (see this module's docstring and the
    design note's "Network posture" section: "outbound egress can be locked
    down to nothing once boot completes") -- there is nothing to fetch and no
    AWS API the guest is allowed to call (no instance profile is attached
    either, see :func:`build_run_instances_args`). Revoking the one default
    rule AWS creates for a new security group leaves it with zero egress
    rules, i.e. fully locked down.
    """
    return [
        "ec2",
        "revoke-security-group-egress",
        "--group-id",
        group_id,
        "--ip-permissions",
        json.dumps(
            [
                {
                    "IpProtocol": "-1",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                }
            ]
        ),
    ]


# --------------------------------------------------------------------------- #
# RunInstances / TerminateInstances argument construction
# --------------------------------------------------------------------------- #


def build_run_instances_args(
    *,
    ami_id: str,
    instance_type: str,
    security_group_id: str,
    region: str,
    job_id: str,
    spot: bool = True,
    key_name: str | None = None,
    subnet_id: str | None = None,
    iam_instance_profile_arn: str | None = None,
) -> list[str]:
    """Build the ``aws ec2 run-instances`` argv for one job.

    Always sets ``InstanceInitiatedShutdownBehavior=terminate`` (guardrail
    mechanics SS3's host-side backstop) and tags the instance
    ``klt-remote-sim=<job_id>`` for :meth:`RemoteLauncher.terminate` and any
    operator audit. ``run-instances`` is built to be invoked exactly once per
    job -- a retry-to-read-the-error would risk launching a second billable
    instance, the same discipline ``repo-remote.sh``'s own ``aws_create``
    comment documents.

    Never emits ``--iam-instance-profile`` unless ``iam_instance_profile_arn``
    is explicitly given -- the default is **no instance profile at all**
    (decision 4's minimal-credential host profile: baked AMI + SSH/SCP
    transport means the guest never calls an AWS API). The scoped
    ``s3:PutObject``-only fallback profile described in the design note is
    only ever attached when a caller explicitly passes one in; this module
    does not decide when that fallback is warranted (a future fan-out
    issue's call, per the design note).
    """
    args = [
        "ec2",
        "run-instances",
        "--region",
        region,
        "--image-id",
        ami_id,
        "--instance-type",
        instance_type,
        "--security-group-ids",
        security_group_id,
        "--instance-initiated-shutdown-behavior",
        "terminate",
        "--tag-specifications",
        f"ResourceType=instance,Tags=[{{Key=klt-remote-sim,Value={job_id}}}]",
        "--query",
        "Instances[0].InstanceId",
        "--output",
        "text",
    ]
    if key_name:
        args += ["--key-name", key_name]
    if subnet_id:
        args += ["--subnet-id", subnet_id]
    if iam_instance_profile_arn:
        args += ["--iam-instance-profile", f"Arn={iam_instance_profile_arn}"]
    if spot:
        args += [
            "--instance-market-options",
            json.dumps({"MarketType": "spot"}),
        ]
    return args


def build_terminate_instances_args(instance_id: str, region: str) -> list[str]:
    """``aws ec2 terminate-instances`` argv for the explicit teardown path
    (guardrail mechanics SS3(a))."""
    return [
        "ec2",
        "terminate-instances",
        "--region",
        region,
        "--instance-ids",
        instance_id,
    ]


# --------------------------------------------------------------------------- #
# RemoteLauncher: the provision -> ... -> teardown lifecycle
# --------------------------------------------------------------------------- #

#: Signature of the injectable AWS CLI runner: argv (without the leading
#: "aws") in, captured stdout out. Raises :class:`RemoteLaunchError` on a
#: non-zero exit. Defaults to :func:`_run_aws_cli`; tests substitute a fake
#: so no real ``aws`` CLI or network call ever happens in the test suite.
AwsRunner = Callable[[list[str]], str]


def _run_aws_cli(args: list[str]) -> str:
    """Default :data:`AwsRunner`: shells out to the ``aws`` CLI exactly like
    ``repo-remote.sh`` does, so both tools share one authentication/config
    surface (``aws configure`` / env vars / ``~/.aws``) rather than this
    module inventing a second one. Never retried -- see
    :func:`build_run_instances_args`'s docstring on why a `run-instances`
    retry is unsafe.
    """
    if shutil.which("aws") is None:
        raise RemoteLaunchError(
            "the 'aws' CLI was not found on PATH -- install/configure the "
            "AWS CLI to use the remote backend's launcher"
        )
    completed = subprocess.run(
        ["aws", *args], capture_output=True, text=True, timeout=120
    )
    if completed.returncode != 0:
        raise RemoteLaunchError(
            f"aws {' '.join(args[:2])} failed: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


class RemoteLauncher:
    """Provision one cold, per-job spot (default) or on-demand EC2 instance
    sized for ``corner_count``, guaranteeing teardown on every exit path.

    Usage::

        with RemoteLauncher(
            region="us-east-1", pdk="sky130A", corner_count=5,
            launcher_cidr="203.0.113.4/32",
        ) as launcher:
            info = launcher.provision()
            ...  # run + collect (a future fan-out issue's responsibility)
        # __exit__ has already called terminate() here, on ANY exit reason:
        # normal completion, an exception raised inside the block, or a
        # caught SIGINT/SIGTERM (see _install_signal_handlers).

    Two independent teardown mechanisms, per guardrail mechanics SS3:

    - (a) This class's own ``__exit__`` always calls :meth:`terminate` --
      Python's ``with`` statement already guarantees this on normal
      completion or any exception; :meth:`_install_signal_handlers`
      additionally converts a delivered SIGINT/SIGTERM into a
      :class:`RemoteLauncherInterrupted` exception so an OS signal takes the
      same guaranteed path (SIGTERM has no default Python exception the way
      SIGINT/``KeyboardInterrupt`` does, so it needs this explicit
      conversion).
    - (b) ``InstanceInitiatedShutdownBehavior=terminate`` (set at launch by
      :func:`build_run_instances_args`) plus the idle-shutdown guard baked
      into the AMI (see :func:`idle_guard_install_script`) are the host-side
      backstop, independent of whether (a)'s explicit ``terminate-instances``
      call ever arrives (e.g. the launcher process itself is killed
      uncatchably, ``SIGKILL``).
    """

    def __init__(
        self,
        *,
        region: str,
        pdk: str,
        corner_count: int,
        job_id: str,
        threads_per_corner: int = ASSUMED_THREADS_PER_CORNER,
        spot: bool = True,
        max_hourly_cost_usd: float | None = None,
        launcher_cidr: str | None = None,
        security_group_id: str | None = None,
        key_name: str | None = None,
        subnet_id: str | None = None,
        manifest_path: str | Path | None = None,
        aws_runner: AwsRunner | None = None,
        retry_on_demand_on_spot_failure: bool = True,
    ) -> None:
        self.region = region
        self.pdk = pdk
        self.corner_count = corner_count
        self.job_id = job_id
        self.threads_per_corner = threads_per_corner
        self.spot = spot
        self.max_hourly_cost_usd = max_hourly_cost_usd
        self.launcher_cidr = launcher_cidr
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.subnet_id = subnet_id
        self.manifest_path = manifest_path
        self._aws = aws_runner if aws_runner is not None else _run_aws_cli
        self.retry_on_demand_on_spot_failure = retry_on_demand_on_spot_failure

        self.instance_id: str | None = None
        self.instance_type: str | None = None
        self.ami: dict[str, str] | None = None
        self.estimated_hourly_cost_usd: float | None = None
        self.used_spot: bool | None = None
        self.spin_up_s: float | None = None
        self._created_security_group = False
        self._prev_handlers: dict[int, Any] = {}

    # -- context manager: guaranteed teardown (mechanism (a)) --------------

    def __enter__(self) -> RemoteLauncher:
        self._install_signal_handlers()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.terminate()
        finally:
            self._restore_signal_handlers()
        return False  # never swallow an exception raised in the block

    def _install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._prev_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle_signal)

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._prev_handlers.items():
            signal.signal(sig, handler)
        self._prev_handlers.clear()

    def _handle_signal(self, signum: int, frame: FrameType | None) -> None:
        del frame
        raise RemoteLauncherInterrupted(
            f"received signal {signal.Signals(signum).name}, tearing down "
            f"instance {self.instance_id!r}"
        )

    # -- provisioning --------------------------------------------------------

    def provision(self) -> dict[str, Any]:
        """Resolve sizing/AMI/cost, enforce the cost gate, then launch the
        instance. Returns the same shape the design note's proposed
        ``environment.remote`` response block documents (minus fields only a
        completed run can know, e.g. ``spin_up_s`` -- set once
        :meth:`provision` observes the instance reach ``running``).

        Raises :class:`RemoteLaunchError` before any billable
        ``run-instances`` call if sizing, AMI resolution, or the cost gate
        fails -- "never a silent default", per the design note's guardrail
        mechanics SS1.
        """
        if self.instance_id is not None:
            raise RemoteLaunchError(
                "provision() already called for this launcher (job_id="
                f"{self.job_id!r}) -- one launcher instance is one job"
            )

        self.instance_type = select_instance_type(
            self.corner_count, self.threads_per_corner
        )
        self.ami = resolve_ami(self.pdk, self.region, self.manifest_path)
        self.estimated_hourly_cost_usd = require_cost_config(
            region=self.region,
            instance_type=self.instance_type,
            spot=self.spot,
            max_hourly_cost_usd=self.max_hourly_cost_usd,
        )

        security_group_id = self._resolve_security_group()

        started = time.monotonic()
        instance_id, used_spot = self._launch(security_group_id)
        self.instance_id = instance_id
        self.used_spot = used_spot
        if used_spot != self.spot:
            # Recompute the estimate for the fallback path so the reported
            # cost reflects what actually ran, not just what was requested
            # (see the design note's response-field table: "an instance-type
            # override or spot-to-on-demand fallback should be visible
            # here").
            self.estimated_hourly_cost_usd, _ = estimate_cost(
                self.instance_type, spot=used_spot
            )
        self.spin_up_s = round(time.monotonic() - started, 3)

        return {
            "provider": "aws",
            "region": self.region,
            "instance_type": self.instance_type,
            "instance_id": self.instance_id,
            "spot": self.used_spot,
            "estimated_hourly_cost_usd": self.estimated_hourly_cost_usd,
            "ami_id": self.ami["ami_id"],
            "pdk_snapshot": self.ami["pdk_snapshot"],
            "spin_up_s": self.spin_up_s,
        }

    def _resolve_security_group(self) -> str:
        if self.security_group_id:
            return self.security_group_id
        if not self.launcher_cidr:
            raise RemoteLaunchError(
                "either security_group_id or launcher_cidr is required to "
                "provision -- the design note's SSH-inbound-only network "
                "posture needs one or the other to build the ingress rule"
            )
        group_id = self._aws(
            [
                "ec2",
                "create-security-group",
                "--region",
                self.region,
                "--group-name",
                f"klt-remote-sim-{self.job_id}",
                "--description",
                f"klt remote sim job {self.job_id} (ephemeral)",
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

    def _launch(self, security_group_id: str) -> tuple[str, bool]:
        """Launch the instance, implementing the design note's resolved
        open question ("Fallback from spot to on-demand"): on a spot-capacity
        failure, retry on-demand once (the default,
        ``retry_on_demand_on_spot_failure=True``) for higher job reliability
        -- clearly reported back via the response's ``spot`` field so the
        caller can see the estimate no longer matches what was requested,
        rather than that mismatch being silent. Set
        ``retry_on_demand_on_spot_failure=False`` to instead let a
        spot-capacity failure propagate as a loud :class:`RemoteLaunchError`
        (matching the design note's other option, "surface as an `error`
        corner [matrix] requiring the caller to retry explicitly").
        """
        args = build_run_instances_args(
            ami_id=self.ami["ami_id"],
            instance_type=self.instance_type,
            security_group_id=security_group_id,
            region=self.region,
            job_id=self.job_id,
            spot=self.spot,
            key_name=self.key_name,
            subnet_id=self.subnet_id,
        )
        try:
            instance_id = self._aws(args)
            return instance_id, self.spot
        except RemoteLaunchError as exc:
            if not (
                self.spot
                and self.retry_on_demand_on_spot_failure
                and _is_spot_capacity_error(str(exc))
            ):
                raise
            on_demand_args = build_run_instances_args(
                ami_id=self.ami["ami_id"],
                instance_type=self.instance_type,
                security_group_id=security_group_id,
                region=self.region,
                job_id=self.job_id,
                spot=False,
                key_name=self.key_name,
                subnet_id=self.subnet_id,
            )
            instance_id = self._aws(on_demand_args)
            return instance_id, False

    # -- connectivity (issue #265's SSH/SCP transport) -----------------------

    def get_public_ip(self) -> str:
        """Wait for the provisioned instance to reach ``running`` and return
        its public IP address.

        Additive to :meth:`provision` rather than folded into it: existing
        callers of :meth:`provision` (and its test suite) are unaffected --
        this method is opt-in, called by the ``remote`` `klt sim` backend's
        SSH/SCP transport (#265) only once it actually needs to open a
        connection. Uses the same injectable ``aws_runner`` :meth:`provision`
        does, so no real ``aws`` CLI call happens in a test that never calls
        this method.

        Raises :class:`RemoteLaunchError` if :meth:`provision` has not
        succeeded yet, or if the instance has no public IP address (a
        private-subnet instance is not supported by the SSH/SCP transport --
        see the design note's "Network posture", which assumes direct SSH
        reachability from the launcher).
        """
        if self.instance_id is None:
            raise RemoteLaunchError(
                "provision() must succeed before get_public_ip() can be called"
            )
        self._aws(
            [
                "ec2",
                "wait",
                "instance-running",
                "--region",
                self.region,
                "--instance-ids",
                self.instance_id,
            ]
        )
        ip = self._aws(
            [
                "ec2",
                "describe-instances",
                "--region",
                self.region,
                "--instance-ids",
                self.instance_id,
                "--query",
                "Reservations[0].Instances[0].PublicIpAddress",
                "--output",
                "text",
            ]
        ).strip()
        if not ip or ip == "None":
            raise RemoteLaunchError(
                f"instance {self.instance_id} has no public IP address -- "
                "the SSH/SCP transport requires direct SSH reachability "
                "(see the design note's Network posture section)"
            )
        return ip

    # -- teardown (mechanism (a)'s explicit call) ---------------------------

    def terminate(self) -> None:
        """Call ``terminate-instances`` for this job's instance, and clean up
        the security group this launcher created (if any). Idempotent and
        safe to call multiple times or when :meth:`provision` never
        succeeded (nothing to do) -- this is exactly the "normal completion,
        exception, or caught signal all reach the same teardown call" path
        guardrail mechanics SS3(a) requires.

        Never raises on the security-group cleanup step failing (best-effort
        -- an orphaned, harmless-when-empty security group is a much smaller
        problem than an exception here masking the fact that the instance
        itself *was* torn down); the instance-terminate call's own failure
        does raise, since a caller needs to know teardown may not have
        happened.
        """
        if self.instance_id is not None:
            self._aws(build_terminate_instances_args(self.instance_id, self.region))
            self.instance_id = None

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


class RemoteLauncherInterrupted(Exception):
    """Raised from :meth:`RemoteLauncher._handle_signal` when the launcher
    process receives SIGINT/SIGTERM while a job is in flight, so the
    ``with RemoteLauncher(...) as launcher:`` block's ``__exit__`` runs the
    same guaranteed :meth:`RemoteLauncher.terminate` path a normal exception
    would (see :class:`RemoteLauncher`'s docstring, mechanism (a))."""


def _is_spot_capacity_error(message: str) -> bool:
    return any(marker in message for marker in _SPOT_CAPACITY_ERROR_MARKERS)
