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
- **The per-job security group's teardown gets the same belt-and-braces
  discipline (issue #617)**: (a) :meth:`RemoteLauncher.terminate` retries its
  ``delete-security-group`` call with bounded backoff (see
  :data:`_SECURITY_GROUP_DELETE_ATTEMPTS`) rather than the single attempt
  that used to reliably lose the race against ``terminate-instances``'s
  asynchronous ENI release; (b) :func:`reap_orphaned_security_groups` is an
  independent backstop -- run opportunistically from
  :meth:`RemoteLauncher._resolve_security_group` before a new job creates its
  own group, and separately callable as a standalone maintenance routine --
  that lists and deletes any ``klt-remote-sim-klt-sim-*`` group with no
  attached network interface, closing the gap for a run that never reaches
  ``terminate()`` at all (killed process, interrupted provisioning).

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
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import Any

#: Step 2 of :func:`load_ami_manifest`'s 4-step resolution order -- an
#: explicit manifest path, consulted when no ``manifest_path`` argument
#: (``request.remote.ami_manifest``, plumbed through by ``sim.py``'s
#: ``_run_remote``) is given. Mirrors ``pdk.py``'s ``$PDK_ROOT`` convention
#: (issue #370).
AMI_MANIFEST_ENV_VAR = "KLT_AMI_MANIFEST"

#: Step 3 of :func:`load_ami_manifest`'s resolution order -- where
#: ``scripts/aws/build-remote-sim-ami.sh`` additionally writes an
#: operator-built AMI's manifest (alongside the repo checkout's own
#: ``data/`` copy) so it is usable from *any* ``klt`` install on that
#: machine immediately, no release required (issue #370 -- a tool-installed
#: ``klt`` resolves :data:`DEFAULT_MANIFEST_PATH` inside its own installed
#: package-data dir, never an operator's checkout).
USER_MANIFEST_PATH: Path = Path(
    "~/.config/klt/remote-sim-ami-manifest.json"
).expanduser()

#: Step 4 (final fallback) of :func:`load_ami_manifest`'s resolution order:
#: <repo root>/data/remote-sim-ami-manifest.json -- this file lives at
#: <repo root>/src/klayout_tools/remote_launcher.py. Mirrors kb.py's
#: DEFAULT_KB_ROOT convention: walk up from this module's own location so the
#: module works the same from an installed wheel's source checkout or a repo
#: clone; every function also accepts an explicit path override for tests.
#: For a tool-installed ``klt`` (uv tool / pipx / pip) this resolves inside
#: the installed package's own data dir -- the historically sole location,
#: now the last-resort fallback behind :data:`USER_MANIFEST_PATH` and
#: :data:`AMI_MANIFEST_ENV_VAR`.
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

#: PDK **families**, used to map a request's ``models.pdk`` -- which is a local
#: *variant* name -- onto the key the AMI manifest publishes under. Longest
#: first, so a prefix match can never pick the shorter of two overlapping
#: families. Same convention as ``pdk._CORNER_PDK_FAMILIES`` and
#: ``pdk_models._pdk_variant_family``; duplicated rather than imported for the
#: reason those two already duplicate each other -- see #615.
_AMI_PDK_FAMILIES: tuple[str, ...] = ("gf180mcu", "sky130")

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

#: Bounded retry window for :meth:`RemoteLauncher.terminate`'s
#: ``delete-security-group`` call (issue #617, implementation option 2):
#: ``terminate-instances`` is asynchronous, so the ENI backing this job's
#: security group is typically still attached for a few seconds after the
#: call returns -- AWS refuses ``DeleteSecurityGroup`` with a
#: dependency-violation error until the instance actually reaches
#: ``terminated`` and releases it. A single attempt (the pre-#617 behavior)
#: reliably loses this race; a handful of short-backoff retries catches the
#: common case cheaply, without paying the 30-60s+ latency of an explicit
#: ``ec2 wait instance-terminated`` (the rejected option 1) on every job's
#: teardown.
_SECURITY_GROUP_DELETE_ATTEMPTS = 5

#: Linear backoff between :data:`_SECURITY_GROUP_DELETE_ATTEMPTS` retries, in
#: seconds. 5 attempts x 2s = up to 10s of extra teardown latency in the
#: worst case -- a small, bounded cost against the 30-60s+ of option 1.
_SECURITY_GROUP_DELETE_BACKOFF_S = 2.0

#: Name prefix per-job security groups are created under (see
#: :meth:`RemoteLauncher._resolve_security_group`'s
#: ``--group-name klt-remote-sim-{job_id}``, with ``job_id`` always
#: ``klt-sim-<hex>`` per ``sim.py``'s ``_run_remote``). Deliberately includes
#: the trailing ``-`` so :func:`reap_orphaned_security_groups`'s default
#: never matches the bare ``klt-remote-sim`` base group -- a long-lived,
#: shared group an operator can reuse via an explicit
#: ``security_group_id``/``remote.security_group_id`` -- which must never be
#: swept up by the reaper.
_PER_JOB_SECURITY_GROUP_NAME_PREFIX = "klt-remote-sim-klt-sim-"


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


def _candidate_manifest_paths(
    manifest_path: str | Path | None,
) -> list[tuple[Path, str]]:
    """Build the ordered ``(path, resolved_via)`` search candidates for the
    AMI manifest, mirroring ``pdk.py``'s ``_candidate_roots`` "first hit
    wins" shape (issue #370).

    Resolution order:

    1. ``manifest_path`` -- an explicit override (``request.remote.ami_manifest``,
       plumbed through by ``sim.py``'s ``_run_remote``). Disables the rest of
       the search, same as ``pdk.find_pdk``'s ``root=`` parameter: an
       explicit path that does not exist is an error, not a silent fallback
       (the module's own "no silent default" discipline -- a wrong AMI
       silently used is worse than a loud failure).
    2. :data:`AMI_MANIFEST_ENV_VAR` (``$KLT_AMI_MANIFEST``), when (1) is not
       given.
    3. :data:`USER_MANIFEST_PATH` -- where
       ``scripts/aws/build-remote-sim-ami.sh`` additionally writes an
       operator-built AMI's manifest, usable by any ``klt`` install on the
       machine immediately, no release required.
    4. :data:`DEFAULT_MANIFEST_PATH` -- the packaged default (historically
       the sole location; a tool-installed ``klt`` resolves this inside its
       own installed package-data dir, never an operator's checkout -- see
       (3) for the fix).
    """
    if manifest_path is not None:
        return [(Path(manifest_path), "explicit ami_manifest override")]

    candidates: list[tuple[Path, str]] = []
    env_value = os.environ.get(AMI_MANIFEST_ENV_VAR)
    if env_value:
        candidates.append(
            (
                Path(env_value).expanduser(),
                f"${AMI_MANIFEST_ENV_VAR} environment variable",
            )
        )
    candidates.append((USER_MANIFEST_PATH, f"user config: {USER_MANIFEST_PATH}"))
    candidates.append(
        (DEFAULT_MANIFEST_PATH, f"packaged default: {DEFAULT_MANIFEST_PATH}")
    )
    return candidates


def _manifest_not_found_message(candidates: list[tuple[Path, str]]) -> str:
    """Build the actionable "AMI manifest not found" message -- mirrors
    ``pdk._not_found_message``'s "tried: X (via), Y (via)" shape (issue
    #370) so every location searched is debuggable, not just the one this
    module happened to check first."""
    tried = ", ".join(f"{via} ({path})" for path, via in candidates)
    return (
        f"AMI manifest not found. Searched, in order: {tried}. Run "
        "scripts/aws/build-remote-sim-ami.sh to build and publish one, or "
        "point request.remote.ami_manifest (or $KLT_AMI_MANIFEST) at an "
        "existing manifest."
    )


def load_ami_manifest(manifest_path: str | Path | None = None) -> dict[str, Any]:
    """Load the versioned AMI manifest (see ``scripts/aws/build-remote-sim-ami.sh``,
    the build/refresh pipeline that produces it), resolving its path via
    :func:`_candidate_manifest_paths`'s 4-step "first hit wins" order.

    Raises :class:`RemoteLaunchError` if no candidate manifest exists, or the
    first one found is unreadable/malformed -- resolving an AMI to run real
    corners against is exactly the kind of cost-relevant fact the design
    note's "no silent default" discipline applies to (a wrong or stale AMI
    silently used is worse than a loud failure).
    """
    candidates = _candidate_manifest_paths(manifest_path)
    path, resolved_via = next(
        ((p, via) for p, via in candidates if p.is_file()), (None, None)
    )
    if path is None:
        raise RemoteLaunchError(_manifest_not_found_message(candidates))
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteLaunchError(
            f"AMI manifest at {path} ({resolved_via}) is not valid JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or "images" not in manifest:
        raise RemoteLaunchError(
            f"AMI manifest at {path} ({resolved_via}) is missing a top-level "
            "'images' array"
        )
    return manifest


def ami_pdk_key(pdk: str) -> str:
    """Map a request's ``models.pdk`` to the key the AMI manifest publishes
    under, accepting the *local variant* name the same request uses on the
    ``local`` backend.

    ``models.pdk`` has one meaning everywhere else in ``klt sim``: the PDK
    variant directory to resolve locally (``sim._resolve_model_lib`` passes it
    straight to ``pdk.find_pdk(variant=...)``). The AMI manifest, however, is
    keyed by whatever ``build-remote-sim-ami.sh --pdk`` was given, which for
    gf180mcu is the bare *family*.

    For sky130 the two coincide -- ``sky130A`` is both a real variant
    directory and the manifest key -- which is why #615 went unnoticed through
    Epic #253's validation. gf180mcu cannot coincide: its variants are
    ``gf180mcuA``-``D`` and its manifest key is ``gf180mcu``, so before this
    mapping existed there was no value of ``models.pdk`` that both resolved
    locally *and* found an AMI.

    Exact matches win, so an explicit manifest key still works; otherwise the
    variant is reduced to its family by prefix.
    """
    if pdk in SUPPORTED_PDKS:
        return pdk
    for family in _AMI_PDK_FAMILIES:
        if pdk.startswith(family):
            if family in SUPPORTED_PDKS:
                return family
            break
    raise RemoteLaunchError(
        f"unsupported PDK '{pdk}' for the remote backend "
        f"(supported: {', '.join(SUPPORTED_PDKS)}). `models.pdk` carries the "
        "local variant name and is reduced to its family to find the AMI "
        "(e.g. 'gf180mcuC' -> 'gf180mcu'), so a variant whose family has no "
        "published AMI is unsupported even when it resolves locally."
    )


def resolve_ami(
    pdk: str, region: str, manifest_path: str | Path | None = None
) -> dict[str, str]:
    """Resolve ``pdk``/``region`` to the manifest's most recently built
    published AMI, returning ``{"ami_id": ..., "pdk_snapshot": ...,
    "ngspice_version": ..., "built_at": ...}``.

    ``pdk`` is the request's ``models.pdk`` -- a *local variant* name -- and is
    mapped to the manifest key by :func:`ami_pdk_key` (``"gf180mcuC"`` ->
    ``"gf180mcu"``), so the same value works on both backends (#615).

    Raises :class:`RemoteLaunchError` for a ``pdk`` whose family has no
    published AMI, or when the manifest has no published entry for the
    ``(key, region)`` pair -- never silently substitutes a different region's
    or a stale AMI.
    """
    key = ami_pdk_key(pdk)

    manifest = load_ami_manifest(manifest_path)
    candidates = [
        image
        for image in manifest.get("images", [])
        if image.get("pdk") == key and image.get("region") == region
    ]
    if not candidates:
        # Name the manifest key as well as the requested variant when they
        # differ -- otherwise "no published AMI for pdk='gf180mcu'" reads as a
        # typo to someone who wrote 'gf180mcuC' in the request.
        asked = f"{pdk!r}" if key == pdk else f"{pdk!r} (manifest key {key!r})"
        raise RemoteLaunchError(
            f"no published AMI for pdk={asked} region={region!r} in the "
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


# --------------------------------------------------------------------------- #
# Security-group teardown: bounded retry + independent reaper (issue #617)
# --------------------------------------------------------------------------- #

#: Signature of the injectable sleep function used between
#: :data:`_SECURITY_GROUP_DELETE_ATTEMPTS` retries. Defaults to
#: :func:`time.sleep`; tests inject a no-op so the retry loop's worst case
#: doesn't cost real wall-clock time.
SleepFn = Callable[[float], None]


def _delete_security_group(
    aws: AwsRunner,
    region: str,
    group_id: str,
    *,
    attempts: int = _SECURITY_GROUP_DELETE_ATTEMPTS,
    backoff_s: float = _SECURITY_GROUP_DELETE_BACKOFF_S,
    sleep_fn: SleepFn = time.sleep,
) -> bool:
    """Best-effort ``delete-security-group``, retried up to ``attempts``
    times with ``backoff_s`` linear backoff between attempts, instead of the
    single shot :meth:`RemoteLauncher.terminate` used before #617.

    Returns whether the delete ultimately succeeded. Never raises -- matches
    the existing "must not raise" discipline for this call (an orphaned,
    harmless-when-empty security group is a much smaller problem than an
    exception here masking the fact that the instance itself *was* torn
    down); callers that need to know about a still-attached group after
    exhausting the window rely on this return value or on
    :func:`reap_orphaned_security_groups` catching it on a later run.
    """
    for attempt in range(1, attempts + 1):
        try:
            aws(
                [
                    "ec2",
                    "delete-security-group",
                    "--region",
                    region,
                    "--group-id",
                    group_id,
                ]
            )
            return True
        except RemoteLaunchError:
            if attempt == attempts:
                return False
            sleep_fn(backoff_s)
    return False  # pragma: no cover -- loop always returns above


def reap_orphaned_security_groups(
    region: str,
    *,
    aws_runner: AwsRunner | None = None,
    name_prefix: str = _PER_JOB_SECURITY_GROUP_NAME_PREFIX,
    sleep_fn: SleepFn = time.sleep,
) -> list[str]:
    """Independent backstop for per-job security-group teardown
    (implementation option 3, issue #617): list every security group named
    ``<name_prefix>*`` with **no attached network interface**, delete each
    (bounded-retried the same way :meth:`RemoteLauncher.terminate` is), and
    return the group IDs actually deleted.

    This closes the gap :meth:`RemoteLauncher.terminate`'s own retry cannot:
    a run that never reaches ``terminate()`` at all -- the launcher process
    is killed uncatchably (``SIGKILL``), or ``_resolve_security_group``
    creates the group but the following ``run-instances`` call never
    completes -- leaks a group with nothing left to retry the delete for.
    Callable standalone as a maintenance routine, and called opportunistically
    from :meth:`RemoteLauncher._resolve_security_group` before a new job
    creates its own group (see that method).

    A group still attached to a network interface (in use by a running, or a
    `terminating`-but-not-yet-released, instance) is left alone -- the same
    dependency-violation condition that makes a single-shot
    ``delete-security-group`` fail, checked up front here instead of
    discovered via a failed delete, so a group genuinely still in use is
    never even attempted.

    Never raises for an individual group's failed delete (best-effort, folded
    into the returned list simply omitting it); the ``describe-*`` calls
    themselves *do* propagate :class:`RemoteLaunchError` -- a caller invoking
    this directly as a maintenance tool should see a listing failure, while
    the opportunistic call site inside :meth:`RemoteLauncher.provision`
    catches and swallows it so a reaper hiccup never blocks provisioning.
    """
    aws = aws_runner if aws_runner is not None else _run_aws_cli

    raw_ids = aws(
        [
            "ec2",
            "describe-security-groups",
            "--region",
            region,
            "--filters",
            f"Name=group-name,Values={name_prefix}*",
            "--query",
            "SecurityGroups[].GroupId",
            "--output",
            "text",
        ]
    )
    candidate_ids = raw_ids.split()

    deleted: list[str] = []
    for group_id in candidate_ids:
        raw_enis = aws(
            [
                "ec2",
                "describe-network-interfaces",
                "--region",
                region,
                "--filters",
                f"Name=group-id,Values={group_id}",
                "--query",
                "NetworkInterfaces[].NetworkInterfaceId",
                "--output",
                "text",
            ]
        )
        if raw_enis.strip():
            continue  # still attached -- not an orphan, leave it alone

        if _delete_security_group(aws, region, group_id, sleep_fn=sleep_fn):
            deleted.append(group_id)

    return deleted


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

    ``launcher_cidr`` accepts one CIDR; a caller behind multiple public
    egress IPs (e.g. an office VPN plus a CI runner's own NAT IP) can also
    pass ``launcher_cidrs=[...]`` -- either alone, or alongside
    ``launcher_cidr``, in which case the two are unioned (see
    :meth:`_resolve_launcher_cidrs`), one ingress rule per distinct CIDR.

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
        launcher_cidrs: list[str] | None = None,
        security_group_id: str | None = None,
        key_name: str | None = None,
        subnet_id: str | None = None,
        # AMI manifest override (typically request.remote.ami_manifest,
        # plumbed through by sim.py's _run_remote). None defers to
        # load_ami_manifest's/resolve_ami's own 4-step resolution order
        # ($KLT_AMI_MANIFEST -> user-scope -> packaged default) -- see
        # _candidate_manifest_paths (issue #370).
        manifest_path: str | Path | None = None,
        aws_runner: AwsRunner | None = None,
        retry_on_demand_on_spot_failure: bool = True,
        # issue #617: bounded-retry the security-group delete in terminate()
        # (sleep_fn is the injectable backoff sleep -- tests pass a no-op so
        # the retry window costs no real wall-clock time), and opportunistically
        # reap other jobs' orphaned groups before creating this one's own (set
        # False to disable, e.g. a caller that already reaps on its own
        # cadence and wants to avoid the extra describe-* calls per job).
        sleep_fn: SleepFn | None = None,
        reap_orphans_on_provision: bool = True,
    ) -> None:
        self.region = region
        self.pdk = pdk
        self.corner_count = corner_count
        self.job_id = job_id
        self.threads_per_corner = threads_per_corner
        self.spot = spot
        self.max_hourly_cost_usd = max_hourly_cost_usd
        self.launcher_cidr = launcher_cidr
        self.launcher_cidrs = launcher_cidrs
        self.security_group_id = security_group_id
        self.key_name = key_name
        self.subnet_id = subnet_id
        self.manifest_path = manifest_path
        self._aws = aws_runner if aws_runner is not None else _run_aws_cli
        self.retry_on_demand_on_spot_failure = retry_on_demand_on_spot_failure
        self._sleep: SleepFn = sleep_fn if sleep_fn is not None else time.sleep
        self.reap_orphans_on_provision = reap_orphans_on_provision

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

    def _resolve_launcher_cidrs(self) -> list[str]:
        """Combine ``launcher_cidr`` and ``launcher_cidrs`` into one ordered,
        de-duplicated list -- both knobs can be given together (multi-egress
        callers, e.g. an office VPN plus a CI runner's NAT IP), in which case
        they are unioned rather than one silently overriding the other:
        ``launcher_cidr`` (if set) comes first, followed by any entries in
        ``launcher_cidrs`` not already present. A caller that only ever sets
        one of the two sees identical behavior to before this knob existed.
        """
        cidrs: list[str] = []
        if self.launcher_cidr:
            cidrs.append(self.launcher_cidr)
        for cidr in self.launcher_cidrs or ():
            if cidr not in cidrs:
                cidrs.append(cidr)
        return cidrs

    def _resolve_security_group(self) -> str:
        if self.security_group_id:
            return self.security_group_id
        cidrs = self._resolve_launcher_cidrs()
        if not cidrs:
            raise RemoteLaunchError(
                "either security_group_id or launcher_cidr/launcher_cidrs is "
                "required to provision -- the design note's SSH-inbound-only "
                "network posture needs one of these to build the ingress "
                "rule(s)"
            )

        if self.reap_orphans_on_provision:
            # Independent backstop (issue #617, implementation option 3):
            # opportunistically clean up other jobs' orphaned groups before
            # this job creates its own, so a run that never reached
            # terminate() (killed process, interrupted provisioning) doesn't
            # accumulate indefinitely. Best-effort -- a reaper hiccup (e.g. a
            # permissions gap on describe-network-interfaces) must never
            # block this job's own provisioning.
            try:
                reap_orphaned_security_groups(
                    self.region, aws_runner=self._aws, sleep_fn=self._sleep
                )
            except RemoteLaunchError:
                pass

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
        # One ingress rule per CIDR -- build_security_group_ingress_args
        # itself stays single-CIDR; multi-egress support is a loop at this
        # call site rather than a per-CIDR variant of that helper.
        for cidr in cidrs:
            self._aws(build_security_group_ingress_args(cidr, group_id))
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

        Before the fallback's own ``run-instances`` call, the on-demand
        estimate is re-checked against ``max_hourly_cost_usd`` -- the
        up-front cost gate in :meth:`provision` only ever validated the
        *requested* (spot) rate, and on-demand is reliably pricier, so a spot
        failure must not silently walk past a ceiling the caller set
        specifically to bound spend. A failure here raises
        :class:`RemoteLaunchError` with no further billable call made,
        distinguishing this "fallback leg refused by the cost gate" failure
        from :meth:`provision`'s own up-front spot-side cost-gate rejection
        so the caller can tell which leg failed.
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

            on_demand_hourly, _approximate = estimate_cost(
                self.instance_type, spot=False
            )
            if (
                self.max_hourly_cost_usd is not None
                and on_demand_hourly > self.max_hourly_cost_usd
            ):
                raise RemoteLaunchError(
                    f"spot capacity failure ({exc}) would normally fall back "
                    f"to on-demand, but the fallback's own cost check "
                    f"refused it: estimated hourly cost ${on_demand_hourly:.4f} "
                    f"for {self.instance_type} (on-demand) exceeds "
                    f"remote.max_hourly_cost_usd=${self.max_hourly_cost_usd:.4f} "
                    "-- refusing the on-demand fallback (no on-demand "
                    "run-instances call was made)"
                ) from exc

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

        The security-group delete is bounded-retried (issue #617; see
        :func:`_delete_security_group`) rather than attempted once --
        ``terminate-instances`` is asynchronous, so the ENI backing this
        group is often still attached immediately after this call returns,
        and a single attempt reliably lost that race. Still best-effort at
        the end of the retry window: an orphaned, harmless-when-empty
        security group is a much smaller problem than an exception here
        masking the fact that the instance itself *was* torn down, and
        :func:`reap_orphaned_security_groups` is the independent backstop
        for whatever the retry window doesn't catch. The instance-terminate
        call's own failure does raise, since a caller needs to know teardown
        may not have happened.
        """
        if self.instance_id is not None:
            self._aws(build_terminate_instances_args(self.instance_id, self.region))
            self.instance_id = None

        if self._created_security_group and self.security_group_id:
            _delete_security_group(
                self._aws,
                self.region,
                self.security_group_id,
                sleep_fn=self._sleep,
            )
            self._created_security_group = False


class RemoteLauncherInterrupted(Exception):
    """Raised from :meth:`RemoteLauncher._handle_signal` when the launcher
    process receives SIGINT/SIGTERM while a job is in flight, so the
    ``with RemoteLauncher(...) as launcher:`` block's ``__exit__`` runs the
    same guaranteed :meth:`RemoteLauncher.terminate` path a normal exception
    would (see :class:`RemoteLauncher`'s docstring, mechanism (a))."""


def _is_spot_capacity_error(message: str) -> bool:
    return any(marker in message for marker in _SPOT_CAPACITY_ERROR_MARKERS)
