"""Tests for `klt sim` and the `klayout_tools.sim` library.

Two tiers, per the issue's testing requirement (#91):

- **Unit tests** (the majority) exercise the corner-matrix expansion, log
  classification, `.meas` parsing, limit evaluation, rawfile parsing, and
  model-library resolution as pure functions -- either directly, or by
  stubbing `subprocess.run` so `run_sim`'s full per-corner pipeline is
  exercised without ever invoking the real `ngspice` binary. These always
  run, everywhere, and are the ones a classification/parsing regression
  actually gets caught by.
- **Integration tests** (`@pytest.mark.skipif(not HAVE_NGSPICE, ...)`) run
  the real `ngspice -b` subprocess end to end -- corner expansion, `.lib`
  process-corner selection, `alter`-based supply sweep, `.temp`, `.meas`
  extraction, and the optional waveform artifact -- against tiny, synthetic,
  non-PDK fixtures (see `examples/sim/generate.py`'s docstring for why no
  real PDK data is vendored here). CI installs `ngspice` via the package
  manager (`.github/workflows/ci.yml`) so these always run there; they skip
  with a clear reason on a dev machine without it, rather than silently
  passing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from klayout_tools import pdk, sim
from klayout_tools.cli import main

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "sim"


# --------------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------------- #


def _write_body(tmp_path: Path, name: str = "body.spice") -> Path:
    path = tmp_path / name
    path.write_text(".param vdd=1.0\nVdd vdd 0 DC {vdd}\nR1 vdd out 1k\nC1 out 0 1n\n")
    return path


def _write_corner_lib(tmp_path: Path, name: str = "corner.lib") -> Path:
    path = tmp_path / name
    path.write_text(
        ".lib tt\n.param corner_scale=1.0\n.endl tt\n\n"
        ".lib ss\n.param corner_scale=0.9\n.endl ss\n"
    )
    return path


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


# --------------------------------------------------------------------------- #
# load_request
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim.load_request(str(tmp_path / "nope.json"))


def test_load_request_is_a_directory(tmp_path):
    with pytest.raises(sim.SimError, match="not a file"):
        sim.load_request(str(tmp_path))


def test_load_request_not_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("this is not json {{{")
    with pytest.raises(sim.SimError, match="not valid JSON"):
        sim.load_request(str(path))


def test_load_request_non_object(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(sim.SimError, match="JSON object"):
        sim.load_request(str(path))


@pytest.mark.parametrize("missing_field", ["netlist", "analysis"])
def test_load_request_missing_required_field(tmp_path, missing_field):
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    del request[missing_field]
    path = _write_request(tmp_path, request)
    with pytest.raises(sim.SimError, match=missing_field):
        sim.load_request(str(path))


def test_load_request_does_not_require_models_or_schema(tmp_path):
    # `models` is validated in run_sim (only when corners.process is set);
    # a bare "schema" field (the spike's shape) is never required.
    request = {"netlist": "x.spice", "analysis": {"kind": "tran", "args": "1n 1u"}}
    path = _write_request(tmp_path, request)
    assert sim.load_request(str(path)) == request


# --------------------------------------------------------------------------- #
# run_sim: request-level validation (raised before any corner runs)
# --------------------------------------------------------------------------- #


def test_run_sim_unsupported_engine_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "engine": "xyce",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported engine"):
        sim.run_sim(str(request))


def test_run_sim_unsupported_backend_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "bogus",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request))


def test_run_sim_unsupported_backend_via_cli_flag_raises(tmp_path):
    # The --backend flag overrides the request field and is validated the
    # same way (unknown name -> SimError, not a silent fallback to local).
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported backend"):
        sim.run_sim(str(request), backend="remote")


def test_run_sim_netlist_not_found_raises(tmp_path):
    request = _write_request(
        tmp_path,
        {"netlist": "missing.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    with pytest.raises(sim.SimError, match="netlist not found"):
        sim.run_sim(str(request))


def test_run_sim_process_corner_without_models_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {"process": ["tt"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="models.lib"):
        sim.run_sim(str(request))


def test_run_sim_analysis_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran"}},
    )
    with pytest.raises(sim.SimError, match="analysis"):
        sim.run_sim(str(request))


def test_run_sim_measurement_missing_fields_raises(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [{"name": "vout"}],
        },
    )
    with pytest.raises(sim.SimError, match="measurements"):
        sim.run_sim(str(request))


def test_run_sim_netlist_source_invalid_value_raises(tmp_path):
    # Per the JSON contract's error shape (docs/json-contract.md), an
    # unrecognized netlist_source is a loud application error (SimError,
    # exit 1) -- never silently ignored or coerced.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": "post-layout",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    with pytest.raises(sim.SimError, match="unsupported netlist_source"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# `.meas op` rejection -- ngspice has no `.MEASURE OP` analysis type (#205)
# --------------------------------------------------------------------------- #


def test_validate_meas_card_rejects_op():
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim._validate_meas_card("vout_meas", ".meas op vout_meas find v(out)")


def test_validate_meas_card_accepts_supported_types():
    # Every ngspice-implemented `.meas` type is accepted -- no SimError.
    for card in (
        ".meas dc vout find v(out) at=1.0",
        ".meas ac vout find v(out) at=1k",
        ".meas tran vout find v(out) at=1n",
        ".meas sp vout find v(out) at=1k",
        ".measure tran vout find v(out) at=1n",  # long-form spelling
    ):
        sim._validate_meas_card("vout", card)  # must not raise


def test_run_sim_analysis_kind_op_with_meas_op_card_raises_clear_error(tmp_path):
    # The exact pairing this issue reports: `analysis.kind: "op"` paired with
    # a `.meas op` card must fail with a clear, actionable SimError *before*
    # ngspice ever runs -- never a raw
    # "Error: unrecognized analysis type 'op'" ngspice parse failure.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "op", "args": ""},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match=r"\.MEASURE OP"):
        sim.run_sim(str(request))


def test_run_sim_meas_op_card_rejected_even_with_non_op_analysis_kind(tmp_path):
    # A `.meas op` card is invalid on ngspice's own terms regardless of the
    # request's `analysis.kind` -- not just when the two literally match.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas op vout_meas find v(out)"}
            ],
        },
    )
    with pytest.raises(sim.SimError, match="unsupported .meas analysis type"):
        sim.run_sim(str(request))


# --------------------------------------------------------------------------- #
# Model-library resolution (klt pdk integration, #45)
# --------------------------------------------------------------------------- #


def test_resolve_models_lib_via_pdk_variant(tmp_path):
    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech" / "ngspice"
    variant_dir.mkdir(parents=True)
    lib_path = variant_dir / "sky130.lib.spice"
    lib_path.write_text(".lib tt\n.endl tt\n")
    # A minimal libs.tech marker so pdk.find_pdk recognises the variant.
    (install_root / "sky130A" / "libs.tech" / "klayout").mkdir()

    resolved = sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_via_pdk_missing_install_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim._resolve_models_lib(
            {
                "pdk": "sky130A",
                "pdk_root": str(tmp_path / "nope"),
                "lib": "sky130.lib.spice",
            },
            request_dir=str(tmp_path),
        )


def test_resolve_models_lib_env_var_expansion(tmp_path, monkeypatch):
    lib_path = _write_corner_lib(tmp_path)
    monkeypatch.setenv("PDK_ROOT", str(tmp_path))

    resolved = sim._resolve_models_lib(
        {"lib": "$PDK_ROOT/corner.lib"}, request_dir=str(tmp_path)
    )

    assert resolved == str(lib_path)


def test_resolve_models_lib_relative_to_request_dir(tmp_path):
    _write_corner_lib(tmp_path)

    resolved = sim._resolve_models_lib({"lib": "corner.lib"}, request_dir=str(tmp_path))

    assert resolved == str(tmp_path / "corner.lib")


def test_resolve_models_lib_missing_lib_field_raises(tmp_path):
    with pytest.raises(sim.SimError, match="models.lib"):
        sim._resolve_models_lib({}, request_dir=str(tmp_path))


def test_resolve_models_lib_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError, match="not found"):
        sim._resolve_models_lib({"lib": "nope.lib"}, request_dir=str(tmp_path))


# --------------------------------------------------------------------------- #
# Corner-matrix expansion
# --------------------------------------------------------------------------- #


def test_expand_corners_defaults_to_single_point():
    points = sim._expand_corners({}, [])

    assert len(points) == 1
    (point,) = points
    assert point.process is None
    assert point.supply_v == {}
    assert point.temperature_c == 27
    assert point.corner_id == "default/novdd/27C"


def test_expand_corners_cross_product_order():
    points = sim._expand_corners(
        {
            "process": ["tt", "ss"],
            "supply_v": {"vdd": [1.62, 1.98]},
            "temperature_c": [-40, 125],
        },
        [],
    )

    # process outermost, temperature innermost -- odometer-style.
    ids = [p.corner_id for p in points]
    assert ids == [
        "tt/1.620V/-40C",
        "tt/1.620V/125C",
        "tt/1.980V/-40C",
        "tt/1.980V/125C",
        "ss/1.620V/-40C",
        "ss/1.620V/125C",
        "ss/1.980V/-40C",
        "ss/1.980V/125C",
    ]


def test_expand_corners_exclude_partial_match():
    points = sim._expand_corners(
        {"process": ["tt", "ss"], "temperature_c": [-40, 125]},
        [{"process": "ss", "temperature_c": -40}],
    )

    ids = [p.corner_id for p in points]
    assert "ss/novdd/-40C" not in ids
    assert len(ids) == 3


def test_expand_corners_supply_rails_move_together():
    points = sim._expand_corners(
        {"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7, 2.0]}}, []
    )

    assert len(points) == 2
    assert points[0].supply_v == {"vdd": 1.62, "vdda": 1.7}
    assert points[1].supply_v == {"vdd": 1.98, "vdda": 2.0}


def test_expand_corners_mismatched_supply_lengths_raises():
    with pytest.raises(sim.SimError, match="same length"):
        sim._expand_corners({"supply_v": {"vdd": [1.62, 1.98], "vdda": [1.7]}}, [])


def test_corner_id_multi_rail_format():
    point = sim.CornerPoint("tt", {"vdd": 1.8, "vdda": 1.7}, 27)
    assert point.corner_id == "tt/vdd=1.800_vdda=1.700V/27C"


def test_corner_slug_is_filesystem_safe():
    point = sim.CornerPoint("ss", {"vdd": 1.62}, -40)
    assert "/" not in point.slug
    assert point.slug == "ss_1p620V_n40C"


# --------------------------------------------------------------------------- #
# .meas log parsing
# --------------------------------------------------------------------------- #


def test_parse_measurements_success():
    log = (
        "  Measurements for Transient Analysis\n\n"
        "vout_final          =  1.00000e+00\n"
        "vout_avg            =  9.50000e-01 from=  5.00000e-06 to=  1.00000e-05\n"
    )
    assert sim._parse_measurements(log) == {"vout_final": 1.0, "vout_avg": 0.95}


def test_parse_measurements_failed_is_absent():
    log = (
        "  Measurements for Transient Analysis\n\n\n"
        "Error: measure  vout_high  find(AT) : out of interval\n"
        " .meas tran vout_high find v(out) when v(out)=5 failed!\n"
    )
    assert sim._parse_measurements(log) == {}


def test_parse_measurements_ignores_unrelated_lines():
    log = (
        "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n"
        "Node                                   Voltage\n"
        "vdd                                          1\n"
        "No. of Data Rows : 10008\n"
    )
    assert sim._parse_measurements(log) == {}


# --------------------------------------------------------------------------- #
# Diagnostic classification (from log text, never exit code)
# --------------------------------------------------------------------------- #


def test_classify_diagnostics_singular_matrix():
    log = "Warning: singular matrix:  check node b\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert codes == ["singular_matrix"]


def test_classify_diagnostics_nonconvergence():
    log = "Warning: Dynamic gmin stepping failed\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "nonconvergence" in codes


def test_classify_diagnostics_netlist_error():
    log = "Error: unknown subckt: foo\n"
    codes = [d["code"] for d in sim._classify_diagnostics(log)]
    assert "netlist" in codes


def test_classify_diagnostics_clean_log_is_empty():
    log = "Note: Transient op finished successfully\nngspice-46 done\n"
    assert sim._classify_diagnostics(log) == []


def test_classify_diagnostics_does_not_false_positive_on_title_text():
    # A netlist comment that happens to mention "singular matrix" in prose
    # (not an actual `Warning:` line) must not be misclassified.
    log = (
        "Circuit: * a singular matrix test circuit\n"
        "Note: Transient op finished successfully\n"
    )
    assert sim._classify_diagnostics(log) == []


# --------------------------------------------------------------------------- #
# Recovered singular_matrix/nonconvergence classification (#205)
# --------------------------------------------------------------------------- #


def test_recovered_from_stepping_true_when_measurements_all_resolved():
    log = (
        "Warning: singular matrix:  check node b\n"
        "Note: Transient op finished successfully\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert sim._recovered_from_stepping(measurements_spec, measurement_results, log)


def test_recovered_from_stepping_false_with_no_measurements_declared():
    # No measurements[] at all -- nothing independently confirms the run
    # actually succeeded, so a singular-matrix classification stays fatal.
    log = "Warning: singular matrix:  check node b\n"
    assert sim._recovered_from_stepping([], [], log) is False


def test_recovered_from_stepping_false_when_a_measurement_errored():
    log = "Warning: singular matrix:  check node b\n"
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    measurement_results = [{"name": "vout", "value": None, "status": "error"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


def test_recovered_from_stepping_false_on_simulation_aborted_trailer():
    log = (
        "Warning: singular matrix:  check node b\n"
        "doAnalyses: TRAN:  Timestep too small\n"
        "tran simulation(s) aborted\n"
    )
    measurements_spec = [{"name": "vout", "spice": ".meas tran vout find v(out) at=1n"}]
    # Even if a stale measurement value somehow parsed, the abort trailer
    # alone is decisive.
    measurement_results = [{"name": "vout", "value": 1.0, "status": "pass"}]
    assert (
        sim._recovered_from_stepping(measurements_spec, measurement_results, log)
        is False
    )


# --------------------------------------------------------------------------- #
# Limit evaluation (margin sign convention)
# --------------------------------------------------------------------------- #


def test_evaluate_limits_no_limits_never_fails():
    status, margin = sim._evaluate_limits(1.0, None)
    assert status == "pass"
    assert margin is None


def test_evaluate_limits_pass_within_bounds():
    status, margin = sim._evaluate_limits(1.20, {"min": 1.19, "max": 1.21})
    assert status == "pass"
    assert margin == pytest.approx(0.01)  # nearest binding limit, positive


def test_evaluate_limits_fail_below_min():
    status, margin = sim._evaluate_limits(1.10, {"min": 1.19, "max": 1.21})
    assert status == "fail"
    assert margin < 0


def test_evaluate_limits_fail_above_max():
    status, margin = sim._evaluate_limits(5e-6, {"max": 4e-6})
    assert status == "fail"
    assert margin == pytest.approx(-1e-6)


def test_evaluate_limits_pass_min_only():
    status, margin = sim._evaluate_limits(2.0, {"min": 1.0})
    assert status == "pass"
    assert margin == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Measurement rollup (worst-case selection, aggregate status)
# --------------------------------------------------------------------------- #


def test_rollup_measurements_worst_case_is_most_negative_margin():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "vref", "value": 1.20, "margin": 0.01, "status": "pass"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "vref", "value": 1.25, "margin": -0.04, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "vref", "unit": "V", "limits": {"max": 1.21}}], corners
    )

    (entry,) = rollup
    assert entry["status"] == "fail"
    assert entry["worst_case"]["corner_id"] == "b"
    assert entry["worst_case"]["value"] == 1.25


def test_rollup_measurements_error_outranks_fail():
    corners = [
        {
            "corner_id": "a",
            "measurements": [
                {"name": "iq", "value": None, "margin": None, "status": "error"}
            ],
        },
        {
            "corner_id": "b",
            "measurements": [
                {"name": "iq", "value": 6e-6, "margin": -1e-6, "status": "fail"}
            ],
        },
    ]
    rollup = sim._rollup_measurements(
        [{"name": "iq", "unit": "A", "limits": {"max": 5e-6}}], corners
    )

    assert rollup[0]["status"] == "error"


# --------------------------------------------------------------------------- #
# ASCII rawfile -> waveform JSON
# --------------------------------------------------------------------------- #

_ASCII_RAWFILE = """Title: * rawfile test
Date: Fri Jul 31 12:00:00  2026
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 3
Variables:
\t0\ttime\ttime
\t1\tv(out)\tvoltage
Values:
 0\t0.000000000000000e+00
\t1.000000000000000e+00

 1\t1.000000000000000e-11
\t1.000000000000000e+00

 2\t2.000000000000000e-11
\t9.900000000000000e-01

"""


def test_parse_ascii_rawfile(tmp_path):
    path = tmp_path / "waveform.raw"
    path.write_text(_ASCII_RAWFILE)

    waveform = sim.parse_ascii_rawfile(str(path))

    assert waveform["plotname"] == "Transient Analysis"
    assert waveform["variables"] == [
        {"index": 0, "name": "time", "type": "time"},
        {"index": 1, "name": "v(out)", "type": "voltage"},
    ]
    assert waveform["points"] == [
        [0.0, 1.0],
        [1e-11, 1.0],
        [2e-11, 0.99],
    ]


def test_parse_ascii_rawfile_not_a_rawfile_raises(tmp_path):
    path = tmp_path / "notraw.txt"
    path.write_text("hello world\n")
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(path))


def test_parse_ascii_rawfile_missing_file_raises(tmp_path):
    with pytest.raises(sim.SimError):
        sim.parse_ascii_rawfile(str(tmp_path / "nope.raw"))


# --------------------------------------------------------------------------- #
# run_sim with a stubbed ngspice subprocess (no binary required)
# --------------------------------------------------------------------------- #


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _stub_subprocess_run(
    monkeypatch,
    *,
    log_text: str = "",
    stdout: str = "** ngspice-99\n",
    side_effect=None,
):
    def fake_run(cmd, capture_output, text, timeout):
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return _FakeCompleted(stdout)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)


def test_run_sim_stubbed_missing_binary_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert "unknown" in codes


def test_run_sim_stubbed_timeout_is_corner_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 5},
        },
    )
    _stub_subprocess_run(
        monkeypatch, side_effect=subprocess.TimeoutExpired(cmd=["ngspice"], timeout=5)
    )

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = [d["code"] for d in corner["diagnostics"]]
    assert codes == ["timeout"]


def test_run_sim_stubbed_pass(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                    "limits": {"min": 0.9, "max": 1.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["passed"] == 1
    assert report["environment"]["engine_version"] == "99"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][0]["status"] == "pass"


@pytest.mark.parametrize("netlist_source", ["schematic", "extracted"])
def test_run_sim_stubbed_netlist_source_echoed_in_environment(
    tmp_path, monkeypatch, netlist_source
):
    # request.netlist_source, when present and valid, is echoed verbatim
    # into environment.netlist_source (see docs/cli/sim.md's "Post-layout
    # verification" section) -- this is how a caller distinguishes a
    # pre-layout (S6) sim pass from a post-layout (S9) one.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "netlist_source": netlist_source,
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert report["environment"]["netlist_source"] == netlist_source


def test_run_sim_stubbed_netlist_source_absent_omits_environment_key(
    tmp_path, monkeypatch
):
    # Omitting netlist_source is unchanged, backward-compatible behavior:
    # no netlist_source key appears in environment at all (not null, not
    # an empty string) and every other field/behavior is unaffected.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    report = sim.run_sim(str(request))

    assert "netlist_source" not in report["environment"]
    assert report["status"] == "pass"


def _stripped_report(report: dict) -> dict:
    """Drop the two fields the docs flag as legitimately varying between
    runs (`runtime_s`, `engine_version`) so the rest of the report can be
    compared for byte-identical equality (see docs/cli/sim.md's
    byte-exact-fixture caveat)."""
    clone = json.loads(json.dumps(report))
    clone["environment"].pop("engine_version", None)
    for corner in clone["corners"]:
        corner.pop("runtime_s", None)
    return clone


def test_run_sim_local_backend_is_byte_identical_to_default(tmp_path, monkeypatch):
    # The `local` backend (explicit, and via the --backend flag override)
    # must reproduce the pre-seam default behaviour exactly: same report
    # JSON, same corner ordering. Uses a multi-corner matrix so ordering is
    # actually exercised, not just a single-corner smoke test.
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    base = {
        "netlist": "body.spice",
        "models": {"lib": "corner.lib"},
        "corners": {
            "process": ["tt", "ss"],
            "temperature_c": [-40, 125],
        },
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
    }
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    default_req = _write_request(tmp_path, base, name="default.json")
    explicit_req = _write_request(
        tmp_path, {**base, "backend": "local"}, name="explicit.json"
    )

    default_report = sim.run_sim(str(default_req))
    explicit_report = sim.run_sim(str(explicit_req))
    flag_report = sim.run_sim(str(default_req), backend="local")

    # `netlist` echoes the request path field, identical across all three.
    assert _stripped_report(default_report) == _stripped_report(explicit_report)
    assert _stripped_report(default_report) == _stripped_report(flag_report)

    # Ordering is the odometer process x temperature expansion, unchanged.
    assert [c["corner_id"] for c in default_report["corners"]] == [
        "tt/novdd/-40C",
        "tt/novdd/125C",
        "ss/novdd/-40C",
        "ss/novdd/125C",
    ]


# --------------------------------------------------------------------------- #
# local-parallel backend (#255)
# --------------------------------------------------------------------------- #


def test_run_sim_local_parallel_backend_is_order_identical_to_local(
    tmp_path, monkeypatch
):
    # Corners share nothing, so `local-parallel` must reassemble results in
    # the same odometer order `local` produces, regardless of which corner's
    # subprocess happens to finish first. Sleep inversely with temperature
    # so corners complete in *reverse* submission order under the pool,
    # actually exercising reassembly rather than accidentally passing
    # because completion order matched submission order.
    _write_body(tmp_path)
    base = {
        "netlist": "body.spice",
        "corners": {"temperature_c": [10, 20, 30, 40]},
        "analysis": {"kind": "tran", "args": "1n 1u"},
        "measurements": [
            {
                "name": "vout",
                "spice": ".meas tran vout FIND v(out) AT=1u",
                "limits": {"min": 0.5},
            }
        ],
        "options": {"max_workers": 4},
    }

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        time.sleep(max(0.0, 40.0 - temp) * 0.005)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    parallel_req = _write_request(
        tmp_path, {**base, "backend": "local-parallel"}, name="parallel.json"
    )
    local_req = _write_request(tmp_path, base, name="local.json")

    parallel_report = sim.run_sim(str(parallel_req))
    local_report = sim.run_sim(str(local_req))

    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        "default/novdd/10C",
        "default/novdd/20C",
        "default/novdd/30C",
        "default/novdd/40C",
    ]
    assert [c["corner_id"] for c in parallel_report["corners"]] == [
        c["corner_id"] for c in local_report["corners"]
    ]
    assert _stripped_report(parallel_report) == _stripped_report(local_report)


def test_run_sim_local_parallel_failing_corner_does_not_abort_siblings(
    tmp_path, monkeypatch
):
    # A corner that times out (or otherwise errors) is reported exactly as
    # `local` reports it, and the other corners in the same pool still
    # complete and report their own real status.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "corners": {"temperature_c": [10, 20, 30]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"max_workers": 3},
        },
    )

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[2]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text()
        temp = float(re.search(r"\.temp\s+(-?[\d.]+)", deck_text).group(1))
        if temp == 20:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(
                "  Measurements for Transient Analysis\n\n"
                "vout                =  1.00000e+00\n"
            )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    assert report["passed"] == 2
    assert report["errored"] == 1
    statuses = {c["corner_id"]: c["status"] for c in report["corners"]}
    assert statuses == {
        "default/novdd/10C": "pass",
        "default/novdd/20C": "error",
        "default/novdd/30C": "pass",
    }
    (errored_corner,) = [c for c in report["corners"] if c["status"] == "error"]
    assert any(d["code"] == "timeout" for d in errored_corner["diagnostics"])


def test_run_sim_local_parallel_default_max_workers_is_used(tmp_path, monkeypatch):
    # No explicit `max_workers` (request field or --flag) -> the backend
    # falls back to `_default_max_workers()`, never an unbounded pool.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)
    monkeypatch.setattr(sim, "_default_max_workers", lambda: 2)

    sim.run_sim(str(request))

    assert seen_workers["max_workers"] == 2


def test_run_sim_max_workers_flag_overrides_request_field(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": 5},
        },
    )
    _stub_subprocess_run(monkeypatch)

    seen_workers = {}
    real_pool = sim.ThreadPoolExecutor

    class _RecordingPool(real_pool):
        def __init__(self, max_workers=None, *args, **kwargs):
            seen_workers["max_workers"] = max_workers
            super().__init__(*args, max_workers=max_workers, **kwargs)

    monkeypatch.setattr(sim, "ThreadPoolExecutor", _RecordingPool)

    sim.run_sim(str(request), max_workers=1)

    assert seen_workers["max_workers"] == 1


@pytest.mark.parametrize("bad_value", [0, -1, 1.5, "3"])
def test_run_sim_invalid_max_workers_raises(tmp_path, bad_value):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "backend": "local-parallel",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"max_workers": bad_value},
        },
    )
    with pytest.raises(sim.SimError, match="max_workers"):
        sim.run_sim(str(request))


def test_default_max_workers_derives_from_cpu_count(monkeypatch):
    monkeypatch.setattr(sim.os, "cpu_count", lambda: 32)
    assert sim._default_max_workers() == 4

    monkeypatch.setattr(sim.os, "cpu_count", lambda: 1)
    assert sim._default_max_workers() == 1

    monkeypatch.setattr(sim.os, "cpu_count", lambda: None)
    assert sim._default_max_workers() == 1


def test_run_sim_stubbed_fail(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.5},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "fail"
    assert report["failed"] == 1


def test_run_sim_stubbed_missing_measurement_is_error(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch, log_text="  Measurements for Transient Analysis\n\n"
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"
    assert any(d["code"] == "measurement" for d in corner["diagnostics"])


#: A real ngspice-46 log (captured against the sky130 5T OTA composed by
#: `klt gen-compose`, per this issue's own repro) where gmin/source stepping
#: recovers from a singular DC operating-point matrix and the transient
#: still completes, producing correct measurement values -- yet also logs
#: "gmin stepping failed"/"source stepping failed" text along the way,
#: which is exactly why the fix cannot look at `singular_matrix` alone (see
#: `_recovered_from_stepping`'s docstring).
_RECOVERED_SINGULAR_MATRIX_LOG = (
    "Doing analysis at TEMP = 27.000000 and TNOM = 27.000000\n\n"
    "Using SPARSE 1.3 as Direct Linear Solver\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Note: Starting dynamic gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: Dynamic gmin stepping failed\n"
    "Note: Starting true gmin stepping\n"
    "Warning: singular matrix:  check node xota.g1\n\n"
    "Warning: True gmin stepping failed\n"
    "Note: Starting source stepping\n"
    "Warning: source stepping failed\n"
    "Note: Transient op started\n"
    "Note: Transient op finished successfully\n\n"
    "  Measurements for Transient Analysis\n\n"
    "vout_meas           =  1.00000e+00\n"
    "tail_meas           =  5.00000e-01\n"
)


def test_run_sim_stubbed_recovered_singular_matrix_reports_pass(tmp_path, monkeypatch):
    # The exact false positive this issue reports: a `singular matrix`
    # warning that ngspice's own gmin/source stepping recovers from, still
    # producing correct measurement values, must report `status: "pass"`,
    # not `"error"`.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {
                    "name": "vout_meas",
                    "spice": ".meas tran vout_meas find v(vout_node) at=1n",
                    "limits": {"min": 0.9, "max": 1.1},
                },
                {
                    "name": "tail_meas",
                    "spice": ".meas tran tail_meas find v(tail_node) at=1n",
                    "limits": {"min": 0.4, "max": 0.6},
                },
            ],
        },
    )
    _stub_subprocess_run(monkeypatch, log_text=_RECOVERED_SINGULAR_MATRIX_LOG)

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    (corner,) = report["corners"]
    assert corner["status"] == "pass"
    assert corner["measurements"][0]["value"] == 1.0
    assert corner["measurements"][1]["value"] == 0.5
    # Recorded for visibility, but downgraded -- not fatal.
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "warning"
    assert codes.get("nonconvergence") == "warning"


def test_run_sim_stubbed_unrecovered_singular_matrix_still_errors(
    tmp_path, monkeypatch
):
    # A genuinely unrecovered singular matrix (ngspice's own abort trailer
    # present, no measurement values produced) must still classify as
    # `status: "error"` -- this fix narrows the false positive, it does not
    # remove the diagnostic.
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1n"},
            "measurements": [
                {"name": "vout_meas", "spice": ".meas tran vout_meas find v(out) at=1n"}
            ],
        },
    )
    log = (
        "Warning: singular matrix:  check node b\n"
        "Warning: True gmin stepping failed\n"
        "Warning: source stepping failed\n"
        "Error: Transient op failed, timestep too small\n"
        "doAnalyses: TRAN:  Timestep too small; initial timepoint: cause unrecorded.\n"
        "tran simulation(s) aborted\n"
    )
    _stub_subprocess_run(monkeypatch, log_text=log)

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["status"] == "error"
    codes = {d["code"]: d["severity"] for d in corner["diagnostics"]}
    assert codes["singular_matrix"] == "error"


def test_run_sim_keep_artifacts_writes_log(tmp_path, monkeypatch):
    _write_body(tmp_path)
    artifacts_dir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request), artifacts_dir=str(artifacts_dir))

    log_path = report["corners"][0]["artifacts"]["log"]
    assert log_path is not None
    assert Path(log_path).read_text() == "clean run\n"
    assert Path(log_path).is_relative_to(artifacts_dir)


def test_run_sim_without_keep_artifacts_cleans_up(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    report = sim.run_sim(str(request))

    assert report["corners"][0]["artifacts"] == {
        "log": None,
        "raw": None,
        "waveform": None,
    }


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_cli_stubbed_json_contract(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "unit": "V",
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    exit_code = main(["sim", str(request), "--format", "json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data.keys()) == {
        "schema_version",
        "netlist",
        "status",
        "corner_count",
        "passed",
        "failed",
        "errored",
        "environment",
        "measurements",
        "corners",
    }


def test_cli_default_format_is_text(tmp_path, monkeypatch, capsys):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    exit_code = main(["sim", str(request)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "netlist:" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_cli_exit_code_measurement_failed(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
    )
    _stub_subprocess_run(
        monkeypatch,
        log_text=(
            "  Measurements for Transient Analysis\n\n"
            "vout                =  1.00000e+00\n"
        ),
    )

    assert main(["sim", str(request), "--format", "json"]) == 3


def test_cli_exit_code_corner_errored(tmp_path, monkeypatch):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {"netlist": "body.spice", "analysis": {"kind": "tran", "args": "1n 1u"}},
    )
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    assert main(["sim", str(request), "--format", "json"]) == 4


def test_cli_unresolvable_request_error_envelope(tmp_path, capsys):
    exit_code = main(["sim", str(tmp_path / "nope.json"), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "sim"
    assert "not found" in error["error"]["message"]


def test_cli_outdir_flag_overrides_default(tmp_path, monkeypatch):
    _write_body(tmp_path)
    outdir = tmp_path / "custom-out"
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"keep_artifacts": True},
        },
    )
    _stub_subprocess_run(monkeypatch, log_text="clean run\n")

    main(["sim", str(request), "--outdir", str(outdir), "--format", "json"])

    assert outdir.is_dir()
    assert any(outdir.rglob("ngspice.log"))


# --------------------------------------------------------------------------- #
# Integration: real ngspice (skipped when not installed)
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_integration_process_corner_selects_lib_section(tmp_path):
    _write_body(tmp_path)
    _write_corner_lib(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "models": {"lib": "corner.lib"},
            "corners": {"process": ["tt", "ss"]},
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    by_process = {
        c["process"]: c["measurements"][0]["value"] for c in report["corners"]
    }
    # tt: corner_scale isn't referenced by this body -- both corners just
    # confirm .lib section selection didn't error; values are equal here.
    assert set(by_process) == {"tt", "ss"}


@_SKIP_NO_NGSPICE
def test_integration_supply_and_temperature_sweep(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "corners": {
                "supply_v": {"vdd": [1.0, 2.0]},
                "temperature_c": [27, 125],
            },
            "analysis": {"kind": "tran", "args": "1n 5u"},
            "measurements": [
                {
                    "name": "vout_final",
                    "spice": ".meas tran vout_final FIND v(out) AT=5u",
                    "unit": "V",
                    "limits": {"min": 0.5, "max": 2.5},
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "pass"
    assert report["corner_count"] == 4
    values = sorted(c["measurements"][0]["value"] for c in report["corners"])
    assert values == pytest.approx([1.0, 1.0, 2.0, 2.0])


@_SKIP_NO_NGSPICE
def test_integration_timeout_is_killed_and_classified(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "options": {"timeout_s": 60},
        },
    )
    # Force a real timeout deterministically -- ngspice's own process-startup
    # overhead (tens of milliseconds, per the stubbed tests' runtime_s
    # values) reliably exceeds an absurdly small budget, without needing to
    # construct an actual nonconvergent hang.
    request_data = json.loads(request.read_text())
    request_data["options"]["timeout_s"] = 0.001
    request.write_text(json.dumps(request_data))

    report = sim.run_sim(str(request))

    (corner,) = report["corners"]
    assert corner["status"] == "error"
    assert any(d["code"] == "timeout" for d in corner["diagnostics"])


@_SKIP_NO_NGSPICE
def test_integration_missing_measurement_value_is_error(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
    )

    report = sim.run_sim(str(request))

    assert report["status"] == "error"
    (corner,) = report["corners"]
    assert corner["measurements"][0]["value"] is None
    assert corner["measurements"][0]["status"] == "error"


@_SKIP_NO_NGSPICE
def test_integration_waveform_artifact(tmp_path):
    _write_body(tmp_path)
    request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=1u"}
            ],
            "options": {"keep_artifacts": True, "waveforms": True},
        },
    )

    report = sim.run_sim(str(request), artifacts_dir=str(tmp_path / "artifacts"))

    (corner,) = report["corners"]
    waveform_path = corner["artifacts"]["waveform"]
    assert waveform_path is not None
    waveform = json.loads(Path(waveform_path).read_text())
    assert waveform["variables"][0]["name"] == "time"
    assert len(waveform["points"]) > 0


@_SKIP_NO_NGSPICE
def test_integration_exit_codes(tmp_path):
    _write_body(tmp_path)
    pass_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"min": 0.5},
                }
            ],
        },
        name="pass_request.json",
    )
    assert main(["sim", str(pass_request), "--format", "json"]) == 0

    fail_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "vout",
                    "spice": ".meas tran vout FIND v(out) AT=1u",
                    "limits": {"max": 0.1},
                }
            ],
        },
        name="fail_request.json",
    )
    assert main(["sim", str(fail_request), "--format", "json"]) == 3

    error_request = _write_request(
        tmp_path,
        {
            "netlist": "body.spice",
            "analysis": {"kind": "tran", "args": "1n 1u"},
            "measurements": [
                {
                    "name": "never",
                    "spice": ".meas tran never FIND v(out) WHEN v(out)=99",
                }
            ],
        },
        name="error_request.json",
    )
    assert main(["sim", str(error_request), "--format", "json"]) == 4


# --------------------------------------------------------------------------- #
# Worked example (examples/sim/), regenerated by examples/sim/generate.py
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
@pytest.mark.skipif(
    not EXAMPLES_DIR.exists(), reason="examples/sim/ fixtures not generated"
)
def test_examples_sim_worked_example_passes():
    request_path = EXAMPLES_DIR / "request.json"
    if not request_path.exists():
        pytest.skip(
            "examples/sim/request.json not generated -- run examples/sim/generate.py"
        )

    report = sim.run_sim(str(request_path))

    assert report["schema_version"] == 1
    assert report["status"] == "pass"
    assert report["corner_count"] == 8
    assert report["measurements"][0]["name"] == "vout"


def test_pdk_module_is_the_only_resolution_path(monkeypatch, tmp_path):
    """Guard against a future regression re-introducing a hand-rolled PDK
    path lookup: `_resolve_models_lib`'s pdk-variant branch must go through
    `klayout_tools.pdk.find_pdk` (issue #45), not a private reimplementation."""
    calls = []
    original = pdk.find_pdk

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(sim, "find_pdk", spy)

    install_root = tmp_path / "install"
    variant_dir = install_root / "sky130A" / "libs.tech"
    (variant_dir / "ngspice").mkdir(parents=True)
    (variant_dir / "ngspice" / "sky130.lib.spice").write_text(".lib tt\n.endl tt\n")

    sim._resolve_models_lib(
        {
            "pdk": "sky130A",
            "pdk_root": str(install_root),
            "lib": "libs.tech/ngspice/sky130.lib.spice",
        },
        request_dir=str(tmp_path),
    )

    assert len(calls) == 1
