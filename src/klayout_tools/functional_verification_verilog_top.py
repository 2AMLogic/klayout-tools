"""Verilog-top (self-checking Verilog testbench) path of the cocotb
functional-verification flow.

Split out of ``functional_verification.py`` (the #1909 SDF-split precedent).
Names that stay in ``functional_verification.py`` are reached as
``_fv.<name>`` at call time, so monkeypatching them on
``klayout_tools.functional_verification`` still takes effect; every name is
re-exported from there.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from ._artifact_envelope import hashed_path_envelope as _hashed_path_envelope
from ._verilog_top import plusargs, require_module_declared

VERILOG_TOP_FIELDS = (
    "verilog_top",
    "pass_marker",
    "fail_marker",
    "dut_instance",
)

#: ``options`` keys the Verilog-top path cannot honor (each depends on cocotb
#: or on a per-test result stream), mapped to the value that means "unset".
_VERILOG_TOP_REJECTED_OPTIONS = (
    ("coverage", False),
    ("initial_state", None),
    ("trace", False),
    ("random_seed", None),
    ("timescale", None),
)


def _verilog_top_fields(testbench: dict[str, Any]) -> dict[str, Any]:
    """Shape-check the ``verilog_top`` testbench fields."""
    if "module" in testbench:
        raise _fv.FunctionalVerificationError(
            "request.testbench must give exactly one of 'module' (cocotb) or "
            "'verilog_top' (Verilog bench), not both"
        )
    unknown = sorted(
        set(testbench)
        - set(VERILOG_TOP_FIELDS)
        - {"search_path", "testcase", "plusargs"}
    )
    if unknown:
        raise _fv.FunctionalVerificationError(
            "request.testbench has unknown field(s) for verilog_top: "
            + ", ".join(unknown)
        )
    for cocotb_only in ("search_path", "testcase"):
        if testbench.get(cocotb_only) is not None:
            raise _fv.FunctionalVerificationError(
                f"request.testbench.{cocotb_only} applies to a cocotb "
                "testbench and cannot be combined with verilog_top"
            )
    resolved: dict[str, Any] = {}
    for name in VERILOG_TOP_FIELDS:
        value = testbench.get(name)
        if value is None and name == "verilog_top":
            raise _fv.FunctionalVerificationError(
                "request.testbench.verilog_top must be a non-empty string"
            )
        if value is not None and (not isinstance(value, str) or not value):
            raise _fv.FunctionalVerificationError(
                f"request.testbench.{name} must be a non-empty string when given"
            )
        resolved[name] = value
    resolved["plusargs"] = plusargs(
        testbench.get("plusargs"), _fv.FunctionalVerificationError
    )
    return resolved


def _reject_verilog_top_options(engine: str, options: Any) -> None:
    if engine != "icarus":
        raise _fv.FunctionalVerificationError(
            f"testbench.verilog_top requires engine 'icarus' -- engine "
            f"'{engine}' has no Verilog-bench path"
        )
    opts = options if isinstance(options, dict) else {}
    for key, unset in _VERILOG_TOP_REJECTED_OPTIONS:
        if opts.get(key, unset) != unset:
            raise _fv.FunctionalVerificationError(
                f"options.{key} cannot be combined with testbench.verilog_top "
                "-- not supported on the Verilog-bench path (it has no cocotb "
                "run to apply it to)"
            )


def _resolve_verilog_top(
    testbench: Any, sources: list[str], engine: str, options: Any
) -> dict[str, Any] | None:
    """Validate the Verilog-bench form of ``request.testbench`` (issue #4083).

    Returns ``None`` when ``testbench.verilog_top`` is absent (the ordinary
    cocotb path), else ``{"verilog_top", "pass_marker", "fail_marker",
    "dut_instance", "plusargs"}``. ``testbench.module`` and
    ``testbench.verilog_top`` are mutually exclusive. The bench's verdict is
    derived without cocotb (see :func:`_run_verilog_top`), so every option
    that needs cocotb is rejected here rather than silently ignored.
    """
    if not isinstance(testbench, dict) or "verilog_top" not in testbench:
        return None
    resolved = _verilog_top_fields(testbench)
    _reject_verilog_top_options(engine, options)
    top = resolved["verilog_top"]
    assert top is not None
    require_module_declared(top, sources, _fv.FunctionalVerificationError)
    return resolved


def _resolve_testbench_target(
    verilog_tb: dict[str, Any] | None, testbench: Any, request_dir: str
) -> tuple[str, str, str | list[str] | None]:
    """``(module, module_dir, testcase)`` for either testbench form."""
    if verilog_tb is None:
        return _fv._resolve_testbench(testbench, request_dir)
    top = verilog_tb["verilog_top"]
    assert top is not None
    return top, request_dir, None


def _sdf_annotate_scope(
    verilog_tb: dict[str, Any] | None, sdf_scope: str | None = None
) -> str:
    """Hierarchical scope the generated ``$sdf_annotate`` shim targets;
    ``options.sdf.scope`` (issue #4521) descends further below the DUT."""
    if verilog_tb is None:
        base = f"{_fv.SDF_WRAPPER_MODULE}.{_fv.SDF_WRAPPER_DUT_INSTANCE}"
        return f"{base}.{_fv._sdf_scope_reference(sdf_scope)}" if sdf_scope else base
    dut_instance = verilog_tb["dut_instance"]
    assert dut_instance is not None
    return dut_instance


def _sdf_extend_sources(
    verilog_tb: dict[str, Any] | None,
    sources: list[str],
    *,
    wrapper_path: str,
    shim_path: str,
    hdl_toplevel: str,
    ports: Any,
    wrapper_parameters: tuple[str, list[str]] = ("", []),
) -> tuple[list[str], str]:
    """Add the SDF scaffolding to ``sources``; return it with the build top.

    A Verilog bench already nests the DUT one level down, so there is no
    pass-through wrapper: the bench itself is the second root alongside the
    shim.
    """
    if verilog_tb is not None:
        top = verilog_tb["verilog_top"]
        assert top is not None
        return [*sources, shim_path], top
    _fv._write_sdf_dut_wrapper(
        wrapper_path,
        hdl_toplevel=hdl_toplevel,
        ports=ports,
        parameters=wrapper_parameters,
    )
    return [*sources, wrapper_path, shim_path], _fv.SDF_WRAPPER_MODULE


def _check_verilog_top_sdf(
    verilog_tb: dict[str, Any] | None, sdf: Any, parameters: Any
) -> None:
    """``testbench.dut_instance`` is the SDF scope, so it is required with
    ``options.sdf`` and meaningless without it. ``request.parameters`` is
    rejected: it would have to target the bench, not the DUT. A no-op on the
    cocotb path (``verilog_tb is None``)."""
    if verilog_tb is None:
        return
    if parameters:
        raise _fv.FunctionalVerificationError(
            "request.parameters cannot be combined with testbench.verilog_top"
        )
    if sdf is not None and sdf.get("scope"):
        raise _fv.FunctionalVerificationError(
            "options.sdf.scope only applies to the cocotb path; with "
            "testbench.verilog_top the scope is testbench.dut_instance"
        )
    if sdf is not None and not verilog_tb["dut_instance"]:
        raise _fv.FunctionalVerificationError(
            "options.sdf with testbench.verilog_top requires "
            "testbench.dut_instance (the DUT's hierarchical path inside the "
            "bench, e.g. 'tb.dut') -- it is the $sdf_annotate scope"
        )
    if sdf is None and verilog_tb["dut_instance"]:
        raise _fv.FunctionalVerificationError(
            "testbench.dut_instance only applies together with options.sdf"
        )


def _verilog_top_sdf_gate(
    sdf: dict[str, str] | None,
    engine: str,
    build_log: str,
    test_log: str,
    sdf_pre_dropped_counts: dict[str, int],
) -> dict[str, int]:
    """Raise on SDF back-annotation diagnostics; return the dropped counts."""
    if sdf is None:
        return {}
    diagnostics, scanned = _fv._scan_sdf_diagnostics(build_log, test_log)
    if diagnostics:
        shown = " | ".join(diagnostics[:5])
        more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
        raise _fv.FunctionalVerificationError(
            f"SDF back-annotation of '{sdf['file']}' did not fully apply: "
            f"{len(diagnostics)} diagnostic(s) in the {engine} transcript "
            f"-- {shown}{more}. The simulator exits 0 after skipping an "
            "annotation it cannot apply, so this run's verdict would have "
            "been a zero-delay one reported as if it were annotated."
        )
    return {**sdf_pre_dropped_counts, **scanned}


def _verilog_top_error(
    returncode: int, transcript: str, pass_marker: str | None, fail_marker: str | None
) -> str | None:
    """Why the bench run failed, or ``None`` when it passed."""
    if returncode != 0:
        return f"vvp exited with status {returncode}"
    if fail_marker and fail_marker in transcript:
        return f"fail_marker {fail_marker!r} found in the transcript"
    if pass_marker and pass_marker not in transcript:
        return (
            f"pass_marker {pass_marker!r} not found in the transcript "
            "(vvp exited 0 without reporting a pass)"
        )
    return None


def _run_verilog_top(
    verilog_tb: dict[str, Any],
    *,
    engine: str,
    hdl_toplevel: str,
    sources: list[str],
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    build_dir: str,
    build_log: str,
    test_log: str,
    sdf: dict[str, str] | None,
    sdf_pre_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
    sdf_renamed: list[dict[str, str]],
    source_defines: dict[str, Any],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the bench with ``iverilog``, run it with ``vvp`` and assemble the
    ordinary report (issue #4083). No cocotb is involved.

    The verdict never trusts an exit code alone: the run passes only when
    ``vvp`` exits 0, no ``fail_marker`` substring appears in the transcript
    and, when ``pass_marker`` is declared, that substring does appear. A
    ``$fatal`` exits nonzero and so fails. A bench that exits 0 without ever
    printing its ``pass_marker`` fails, so a bench that silently did nothing
    cannot read as green. The SDF diagnostic gate runs first, exactly as on
    the cocotb path.
    """
    top = verilog_tb["verilog_top"]
    pass_marker = verilog_tb["pass_marker"]
    fail_marker = verilog_tb["fail_marker"]
    plusargs: list[str] = verilog_tb["plusargs"]
    assert top is not None

    os.makedirs(build_dir, exist_ok=True)
    vvp_path = os.path.join(build_dir, "sim.vvp")
    cmd = ["iverilog", "-o", vvp_path, "-s", top, "-g2012"]
    cmd += [f"-I{path}" for path in includes]
    for key, value in defines.items():
        cmd.append(f"-D{key}" if value is None else f"-D{key}={value}")
    cmd += build_args + sources
    try:
        with open(build_log, "w", encoding="utf-8") as handle:
            built = subprocess.run(
                cmd, stdout=handle, stderr=subprocess.STDOUT, check=False
            )
    except OSError as exc:
        raise _fv.FunctionalVerificationError(
            f"could not launch the icarus build: {exc}"
        ) from exc
    if built.returncode != 0:
        raise _fv.FunctionalVerificationError(
            f"icarus build/elaboration failed (exit {built.returncode}): "
            f"{_fv._log_tail(build_log)}"
        )

    try:
        with open(test_log, "w", encoding="utf-8") as handle:
            ran = subprocess.run(
                ["vvp", vvp_path, *plusargs],
                stdout=handle,
                stderr=subprocess.STDOUT,
                cwd=build_dir,
                check=False,
            )
    except OSError as exc:
        raise _fv.FunctionalVerificationError(f"could not launch vvp: {exc}") from exc
    with open(test_log, encoding="utf-8", errors="replace") as handle:
        transcript = handle.read()

    sdf_dropped_counts = _verilog_top_sdf_gate(
        sdf, engine, build_log, test_log, sdf_pre_dropped_counts
    )
    error_message = _verilog_top_error(
        ran.returncode, transcript, pass_marker, fail_marker
    )
    entry: dict[str, Any] = {
        "name": top,
        "status": "failed" if error_message else "passed",
        "sim_time_ns": None,
        "real_time_s": None,
    }
    if error_message:
        entry["error_type"] = "VerilogBenchFailure"
        entry["error_message"] = error_message
    failed_count = 1 if error_message else 0

    report = _fv._Report(
        {
            "schema_version": _fv.SCHEMA_VERSION,
            "engine": engine,
            "hdl_toplevel": hdl_toplevel,
            "testbench": top,
            "status": "fail" if failed_count else "pass",
            "test_count": 1,
            "passed_count": 1 - failed_count,
            "failed_count": failed_count,
            "skipped_count": 0,
            "tests": [entry],
            "coverage": None,
            "trace": None,
            "environment": {
                "engine": engine,
                "engine_version": _fv._engine_version(engine),
                "tools": tools,
                "cocotb_version": None,
                "results_xml": _hashed_path_envelope(None),
                "test_log": _hashed_path_envelope(test_log),
                "build_log": _hashed_path_envelope(build_log),
                "random_seed": None,
                "source_defines": source_defines,
                "plusargs": plusargs,  # issue #5006; `[]` when absent
                "initial_state": None,
                "sdf": (
                    None
                    if sdf is None
                    else {
                        "file": sdf["file"],
                        "corner": sdf["corner"],
                        "entries": sdf["entries"],
                        **({"scope": sdf["scope"]} if "scope" in sdf else {}),
                        "annotated": True,
                        "partial": bool(sdf_dropped_counts),
                        "dropped": _fv._sdf_dropped_report(
                            sdf_dropped_counts, sdf_pre_dropped_arcs
                        ),
                        **_fv._sdf_renamed_report(sdf_renamed),
                    }
                ),
            },
        }
    )
    report.local_paths = {
        "results_xml": None,
        "test_log": test_log,
        "build_log": build_log,
    }
    return report


def _reject_verilog_top_mutations(testbench: Any) -> None:
    if isinstance(testbench, dict) and "verilog_top" in testbench:
        raise _fv.FunctionalVerificationError(
            "testbench.verilog_top cannot be combined with --mutations -- "
            "not yet designed (the Verilog-top path has no per-test results "
            "to attribute a kill to)"
        )


# Imported last so that whichever of the two modules is imported first, the
# other finds every name it needs already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
