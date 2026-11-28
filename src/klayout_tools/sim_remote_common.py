"""Helpers shared by the ``remote`` (EC2) backend's dispatch paths --
``sim_remote._run_remote`` (one host for the whole corner matrix),
``sim_remote._run_remote_fleet`` (one host per shard), and
``sim_decks._run_decks_remote`` (one host for a deck list) -- so a deck-list
request is validated, launched, waited for, and reported exactly like a
corner request (issue #8127; ``sim_decks`` had reached into ``sim_remote``'s
private SSH-wait constants for want of a shared helper).

Error messages and ``environment.remote`` keys built here are part of the
JSON contract (``docs/json-contract.md``); change them here, once.

This is its own module rather than a slice of ``sim_remote`` because
``sim_remote`` sits at its per-file size ceiling
(``scripts/check_file_size_baseline.py``) and these helpers together take
more lines than the copies they replace inside that one file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import remote_transport
from .sim_errors import SimError

if TYPE_CHECKING:
    from .remote_launcher import RemoteLauncher

#: Default idle-poll interval for :func:`remote_transport.wait_for_ssh` when
#: called from :func:`wait_for_remote_ssh` -- kept short so tests exercising
#: a timeout don't stall, while still reasonable against real EC2 boot times.
_REMOTE_SSH_POLL_INTERVAL_S = 5.0

#: Default overall SSH-readiness budget. Originally 240s per the design
#: note's documented 1-3 minute spin-up estimate plus slack (decision 3);
#: raised to 600s after the first live run observed cold boots (AMI
#: first-boot cloud-init, not just instance ``running``) taking longer than
#: that budget on some instance types/regions -- see
#: docs/design/remote-sim-backend-spike.md. Still overridable per-request via
#: ``remote.ssh_ready_timeout_s``.
_REMOTE_SSH_READY_TIMEOUT_S = 600.0


def require_remote_config(
    request: dict[str, Any], *, fleet: bool = False
) -> tuple[dict[str, Any], Any, str, str]:
    """Validate the ``remote``-backend request config and return
    ``(remote_spec, pdk, ssh_key_path, ssh_user)``: ``models.pdk`` selects
    the baked-AMI PDK and ``remote.ssh_key_path`` pairs with
    ``remote.key_name``, a missing one raising :class:`SimError` with the
    same message on every path. ``fleet=True`` also requires
    ``remote.region``, checked between the two, and scopes the key
    requirement to every provisioned instance."""
    remote_spec = request.get("remote") or {}
    pdk = (request.get("models") or {}).get("pdk")
    if not pdk:
        raise SimError(
            "backend 'remote' requires request.models.pdk (selects which "
            "baked-AMI PDK to provision -- see "
            "remote_launcher.SUPPORTED_PDKS and docs/cli/sim.md)"
        )
    if fleet and not remote_spec.get("region"):
        raise SimError("backend 'remote' requires request.remote.region")
    ssh_key_path = remote_spec.get("ssh_key_path")
    if not ssh_key_path:
        instances = "every" if fleet else "the"
        raise SimError(
            "backend 'remote' requires request.remote.ssh_key_path (local "
            "private key matching request.remote.key_name, used to SSH/SCP "
            f"into {instances} provisioned instance)"
        )
    ssh_user = remote_spec.get("ssh_user") or remote_transport.DEFAULT_SSH_USER
    return remote_spec, pdk, ssh_key_path, ssh_user


def build_remote_launcher(
    launcher_cls: type[RemoteLauncher],
    *,
    pdk: str,
    unit_count: int,
    job_id: str,
    remote_spec: dict[str, Any],
) -> RemoteLauncher:
    """Build the one-host ``RemoteLauncher`` from ``request.remote`` for
    ``unit_count`` units (corners or decks). ``launcher_cls`` is an argument
    so each caller resolves the class on its own module namespace at call
    time, where the tests monkeypatch it (``sim.RemoteLauncher``,
    ``sim_decks.RemoteLauncher``). ``region`` is forwarded as the request
    carries it: only the fleet path requires it, so it may be ``None``."""
    return launcher_cls(
        region=remote_spec.get("region"),  # type: ignore[arg-type]
        pdk=pdk,
        corner_count=unit_count,
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


def wait_for_remote_ssh(
    public_ip: str, *, ssh_user: str, ssh_key_path: str, remote_spec: dict[str, Any]
) -> None:
    """Block until the provisioned host at ``public_ip`` accepts SSH, under
    the per-request ``remote.ssh_ready_timeout_s`` override (else the
    default readiness budget) -- the one :func:`remote_transport.wait_for_ssh`
    call every remote path shares."""
    remote_transport.wait_for_ssh(
        public_ip,
        user=ssh_user,
        identity_file=ssh_key_path,
        timeout_s=remote_spec.get("ssh_ready_timeout_s", _REMOTE_SSH_READY_TIMEOUT_S),
        poll_interval_s=_REMOTE_SSH_POLL_INTERVAL_S,
    )


def build_remote_environment(
    info: dict[str, Any],
    *,
    spin_up_s: float | None = None,
    attempts: int | None = None,
    strict: bool = True,
) -> dict[str, Any]:
    """The ``environment.remote`` block for one provisioned host: the
    instance metadata every remote path reports, then ``spin_up_s`` (a host
    this process provisioned) or ``attempts`` (a fleet shard) when given.
    ``strict=False`` reads a fleet shard's possibly-absent ``environment``
    leniently, a missing key reporting ``None``; a directly provisioned host
    must carry every key."""
    keys = (
        "provider",
        "region",
        "instance_type",
        "instance_id",
        "spot",
        "estimated_hourly_cost_usd",
        "ami_id",
        "pdk_snapshot",
    )
    if strict:
        environment = {key: info[key] for key in keys}
    else:
        environment = {key: info.get(key) for key in keys}
    if spin_up_s is not None:
        environment["spin_up_s"] = spin_up_s
    if attempts is not None:
        environment["attempts"] = attempts
    return environment
