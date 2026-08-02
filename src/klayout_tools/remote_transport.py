"""SSH/SCP push-then-pull transport for a remote ``klt`` job -- generalized
in issue #278 (Epic #253 Phase 3) so a future ``extract``/``lvs``/DRC remote
backend can reuse this module directly, per the epic's "job type is a
parameter of the backend" success criterion.

Implements the Phase 2 transport decision from
``docs/design/remote-sim-backend-spike.md`` ("Transport: SSH/SCP
push-then-pull by default" -- the credential-minimalism win from #264's
no-IAM-profile default): the launcher (running with the operator's own
AWS/SSH credentials) pushes a job's input files to the instance
:mod:`klayout_tools.remote_launcher` provisioned, runs one caller-supplied
remote command in the pushed job directory, and pulls a caller-named
artifacts subdirectory back over the same channel. The guest never calls an
AWS API (see ``remote_launcher.build_run_instances_args``'s "no IAM instance
profile by default").

**What is pushed, what runs, and what is collected back are all data**,
carried by :class:`JobDescription` (built from :class:`JobInput`) --
:mod:`klayout_tools.sim`'s ``remote`` backend is the only caller today
(see ``sim._build_remote_job_description``), and it supplies the ``klt
sim ... --backend local-parallel`` command, the pushed netlist/request
files, and the ``.klt/sim`` artifacts subdirectory as *its own* job
description; nothing in this module hard-codes ``klt sim``, a netlist, or a
SPICE report. See ``docs/design/remote-job-description.md`` for the full
contract a future job type implements against.

Every function here takes an injectable ``runner`` (mirroring
``remote_launcher.AwsRunner``) so :mod:`klayout_tools.sim`'s ``remote``
backend and this module's own test suite never invoke the real ``ssh``/
``scp`` binaries or touch a network socket -- see CLAUDE.md's "headless
always"/"runnable in CI" requirement.

Scope note: this module is transport plumbing only. Corner expansion,
ordering, measurement extraction, and pass/fail classification are never
reimplemented here -- for the ``klt sim`` job type they live entirely in the
``klt sim`` invocation that runs *on the remote host* (the same ``sim.py``
module, unmodified), per decision 5's "same code path" guarantee. This
module's job is limited to "get the inputs there, run the command, get the
named output directory back" -- for *any* job description, not just sim's.
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
from dataclasses import dataclass
from typing import Any

#: Default SSH login user for the baked Ubuntu-based AMI (see
#: ``scripts/aws/build-remote-sim-ami.sh``, which builds from a stock Ubuntu
#: 22.04 base image). Overridable per request via ``remote.ssh_user``.
DEFAULT_SSH_USER = "ubuntu"

#: Job-relative filename the pushed netlist is written to on the remote
#: host, for the ``klt sim`` job description -- referenced by the generated
#: remote request's own ``netlist`` field (see ``sim._build_remote_request``).
#: A sim-specific convenience constant, not a module-wide assumption: any
#: :class:`JobInput`'s ``remote_name`` may be any job-relative filename a
#: caller's job description chooses.
REMOTE_NETLIST_FILENAME = "netlist.cir"

#: Job-relative filename the generated remote request document is written
#: to, for the ``klt sim`` job description -- this is the path the remote
#: ``klt sim`` invocation itself is pointed at. Same convenience-constant
#: caveat as :data:`REMOTE_NETLIST_FILENAME`.
REMOTE_REQUEST_FILENAME = "request.json"

#: Default job-relative artifacts subdirectory :func:`artifacts_root`
#: resolves to when a caller doesn't override it -- mirrors ``sim.run_sim``'s
#: own default (a ``.klt/sim/`` directory next to the request file), since
#: the ``klt sim`` job description never passes the remote invocation an
#: explicit ``--outdir`` override. A future job type supplies its own value
#: via ``JobDescription.artifacts_relative_dir``.
DEFAULT_ARTIFACTS_RELATIVE_DIR = ".klt/sim"

#: Default per-command SSH connect timeout (seconds) -- how long a single
#: SSH attempt waits to establish the TCP+handshake, independent of
#: ``wait_for_ssh``'s own overall retry budget.
DEFAULT_CONNECT_TIMEOUT_S = 10


class RemoteTransportError(Exception):
    """Raised when the SSH/SCP push-then-pull transport itself fails: SSH
    never becomes reachable, a push/pull command fails, or the remote job's
    own command does not return the output its :class:`JobDescription`
    promised (parseable JSON on stdout, when ``parse_json_stdout`` is set).

    Mirrors ``remote_launcher.RemoteLaunchError``'s "fails loudly, never a
    silent default" role, scoped to the transport rather than provisioning.
    :mod:`klayout_tools.sim`'s ``remote`` backend re-raises this as
    :class:`klayout_tools.sim.SimError` -- a transport failure means no
    corner ever ran, the same "sweep never started" class as an unresolvable
    netlist or model library.
    """


# --------------------------------------------------------------------------- #
# Generic job description (issue #278 / Epic #253 Phase 3)
# --------------------------------------------------------------------------- #
#
# Everything that used to be "what klt sim pushes / runs / collects" is now
# data a caller builds and passes in, not something push_job/run_remote_job/
# pull_artifacts hard-code. See docs/design/remote-job-description.md for the
# contract a future extract/lvs/DRC remote backend implements against.


@dataclass(frozen=True)
class JobInput:
    """One local file (or inline text payload) :func:`push_job` uploads into
    the remote job directory before :class:`JobDescription`'s ``command``
    runs, landing at ``<remote_job_dir>/<remote_name>``.

    Exactly one of ``local_path`` (an existing file, uploaded directly via
    ``scp``) or ``content`` (a string written to a local temp file, uploaded,
    then removed -- see :func:`push_job`) must be given. ``label`` is a
    short human-readable name used only in a push failure's error message
    (e.g. ``"scp <label> push failed"``) -- it never reaches the remote host.
    """

    remote_name: str
    label: str
    local_path: str | None = None
    content: str | None = None

    def __post_init__(self) -> None:
        if (self.local_path is None) == (self.content is None):
            raise ValueError(
                "JobInput requires exactly one of local_path or content "
                f"(remote_name={self.remote_name!r})"
            )


@dataclass(frozen=True)
class JobDescription:
    """The generic push/run/collect contract :func:`push_job`,
    :func:`run_remote_job`, and :func:`pull_artifacts` execute against --
    the parameterization this module's docstring and
    ``docs/design/remote-job-description.md`` describe.

    - ``inputs``: what :func:`push_job` uploads into the remote job
      directory (the "input payload").
    - ``command``: the shell command :func:`run_remote_job` runs *inside*
      the remote job directory (the "remote command") -- this module always
      ``cd``'s into the job directory first, so ``command`` itself only
      needs to reference job-relative paths (see :class:`JobInput`'s
      ``remote_name``).
    - ``success_exit_codes``: which exit codes from ``command`` mean "ran to
      completion and produced parseable output" rather than "the transport
      itself failed" -- e.g. ``klt sim``'s own 0 (pass)/3 (measurement
      failure)/4 (corner error) all mean a report exists to parse.
    - ``parse_json_stdout``: whether :func:`run_remote_job` should parse
      ``command``'s stdout as a JSON object and return it (the default,
      matching every `klt` verb's own JSON-contract convention) or return
      the raw stdout string unparsed.
    - ``artifacts_relative_dir``: the job-relative directory
      :func:`pull_artifacts` copies back (the "output collection glob/
      manifest", in the simplest form this issue implements: one directory
      tree, not yet a glob pattern -- see the design note's "Open
      questions"). ``None`` means the job produces nothing to pull back.
    - ``label``: a short human-readable name used in :class:`JobDescription`-
      generic error/log messages (e.g. ``"remote '<label>' failed"``);
      never sent to the remote host.
    """

    label: str
    inputs: tuple[JobInput, ...]
    command: str
    success_exit_codes: tuple[int, ...] = (0,)
    parse_json_stdout: bool = True
    artifacts_relative_dir: str | None = None


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


def artifacts_root(
    remote_job_dir: str, relative_dir: str = DEFAULT_ARTIFACTS_RELATIVE_DIR
) -> str:
    """Join ``remote_job_dir`` with a job-relative artifacts directory,
    defaulting to :data:`DEFAULT_ARTIFACTS_RELATIVE_DIR` (the ``klt sim`` job
    description's own ``.klt/sim`` convention -- see that constant's
    docstring). ``relative_dir`` is normally a :class:`JobDescription`'s own
    ``artifacts_relative_dir``, so a future job type's differently-shaped
    output directory resolves the same way without this function hard-coding
    ``klt sim``'s path.
    """
    return f"{remote_job_dir}/{relative_dir}"


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
    job: JobDescription,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 60.0,
    runner: CommandRunner | None = None,
) -> None:
    """Create ``remote_job_dir`` and push every ``job.inputs`` entry into it,
    at ``<remote_job_dir>/<JobInput.remote_name>``.

    A :class:`JobInput` with ``content`` set is written to a local temp file
    first (removed again once its upload completes or fails); one with
    ``local_path`` set is uploaded directly. Only ``job.inputs`` is ever
    pushed -- per decision 4, the PDK model library (or, for a future job
    type, any other large baked dependency) is already on the AMI, never
    fetched/pushed per job.
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

    for item in job.inputs:
        dest = f"{remote_job_dir}/{item.remote_name}"
        if item.local_path is not None:
            scp_argv = build_scp_upload_argv(
                item.local_path,
                dest,
                host=host,
                user=user,
                identity_file=identity_file,
                port=port,
            )
            _run_checked(run, scp_argv, timeout_s, f"scp {item.label} push")
            continue

        suffix = os.path.splitext(item.remote_name)[1] or ".tmp"
        tmp_handle = tempfile.NamedTemporaryFile(
            "w", suffix=suffix, delete=False, encoding="utf-8"
        )
        try:
            assert item.content is not None  # JobInput.__post_init__ guarantees this
            tmp_handle.write(item.content)
            tmp_handle.close()
            scp_argv = build_scp_upload_argv(
                tmp_handle.name,
                dest,
                host=host,
                user=user,
                identity_file=identity_file,
                port=port,
            )
            _run_checked(run, scp_argv, timeout_s, f"scp {item.label} push")
        finally:
            os.unlink(tmp_handle.name)


def run_remote_job(
    *,
    host: str,
    remote_job_dir: str,
    job: JobDescription,
    timeout_s: float,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    runner: CommandRunner | None = None,
) -> Any:
    """SSH-invoke ``job.command`` inside ``remote_job_dir`` and, when
    ``job.parse_json_stdout`` is true (the default), parse its stdout as a
    JSON value and return it; otherwise return the raw stdout string.

    For the ``klt sim`` job description (see
    ``sim._build_remote_job_description``), this is the literal reuse
    point: the code that expands corners, fans them across a worker pool,
    extracts measurements, and classifies pass/fail is ``sim.py``'s own
    ``local-parallel`` backend, executing on the provisioned box exactly as
    it would for a local caller -- nothing here re-implements any of that
    logic (see this module's docstring and decision 5).

    ``job.success_exit_codes`` are the exit codes that mean "the command ran
    to completion and produced the promised output" -- for ``klt sim``,
    0 (pass)/3 (measurement failure)/4 (corner error) all mean the sweep ran
    and produced a report (see ``docs/cli/sim.md``'s exit-code table). Any
    other exit code (a ``SimError`` on the remote side, an ``ssh``
    connection failure, ``klt`` missing on ``PATH``, ...) means no output
    exists to parse, and raises :class:`RemoteTransportError`.
    """
    run = runner or _run_subprocess
    remote_command = f"cd {shlex.quote(remote_job_dir)} && {job.command}"
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
            f"remote '{job.label}' timed out after {timeout_s}s"
        ) from exc

    if result.returncode not in job.success_exit_codes:
        raise RemoteTransportError(
            f"remote '{job.label}' failed (exit {result.returncode}): "
            f"{(result.stderr or '').strip()}"
        )
    if not job.parse_json_stdout:
        return result.stdout
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteTransportError(
            f"remote '{job.label}' did not return valid JSON on stdout: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise RemoteTransportError(f"remote '{job.label}' returned non-object JSON")
    return parsed


def pull_artifacts(
    *,
    host: str,
    remote_job_dir: str,
    local_artifacts_dir: str,
    job: JobDescription,
    user: str = DEFAULT_SSH_USER,
    identity_file: str | None = None,
    port: int = 22,
    timeout_s: float = 180.0,
    runner: CommandRunner | None = None,
) -> None:
    """Pull ``job.artifacts_relative_dir`` (job-relative -- for ``klt sim``,
    the ``keep_artifacts`` tree of logs/rawfiles/waveform JSON) down into
    ``local_artifacts_dir``, over the same SSH/SCP channel. A no-op when
    ``job.artifacts_relative_dir`` is ``None`` (the job produces nothing to
    pull back).

    Stages the pull into a fresh temp directory first, then copies its
    contents into ``local_artifacts_dir`` -- avoids depending on ``scp``'s
    destination-exists-or-not rename semantics, which differ depending on
    whether ``local_artifacts_dir`` already exists.
    """
    if job.artifacts_relative_dir is None:
        return

    run = runner or _run_subprocess
    remote_source = artifacts_root(remote_job_dir, job.artifacts_relative_dir)
    staging_dir = tempfile.mkdtemp(prefix="klt-remote-job-pull-")
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
