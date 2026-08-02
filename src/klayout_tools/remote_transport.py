"""SSH/SCP push-then-pull transport for the ``remote`` `klt sim` backend.

Implements the Phase 2 transport decision from
``docs/design/remote-sim-backend-spike.md`` ("Transport: SSH/SCP
push-then-pull by default" -- the credential-minimalism win from #264's
no-IAM-profile default): the launcher (running with the operator's own
AWS/SSH credentials) pushes the netlist and a request-specific copy of the
request document to the instance :mod:`klayout_tools.remote_launcher`
provisioned, invokes the **same** ``local-parallel`` worker-pool code
``sim.py`` already implements (#255) on that box via ``klt sim ... --backend
local-parallel``, and pulls the resulting report/artifacts back over the
same channel. The guest never calls an AWS API (see
``remote_launcher.build_run_instances_args``'s "no IAM instance profile by
default").

Every function here takes an injectable ``runner`` (mirroring
``remote_launcher.AwsRunner``) so :mod:`klayout_tools.sim`'s ``remote``
backend and this module's own test suite never invoke the real ``ssh``/
``scp`` binaries or touch a network socket -- see CLAUDE.md's "headless
always"/"runnable in CI" requirement.

Scope note: this module is transport plumbing only. Corner expansion,
ordering, measurement extraction, and pass/fail classification are never
reimplemented here -- they live entirely in the ``klt sim`` invocation that
runs *on the remote host* (the same ``sim.py`` module, unmodified), per
decision 5's "same code path" guarantee. This module's job is limited to
"get the inputs there, get the report/artifacts back."
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Any

#: Default SSH login user for the baked Ubuntu-based AMI (see
#: ``scripts/aws/build-remote-sim-ami.sh``, which builds from a stock Ubuntu
#: 22.04 base image). Overridable per request via ``remote.ssh_user``.
DEFAULT_SSH_USER = "ubuntu"

#: Job-relative filename the pushed netlist is written to on the remote
#: host -- referenced by the generated remote request's own ``netlist``
#: field (see ``sim._build_remote_request``).
REMOTE_NETLIST_FILENAME = "netlist.cir"

#: Job-relative filename the generated remote request document is written
#: to -- this is the path the remote ``klt sim`` invocation itself is
#: pointed at.
REMOTE_REQUEST_FILENAME = "request.json"

#: Default per-command SSH connect timeout (seconds) -- how long a single
#: SSH attempt waits to establish the TCP+handshake, independent of
#: ``wait_for_ssh``'s own overall retry budget.
DEFAULT_CONNECT_TIMEOUT_S = 10


class RemoteTransportError(Exception):
    """Raised when the SSH/SCP push-then-pull transport itself fails: SSH
    never becomes reachable, a push/pull command fails, or the remote ``klt
    sim`` invocation does not return a parseable report.

    Mirrors ``remote_launcher.RemoteLaunchError``'s "fails loudly, never a
    silent default" role, scoped to the transport rather than provisioning.
    :mod:`klayout_tools.sim`'s ``remote`` backend re-raises this as
    :class:`klayout_tools.sim.SimError` -- a transport failure means no
    corner ever ran, the same "sweep never started" class as an unresolvable
    netlist or model library.
    """


#: Signature of the injectable low-level command runner: full argv
#: (including the leading ``ssh``/``scp`` binary name) plus a timeout, in;
#: the completed process out. Defaults to :func:`_run_subprocess`; tests
#: substitute a fake so no test in this module's own suite (or
#: ``sim.py``'s ``remote``-backend tests) ever shells out for real -- see
#: this module's docstring.
CommandRunner = Callable[[list[str], float], "subprocess.CompletedProcess[str]"]


def _run_subprocess(
    argv: list[str], timeout_s: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)


# --------------------------------------------------------------------------- #
# Remote path conventions
# --------------------------------------------------------------------------- #


def job_dir(user: str, job_id: str) -> str:
    """The per-job remote working directory, e.g. ``/home/ubuntu/<job_id>``."""
    return f"/home/{user}/{job_id}"


def artifacts_root(remote_job_dir: str) -> str:
    """Where a remote ``klt sim`` run writes ``keep_artifacts`` output, given
    it is invoked against ``<remote_job_dir>/request.json``.

    Mirrors ``sim.run_sim``'s own default: "a ``.klt/sim/`` directory next to
    the request file" -- since the pushed request lives at
    ``<remote_job_dir>/request.json``, its default ``artifacts_dir`` is
    ``<remote_job_dir>/.klt/sim``, and the remote invocation is never given
    an explicit ``--outdir`` override, so this default always applies.
    """
    return f"{remote_job_dir}/.klt/sim"


# --------------------------------------------------------------------------- #
# ssh/scp argv construction
# --------------------------------------------------------------------------- #


def _ssh_options(
    *, identity_file: str | None, port: int, connect_timeout: float
) -> list[str]:
    opts = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={int(connect_timeout)}",
    ]
    if identity_file:
        opts += ["-i", identity_file]
    return opts


def build_ssh_argv(
    *,
    host: str,
    remote_command: str,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
) -> list[str]:
    """Build a non-interactive ``ssh ... "<remote_command>"`` argv."""
    options = _ssh_options(
        identity_file=identity_file, port=port, connect_timeout=connect_timeout
    )
    return ["ssh", "-p", str(port)] + options + [f"{user}@{host}", remote_command]


def build_scp_upload_argv(
    local_path: str,
    remote_path: str,
    *,
    host: str,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    recursive: bool = False,
) -> list[str]:
    """Build an ``scp <local_path> user@host:<remote_path>`` argv (push)."""
    argv = ["scp", "-P", str(port)]
    if recursive:
        argv.append("-r")
    argv += _ssh_options(
        identity_file=identity_file, port=port, connect_timeout=connect_timeout
    )
    argv += [local_path, f"{user}@{host}:{remote_path}"]
    return argv


def build_scp_download_argv(
    remote_path: str,
    local_path: str,
    *,
    host: str,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
    recursive: bool = True,
) -> list[str]:
    """Build an ``scp user@host:<remote_path> <local_path>`` argv (pull)."""
    argv = ["scp", "-P", str(port)]
    if recursive:
        argv.append("-r")
    argv += _ssh_options(
        identity_file=identity_file, port=port, connect_timeout=connect_timeout
    )
    argv += [f"{user}@{host}:{remote_path}", local_path]
    return argv


def _run_checked(
    runner: CommandRunner, argv: list[str], timeout_s: float, label: str
) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(argv, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RemoteTransportError(f"{label} timed out after {timeout_s}s") from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise RemoteTransportError(
            f"{label} failed (exit {result.returncode}): {detail}"
        )
    return result


# --------------------------------------------------------------------------- #
# SSH readiness
# --------------------------------------------------------------------------- #


def wait_for_ssh(
    host: str,
    *,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 180.0,
    poll_interval_s: float = 5.0,
    runner: CommandRunner | None = None,
) -> float:
    """Poll SSH connectivity (``ssh ... true``) until it succeeds or
    ``timeout_s`` elapses. Returns elapsed wall-clock seconds on success --
    the "boot + cloud-init + sshd ready" component of the design note's
    "ready to accept the first corner" ``spin_up_s`` measurement (the
    provisioning-side component, "instance reached ``running``", is
    ``remote_launcher.RemoteLauncher.get_public_ip``'s own wait).

    Raises :class:`RemoteTransportError` if SSH never becomes reachable
    within the budget -- never hangs indefinitely.
    """
    run = runner or _run_subprocess
    started = time.monotonic()
    deadline = started + timeout_s
    last_detail = "no successful connection attempt"

    while True:
        argv = build_ssh_argv(
            host=host,
            user=user,
            identity_file=identity_file,
            port=port,
            connect_timeout=min(poll_interval_s, DEFAULT_CONNECT_TIMEOUT_S),
            remote_command="true",
        )
        try:
            result = run(argv, poll_interval_s + DEFAULT_CONNECT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            result = None

        if result is not None and result.returncode == 0:
            return round(time.monotonic() - started, 3)
        if result is not None:
            last_detail = (result.stderr or "").strip() or last_detail

        if time.monotonic() >= deadline:
            raise RemoteTransportError(
                f"timed out waiting for SSH on {host} after {timeout_s}s: {last_detail}"
            )
        time.sleep(poll_interval_s)


# --------------------------------------------------------------------------- #
# Push -> run -> pull
# --------------------------------------------------------------------------- #


def push_job(
    *,
    host: str,
    remote_job_dir: str,
    local_netlist_path: str,
    remote_request: dict[str, Any],
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 60.0,
    runner: CommandRunner | None = None,
) -> None:
    """Create ``remote_job_dir`` and push the netlist plus the generated
    remote request document into it.

    Only the netlist and the (kilobytes-scale) request document are pushed
    per job -- per decision 4, the PDK model library is already baked into
    the AMI, never fetched/pushed per job.
    """
    run = runner or _run_subprocess

    mkdir_argv = build_ssh_argv(
        host=host,
        user=user,
        identity_file=identity_file,
        port=port,
        remote_command=f"mkdir -p {shlex.quote(remote_job_dir)}",
    )
    _run_checked(run, mkdir_argv, timeout_s, "remote mkdir")

    netlist_dest = f"{remote_job_dir}/{REMOTE_NETLIST_FILENAME}"
    scp_netlist_argv = build_scp_upload_argv(
        local_netlist_path,
        netlist_dest,
        host=host,
        user=user,
        identity_file=identity_file,
        port=port,
    )
    _run_checked(run, scp_netlist_argv, timeout_s, "scp netlist push")

    tmp_handle = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    )
    try:
        json.dump(remote_request, tmp_handle)
        tmp_handle.close()
        request_dest = f"{remote_job_dir}/{REMOTE_REQUEST_FILENAME}"
        scp_request_argv = build_scp_upload_argv(
            tmp_handle.name,
            request_dest,
            host=host,
            user=user,
            identity_file=identity_file,
            port=port,
        )
        _run_checked(run, scp_request_argv, timeout_s, "scp request push")
    finally:
        os.unlink(tmp_handle.name)


def run_remote_sim(
    *,
    host: str,
    remote_job_dir: str,
    timeout_s: float,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    runner: CommandRunner | None = None,
) -> dict[str, Any]:
    """SSH-invoke ``klt sim request.json --backend local-parallel --format
    json`` in ``remote_job_dir`` and parse its stdout as the report JSON.

    This is the literal reuse point: the code that expands corners, fans
    them across a worker pool, extracts measurements, and classifies
    pass/fail is ``sim.py``'s own ``local-parallel`` backend, executing on
    the provisioned box exactly as it would for a local caller -- nothing
    here re-implements any of that logic (see this module's docstring and
    decision 5).

    ``klt sim``'s own exit codes 0 (pass)/3 (measurement failure)/4 (corner
    error) all mean the sweep ran and produced a report (see
    ``docs/cli/sim.md``'s exit-code table) -- only some other exit code (a
    ``SimError`` on the remote side, an ``ssh`` connection failure, ``klt``
    missing on ``PATH``, ...) means no report exists to parse, and raises
    :class:`RemoteTransportError`.
    """
    run = runner or _run_subprocess
    remote_command = (
        f"cd {shlex.quote(remote_job_dir)} && "
        f"klt sim {REMOTE_REQUEST_FILENAME} --backend local-parallel --format json"
    )
    argv = build_ssh_argv(
        host=host,
        user=user,
        identity_file=identity_file,
        port=port,
        remote_command=remote_command,
    )
    try:
        result = run(argv, timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise RemoteTransportError(
            f"remote 'klt sim' timed out after {timeout_s}s"
        ) from exc

    if result.returncode not in (0, 3, 4):
        raise RemoteTransportError(
            f"remote 'klt sim' failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteTransportError(
            f"remote 'klt sim' did not return valid JSON on stdout: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise RemoteTransportError("remote 'klt sim' returned non-object JSON")
    return report


def pull_artifacts(
    *,
    host: str,
    remote_job_dir: str,
    local_artifacts_dir: str,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 180.0,
    runner: CommandRunner | None = None,
) -> None:
    """Pull the remote run's ``keep_artifacts`` tree (logs/rawfiles/waveform
    JSON) down into ``local_artifacts_dir``, over the same SSH/SCP channel.

    Stages the pull into a fresh temp directory first, then copies its
    contents into ``local_artifacts_dir`` -- avoids depending on ``scp``'s
    destination-exists-or-not rename semantics, which differ depending on
    whether ``local_artifacts_dir`` already exists.
    """
    run = runner or _run_subprocess
    remote_source = artifacts_root(remote_job_dir)
    staging_dir = tempfile.mkdtemp(prefix="klt-remote-sim-pull-")
    try:
        staged_target = os.path.join(staging_dir, "pulled")
        scp_argv = build_scp_download_argv(
            remote_source,
            staged_target,
            host=host,
            user=user,
            identity_file=identity_file,
            port=port,
            recursive=True,
        )
        _run_checked(run, scp_argv, timeout_s, "scp artifacts pull")

        if os.path.isdir(staged_target):
            os.makedirs(local_artifacts_dir, exist_ok=True)
            shutil.copytree(staged_target, local_artifacts_dir, dirs_exist_ok=True)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)


def cleanup_job(
    *,
    host: str,
    remote_job_dir: str,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 30.0,
    runner: CommandRunner | None = None,
) -> None:
    """Best-effort ``rm -rf`` of the per-job remote working directory.

    Never raises -- the instance is about to be torn down by
    ``RemoteLauncher`` regardless (see its guaranteed-teardown guarantee),
    so a failure here (already-dead SSH session, network blip) is not worth
    surfacing as a run failure once the report/artifacts are already safely
    collected.
    """
    run = runner or _run_subprocess
    argv = build_ssh_argv(
        host=host,
        user=user,
        identity_file=identity_file,
        port=port,
        remote_command=f"rm -rf {shlex.quote(remote_job_dir)}",
    )
    try:
        run(argv, timeout_s)
    except (subprocess.TimeoutExpired, OSError):
        pass
