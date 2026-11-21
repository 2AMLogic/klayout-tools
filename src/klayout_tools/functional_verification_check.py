"""Offline input verification for committed functional-verification reports."""

from __future__ import annotations

import re
from typing import Any

from . import functional_verification as fv
from ._provenance import INPUT_ROLE_SOURCE
from ._report_verify import build_check_result, hash_check, load_committed_report
from .functional_verification_run_inputs import resolve_run_inputs


def _invalid_report(field: str) -> fv.FunctionalVerificationError:
    return fv.FunctionalVerificationError(
        f"Committed functional-verification report has missing or invalid {field}. "
        "Regenerate the report with klt functional-verification."
    )


def _digest(block: Any, path: str, key: str) -> str:
    value = block.get(key) if isinstance(block, dict) else None
    pattern = r"sha256:[0-9a-f]{64}" if key == "content_hash" else r"[0-9a-f]{64}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise _invalid_report(f"{path}.{key}")
    return value


def _input_pins(
    provenance: Any, sources: Any, synth: Any, has_sdf: bool
) -> dict[str, str]:
    """Read all required hashes for this input mode before comparing any of them."""
    pins = {
        "provenance.request_sha256": _digest(provenance, "provenance", "request_sha256")
    }
    if not isinstance(sources, list) or not sources:
        raise _invalid_report("environment.sources")
    for index, source in enumerate(sources):
        path = f"environment.sources[{index}]"
        pins[f"{path}.sha256"] = _digest(source, path, "sha256")
    if synth is None:
        rtl = provenance.get("input")
        pins["provenance.input.content_hash"] = _digest(
            rtl, "provenance.input", "content_hash"
        )
        if rtl.get("role") != INPUT_ROLE_SOURCE:
            raise _invalid_report("provenance.input.role")
    else:
        if "input" not in provenance or provenance["input"] is not None:
            raise _invalid_report("provenance.input for synthesized netlist")
        pins["from_synthesize.netlist_sha256"] = _digest(
            synth, "from_synthesize", "netlist_sha256"
        )
    if has_sdf:
        pins["provenance.sdf_sha256"] = _digest(provenance, "provenance", "sdf_sha256")
    elif "sdf_sha256" in provenance:
        raise _invalid_report("environment.sdf for provenance.sdf_sha256")
    return pins


def _committed_pins(report: dict[str, Any]) -> dict[str, str]:
    if report.get("status") not in ("pass", "fail"):
        raise _invalid_report("functional-verification status")
    env = report.get("environment")
    if not isinstance(env, dict) or "sdf" not in env:
        raise _invalid_report("environment.sdf")
    if env["sdf"] is not None and not isinstance(env["sdf"], dict):
        raise _invalid_report("environment.sdf")
    return _input_pins(
        report.get("provenance"),
        env.get("sources"),
        report.get("from_synthesize"),
        env["sdf"] is not None,
    )


def _current_pins(
    doc: dict[str, Any], request_dir: str, pdk_variant: str | None, pdk_root: str | None
) -> dict[str, str]:
    engine = fv._resolve_engine(doc)
    options = doc.get("options")
    sources, inputs = resolve_run_inputs(
        doc,
        request_dir,
        doc["hdl_toplevel"],
        fv._path_envelope,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        verify_synthesis_pin=False,
    )

    # Validate the recipe without loading the bench, inspecting HDL modules,
    # probing required tools, or setting up simulator artifacts.
    testbench = doc["testbench"]
    verilog_tb = fv._resolve_verilog_top(testbench, None, engine, options)
    if verilog_tb is None:
        fv._resolve_testbench(testbench, request_dir, require_module=False)
    _, _, _, _, _, _, sdf, _ = fv._resolve_options(
        options, engine, request_dir, require_include_dirs=False
    )
    fv._check_verilog_top_sdf(verilog_tb, sdf, doc.get("parameters"))
    initial_state = fv._resolve_initial_state(
        options, engine, request_dir, require_netlist=False
    )
    if initial_state is not None and sdf is not None:
        raise fv.FunctionalVerificationError(
            "options.initial_state cannot be combined with options.sdf"
        )
    fv._resolve_parameters(doc.get("parameters"))
    fv._resolve_env(doc.get("env"), verilog_top=verilog_tb is not None)
    fv._requires_tool_entries(doc.get("requires_tools"))
    fv._resolve_source_defines(options, sources, request_dir)
    inputs.pin_sdf(sdf)
    return _input_pins(
        inputs.provenance, inputs.sources, inputs.from_synthesize, sdf is not None
    )


def check_functional_verification_report(
    report_path: str,
    request: str,
    *,
    base_dir: str | None = None,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    report = load_committed_report(report_path, fv.FunctionalVerificationError)
    expected = _committed_pins(report)
    doc, directory = fv.load_request_arg(request, base_dir=base_dir)
    try:
        actual = _current_pins(doc, directory, pdk_variant, pdk_root)
    except OSError as exc:
        raise fv.FunctionalVerificationError(
            f"Cannot read functional-verification input: {exc}"
        ) from exc
    # Changing input modes legitimately adds or removes pins. With the same
    # request, those mode-specific pins must already be in the committed report.
    if expected["provenance.request_sha256"] == actual["provenance.request_sha256"]:
        for field in (
            "provenance.sdf_sha256",
            "provenance.input.content_hash",
            "from_synthesize.netlist_sha256",
        ):
            if (field in actual) != (field in expected):
                raise _invalid_report(field)
    checks = [
        hash_check(field, expected.get(field), actual.get(field))
        for field in dict.fromkeys((*expected, *actual))
    ]
    return build_check_result(report_path=report_path, checks=checks)
