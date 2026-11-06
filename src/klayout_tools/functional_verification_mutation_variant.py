"""Isolated per-variant build-and-test machinery for mutation testing.

Split out of ``functional_verification_mutation.py``: the forked-child
``_build_and_test_variant`` and its bounded parent-side runner
(:func:`_run_isolated_variant`). Names that stay in ``functional_verification.py``
are reached as ``_fv.<name>`` at call time; every name is re-exported from there.
"""

from __future__ import annotations

import multiprocessing
import os
import queue as _queue_module
import signal
from typing import Any


def _build_and_test_variant(
    *,
    engine: str,
    sources: list[str],
    hdl_toplevel: str,
    build_dir: str,
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    env: dict[str, str],
    build_log: str,
    module: str,
    module_dir: str,
    testcase: str | list[str] | None,
    random_seed: int | None,
    test_dir: str,
    results_xml: str,
    test_log: str,
) -> None:
    """``_run_build`` then ``_run_test`` against whatever source bytes are on
    disk right now -- the isolated per-mutant build+test this module runs
    once per resolved proposal, inside :func:`plan.applied(index)
    <klayout_tools._vendor.mutation_variants.MutationVariantPlan.applied>`
    so the mutated bytes are only ever on disk for the duration of this call.

    Always runs inside the forked child process :func:`_run_isolated_variant`
    starts -- never called directly from the parent process -- so that a
    mutation that wedges the simulator (spike §2's mutant #4: an FSM that
    never reaches its terminal count) can be killed outright by the parent
    once :data:`DEFAULT_MUTATION_TIMEOUT_S` elapses, which is not possible
    for a blocking in-process call once cocotb's own ``Runner.test()`` has
    handed control to a synchronous ``subprocess.run()`` with no timeout
    parameter of its own (verified against cocotb 2.0.1's own
    ``cocotb_tools/runner.py``).
    """
    runner_module = _fv._import_runner()
    engine_runner = _fv._run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        build_dir=build_dir,
        build_args=build_args,
        defines=defines,
        includes=includes,
        timescale=timescale,
        parameters=parameters,
        log_path=build_log,
    )
    _fv._run_test(
        engine_runner,
        engine=engine,
        module=module,
        module_dir=module_dir,
        hdl_toplevel=hdl_toplevel,
        testcase=testcase,
        random_seed=random_seed,
        build_dir=build_dir,
        test_dir=test_dir,
        results_xml=results_xml,
        timescale=timescale,
        parameters=parameters,
        extra_env=env,
        log_path=test_log,
    )


def _mutation_subprocess_entry(kwargs: dict[str, Any], result_queue: Any) -> None:
    """Target of the forked child process :func:`_run_isolated_variant`
    starts. ``os.setpgrp()`` moves the child into its own process group
    *before* it can spawn cocotb's own simulator subprocess, so the parent
    can SIGKILL the whole group -- child plus simulator grandchild -- rather
    than orphaning the simulator process on a timeout kill.
    """
    try:
        os.setpgrp()
    except OSError:  # pragma: no cover - defensive; absent on non-POSIX
        pass
    try:
        _build_and_test_variant(**kwargs)
    except BaseException as exc:  # noqa: BLE001 - reported back, never raised across the process boundary
        try:
            result_queue.put(("error", f"{exc}"))
        except Exception:  # pragma: no cover - defensive: an unpicklable exc
            result_queue.put(("error", f"{type(exc).__name__}"))
        return
    result_queue.put(("ok", None))


def _kill_process_tree(pid: int | None) -> None:
    """SIGKILL ``pid``'s whole process group (see :func:`_mutation_subprocess_entry`),
    falling back to just ``pid`` if the group lookup itself fails. Never
    raises -- called only after a timeout has already been decided; a kill
    that fails because the process already exited on its own is not this
    function's problem to report."""
    if pid is None:
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _run_isolated_variant(
    *, timeout_s: float, **build_and_test_kwargs: Any
) -> tuple[bool, tuple[str, str | None]]:
    """Run :func:`_build_and_test_variant` in a forked child process bounded
    by ``timeout_s`` wall-clock seconds -- the ``klt``-level per-mutant
    subprocess timeout (issue #1592, spike "Open questions"), independent of
    and in addition to whatever cycle bound the testbench's own code has.

    Forking (rather than, say, a ``threading.Thread`` with a join timeout)
    is the load-bearing choice: a thread cannot forcibly terminate a
    blocking ``subprocess.run()`` several call frames down inside cocotb's
    own ``Runner.test()``, which has no timeout parameter to begin with
    (this module's own :func:`_build_and_test_variant` docstring). A forked
    child gives this function an OS process-group handle it can SIGKILL
    outright on a timeout -- see :func:`_kill_process_tree`.

    Returns ``(timed_out, (status, detail))``:

    - ``timed_out=True`` -- the child did not finish within ``timeout_s`` and
      was killed. Booley's own rule, ported verbatim (spike §1a): **"a
      timed-out mutant counts as detected because the mutation can wedge the
      design"** -- the caller classifies this as ``"killed"``, never
      ``"rejected"``.
    - ``timed_out=False, ("ok", None)`` -- the isolated build+test ran to
      completion (a failing *regression* is not an error here -- see
      :func:`_run_test`); the caller compares its ``results.xml`` against the
      baseline's own to classify ``"killed"``/``"survived"``.
    - ``timed_out=False, ("error", detail)`` -- the isolated build+test
      raised (a build/elaboration failure, a missing ``results.xml``, or any
      other exception) before producing a trustworthy verdict; the caller
      classifies this ``"rejected"``.
    """
    if "fork" not in multiprocessing.get_all_start_methods():
        # POSIX-only by construction (`os.setpgrp`/`os.killpg` above are
        # both POSIX-only too) -- a clear request-time error beats a cryptic
        # platform failure deep inside `multiprocessing`.
        raise _fv.FunctionalVerificationError(
            "--mutations requires OS process-fork support (POSIX) to bound "
            "each isolated per-mutant build+test with a klt-level subprocess "
            "timeout -- unavailable on this platform"
        )

    ctx = multiprocessing.get_context("fork")
    result_queue: Any = ctx.Queue()
    process = ctx.Process(
        target=_mutation_subprocess_entry,
        args=(build_and_test_kwargs, result_queue),
    )
    process.start()
    process.join(timeout_s)
    if process.is_alive():
        _kill_process_tree(process.pid)
        process.join(_fv._MUTATION_KILL_GRACE_S)
        return True, ("error", f"timed out after {timeout_s}s")

    try:
        return False, result_queue.get_nowait()
    except _queue_module.Empty:
        return False, (
            "error",
            "isolated build/test process exited without a result "
            f"(exit code {process.exitcode})",
        )


def _first_new_failure(
    baseline_tests: list[dict[str, Any]], mutant_tests: list[dict[str, Any]]
) -> str | None:
    """The first ``@cocotb.test()`` name in ``mutant_tests`` that failed
    where the baseline did not (spike §3c's ``first_killing_test``), or
    ``None`` if every mutant failure (if any) also failed on the baseline.

    The baseline-must-pass gate (:func:`run_functional_verification_with_mutations`)
    means ``baseline_tests`` never actually contains a ``"failed"`` entry by
    the time this is called -- so in practice this reduces to "the first
    mutant test that failed at all" -- but the lookup is written generally
    rather than assumed, since nothing about this function's own contract
    depends on that invariant holding.
    """
    baseline_status_by_name = {test["name"]: test["status"] for test in baseline_tests}
    for test in mutant_tests:
        baseline_status = baseline_status_by_name.get(test["name"])
        if test["status"] == "failed" and baseline_status != "failed":
            return test["name"]
    return None


# Imported last so that whichever module is imported first, the others find
# every name they need already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
