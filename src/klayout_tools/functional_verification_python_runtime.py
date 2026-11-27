"""Pre-build measurement of the CPython runtime the Icarus ``vvp`` process
loads for cocotb (issue #8015).

Split out of ``functional_verification.py`` (the #1909 SDF-split precedent,
for headroom under the per-file size ratchet). Names that stay there are
reached as ``_fv.<name>`` at call time, so monkeypatching them on
``klayout_tools.functional_verification`` still takes effect.

The wheel-tag pre-flight (``_fv._check_cocotb_abi_compatibility``) asks
whether cocotb was *built* for klt's ``major.minor``. It cannot see which
CPython the simulator process *loads*: a ``cp313`` wheel passes whether
``vvp`` ends up on 3.13.18 or on 3.13.0rc1, and the run then dies without a
``results.xml`` -- after the build, naming no library. Five findings pin down
what a trustworthy check has to do (measured 2026-11-27 on cocotb 2.1.0 +
Icarus 13.0):

1. **cocotb's banner is not the runtime.** ``Using Python X interpreter at
   ...`` prints the wheel's compile-time ``PY_VERSION`` -- ``3.14.0`` here,
   inside a ``vvp`` whose ``Py_GetVersion()`` said ``3.14.9``. A healthy
   install shows a *differing* banner, so comparing it would refuse every
   one of them.
2. **A configured libpython is not the runtime either.**
   ``cocotb_tools.config --libpython`` and the transcript's ``Loaded entry
   library:`` line name what cocotb *asked* the loader for (``LIBPYTHON_LOC``,
   else ``find_libpython`` resolved in the *klt* process) unless ``GPI_USERS``
   is already set. Which object ends up supplying the C-API inside ``vvp``
   also depends on that process's own loader environment -- so the probe goes
   through the real launcher (on some workers ``vvp`` is a wrapper script)
   and measures in there.
3. **Measure in-process, never from a helper.** A Python helper process
   would report *its own* libpython. Inside ``vvp``, ``Py_GetVersion`` looked
   up in the process-global symbol table is by definition the CPython
   supplying the C-API. The kernel mapping at that address identifies the
   device and inode for checking the path reported by ``dladdr``.
4. **Only a confirmed difference refuses the run.** Release components are
   compared including level and serial (``3.13.0rc1`` differs from
   ``3.13.18``; compiler and build-date text never enter). Anything the probe
   cannot establish -- a ``vvp`` that cannot start Python, a result it cannot
   read, a platform without ``fork``, a scratch directory or pipe it cannot
   create -- is *inconclusive*: the ordinary run proceeds and keeps its own
   failure diagnostics. A pre-flight that cannot measure must never fail a
   run the simulator might still serve, which is the discipline the
   wheel-tag check already follows.
5. **The probe measures only as far as Python gets.** Its test runs once
   cocotb's regression does. A *cross-minor* libpython usually breaks the
   embedded interpreter before that -- reproduced live by pointing
   ``LIBPYTHON_LOC`` at a 3.13 libpython under klt 3.14: the transcript shows
   ``Unexpected sys.executable value`` and an import of the 3.14 stdlib's
   ``_typing`` failing -- so that shape is inconclusive by construction and
   the ordinary run's transcript stays the evidence. What the probe adds over
   the wheel tag is the difference the tag cannot see: another release of
   klt's own minor -- another patch, a pre-release -- where Python runs and
   nothing else looks wrong.
"""

from __future__ import annotations

import json
import multiprocessing
import multiprocessing.connection
import os
import re
import shutil
import signal
import sys
import tempfile
from typing import Any

from .functional_verification_base import FunctionalVerificationError

#: Wall-clock bound on the probe: one build plus one cocotb test on an empty
#: module, ~0.5 s measured, nearly all of it Python starting inside ``vvp``.
#: Generous for a loaded shared worker, and a bound nonetheless -- it runs
#: *before* the user's own regression, which has no klt-level timeout.
PYTHON_RUNTIME_PROBE_TIMEOUT_S = 60.0

#: How long to wait, after SIGKILL to a timed-out probe's process group, for
#: the child to be reaped before moving on -- never itself an unbounded wait.
_PROBE_KILL_GRACE_S = 5.0

#: The Verilog module the probe compiles, and the top-level Python module of
#: its single cocotb test: ``functional_verification_python_runtime_probe.py``
#: copied under this name into the scratch directory, so the simulator
#: imports it by a name no ``klayout_tools`` module shadows or shares.
PROBE_MODULE = "klt_python_runtime_probe"

_PROBE_VERILOG = f"module {PROBE_MODULE};\nendmodule\n"
_PROBE_TEST_SOURCE = "functional_verification_python_runtime_probe.py"
_PROBE_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

#: The release token ``Py_GetVersion()`` (and ``sys.version``) starts with:
#: ``major.minor[.micro]`` plus the ``sys.version_info`` release-level
#: spellings CPython has used, ended by whitespace or end of string so a
#: malformed token never parses as a prefix of itself.
_RELEASE_RE = re.compile(
    r"^\s*(\d+)\.(\d+)(?:\.(\d+))?(alpha|beta|candidate|a|b|rc)?(\d+)?(?=\s|$)"
)
_RELEASE_LEVELS = {
    "a": "alpha",
    "alpha": "alpha",
    "b": "beta",
    "beta": "beta",
    "rc": "candidate",
    "candidate": "candidate",
}

Release = tuple[int, int, int, str, int]


def _release(text: str) -> Release | None:
    """``(major, minor, micro, releaselevel, serial)`` of the release token
    ``text`` starts with -- the shape of ``sys.version_info`` -- or ``None``
    when it does not start with one."""
    match = _RELEASE_RE.match(text)
    if match is None:
        return None
    major, minor, micro, level, serial = match.groups()
    return (
        int(major),
        int(minor),
        int(micro or 0),
        _RELEASE_LEVELS[level] if level else "final",
        int(serial or 0),
    )


def _format_release(release: Release) -> str:
    """``X.Y.Z``, with ``aN``/``bN``/``rcN`` for a pre-release -- the
    ``platform.python_version()`` spelling."""
    suffix = {"alpha": "a", "beta": "b", "candidate": "rc"}.get(release[3], "")
    serial = release[4] if suffix else ""
    return f"{release[0]}.{release[1]}.{release[2]}{suffix}{serial}"


def _klt_release() -> Release:
    """Release of the interpreter running klt."""
    info = sys.version_info
    return (info.major, info.minor, info.micro, info.releaselevel, info.serial)


def _inconclusive(reason: str) -> dict[str, Any]:
    return {"status": "inconclusive", "reason": reason}


def _probe_child(
    conn: multiprocessing.connection.Connection,
    parent_conn: multiprocessing.connection.Connection,
    target: Any,
    kwargs: dict[str, Any],
) -> None:
    """Run the target only after the parent has established our process group."""
    parent_conn.close()  # A lost parent must let recv_bytes() see EOF.
    try:
        conn.recv_bytes()
        conn.send(("measured", target(**kwargs)))
    except BaseException as exc:
        try:
            conn.send(("inconclusive", {"reason": f"{type(exc).__name__}: {exc}"}))
        except OSError:  # pragma: no cover - defensive: the parent is gone
            pass
    finally:
        conn.close()


def _kill_probe_child(pid: int, group_created: bool) -> None:
    """Signal the group established by the parent, or just the waiting child."""
    if group_created:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return  # The group has already exited.
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_bounded(target: Any, **kwargs: Any) -> dict[str, Any]:
    """``target(**kwargs)`` in a forked child bounded by
    :data:`PYTHON_RUNTIME_PROBE_TIMEOUT_S`. Never raises and never leaves the
    child running: returns ``target``'s dict under ``"status": "measured"``,
    or ``{"status": "inconclusive", "reason": ...}``.

    Forking, rather than a thread with a join timeout, is the load-bearing
    choice -- a thread cannot terminate a blocking ``subprocess.run()`` down
    inside cocotb's ``Runner.test()``, which has no timeout parameter (see
    ``_run_isolated_variant``, the precedent this follows). It also gives the
    child exactly this process's ``sys.path``, which cocotb propagates to the
    simulator as ``PYTHONPATH``: the probe's import path is the request's own.
    """
    try:
        context = multiprocessing.get_context("fork")
    except ValueError as exc:
        return _inconclusive(f"no fork start method on this platform ({exc})")
    try:
        parent_conn, child_conn = context.Pipe(duplex=True)
    except OSError as exc:  # out of file descriptors, say
        return _inconclusive(f"could not open the probe's result pipe ({exc})")
    try:
        process = context.Process(
            target=_probe_child,
            args=(child_conn, parent_conn, target, kwargs),
            daemon=True,
        )
        process.start()
    except Exception as exc:
        # A daemonic multiprocessing parent may not fork children, for one.
        parent_conn.close()
        child_conn.close()
        return _inconclusive(f"could not start the probe process ({exc})")
    child_conn.close()
    pid = process.pid
    assert pid is not None  # process.start() succeeded.
    group_created = False
    try:
        try:
            os.setpgid(pid, pid)
        except OSError as exc:
            return _inconclusive(
                f"could not give the probe its own process group ({exc})"
            )
        group_created = True
        parent_conn.send_bytes(b"")  # The child can now launch the simulator.
        if not parent_conn.poll(PYTHON_RUNTIME_PROBE_TIMEOUT_S):
            return _inconclusive(
                f"no result within {PYTHON_RUNTIME_PROBE_TIMEOUT_S:g}s"
            )
        status, payload = parent_conn.recv()
        return {**payload, "status": status}
    except (EOFError, OSError):
        pass  # The driver exited without a result. Report its exit code after join.
    finally:
        parent_conn.close()
        # Keep the driver's PID reserved until after signalling its group.
        # is_alive() and join() can both reap an exited driver.
        _kill_probe_child(pid, group_created)
        process.join(_PROBE_KILL_GRACE_S)
    return _inconclusive(
        f"the probe process exited without a result (exit code {process.exitcode})"
    )


def _probe_in_process(
    *,
    runner_module: Any,
    module_dirs: list[str],
    env: dict[str, str],
    timescale: tuple[str, str],
    test_dir: str,
    workdir: str,
) -> dict[str, str | None]:
    """Build and run the probe through the request's own ``_fv._run_build`` /
    ``_fv._run_test`` -- hence the same ``vvp`` launcher, the same
    ``GPI_USERS`` and ``PYTHONPATH`` cocotb derives from this process, and the
    same ``request.env`` -- then return what its test wrote. Raises whatever
    stops it; :func:`_probe_child` relays that as inconclusive.

    ``vvp`` starts in ``test_dir``, the working directory the request's own
    run gets, because that is where a relative ``PATH`` entry (a wrapper
    ``tools/vvp``) is resolved from: a probe started elsewhere could select
    another launcher and measure a runtime the real run never loads. Only
    the probe's artifacts -- build, logs, ``results.xml``, its result file --
    stay in the scratch ``workdir``.

    The user's ``module_dirs`` stay on the import path ahead of everything
    but the scratch directory itself, exactly as in the real run: a helper of
    theirs that shadows a module decides both runs alike. What is
    deliberately *not* carried over is anything that could keep the probe's
    single test from running -- ``testcase``, ``plusargs``, build args.
    """
    build_dir = os.path.join(workdir, "sim_build")
    result_path = os.path.join(workdir, "result.json")
    # Runner.test merges os.environ over extra_env. Keep the user's selection
    # and coverage (including its legacy alias) out of this scratch test.
    probe_env = env.copy()
    for key in (
        "COCOTB_TEST_FILTER",
        "COCOTB_TESTCASE",
        "COCOTB_USER_COVERAGE",
        "COVERAGE",
    ):
        os.environ.pop(key, None)
        probe_env.pop(key, None)
    engine_runner = _fv._run_build(
        runner_module,
        engine="icarus",
        sources=[os.path.join(workdir, f"{PROBE_MODULE}.v")],
        hdl_toplevel=PROBE_MODULE,
        build_dir=build_dir,
        build_args=[],
        defines={},
        includes=[],
        timescale=timescale,
        parameters={},
        log_path=os.path.join(workdir, "build.log"),
        waves=False,
    )
    _fv._run_test(
        engine_runner,
        engine="icarus",
        module=PROBE_MODULE,
        module_dirs=[workdir, *module_dirs],
        hdl_toplevel=PROBE_MODULE,
        testcase=None,
        random_seed=None,
        build_dir=build_dir,
        test_dir=test_dir,
        results_xml=os.path.join(workdir, "results.xml"),
        timescale=timescale,
        parameters={},
        extra_env=probe_env,
        log_path=os.path.join(workdir, "test.log"),
        waves=False,
    )
    try:
        with open(result_path, encoding="utf-8") as handle:
            measured = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"the probe test left no readable result ({exc})") from exc
    version = measured.get("py_get_version") if isinstance(measured, dict) else None
    if not isinstance(version, str):
        raise RuntimeError(f"the probe test left an unexpected result: {measured!r}")
    library = measured.get("library")
    return {
        "py_get_version": version,
        "library": library if isinstance(library, str) and library else None,
    }


def _measure_vvp_python(
    *,
    runner_module: Any,
    module_dirs: list[str],
    env: dict[str, str],
    timescale: tuple[str, str],
    test_dir: str,
) -> dict[str, Any]:
    """The probe's outcome: ``{"status": "measured", "py_get_version": ...,
    "library": ...}`` or :func:`_inconclusive`. Never raises: a scratch
    directory, copy or write that fails (a full disk, say) costs the probe,
    not the user's run."""
    try:
        with tempfile.TemporaryDirectory(
            prefix="klt-python-runtime-probe-", ignore_cleanup_errors=True
        ) as workdir:
            shutil.copyfile(
                os.path.join(_PROBE_SOURCE_DIR, _PROBE_TEST_SOURCE),
                os.path.join(workdir, f"{PROBE_MODULE}.py"),
            )
            with open(
                os.path.join(workdir, f"{PROBE_MODULE}.v"), "w", encoding="utf-8"
            ) as handle:
                handle.write(_PROBE_VERILOG)
            return _run_bounded(
                _probe_in_process,
                runner_module=runner_module,
                module_dirs=module_dirs,
                env=env,
                timescale=timescale,
                test_dir=test_dir,
                workdir=workdir,
            )
    except OSError as exc:
        return _inconclusive(f"could not stage the probe ({exc})")


def _mismatch_message(
    own: Release, measured: Release, raw_version: str, library: str | None
) -> str:
    supplied_by = (
        f"supplied by {library}"
        if library
        else "from a library whose path could not be resolved"
    )
    return (
        f"klt is running on CPython {_format_release(own)}, but the Icarus vvp "
        f"process loads CPython {_format_release(measured)} for cocotb "
        f"(Py_GetVersion() inside vvp: {raw_version!r}), {supplied_by}. The "
        "cocotb wheel's Python side was installed for klt's interpreter, but "
        "inside vvp its compiled VPI module runs against a different CPython, "
        "so no results.xml from this run could be trusted. Make vvp load "
        "klt's own libpython -- check LD_LIBRARY_PATH, LIBPYTHON_LOC and "
        "GPI_USERS in the environment the vvp launcher sees, wrapper scripts "
        "included -- or run klt on the interpreter that library belongs to."
    )


def _check_icarus_python_runtime(
    runner_module: Any,
    engine: str,
    module_dirs: list[str],
    env: dict[str, str],
    timescale: tuple[str, str],
    test_dir: str,
) -> None:
    """Before the user's build: refuse the run when the CPython the Icarus
    ``vvp`` process loads is measurably not klt's own; silent on a match and
    on anything the probe could not establish.

    ``engine``/``module_dirs``/``env``/``timescale``/``test_dir`` are the
    request's own, so the probe sees the launcher (``vvp`` is started in the
    same working directory), import path, ``request.env`` and timescale the
    real run will get. No-op for every engine but Icarus (Verilator is out of
    scope); a Verilog bench never reaches this function.
    """
    if engine != "icarus":
        return
    outcome = _measure_vvp_python(
        runner_module=runner_module,
        module_dirs=module_dirs,
        env=env,
        timescale=timescale,
        test_dir=test_dir,
    )
    raw_version = outcome.get("py_get_version")
    if outcome.get("status") != "measured" or not isinstance(raw_version, str):
        return
    measured = _release(raw_version)
    if measured is None:
        return
    own = _klt_release()
    if measured == own:
        return
    library = outcome.get("library")
    raise FunctionalVerificationError(
        _mismatch_message(
            own, measured, raw_version, library if isinstance(library, str) else None
        )
    )


def _interpreter_mismatch_hint(engine: str, diagnostic: str) -> str:
    """The post-run augmentation of a missing ``results.xml`` (issue #1103)
    when a transcript carries one of ``INTERPRETER_MISMATCH_MARKERS``: a lead,
    never proof that the wheel is wrong.

    ``_embed_init_python`` is only the embed function's name in older cocotb
    log lines, and ``Unexpected sys.executable value`` is logged by a bundled
    ``vvp`` whose ``PYTHONHOME`` points at its own Python even though the run
    proceeds. Neither says which CPython was loaded: by the time this runs,
    the pre-build wheel-tag check (and, on Icarus, the runtime probe) have
    already declined to reject the run.
    """
    if engine == "icarus":
        preflight = (
            "The pre-build wheel-tag check and Python runtime probe raise only "
            "on a confirmed mismatch; neither did here."
        )
    else:
        preflight = (
            "The pre-build wheel-tag check raises only on a confirmed mismatch; "
            "it did not here."
        )
    return (
        f"probable cause: the {engine} transcript contains {diagnostic!r} -- "
        "cocotb's Python embed layer objecting to the interpreter it found "
        "inside the simulator. That is a lead, not proof that the wheel is "
        "wrong: the same line appears when vvp loads another libpython (its "
        "wrapper's environment, a preset GPI_USERS), when a bundled vvp sets "
        f"PYTHONHOME, or with a wheel built for another CPython release. {preflight}"
    )


# Imported last so that whichever module is imported first, the others find
# every name they need already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
