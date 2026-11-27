"""Shared OpenROAD-subprocess helpers used by the ``klt`` verb modules that
drive OpenROAD as a subprocess: ``place_and_route.py``'s own place/route/CTS/
in-flow-STA stages, and ``post_route_sta.py``'s standalone post-route timing
verb (issue #1099).

``run_openroad``, ``count_violations``, ``openroad_version`` and their
public peers (issue #8051) were previously defined identically (modulo
docstring length, and -- for ``run_openroad`` -- which ``*Error`` class it
raised) in both modules; they now live here as the single source of truth,
following the same precedent as ``_paths.py`` hosting ``load_request``'s
shared file-read/JSON-parse boilerplate (issue #642).

``run_openroad`` is the one genuine behavioral difference between the two
former copies: ``place_and_route.py`` needs to raise ``PlaceAndRouteError``
on a launch failure, ``post_route_sta.py`` needs ``PostRouteStaError``. That
is preserved here via a caller-supplied ``error_cls`` parameter, the same
parameter ``_openroad_io.py``'s script/metrics/error-text helpers take
(issue #8051); that module exists because this one is at its ceiling.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from subprocess import Popen
from typing import Any

from ._artifact_envelope import ANCHOR_CWD, RepoRoot
from ._openroad_logs import (
    _CONTAINER_ID_WAIT_S,
    _captured_text,
    _LiveLogs,
    _track_container_id,
)
from ._openroad_process import _kill_openroad_run, _termination_signals
from ._provenance import sha256_file
from ._tcl import tcl_literal as _tcl_literal

_OPENROAD_VERSION_RE = re.compile(r"OpenROAD\s+(\S+)")

#: Slack magnitude (ns) at or above which a reported value is OpenSTA's own
#: *unconstrained-design sentinel* rather than a measurement (issue #1865).
#:
#: OpenSTA reports the worst slack of a design with no constrained
#: startpoint/endpoint as its internal infinity, which reaches this repo's
#: responses -- via OpenROAD's ``-metrics`` dump -- as ``1e+39``. That value
#: is a *positive number*, so a naive downstream gate reading
#: ``worst_slack_ns >= 0`` concludes "timing closed" on a design that was
#: never timed at all. The threshold is deliberately far below the sentinel
#: (and far above any physically meaningful slack: ``1e29`` ns is roughly
#: 3e21 years) so it keeps matching if a future OpenSTA build reports its
#: infinity as ``1e+30`` instead.
_UNCONSTRAINED_SLACK_NS = 1e29


def _is_unconstrained_slack(value: float | None) -> bool:
    """Whether ``value`` is OpenSTA's unconstrained-design sentinel rather
    than a real slack measurement (issue #1865) -- see
    :data:`_UNCONSTRAINED_SLACK_NS`. ``None`` (a metric the run never
    populated at all) is **not** the sentinel: absence is a different thing
    from "reported, but not a measurement"."""
    return value is not None and abs(value) >= _UNCONSTRAINED_SLACK_NS


def timing_status(values: Iterable[float | None]) -> str | None:
    """Classify a set of reported slack values as ``"constrained"`` /
    ``"unconstrained"`` / ``None`` (issue #1865).

    This is the mechanical signal that lets a caller distinguish "genuinely
    timed, zero negative slack" from "never had a constrained path to
    measure", without special-casing ``1e+39`` by value in every consumer:

    - ``None`` -- no slack metric was reported at all in this scope (e.g. a
      ``klt place-and-route`` stage whose own OpenROAD reports populate no
      timing key). Nothing to classify; not a claim either way.
    - ``"unconstrained"`` -- **any** reported value is the sentinel. The
      conservative reading on purpose: a gate should require
      ``timing_status == "constrained"`` before trusting *any* slack number
      in the same scope.
    - ``"constrained"`` -- every reported value is a real measurement.
    """
    reported = [value for value in values if value is not None]
    if not reported:
        return None
    if any(_is_unconstrained_slack(value) for value in reported):
        return "unconstrained"
    return "constrained"


@dataclass(frozen=True)
class OpenRoadResult:
    """Captured process output plus this invocation's retained evidence."""

    returncode: int
    stdout: str
    stderr: str
    engine_log: dict[str, Any]

    @contextmanager
    def diagnostics(self, error_cls: type[Exception]) -> Iterator[None]:
        """Preserve the caller's diagnosis, including missing metrics/output."""
        try:
            yield
        except error_cls as exc:
            wrapped = error_cls(_log_error_context(str(exc), self.engine_log))
            wrapped.__dict__.update(exc.__dict__)  # keep fields (#6952)
            raise wrapped from exc


#: The version-independent shape of "the Docker daemon is not reachable"
#: (issue #2632): the word ``daemon`` followed, later on the same line, by the
#: word ``running``.
#:
#: The exact wording is Docker-CLI-version-specific, so it is deliberately
#: *not* hardcoded. Docker Engine Community 29.8.1 prints (reproduced live,
#: 2026-09-30, via a non-destructive ``DOCKER_HOST=unix:///nonexistent.sock``
#: invocation)::
#:
#:     failed to connect to the docker API at unix:///nonexistent.sock; check
#:     if the path is correct and if the daemon is running: dial unix ...
#:
#: while older CLI releases print the long-documented::
#:
#:     Cannot connect to the Docker daemon at unix:///var/run/docker.sock.
#:     Is the docker daemon running?
#:
#: Both satisfy this pattern; neither survives a literal string match against
#: the other.
_CONTAINER_RUNTIME_UNREACHABLE_RE = re.compile(r"(?i)daemon\b.*\brunning\b")

#: Shared lead-in for both recognized container-runtime failures, naming the
#: script that generated the wrapper so a reader can see the ``docker run``
#: line for themselves.
_CONTAINER_RUNTIME_HINT_PREFIX = (
    "the openroad wrapper could not reach its container runtime "
    "(see scripts/install-openroad-docker.sh)"
)


def container_runtime_hint(completed: OpenRoadResult) -> str | None:
    """``None``, or an actionable hint that ``openroad`` never really ran
    because its container runtime was unreachable (issue #2632).

    Every OpenROAD-driving ``klt`` verb shells out to a bare ``openroad``
    argv, which -- per this repo's own documented, CI-used install path
    (``scripts/install-openroad-docker.sh``) -- is very often actually a
    wrapper script around ``docker run --rm -i ... $IMAGE ...``. The wrapper
    forwards the client's stdout, stderr and exit status, so whatever
    Docker prints when it cannot reach its daemon becomes the
    captured ``stderr`` of the run essentially *unchanged*: there is no
    wrapper-added text to strip, and no distinct exit code to key off
    (``docker`` exits ``1``, the same as a genuine OpenROAD/Tcl failure).

    Read alone, that is indistinguishable from "the engine ran and the design
    failed analysis" -- yet the two call for opposite responses. A design or
    input problem is the caller's to fix; an unreachable runtime is an
    environment problem where starting the runtime and retrying the *same*
    request is exactly the right move. So when the captured output carries one
    of two stable signatures, say which one it is:

    - **Runtime unreachable** -- :data:`_CONTAINER_RUNTIME_UNREACHABLE_RE`,
      matched per line. Matching on the version-independent ``daemon ...
      running`` shape rather than either release's full sentence is
      deliberate: the two known wordings share no usable literal substring.
    - **Runtime socket not permitted** -- a line carrying both ``permission
      denied`` and ``docker`` (case-insensitively), Docker's behavior when the
      invoking user lacks access to the daemon socket. Checked first, as the
      narrower of the two signatures.

    Like :func:`~klayout_tools._openroad_mount_hint.mount_namespace_hint`
    (issue #1868), this is safe to *append* to an
    existing engine-error message rather than replace it: the engine's own
    captured diagnosis is still the primary evidence, the hint only says what
    that diagnosis means and what to do next. A caller that does not recognize
    either signature gets today's message byte-for-byte.

    Two further signatures are classified (issue #4440), the *environment*
    failures a fleet worker hits when it cannot run OpenROAD at all:

    - **Wrapper cannot find ``docker``** -- exit ``127`` plus a ``not found``
      line naming ``docker`` (bash's ``command not found`` or sh's ``not
      found``): the wrapper is installed but the container CLI is not.
    - **Image missing** -- Docker's ``Unable to find image`` / ``pull access
      denied`` / ``manifest unknown`` / ``No such image`` lines: the wrapper's
      image was never pulled and cannot be.

    When ``$KLT_SIM_BACKEND`` is ``batch`` (a dispatch host that is meant to
    run OpenROAD on the fleet) the remedy names the fleet path instead of
    local-install advice that cannot work on such a host.
    """
    batch_host = os.environ.get(_BATCH_BACKEND_ENV) == "batch"
    lines = [
        line.lower()
        for stream in (completed.stdout or "", completed.stderr or "")
        for line in stream.splitlines()
    ]
    matchers = [_runtime_unreachable_hint, _image_missing_hint]
    if completed.returncode == 127:
        matchers.insert(1, _docker_missing_hint)
    for matcher in matchers:
        for lowered in lines:
            hint = matcher(lowered, batch_host)
            if hint:
                return hint
    return None


def _runtime_unreachable_hint(lowered: str, batch_host: bool) -> str | None:
    """Socket-permission (checked first, narrower) or daemon-unreachable line."""
    if "permission denied" in lowered and "docker" in lowered:
        remedy = (
            "this host runs OpenROAD on the batch fleet, not locally; " + _BATCH_REMEDY
            if batch_host
            else "grant it access (e.g. add the user to the 'docker' "
            "group, or use a rootless runtime), then retry"
        )
        return (
            f"{_CONTAINER_RUNTIME_HINT_PREFIX} -- this user is not "
            f"permitted to reach the container runtime's socket; {remedy}"
        )
    if _CONTAINER_RUNTIME_UNREACHABLE_RE.search(lowered):
        remedy = (
            "this host runs OpenROAD on the batch fleet, not locally; " + _BATCH_REMEDY
            if batch_host
            else "verify the runtime is installed and its daemon is "
            "running ('docker info'), then retry; the request itself "
            "was never analyzed"
        )
        return f"{_CONTAINER_RUNTIME_HINT_PREFIX} -- {remedy}"
    return None


def _docker_missing_hint(lowered: str, batch_host: bool) -> str | None:
    if "docker" in lowered and "not found" in lowered:
        return (
            f"{_CONTAINER_RUNTIME_HINT_PREFIX} -- the wrapper ran "
            "but the 'docker' command is not installed or not on "
            f"PATH (exit 127); {_install_or_batch_remedy(batch_host)}"
        )
    return None


def _image_missing_hint(lowered: str, batch_host: bool) -> str | None:
    if any(marker in lowered for marker in _IMAGE_MISSING_MARKERS):
        return (
            f"{_CONTAINER_RUNTIME_HINT_PREFIX} -- the OpenROAD "
            "container image is not present and could not be pulled; "
            f"{_install_or_batch_remedy(batch_host, pull=True)}"
        )
    return None


#: Lowercased Docker lines meaning the wrapper's image is missing locally and
#: cannot be fetched (issue #4440).
#: ``klayout_tools.sim.BACKEND_ENV``, duplicated to avoid importing ``sim``.
_BATCH_BACKEND_ENV = "KLT_SIM_BACKEND"

_IMAGE_MISSING_MARKERS = (
    "unable to find image",
    "pull access denied",
    "manifest unknown",
    "no such image",
)

_BATCH_REMEDY = (
    "submit with '--backend batch' (one pdk.rebuild_corners unit per job, "
    "see place_and_route_batch) instead of fixing local Docker"
)


def _install_or_batch_remedy(batch_host: bool, *, pull: bool = False) -> str:
    if batch_host:
        return (
            "this host runs OpenROAD on the batch fleet, not locally; " + _BATCH_REMEDY
        )
    if pull:
        return (
            "pull the image or rebuild it with "
            "scripts/install-openroad-docker.sh, then retry"
        )
    return (
        "install the container runtime (scripts/install-openroad-docker.sh), then retry"
    )


def _launch_failure_hint(exc: OSError) -> str:
    """A named cause for ``openroad`` failing to launch at all (issue #4440):
    binary absent from ``PATH`` versus another launch error."""
    batch_host = os.environ.get(_BATCH_BACKEND_ENV) == "batch"
    if isinstance(exc, FileNotFoundError):
        cause = "the 'openroad' binary is not on PATH"
    elif isinstance(exc, PermissionError):
        cause = "the 'openroad' binary is not executable by this user"
    else:
        return ""
    return f" -- {cause}; {_install_or_batch_remedy(batch_host)}"


def _log_error_context(message: str, record: dict[str, Any]) -> str:
    return f"{message} -- openroad invocation: {json.dumps(record, sort_keys=True)}"


def run_openroad(
    script_path: str,
    metrics_path: str,
    *,
    error_cls: type[Exception],
    engine_logs: list[dict[str, Any]] | None = None,
    container_id_wait_s: float = _CONTAINER_ID_WAIT_S,
    threads: int | None = None,
    repo_root: RepoRoot = ANCHOR_CWD,
) -> OpenRoadResult:
    """See “Retained OpenROAD logs” in docs/cli/place-and-route.md for log streaming.
    Keep partial logs; anchor on ``repo_root``; raise ``error_cls`` if launch fails."""
    invocation_id = uuid.uuid4().hex
    script_hash = sha256_file(script_path)
    logs = _LiveLogs(
        script_path,
        metrics_path,
        invocation_id=invocation_id,
        script_hash=script_hash,
        repo_root=repo_root,
    )

    def retain(outcome, returncode, stdout, stderr):
        data, record = logs.finish(
            outcome, returncode, {"stdout": stdout, "stderr": stderr}
        )
        if engine_logs is not None:
            engine_logs.append(record)
        return data, record

    def stop(proc):
        # A cold pull may populate the cidfile after the launch-time poll.
        container_id = logs.record["container_id"] or logs.read_container_id()
        _kill_openroad_run(proc, container_id, invocation_id)

    thread_args = [] if threads is None else ["-threads", str(threads)]
    argv = [
        "openroad",
        "-no_init",
        "-exit",
        *thread_args,
        "-metrics",
        metrics_path,
        script_path,
    ]
    with _termination_signals() as arm_signals:
        try:
            proc = Popen(
                argv,
                start_new_session=True,
                env=logs.wrapper_environment(),
                **logs.popen_streams(),
            )
        except OSError as exc:
            _, record = retain("launch_failed", None, None, None)
            raise error_cls(
                _log_error_context(
                    f"could not launch openroad: {exc}{_launch_failure_hint(exc)}",
                    record,
                )
            ) from exc

        try:
            arm_signals()
            logs.write_launch_snapshot()
            with _track_container_id(logs, proc, container_id_wait_s):
                stdout, stderr = proc.communicate()
        except subprocess.TimeoutExpired as exc:
            stop(proc)
            _, record = retain("timed_out", None, exc.stdout, exc.stderr)
            raise error_cls(
                _log_error_context(f"openroad timed out: {exc}", record)
            ) from exc
        except BaseException as exc:
            stop(proc)
            _, record = retain(
                "interrupted",
                None,
                getattr(exc, "stdout", None),
                getattr(exc, "stderr", None),
            )
            if isinstance(exc, KeyboardInterrupt):
                exc.args = (
                    *exc.args,
                    _log_error_context("openroad interrupted", record),
                )
            raise
    data, record = retain("exited", proc.returncode, stdout, stderr)
    return OpenRoadResult(
        proc.returncode,
        _captured_text(data["stdout"]),
        _captured_text(data["stderr"]),
        record,
    )


def openroad_version() -> str | None:
    """The resolved OpenROAD build string, or ``None`` if unresolvable --
    never raises.

    ``openroad -version`` prints a **bare** version token on its own stdout
    line (e.g. ``26Q3-771-g7cfb2105c9``) -- confirmed live for issue #425's
    own worked example against a real OpenROAD build, distinct from the
    ``OpenROAD <version>`` banner a no-flag/``-no_init`` script invocation
    prints before running. The banner form is matched first (defensively,
    in case a future build changes ``-version``'s own output shape); the
    bare first-token form is the fallback that matches today's real
    behavior.
    """
    try:
        completed = subprocess.run(
            ["openroad", "-version"], capture_output=True, text=True
        )
    except OSError:
        return None
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    banner_match = _OPENROAD_VERSION_RE.search(stdout)
    return banner_match.group(1) if banner_match else stdout.split()[0]


#: ``report_check_types`` flags whose union is the **setup-side** timing-check
#: population ``report_worst_slack_metric -setup``/``report_tns_metric
#: -setup`` already measure (issue #2741): data setup (``-max_delay``, which
#: also covers output-delay checks), asynchronous **recovery**
#: (``-recovery``), and inferred **clock-gating setup**
#: (``-clock_gating_setup``). ``-max_delay`` on its own filters to data checks
#: only, so before issue #2741 a design whose only failing setup-side check was
#: a recovery or clock-gating check reported a negative WNS/TNS next to a
#: ``setup_violation_count`` of ``0``. Verified live against OpenROAD
#: ``26Q3-1510-g6cb3f2b704``: on a ``sky130_fd_sc_hd__dfrtp_1`` fixture the
#: worst-slack metric picks up the ``asynchronous`` and ``gated clock`` path
#: groups, ``-max_delay`` alone reports neither, and the combined call lists
#: each failing endpoint exactly once (the families never share an endpoint
#: pin: data ``D``, async ``RESET_B``, clock-gating enable).
_SETUP_CHECK_FAMILY_FLAGS = ("-max_delay", "-recovery", "-clock_gating_setup")

#: Hold-side counterpart of :data:`_SETUP_CHECK_FAMILY_FLAGS`: data hold
#: (``-min_delay``), asynchronous **removal** (``-removal``), and inferred
#: **clock-gating hold** (``-clock_gating_hold``) -- the population
#: ``report_worst_slack_metric -hold``/``report_tns_metric -hold`` measure.
_HOLD_CHECK_FAMILY_FLAGS = ("-min_delay", "-removal", "-clock_gating_hold")


def write_sdf_line(sdf_path: str) -> str:
    """The one ``write_sdf`` Tcl command every ``klt`` SDF export emits --
    ``klt place-and-route``'s ``post_route_sdf`` (issue #1002) and ``klt
    sta``'s ``sdf`` (issue #2886). Shared so the two cannot drift: both files
    feed the same consumer (``klt functional-verification``'s
    ``options.sdf``). The rationale for each of the two flags is documented on
    ``place_and_route_sta._spef_sta_script_lines``.
    Emit after ``read_spef`` for real routed-parasitic delays."""
    return f"write_sdf -divider . -include_typ {_tcl_literal(sdf_path)}"


def timing_violation_report_lines(
    *, setup_begin: str, setup_end: str, hold_begin: str, hold_end: str
) -> list[str]:
    """Tcl emitting the marker-delimited setup-side and hold-side violator
    reports :func:`count_violations` scrapes (issue #2741).

    The single source of the check-family policy for every verb that counts
    setup/hold violations -- ``post_route_sta.py``'s standalone session (single
    corner and each multi-corner result) and ``place_and_route.py``'s
    nominal-stage report, which ``place_and_route_sta._spef_sta_script_lines``
    reuses for the post-route SPEF re-time. Each caller passes its own
    module-local markers so the stdout regions stay distinct.

    One ``report_check_types`` call per side (rather than one call per
    family) keeps the output a single block between one marker pair, so the
    existing ``"(VIOLATED)"`` scrape counts it unchanged. ``-format end``
    prints one row per failing endpoint per path group, so the count unit is
    *violating endpoints*, not enumerated timing paths.
    """

    def report_line(flags: tuple[str, ...]) -> str:
        return f"report_check_types {' '.join(flags)} -violators -format end"

    return [
        f'puts "{setup_begin}"',
        report_line(_SETUP_CHECK_FAMILY_FLAGS),
        f'puts "{setup_end}"',
        f'puts "{hold_begin}"',
        report_line(_HOLD_CHECK_FAMILY_FLAGS),
        f'puts "{hold_end}"',
    ]


def count_violations(stdout: str, begin: str, end: str) -> int:
    """Count ``"(VIOLATED)"`` lines between ``begin``/``end`` markers in a
    stage's captured stdout -- OpenROAD has no ``*_metric`` proc for
    setup/hold *violation counts* (only the scalar WNS/TNS), so this is the
    documented ``report_*`` stdout-scrape fallback (contract spike section
    5's build/wrap section). Returns ``0`` when the markers aren't found
    (defensive; should not happen for a successful run) or when the report
    found no violating paths."""
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return 0
    return stdout[start_idx:stop_idx].count("(VIOLATED)")
