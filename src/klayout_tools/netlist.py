"""``klt netlist``: export an xschem schematic to a SPICE netlist, headlessly.

This is the step immediately upstream of everything else ``klt`` does on the
SPICE side: turning a schematic into the netlist ``klt sim`` simulates and
(eventually) ``klt lvs`` compares layout against. Issue #55 filed it as
recurring friction -- every block repo on the open flow (xschem + ngspice) was
hand-rolling the same wrapper, and rediscovering the same three tool
behaviours, one of which is silent.

**This module owns the xschem invocation; the caller never spells it.** That
is the whole point, not an implementation detail:

1. **A missing ``-x`` does not fail, it hangs** (issue #55's fifth sharp edge,
   observed twice: gf180-bandgap 2026-07-31, sg13g2-vco 2026-10-01). Without
   ``-x``, xschem initialises its full Tk GUI *despite* the other batch flags,
   writes a plausible-looking netlist, and then never exits -- it sits in the
   Tk event loop, and a signal arriving during teardown re-enters xschem's own
   non-async-signal-safe ``sig_handler``, which then wedges permanently and
   **ignores SIGTERM**. Nothing is printed, so the caller sees an idle process
   and a stale artifact rather than an error. There is therefore no
   caller-supplied flag list here: :data:`FORCED_XSCHEM_FLAGS` is always
   passed, so the flag cannot be dropped by a copy-pasted command line.
2. **Batch netlisting exits non-zero on success** (edge #1). ``xschem -n -s -q
   -x`` returns a non-zero status after a *successful* export, so the exit
   status is recorded (``xschem.exit_status``) and explicitly **not** the
   verdict (``xschem.exit_status_trusted: false``). Success is "the expected
   output file exists and its mtime is at or after the moment we launched
   xschem" -- see :func:`_resolve_produced_netlist`.
3. **A committed netlist drifts from its schematics silently** (edge #4). The
   netlist is a generated artifact repos commit (so simulation and CI need no
   schematic-capture tool installed), and nothing told them when it went
   stale. :func:`run_netlist` with ``check=True`` regenerates into a temp
   directory and diffs against the committed file, reporting
   ``status: "drifted"`` (exit 3) rather than overwriting it.

Because of (1), every run is bounded: xschem is launched in its own process
group and, on timeout, is sent SIGTERM and then -- since a wedged xschem
ignores SIGTERM -- escalated to SIGKILL on the whole group (see
:func:`_terminate_escalating`). A timeout is reported as a
:class:`NetlistError` naming the bound, the escalation, and the known cause,
never as a silent empty result.

Deliberately **out of scope** for this first slice (issue #55's maintainer
scope-narrowing comment, 2026-10-01): emitting a block as an includable
``.subckt`` rather than a full deck (edge #2; tracked by #2680). The PDK-root
edge (#55 edge #3) is addressed as a *consistency check* (issue #2680, option
A): ``--pdk``/``--pdk-root`` resolve through :mod:`klayout_tools.pdk` and any
PDK root the ``--rcfile`` declares is compared against it, so a disagreement
is surfaced (:func:`_pdk_consistency`) rather than silently ignored. The
``xschemrc`` is still passed through verbatim and never generated.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from . import pdk as pdk_module

#: Payload schema version (``docs/json-contract.md``; versioned per command).
SCHEMA_VERSION = 1

#: The flags this wrapper **always** passes, whatever the caller asks for:
#:
#: - ``-n`` netlist the given schematic (the reason we are running at all);
#: - ``-s`` SPICE netlist format (this slice exports SPICE only);
#: - ``-q`` quit once the command line has been processed;
#: - ``-x`` do **not** use X -- the load-bearing one (see the module
#:   docstring: omitting it hangs rather than failing);
#: - ``-r`` do not use readline, so a batch run can never block on an
#:   interactive Tcl prompt.
#:
#: Order is fixed so ``xschem.argv`` in the payload is stable and diffable.
FORCED_XSCHEM_FLAGS = ("-n", "-s", "-q", "-x", "-r")

#: Default xschem executable. Overridable (``--xschem-binary``) for a pinned
#: install, a PATH wrapper, or a test stub -- never to change the flag set.
DEFAULT_XSCHEM_BINARY = "xschem"

#: Default wall-clock bound on the xschem run, in seconds. A real batch
#: netlist of a block completes in well under a second (issue #55 measured
#: ~0.1s with ``-x``); this bound exists for the *hang*, so it is generous
#: enough never to cut off honest work and short enough that a wedged run
#: fails inside an agent's attention span rather than after an hour.
DEFAULT_TIMEOUT_S = 120.0

#: How long to wait after SIGTERM before escalating to SIGKILL. Short on
#: purpose: a *healthy* xschem never needs it (it has already exited), and the
#: failure this exists for ignores SIGTERM entirely, so waiting longer only
#: delays the error.
SIGTERM_GRACE_S = 2.0

#: Slack allowed when comparing the produced netlist's mtime against the
#: moment xschem was launched. Not cosmetic: HFS+ and some network/container
#: filesystems carry whole-second mtime granularity, so a file genuinely
#: written 10ms after launch can stat as having been written up to a second
#: *before* it. One second is the coarsest granularity we need to tolerate.
FRESHNESS_TOLERANCE_S = 1.0

#: Cap on the number of unified-diff lines carried in a ``--check`` payload.
#: A drifted netlist can differ by thousands of lines; the payload's job is
#: to say *that* it drifted and show the head of the difference, not to be a
#: transport for the whole file (both copies are on disk already).
MAX_DIFF_LINES = 200

#: ``status`` values. ``"generated"`` is the normal export; ``"match"`` /
#: ``"drifted"`` are ``--check``'s verdicts, reusing the same vocabulary (and
#: the same 0/3 exit split) as ``klt drc --check`` and friends.
STATUS_GENERATED = "generated"
STATUS_MATCH = "match"
STATUS_DRIFTED = "drifted"


class NetlistError(Exception):
    """A netlist export could not be completed (bad input, xschem missing,
    xschem hung, or no fresh output file produced)."""


def _sha256_file_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _validate_schematic(schematic: str) -> None:
    """Fail before launching a subprocess, the same fail-fast order
    :func:`klayout_tools.drc.run_drc_klayout_engine` uses for its input."""
    if not os.path.exists(schematic):
        raise NetlistError(f"schematic not found: {schematic}")
    if os.path.isdir(schematic):
        raise NetlistError(f"schematic is a directory, not a file: {schematic}")
    if not os.access(schematic, os.R_OK):
        raise NetlistError(f"schematic is not readable: {schematic}")


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to ``proc``'s whole process group, falling back to the
    single process when the group cannot be addressed.

    The group, not the process: xschem's Tk startup can leave helpers behind,
    and signalling only the parent orphans them (issue #55 observed an
    xschem orphaned to launchd for over an hour). Never raises -- this is
    only ever called once a timeout has already been decided, and a signal
    that fails because the process just exited on its own is not a failure
    worth reporting over the timeout itself.

    **Never signals our own group.** ``start_new_session=True`` puts the child
    in its own session, so this cannot normally happen; the guard is here
    because the consequence if it ever did would be killing the ``klt`` run
    (and its parent shell) rather than the child.
    """
    pgid = None
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            pgid = None
        if pgid is not None and pgid == os.getpgrp():
            pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass
    try:
        proc.send_signal(sig)
    except (OSError, ValueError):
        pass


def _terminate_escalating(proc: subprocess.Popen) -> bool:
    """SIGTERM ``proc``'s group, then SIGKILL it if it is still alive after
    :data:`SIGTERM_GRACE_S`. Returns ``True`` if SIGKILL was needed.

    The escalation is not defensive boilerplate: issue #55 documents xschem
    wedged inside its own re-entered ``sig_handler``, where SIGTERM routes
    straight back into the wedge and is effectively ignored, so ``kill -9``
    was the only way to clean up. Treating SIGTERM as sufficient here would
    leave the hung process behind and report success at having stopped it.
    """
    _signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=SIGTERM_GRACE_S)
        return False
    except subprocess.TimeoutExpired:
        pass
    _signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=SIGTERM_GRACE_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL is uncatchable
        pass
    return True


def _build_argv(
    *,
    xschem_binary: str,
    schematic: str,
    work_dir: str,
    rcfile: str | None,
) -> list[str]:
    """The complete xschem command line, assembled here and nowhere else.

    ``-o work_dir`` always points at a private temp directory, never at the
    caller's output path: the export is judged (and, under ``check``, diffed)
    before anything is written where a consumer could read it, so a hung or
    half-written run can never leave a plausible-looking artifact behind --
    which is exactly how issue #55's gf180-bandgap netlist went silently
    stale.
    """
    argv = [xschem_binary, *FORCED_XSCHEM_FLAGS]
    if rcfile is not None:
        argv.extend(["--rcfile", rcfile])
    argv.extend(["-o", work_dir, schematic])
    return argv


def _launch_xschem(argv: list[str], timeout_s: float) -> tuple[int, str, str, float]:
    """Run ``argv`` under a ``timeout_s`` wall-clock bound.

    Returns ``(exit_status, stdout, stderr, duration_s)``. The exit status is
    returned for the record, **not** as a verdict -- see the module docstring's
    point 2 and :func:`_resolve_produced_netlist`.

    Raises :class:`NetlistError` if the binary is missing, or if the bound is
    exceeded (after :func:`_terminate_escalating` has cleaned the process up).
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # The child gets its own session (and therefore its own process
            # group) so `_signal_group` has a group handle to kill -- see its
            # docstring.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise NetlistError(
            f"could not launch xschem: {argv[0]!r} not found on PATH. Install "
            "xschem (https://xschem.sourceforge.io/) or pass "
            f"--xschem-binary with a path to it. ({exc})"
        ) from exc
    except OSError as exc:
        raise NetlistError(f"could not launch xschem ({argv[0]!r}): {exc}") from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as timeout_exc:
        killed = _terminate_escalating(proc)
        # Reap, and collect whatever the child managed to say before wedging.
        try:
            stdout, stderr = proc.communicate(timeout=SIGTERM_GRACE_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - pipes are closed by now
            stdout, stderr = "", ""
        raise NetlistError(
            _timeout_message(argv, timeout_s, killed, stdout, stderr)
        ) from timeout_exc

    return proc.returncode, stdout or "", stderr or "", time.monotonic() - started


def _timeout_message(
    argv: list[str],
    timeout_s: float,
    killed: bool,
    stdout: str,
    stderr: str,
) -> str:
    """The actionable form of "xschem hung" (issue #55).

    Says the bound, says which signal actually stopped it, and names the one
    cause that produces this symptom in practice -- a GUI-initialising run
    stuck in the Tk event loop -- because the failure itself prints nothing
    and is not discoverable from the process state.
    """
    escalation = (
        "SIGTERM was ignored, so it was SIGKILLed" if killed else "it exited on SIGTERM"
    )
    detail = (stdout or stderr or "").strip()
    suffix = f" xschem's own output before the hang:\n{detail}" if detail else ""
    return (
        f"xschem did not complete within {timeout_s}s and was stopped "
        f"({escalation}); its whole process group was signalled, so nothing "
        "was left running. A batch netlist normally takes well under a "
        "second, so this is a hang rather than slow work: xschem wedges "
        "indefinitely when it initialises its GUI, and can then re-enter its "
        "own signal handler and stop responding to SIGTERM (issue #55). "
        f"`klt netlist` always passes {' '.join(FORCED_XSCHEM_FLAGS)}, so a "
        "missing -x is not the cause here -- check that the schematic's "
        "xschemrc does not launch anything interactive, then raise "
        f"--timeout-s if the design genuinely needs longer. No netlist was "
        f"written. Command: {' '.join(argv)}.{suffix}"
    )


def _fresh_files(work_dir: str, started_at: float) -> list[str]:
    """Names of files in ``work_dir`` whose mtime is at or after
    ``started_at`` (within :data:`FRESHNESS_TOLERANCE_S`), sorted."""
    fresh = []
    for name in sorted(os.listdir(work_dir)):
        path = os.path.join(work_dir, name)
        if not os.path.isfile(path):
            continue
        if os.stat(path).st_mtime >= started_at - FRESHNESS_TOLERANCE_S:
            fresh.append(name)
    return fresh


def _resolve_produced_netlist(
    work_dir: str,
    stem: str,
    started_at: float,
    *,
    exit_status: int,
    stdout: str,
    stderr: str,
) -> str:
    """Decide whether the run succeeded, **without consulting the exit status**.

    xschem returns non-zero after a successful batch netlist (issue #55's
    first sharp edge), so the verdict is: a file exists in the private output
    directory and was written at or after we launched xschem. xschem names a
    SPICE export after the schematic (``foo.sch`` -> ``foo.spice``); that name
    is preferred, and a single fresh file under any other name is accepted as
    well (and reported, via the payload's ``netlist.path``) so a future xschem
    naming change surfaces as a different filename rather than a spurious
    "produced nothing".

    The freshness half matters as much as the existence half: without it, a
    stale netlist left behind by an earlier run would be read as this run's
    output -- the exact drift failure ``--check`` exists to catch.

    Raises :class:`NetlistError`, quoting xschem's own output and its exit
    status, when no fresh file appeared.
    """
    fresh = _fresh_files(work_dir, started_at)
    expected = f"{stem}.spice"
    if expected in fresh:
        return os.path.join(work_dir, expected)
    if len(fresh) == 1:
        return os.path.join(work_dir, fresh[0])

    detail = (stdout or stderr or "").strip()
    if fresh:
        produced = (
            f"It produced {len(fresh)} files, none named {expected!r}: "
            f"{', '.join(fresh)}."
        )
    else:
        produced = f"Expected {expected!r}; the output directory is empty."
    raise NetlistError(
        "xschem produced no fresh netlist file. "
        + produced
        + " (xschem's exit status is deliberately not used as the verdict "
        "here -- it is non-zero even on success -- so this means the export "
        f"really did not happen. xschem exited {exit_status}."
        + (f" Its own output:\n{detail}" if detail else "")
        + ")"
    )


def _netlist_block(path: str | None, data: bytes) -> dict[str, Any]:
    """Identity of the netlist this run produced.

    ``path`` is ``None`` under ``check=True``: the regenerated copy lives in a
    temp directory that is removed before returning, so naming it would hand
    a consumer a path that no longer exists. The ``content_hash`` is the
    durable identity in that mode, and ``drift`` carries the difference.
    """
    return {
        "path": path,
        "bytes": len(data),
        "lines": data.count(b"\n"),
        "content_hash": _sha256_file_hash(data),
    }


def _unified_diff(
    committed: bytes, fresh: bytes, output: str
) -> tuple[list[str], bool]:
    """Unified diff of the committed netlist against the regenerated one,
    capped at :data:`MAX_DIFF_LINES` lines (second element says whether it
    was truncated)."""
    lines = list(
        difflib.unified_diff(
            committed.decode("utf-8", errors="replace").splitlines(),
            fresh.decode("utf-8", errors="replace").splitlines(),
            fromfile=f"{output} (committed)",
            tofile=f"{output} (regenerated)",
            lineterm="",
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        return lines[:MAX_DIFF_LINES], True
    return lines, False


def _check_result(output: str, fresh_data: bytes) -> tuple[str, dict[str, Any]]:
    """``check=True``'s verdict: does the committed netlist at ``output``
    still match what the schematics produce?

    A missing committed netlist is ``"drifted"``, not an error: "the committed
    artifact is not what the schematics produce" is exactly true of a netlist
    that was never committed, and a CI gate wants the same non-zero exit for
    both rather than having to distinguish them.
    """
    if not os.path.isfile(output):
        return STATUS_DRIFTED, {
            "committed_present": False,
            "committed_bytes": None,
            "committed_content_hash": None,
            "diff": [],
            "diff_truncated": False,
            "reason": "no committed netlist at the output path",
        }

    with open(output, "rb") as handle:
        committed = handle.read()
    if committed == fresh_data:
        return STATUS_MATCH, {
            "committed_present": True,
            "committed_bytes": len(committed),
            "committed_content_hash": _sha256_file_hash(committed),
            "diff": [],
            "diff_truncated": False,
            "reason": None,
        }

    diff, truncated = _unified_diff(committed, fresh_data, output)
    return STATUS_DRIFTED, {
        "committed_present": True,
        "committed_bytes": len(committed),
        "committed_content_hash": _sha256_file_hash(committed),
        "diff": diff,
        "diff_truncated": truncated,
        "reason": (
            "the committed netlist differs from the one the schematics "
            "produce now -- re-run without --check to refresh it"
        ),
    }


def _write_output(output: str, data: bytes) -> None:
    parent = os.path.dirname(os.path.abspath(output))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(output, "wb") as handle:
            handle.write(data)
    except OSError as exc:
        raise NetlistError(f"could not write netlist to {output}: {exc}") from exc


_RC_PDK_ROOT_RE = re.compile(
    r"^\s*set\s+(?:::)?(?:env\(PDK_ROOT\)|PDK_ROOT)\s+(\"[^\"]*\"|\{[^}]*\}|\S+)",
    re.MULTILINE,
)


def _rcfile_pdk_root(rcfile: str) -> str | None:
    """The PDK root a ``xschemrc`` statically declares, or ``None``.

    Recognises ``set PDK_ROOT <path>`` and ``set env(PDK_ROOT) <path>`` (the
    last such assignment wins). A value that is computed (contains ``$`` or
    ``[``) cannot be evaluated without a Tcl interpreter and is reported as
    undeclared rather than guessed at.
    """
    try:
        with open(rcfile, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    found = _RC_PDK_ROOT_RE.findall(text)
    if not found:
        return None
    value = found[-1].strip()
    if value[:1] in ('"', "{") and value[-1:] in ('"', "}"):
        value = value[1:-1]
    if not value or "$" in value or "[" in value:
        return None
    return os.path.realpath(os.path.expanduser(value))


def _related(a: str, b: str) -> bool:
    """True if one path equals or contains the other (after realpath)."""
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _pdk_consistency(
    rcfile: str | None, pdk: str | None, pdk_root: str | None
) -> dict[str, Any] | None:
    """Resolve the PDK like ``klt sim`` and compare it to the ``--rcfile``.

    Returns ``None`` when no PDK is in play (the pre-#2680 behaviour,
    unchanged). ``--pdk``/``--pdk-root`` are strict: an unresolvable PDK is a
    :class:`NetlistError`. Without either flag, ``$PDK``/``$PDK_ROOT`` are
    consulted only when an ``--rcfile`` is given, best-effort (failure to
    resolve is silently "no PDK").

    ``consistent`` is ``True``/``False`` when the rcfile declares a PDK root
    that does/doesn't match the resolved root (a declared root may be the
    install root itself or its parent, e.g. ``$PDK_ROOT`` holding
    ``sky130A/``), and ``None`` when there is nothing to compare.
    """
    explicit = pdk is not None or pdk_root is not None
    if not explicit and not (
        rcfile and (os.environ.get("PDK") or os.environ.get("PDK_ROOT"))
    ):
        return None
    try:
        found = pdk_module.find_pdk(pdk, pdk_root)
    except pdk_module.PdkNotFoundError as exc:
        if explicit:
            raise NetlistError(str(exc)) from exc
        return None

    declared = _rcfile_pdk_root(rcfile) if rcfile else None
    consistent: bool | None = None
    warning: str | None = None
    if declared is not None:
        consistent = _related(declared, found["root"])
        if not consistent:
            warning = (
                f"xschemrc {rcfile} declares PDK root {declared}, but the "
                f"resolved PDK {found['variant']} is at {found['root']} "
                f"(via {found['resolved_via']}); the schematic and "
                "simulation sides may be using different PDKs"
            )
    return {
        "variant": found["variant"],
        "version": found.get("version"),
        "resolved_via": found["resolved_via"],
        "root": found["root"],
        "rcfile_pdk_root": declared,
        "consistent": consistent,
        "warning": warning,
        "ambiguity_warning": pdk_module.ambiguity_warning(found),
    }


def run_netlist(
    schematic: str,
    output: str,
    *,
    check: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    xschem_binary: str = DEFAULT_XSCHEM_BINARY,
    rcfile: str | None = None,
    pdk: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Export ``schematic`` to a SPICE netlist at ``output`` via xschem.

    ``check=True`` does not write ``output``: it regenerates into a temp
    directory and compares, reporting ``status: "match"`` or ``"drifted"``
    (with a capped unified diff under ``drift``). This is the staleness gate
    a repo runs in CI over a committed netlist.

    Returns the documented payload (see ``docs/cli/netlist.md``). Raises
    :class:`NetlistError` for every failure mode: unreadable schematic,
    missing/unlaunchable xschem, the timeout (after SIGTERM->SIGKILL
    escalation), no fresh output file, or an unwritable ``output``.
    """
    _validate_schematic(schematic)
    if timeout_s <= 0:
        raise NetlistError(f"--timeout-s must be positive (got {timeout_s})")
    if rcfile is not None and not os.path.isfile(rcfile):
        raise NetlistError(f"xschemrc file not found: {rcfile}")
    pdk_block = _pdk_consistency(rcfile, pdk, pdk_root)

    stem = os.path.splitext(os.path.basename(schematic))[0]
    work_dir = tempfile.mkdtemp(prefix="klt-netlist-")
    try:
        argv = _build_argv(
            xschem_binary=xschem_binary,
            schematic=schematic,
            work_dir=work_dir,
            rcfile=rcfile,
        )
        # Captured *before* the launch, so the freshness test can never be
        # satisfied by a file that already existed (see
        # `_resolve_produced_netlist`).
        started_at = time.time()
        exit_status, stdout, stderr, duration_s = _launch_xschem(argv, timeout_s)
        produced = _resolve_produced_netlist(
            work_dir,
            stem,
            started_at,
            exit_status=exit_status,
            stdout=stdout,
            stderr=stderr,
        )
        with open(produced, "rb") as handle:
            fresh_data = handle.read()

        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "schematic": schematic,
            "output": output,
            "mode": "check" if check else "generate",
            "xschem": {
                "binary": xschem_binary,
                "argv": argv,
                "forced_flags": list(FORCED_XSCHEM_FLAGS),
                "exit_status": exit_status,
                # Load-bearing, not decoration: xschem exits non-zero on a
                # successful batch netlist (issue #55, edge #1), so a consumer
                # reading this payload is told in the payload itself that the
                # number above is a record, not a verdict.
                "exit_status_trusted": False,
                "timeout_s": timeout_s,
                "duration_s": round(duration_s, 3),
            },
        }

        if pdk_block is not None:
            payload["pdk"] = pdk_block

        if check:
            status, drift = _check_result(output, fresh_data)
            payload["status"] = status
            payload["netlist"] = _netlist_block(None, fresh_data)
            payload["drift"] = drift
            return payload

        _write_output(output, fresh_data)
        payload["status"] = STATUS_GENERATED
        payload["netlist"] = _netlist_block(output, fresh_data)
        return payload
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
