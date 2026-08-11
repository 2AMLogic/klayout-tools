"""Tests for `klt size` and the `klayout_tools.size` library (issue #721,
Phase 0 of the analog-sizing epic #705).

Two tiers, mirroring `tests/test_sim.py`'s own split:

- **Unit tests** (the majority) exercise request validation, the log-space/
  log-interpolation search helpers, the monotonic-bracket search, inversion-
  level classification, and sweep-log parsing as pure functions -- either
  directly, or by stubbing `subprocess.run` so `run_size`'s evaluator-error
  path is exercised without ever invoking the real `ngspice` binary.
- **Integration tests** (`@pytest.mark.skipif(not HAVE_NGSPICE, ...)`) run
  the real `ngspice -b` subprocess end to end against the tiny synthetic
  device library in `examples/size/` (see that directory's `generate.py`
  docstring for why it is a bare SPICE `level=1` model, not a real PDK) --
  the diode-connected sweep deck, `alterparam`/`reset` iteration, the
  sky130-style `m<subcircuit>` internal op-point path, and the confirmation
  run. `test_reproduces_hand_derived_single_stage_reference` independently
  re-derives the expected sized width from the synthetic model's own
  textbook square-law equations (not by calling `klt size` a second time)
  and asserts the tool reproduces it.
- **Canary reproduction** (`@_SKIP_NO_SKY130_NGSPICE`, additionally gated on
  a real sky130A ngspice model library being installed) --
  `test_reproduces_canary_5t_ota_input_pair` sizes the *hand-sized* input
  pair of this repo's own sky130 5T OTA canary
  (`examples/design-pipeline/`) against the gm/Id its own committed AC
  simulation implies, on the real PDK models. This is the Phase 0
  acceptance criterion's "reproduces a hand-sized single-stage reference
  from an existing analog canary"; the synthetic square-law test above
  covers the same criterion offline, where no PDK is installed (CI).
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klayout_tools import size
from klayout_tools.cli import main
from klayout_tools.pdk import PdkNotFoundError, find_pdk

HAVE_NGSPICE = shutil.which("ngspice") is not None
_SKIP_NO_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE, reason="ngspice is not installed on this machine"
)

EXAMPLES_DIR = Path(__file__).parent.parent / "examples" / "size"
CANARY_DIR = Path(__file__).parent.parent / "examples" / "design-pipeline"


#: sky130A's *ngspice* model library, the one the canary-reproduction test
#: needs. CI installs only the sky130 **liberty** subset (see
#: `.github/workflows/ci.yml`), which has no `libs.tech/ngspice/` tree, so
#: this resolves to `None` there and the test skips rather than failing.
def _sky130_ngspice_lib() -> str | None:
    try:
        resolution = find_pdk(variant="sky130A")
    except PdkNotFoundError:
        return None
    lib = (
        Path(resolution["root"])
        / resolution["variant"]
        / "libs.tech"
        / "ngspice"
        / "sky130.lib.spice"
    )
    return str(lib) if lib.is_file() else None


SKY130_NGSPICE_LIB = _sky130_ngspice_lib()
_SKIP_NO_SKY130_NGSPICE = pytest.mark.skipif(
    not HAVE_NGSPICE or SKY130_NGSPICE_LIB is None,
    reason="needs ngspice plus an installed sky130A ngspice model library",
)


class _FakeCompleted:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""


def _stub_subprocess_run(monkeypatch, *, log_text: str = "", side_effect=None):
    def fake_run(cmd, capture_output, text, timeout):
        log_path = cmd[cmd.index("-o") + 1]
        if side_effect is not None:
            raise side_effect
        with open(log_path, "w", encoding="utf-8") as handle:
            handle.write(log_text)
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(size.subprocess, "run", fake_run)


#: Same textbook level=1 square-law constants `_write_models_lib` bakes into
#: its synthetic model, plus typical NMOS Vth and mobility (KP) temperature
#: coefficients -- used by `_install_synthetic_ngspice_stub` below to
#: produce a real, physically plausible per-corner gm/Id drift for
#: corner-set tests, without needing the real `ngspice` binary installed
#: (issue #729). Vth's tempco alone barely moves gm/Id at fixed Id in a
#: square-law model with no other nonlinearity (Vth drops out of
#: gm/Id=2/Vov to first order) -- KP's tempco (mobility degrading with
#: temperature) is what actually reproduces the drift `gm/Id` sizing
#: methodology is known to see across a PVT sweep.
_SQUARE_LAW_KP = 120e-6
_SQUARE_LAW_VTO = 0.5
_SQUARE_LAW_LAMBDA = 0.02
_SQUARE_LAW_L = 0.5
_SQUARE_LAW_VTO_TEMPCO = -0.002  # V/degC
_SQUARE_LAW_KP_TEMPCO = -0.005  # fractional/degC


def _square_law_op_point(w_um: float, id_a: float, temperature_c: float) -> dict:
    vto = _SQUARE_LAW_VTO + _SQUARE_LAW_VTO_TEMPCO * (temperature_c - 27.0)
    kp = _SQUARE_LAW_KP * (1 + _SQUARE_LAW_KP_TEMPCO * (temperature_c - 27.0))

    def f(vov: float) -> float:
        vgs = vov + vto
        return (
            0.5 * kp * (w_um / _SQUARE_LAW_L) * vov**2 * (1 + _SQUARE_LAW_LAMBDA * vgs)
            - id_a
        )

    lo, hi = 1e-6, 5.0
    flo = f(lo)
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = f(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    vov = (lo + hi) / 2
    vgs = vov + vto
    gm = (
        kp * (w_um / _SQUARE_LAW_L) * vov * (1 + _SQUARE_LAW_LAMBDA * vgs)
        + 0.5 * kp * (w_um / _SQUARE_LAW_L) * vov**2 * _SQUARE_LAW_LAMBDA
    )
    return {"gm": gm, "id": id_a, "vgs": vgs, "vth": vto}


_ID_DC_RE = re.compile(r"^Idc\b.*\bDC\s+(\S+)$", re.MULTILINE)
_TEMP_RE = re.compile(r"^\.temp\s+(\S+)$", re.MULTILINE)
_OP_ELEMENT_RE = re.compile(r"@m\.x1\.(\w+)\[")
_W_SWEEP_RE = re.compile(r"w_sweep=(\S+)")


def _install_synthetic_ngspice_stub(
    monkeypatch, *, error_temps: set[float] | None = None
):
    """Monkeypatch `size.subprocess.run` with a fake ngspice that reads each
    generated deck's own current/temperature/width-sweep values back out of
    the deck text (rather than returning a fixed canned log) and solves the
    same square-law equations `_write_models_lib`'s synthetic model
    implements, with a linear Vth temperature coefficient folded in (see
    `_square_law_op_point`). Lets a test exercise the full
    `run_size` sweep + confirm + corner-set verification pipeline --
    including genuine per-corner gm/Id drift -- without needing the real
    `ngspice` binary installed. `error_temps` simulates an evaluator failure
    (a fatal log line, no usable operating-point data) for any deck whose
    declared `.temp` is in the set.
    """
    error_temps = error_temps or set()

    def fake_run(cmd, capture_output, text, timeout):
        deck_path = cmd[cmd.index("-b") + 1]
        log_path = cmd[cmd.index("-o") + 1]
        deck_text = Path(deck_path).read_text(encoding="utf-8")

        temperature_c = float(_TEMP_RE.search(deck_text).group(1))
        id_a = abs(float(_ID_DC_RE.search(deck_text).group(1)))
        op_element = _OP_ELEMENT_RE.search(deck_text).group(1)
        w_values = [float(m) for m in _W_SWEEP_RE.findall(deck_text)]

        lines = []
        if temperature_c in error_temps:
            lines.append("fatal error: synthetic evaluator failure")
        else:
            for index, w_um in enumerate(w_values):
                op = _square_law_op_point(w_um, id_a, temperature_c)
                lines.append(f"KLT_SIZE_POINT {index} {w_um!r}")
                lines.append(f"@m.x1.{op_element}[gm] = {op['gm']!r}")
                lines.append(f"@m.x1.{op_element}[id] = {op['id']!r}")
                lines.append(f"@m.x1.{op_element}[vgs] = {op['vgs']!r}")
                lines.append(f"@m.x1.{op_element}[vth] = {op['vth']!r}")

        Path(log_path).write_text(
            "** ngspice-99\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )
        return _FakeCompleted("** ngspice-99\n")

    monkeypatch.setattr(size.subprocess, "run", fake_run)


def _write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request))
    return path


def _write_models_lib(tmp_path: Path, name: str = "models.lib") -> Path:
    path = tmp_path / name
    path.write_text(
        ".lib tt\n"
        ".subckt nmos_demo d g s b\n"
        ".param l=0.5 w=1 nf=1 mult=1\n"
        "mnmos_demo d g s b nmos_demo__model l={l} w={w}\n"
        ".model nmos_demo__model nmos level=1 vto=0.5 kp=120u "
        "lambda=0.02 gamma=0.4 phi=0.7\n"
        ".ends nmos_demo\n"
        ".subckt pmos_demo d g s b\n"
        ".param l=0.5 w=1 nf=1 mult=1\n"
        "mpmos_demo d g s b pmos_demo__model l={l} w={w}\n"
        ".model pmos_demo__model pmos level=1 vto=-0.5 kp=40u "
        "lambda=0.02 gamma=0.4 phi=0.7\n"
        ".ends pmos_demo\n"
        ".endl tt\n"
    )
    return path


def _base_request(models_lib_name: str = "models.lib", **overrides) -> dict:
    request = {
        "device": {
            "kind": "nmos",
            "model": "nmos_demo",
            "l_um": 0.5,
            "w_min_um": 0.5,
            "w_max_um": 20,
        },
        "models": {"lib": models_lib_name},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "target": {"id_a": 2e-05, "gm_id": 8.0},
        "options": {"sweep_points": 20},
    }
    request.update(overrides)
    return request


# --------------------------------------------------------------------------- #
# load_request
# --------------------------------------------------------------------------- #


def test_load_request_missing_file(tmp_path):
    with pytest.raises(size.SizeError, match="not found"):
        size.load_request(str(tmp_path / "nope.json"))


def test_load_request_is_a_directory(tmp_path):
    with pytest.raises(size.SizeError, match="not a file"):
        size.load_request(str(tmp_path))


def test_load_request_not_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json {{{")
    with pytest.raises(size.SizeError, match="not valid JSON"):
        size.load_request(str(path))


def test_load_request_non_object(tmp_path):
    path = tmp_path / "array.json"
    path.write_text("[1, 2, 3]")
    with pytest.raises(size.SizeError, match="JSON object"):
        size.load_request(str(path))


@pytest.mark.parametrize("missing_field", ["device", "models", "target"])
def test_load_request_missing_required_field(tmp_path, missing_field):
    request = _base_request()
    del request[missing_field]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match=missing_field):
        size.load_request(str(path))


# --------------------------------------------------------------------------- #
# run_size: request-level validation (raised before ngspice ever runs)
# --------------------------------------------------------------------------- #


def test_run_size_unsupported_engine_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request(engine="xyce"))
    with pytest.raises(size.SizeError, match="unsupported engine"):
        size.run_size(str(request))


def test_run_size_unsupported_device_kind_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_request()
    request["device"]["kind"] = "finfet"
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="device.kind"):
        size.run_size(str(path))


def test_run_size_w_max_not_greater_than_w_min_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_request()
    request["device"]["w_max_um"] = request["device"]["w_min_um"]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="w_max_um"):
        size.run_size(str(path))


def test_run_size_missing_vdd_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_request()
    del request["corner"]["vdd_v"]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="vdd_v"):
        size.run_size(str(path))


def test_run_size_non_positive_target_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_request()
    request["target"]["id_a"] = -1.0
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="positive"):
        size.run_size(str(path))


def test_run_size_unresolvable_model_library_raises(tmp_path):
    request = _write_request(tmp_path, _base_request(models_lib_name="missing.lib"))
    with pytest.raises(size.SizeError, match="model library not found"):
        size.run_size(str(request))


def test_run_size_sweep_points_below_minimum_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_request()
    request["options"]["sweep_points"] = 2
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="sweep_points"):
        size.run_size(str(path))


# --------------------------------------------------------------------------- #
# run_size: evaluator-error path, stubbed (no real ngspice needed)
# --------------------------------------------------------------------------- #


def test_run_size_stubbed_missing_binary_is_evaluator_error(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    report = size.run_size(str(request))

    assert report["status"] == "error"
    assert report["operating_point"] is None
    # "no stated method is rejected" -- method is always populated, even on
    # a hard evaluator error (issue #721 acceptance criterion).
    assert report["method"]["name"]
    assert "no ngspice" in report["method"]["rationale"]


def test_run_size_stubbed_timeout_is_evaluator_error(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    _stub_subprocess_run(
        monkeypatch, side_effect=subprocess.TimeoutExpired(cmd=["ngspice"], timeout=5)
    )

    report = size.run_size(str(request))

    assert report["status"] == "error"
    assert "did not complete within" in report["method"]["rationale"]


def test_run_size_stubbed_empty_log_is_evaluator_error(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    _stub_subprocess_run(monkeypatch, log_text="")

    report = size.run_size(str(request))

    assert report["status"] == "error"


def test_cli_exit_code_evaluator_errored(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    assert main(["size", str(request), "--format", "json"]) == 4


def test_cli_unresolvable_request_error_envelope(tmp_path, capsys):
    exit_code = main(["size", str(tmp_path / "nope.json"), "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert error["schema_version"] == 1
    assert error["error"]["command"] == "size"
    assert "not found" in error["error"]["message"]


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_log_space_endpoints_and_count():
    values = size._log_space(1.0, 100.0, 5)
    assert len(values) == 5
    assert values[0] == pytest.approx(1.0)
    assert values[-1] == pytest.approx(100.0)
    # log-spaced: equal ratios between consecutive points.
    ratios = [values[i + 1] / values[i] for i in range(len(values) - 1)]
    assert ratios == pytest.approx([ratios[0]] * len(ratios))


def test_log_space_single_point():
    assert size._log_space(2.0, 50.0, 1) == [2.0]


def test_log_interp_exact_bracket_endpoints():
    # Target equal to the low point's own value -> returns the low width.
    assert size._log_interp(1.0, 5.0, 10.0, 15.0, 5.0) == pytest.approx(1.0)
    assert size._log_interp(1.0, 5.0, 10.0, 15.0, 15.0) == pytest.approx(10.0)


def test_log_interp_midpoint_is_geometric_mean_for_linear_gm_id():
    # gm_id at w=1 is 5, at w=4 is 15 -- target 10 (the linear midpoint in
    # gm_id) should land at the geometric mean of the two widths in log
    # space (linear interpolation in ln(w)).
    w = size._log_interp(1.0, 5.0, 4.0, 15.0, 10.0)
    assert w == pytest.approx(math.sqrt(1.0 * 4.0))


def _pt(w_um, gm_id):
    return {"w_um": w_um, "gm_id": gm_id}


def test_find_bracket_within_range():
    points = [_pt(1, 5), _pt(2, 10), _pt(4, 20)]
    bracket, note = size._find_bracket(points, 8)
    assert note == ""
    lo, hi = bracket
    assert lo["w_um"] == 1
    assert hi["w_um"] == 2


def test_find_bracket_below_range_returns_none():
    points = [_pt(1, 5), _pt(2, 10), _pt(4, 20)]
    bracket, note = size._find_bracket(points, 1.0)
    assert bracket is None
    assert "below" in note


def test_find_bracket_above_range_returns_none():
    points = [_pt(1, 5), _pt(2, 10), _pt(4, 20)]
    bracket, note = size._find_bracket(points, 100.0)
    assert bracket is None
    assert "exceeds" in note


def test_find_bracket_exact_hit_on_grid_point():
    points = [_pt(1, 5), _pt(2, 10), _pt(4, 20)]
    bracket, _ = size._find_bracket(points, 10.0)
    assert bracket is not None


@pytest.mark.parametrize(
    "vov,expected",
    [
        (None, "unknown"),
        (-0.05, "weak"),
        (0.0, "weak"),
        (0.05, "moderate"),
        (0.2, "strong"),
    ],
)
def test_classify_inversion(vov, expected):
    assert size._classify_inversion(vov) == expected


def test_default_op_point_element_sky130_convention():
    assert (
        size._default_op_point_element("sky130_fd_pr__nfet_01v8")
        == "msky130_fd_pr__nfet_01v8"
    )


def test_parse_sweep_log_basic():
    log_text = (
        "KLT_SIZE_POINT 0 1.5\n"
        "@m.x1.mdev[gm] = 1.000000e-04\n"
        "@m.x1.mdev[id] = 2.000000e-05\n"
        "@m.x1.mdev[vgs] = 7.500000e-01\n"
        "KLT_SIZE_POINT 1 3.0\n"
        "@m.x1.mdev[gm] = 2.000000e-04\n"
        "@m.x1.mdev[id] = 2.000000e-05\n"
        "@m.x1.mdev[vgs] = 6.500000e-01\n"
    )
    points = size._parse_sweep_log(log_text, 2, [1.5, 3.0])
    assert points[0]["gm_s"] == pytest.approx(1e-4)
    assert points[0]["id_a"] == pytest.approx(2e-5)
    assert points[0]["vth_v"] is None
    assert points[1]["gm_s"] == pytest.approx(2e-4)


def test_parse_sweep_log_missing_point_stays_none():
    # Only one marker printed for a 2-point request (e.g. ngspice crashed
    # partway through the sweep) -- the un-run point is None, not dropped.
    log_text = "KLT_SIZE_POINT 0 1.5\n@m.x1.mdev[gm] = 1.000000e-04\n"
    points = size._parse_sweep_log(log_text, 2, [1.5, 3.0])
    assert points[0]["gm_s"] == pytest.approx(1e-4)
    assert points[1]["gm_s"] is None
    assert points[1]["w_um"] == 3.0


# --------------------------------------------------------------------------- #
# Integration: real ngspice against the tiny synthetic device library
# --------------------------------------------------------------------------- #


@_SKIP_NO_NGSPICE
def test_run_size_nmos_pass(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())

    report = size.run_size(str(request))

    assert report["status"] == "pass"
    assert report["operating_point"]["gm_id"] == pytest.approx(8.0, rel=0.02)
    assert report["operating_point"]["id_a"] == pytest.approx(2e-5, rel=1e-3)
    assert report["method"]["name"]
    assert report["method"]["feasible"] is True
    assert report["environment"]["engine"] == "ngspice"
    assert report["environment"]["engine_version"] is not None


@_SKIP_NO_NGSPICE
def test_run_size_pmos_pass(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            device={
                "kind": "pmos",
                "model": "pmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 20,
            },
            target={"id_a": 2e-05, "gm_id": 6.0},
        ),
    )

    report = size.run_size(str(request))

    assert report["status"] == "pass"
    assert report["device"]["kind"] == "pmos"
    assert report["operating_point"]["gm_id"] == pytest.approx(6.0, rel=0.02)


@_SKIP_NO_NGSPICE
def test_run_size_infeasible_target_reports_fail(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            device={
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 2.0,
            },
            target={"id_a": 2e-05, "gm_id": 50.0},
        ),
    )

    report = size.run_size(str(request))

    assert report["status"] == "fail"
    assert report["method"]["feasible"] is False
    assert report["operating_point"] is not None
    assert report["operating_point"]["w_um"] == pytest.approx(2.0, rel=0.05)


@_SKIP_NO_NGSPICE
def test_cli_exit_codes(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    pass_request = _write_request(tmp_path, _base_request(), name="pass.json")
    fail_request = _write_request(
        tmp_path,
        _base_request(
            device={
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 2.0,
            },
            target={"id_a": 2e-05, "gm_id": 50.0},
        ),
        name="fail.json",
    )

    assert main(["size", str(pass_request), "--format", "json"]) == 0
    assert main(["size", str(fail_request), "--format", "json"]) == 3


@_SKIP_NO_NGSPICE
def test_reproduces_hand_derived_single_stage_reference(tmp_path):
    """The Phase 0 acceptance criterion's "reproduces a hand-sized
    single-stage reference": independently re-derive the expected sized
    width from `nmos_demo`'s own textbook level=1 square-law equations
    (diode-connected: Vds=Vgs), *without* calling `klt size`, then assert
    the tool's ngspice-confirmed result reproduces it.

        Id = 0.5*KP*(W/L)*Vov^2*(1 + lambda*Vgs),  Vgs = Vov + Vto
        gm = dId/dVgs

    solved numerically by bisection on W for a target gm/Id at a fixed Id --
    the same values `examples/size/generate.py`'s worked example uses.
    """
    KP = 120e-6
    VTO = 0.5
    LAMBDA = 0.02
    L = 0.5
    ID = 2e-5
    TARGET_GM_ID = 8.0

    def _solve_vov(w: float) -> float:
        def f(vov: float) -> float:
            vgs = vov + VTO
            return 0.5 * KP * (w / L) * vov**2 * (1 + LAMBDA * vgs) - ID

        lo, hi = 1e-6, 5.0
        flo = f(lo)
        for _ in range(200):
            mid = (lo + hi) / 2
            fm = f(mid)
            if (fm > 0) == (flo > 0):
                lo, flo = mid, fm
            else:
                hi = mid
        return (lo + hi) / 2

    def _gm_id(w: float) -> float:
        vov = _solve_vov(w)
        vgs = vov + VTO
        gm = (
            KP * (w / L) * vov * (1 + LAMBDA * vgs)
            + 0.5 * KP * (w / L) * vov**2 * LAMBDA
        )
        return gm / ID

    lo, hi = 0.5, 20.0
    flo = _gm_id(lo) - TARGET_GM_ID
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = _gm_id(mid) - TARGET_GM_ID
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    hand_derived_w_um = (lo + hi) / 2

    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    report = size.run_size(str(request))

    assert report["status"] == "pass"
    assert report["operating_point"]["w_um"] == pytest.approx(
        hand_derived_w_um, rel=0.03
    )


@_SKIP_NO_NGSPICE
def test_examples_size_worked_example_passes():
    exit_code = main(["size", str(EXAMPLES_DIR / "request.json"), "--format", "json"])
    assert exit_code == 0


@_SKIP_NO_SKY130_NGSPICE
def test_reproduces_canary_5t_ota_input_pair(tmp_path):
    """Reproduce a hand-sized single-stage reference from an existing analog
    canary, on the real PDK models (issue #721's second acceptance
    criterion).

    The canary is this repo's own sky130 5T OTA worked example
    (`examples/design-pipeline/`, Epic #105 Phase 3): a hand-sized NMOS
    input pair (`05-sizing.json`'s `M1`, W/L = 8/0.5 um) biased at half the
    20 uA tail current, whose open-loop AC response was simulated across a
    5x2x2 corner matrix and committed (`sim-ac.result.json`).

    The gm/Id target handed to `klt size` is derived **from the canary's own
    committed simulation evidence, not from its sizing rationale prose**:
    for a single-pole OTA loaded by `CL`, the unity-gain frequency is
    `ugf = gm1 / (2*pi*CL)`, so `gm1 = 2*pi*CL*ugf` and
    `gm/Id = gm1 / (Itail/2)`. Averaged over the canary's four `tt` corners
    that is ~17.0 S/A (the individual `tt` corners span 14.2 S/A at 125C to
    20.0 S/A at -40C -- gm/Id is strongly temperature-dependent, and the
    canary's matrix has no 27C point).

    `klt size` must land near the hand-sized 8 um. It is checked within a
    deliberately generous factor-of-two band, because three documented
    effects sit between the canary's UGF and this command's diode-connected
    operating point, all of them one-directional-in-principle but not
    quantified here:

    1. The canary's corner spread alone maps to roughly 3-9 um of width
       (14.2-20.0 S/A against the same device at fixed Id).
    2. `ugf = gm1/(2*pi*CL)` neglects the OTA's own parasitic loading, so
       the extracted `gm1` is a lower bound on the true device `gm`.
    3. `klt size`'s MVP biases the device diode-connected (`Vds = Vgs`),
       not at the OTA's actual `Vds` -- `docs/cli/size.md`'s "Known
       limitation".

    Measured when this test was written (sky130A `open_pdks
    c6d73a35f524`, ngspice 46): 5.44 um against the hand-sized 8 um, i.e.
    0.68x -- the same region of the gm/Id curve and the same order of
    magnitude, which is what a first-order gm/Id sizing MVP is expected to
    reproduce. A tighter band would encode PDK-release-specific numbers as
    a regression bar; see the epic (#705) for the later phases that size a
    device at its in-circuit `Vds`.
    """
    sizing = json.loads((CANARY_DIR / "05-sizing.json").read_text())
    ac_result = json.loads((CANARY_DIR / "sim-ac.result.json").read_text())

    input_pair = sizing["devices"]["M1"]
    hand_sized_w_um = input_pair["w_um"]
    id_a = (sizing["bias"]["tail_current_ua"] / 2) * 1e-6
    cl_f = sizing["load_cap_pf"] * 1e-12

    tt_ugf = [
        measurement["value"]
        for corner in ac_result["corners"]
        if corner["process"] == "tt"
        for measurement in corner["measurements"]
        if measurement["name"] == "ugf"
    ]
    assert len(tt_ugf) == 4, "canary AC result no longer has four tt corners"
    ugf_mean = sum(tt_ugf) / len(tt_ugf)
    target_gm_id = (2 * math.pi * cl_f * ugf_mean) / id_a

    request = _write_request(
        tmp_path,
        {
            "device": {
                "kind": "nmos",
                "model": input_pair["model"],
                "l_um": input_pair["l_um"],
                "w_min_um": 1.0,
                "w_max_um": 40.0,
            },
            "models": {
                "pdk": "sky130A",
                "lib": "libs.tech/ngspice/sky130.lib.spice",
            },
            "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
            "target": {"id_a": id_a, "gm_id": target_gm_id},
            "options": {
                "sweep_points": 12,
                # This is the only test in the file exercising the real
                # sky130A ngspice deck; the sweep + confirm run's wall-clock
                # cost is dominated by host contention (other processes'
                # CPU share), not by the work itself, so give it a generous
                # ceiling well above size.DEFAULT_TIMEOUT_S (180s) rather
                # than let a busy dev machine time it out under the default
                # budget (#730).
                "timeout_s": 900,
            },
        },
        name="canary.json",
    )

    report = size.run_size(str(request))

    # ngspice -- not the interpolator -- confirms the returned point hits
    # the canary-derived target. On failure, surface the method rationale
    # (which carries the ngspice timeout/error diagnostic) instead of a bare
    # status mismatch, so a starved host reads as a timeout, not a broken
    # sizer (#730).
    assert report["status"] == "pass", (
        f"status={report['status']!r}, rationale="
        f"{report.get('method', {}).get('rationale')!r}"
    )
    assert report["operating_point"]["gm_id"] == pytest.approx(target_gm_id, rel=0.03)
    assert report["operating_point"]["id_a"] == pytest.approx(id_a, rel=1e-3)
    assert report["operating_point"]["l_um"] == input_pair["l_um"]

    sized_w_um = report["operating_point"]["w_um"]
    assert hand_sized_w_um / 2 <= sized_w_um <= hand_sized_w_um * 2, (
        f"sized W={sized_w_um:.3g}um is not within a factor of two of the "
        f"canary's hand-sized W={hand_sized_w_um}um"
    )

    # The stated method/rationale is part of the contract, not decoration.
    assert report["method"]["feasible"] is True
    assert "gm/Id" in report["method"]["rationale"]
    assert report["operating_point"]["inversion_level"] in {
        "weak",
        "moderate",
        "strong",
    }


@_SKIP_NO_NGSPICE
def test_run_size_keep_artifacts_writes_decks(tmp_path):
    _write_models_lib(tmp_path)
    outdir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        _base_request(options={"sweep_points": 10, "keep_artifacts": True}),
    )

    report = size.run_size(str(request), artifacts_dir=str(outdir))

    assert report["status"] == "pass"
    assert (outdir / "sweep.cir").is_file()
    assert (outdir / "confirm.cir").is_file()
    assert report["environment"]["artifacts_dir"] == str(outdir)


# --------------------------------------------------------------------------- #
# Corner-set input parsing (issue #729): _parse_corner_set / _resolve_sizing_index
# --------------------------------------------------------------------------- #


def test_parse_corner_set_legacy_single_corner():
    request = {"corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27}}
    points, sizing_index = size._parse_corner_set(request)

    assert sizing_index == 0
    assert len(points) == 1
    assert points[0]["process"] == "tt"
    assert points[0]["vdd_v"] == 1.8
    assert points[0]["temperature_c"] == 27.0
    assert points[0]["corner_id"] == "tt/27C"


def test_parse_corner_set_legacy_defaults_when_corner_omitted():
    with pytest.raises(size.SizeError, match="vdd_v"):
        size._parse_corner_set({})


def test_parse_corner_set_process_and_temperature_arrays():
    request = {
        "corners": {
            "process": ["tt", "ss", "ff"],
            "temperature_c": [27, -40, 125],
            "vdd_v": 1.8,
        }
    }
    points, sizing_index = size._parse_corner_set(request)

    assert len(points) == 9
    # Default sizing corner: first declared point on each axis.
    assert sizing_index == 0
    assert points[0]["process"] == "tt"
    assert points[0]["temperature_c"] == 27.0
    assert all(p["vdd_v"] == 1.8 for p in points)
    assert {p["process"] for p in points} == {"tt", "ss", "ff"}
    assert {p["temperature_c"] for p in points} == {27.0, -40.0, 125.0}
    # Deterministic order: process (outer) x temperature (inner).
    assert [p["process"] for p in points[:3]] == ["tt", "tt", "tt"]
    assert [p["temperature_c"] for p in points[:3]] == [27.0, -40.0, 125.0]


def test_parse_corner_set_process_bundle():
    request = {
        "corners": {
            "process": [{"name": "typical", "sections": ["mos_tt", "res_tt"]}],
            "temperature_c": [27],
            "vdd_v": 1.8,
        }
    }
    points, _sizing_index = size._parse_corner_set(request)

    assert len(points) == 1
    assert points[0]["process"] == "typical"
    assert points[0]["process_sections"] == ["mos_tt", "res_tt"]
    assert points[0]["corner_id"] == "typical/27C"


def test_parse_corner_set_missing_vdd_raises():
    request = {"corners": {"process": ["tt"], "temperature_c": [27]}}
    with pytest.raises(size.SizeError, match="vdd_v"):
        size._parse_corner_set(request)


def test_parse_corner_set_both_corner_and_corners_raises():
    request = {
        "corner": {"process": "tt", "vdd_v": 1.8},
        "corners": {"process": ["tt"], "vdd_v": 1.8},
    }
    with pytest.raises(size.SizeError, match="not both"):
        size._parse_corner_set(request)


def test_parse_corner_set_explicit_sizing_selects_declared_point():
    request = {
        "corners": {
            "process": ["tt", "ss", "ff"],
            "temperature_c": [27, 125],
            "vdd_v": 1.8,
            "sizing": {"process": "ff", "temperature_c": 125},
        }
    }
    points, sizing_index = size._parse_corner_set(request)

    assert points[sizing_index]["process"] == "ff"
    assert points[sizing_index]["temperature_c"] == 125.0


def test_parse_corner_set_sizing_no_match_raises():
    request = {
        "corners": {
            "process": ["tt", "ss"],
            "vdd_v": 1.8,
            "sizing": {"process": "ff"},
        }
    }
    with pytest.raises(size.SizeError, match="does not match"):
        size._parse_corner_set(request)


def test_resolve_sizing_index_default_is_first_point():
    points = [
        {"process": "tt", "temperature_c": 27.0},
        {"process": "ss", "temperature_c": -40.0},
    ]
    assert size._resolve_sizing_index(points, None) == 0


def test_corner_label_formats_process_and_temperature():
    assert size._corner_label("tt", 27.0) == "tt/27C"
    assert size._corner_label("ss", -40.0) == "ss/-40C"


# --------------------------------------------------------------------------- #
# Corner-set verification pipeline (issue #729): full run_size, stubbed
# ngspice (synthetic square-law model -- see `_install_synthetic_ngspice_stub`)
# --------------------------------------------------------------------------- #


def test_run_size_single_corner_response_includes_corners_block(tmp_path, monkeypatch):
    """The pre-#729 single-corner request shape keeps working unchanged, and
    now additionally carries a `corners` block with exactly one declared/
    result entry -- the additive, backward-compatible shape."""
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_request())
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    assert report["status"] == "pass"
    assert report["operating_point"]["gm_id"] == pytest.approx(8.0, rel=0.02)
    assert report["corner"]["process"] == "tt"
    assert report["corner"]["vdd_v"] == 1.8
    assert report["corner"]["temperature_c"] == 27.0
    corners = report["corners"]
    assert len(corners["declared"]) == 1
    assert corners["sizing"]["corner_id"] == "tt/27C"
    assert corners["hold_across_corners"] is False
    assert len(corners["results"]) == 1
    assert corners["results"][0]["is_sizing"] is True
    assert corners["results"][0]["status"] == "pass"


def test_run_size_corner_set_reports_spread_without_failing_by_default(
    tmp_path, monkeypatch
):
    """A wide PVT spread drifts gm/Id enough that a non-sizing corner misses
    tolerance -- but that alone must not fail the aggregate run (issue #729
    acceptance criterion)."""
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    corners = report["corners"]
    assert len(corners["declared"]) == 2
    assert corners["hold_across_corners"] is False
    results_by_corner = {r["corner_id"]: r for r in corners["results"]}
    sizing_result = results_by_corner["tt/27C"]
    other_result = results_by_corner["tt/125C"]

    assert sizing_result["is_sizing"] is True
    assert sizing_result["status"] == "pass"
    assert other_result["is_sizing"] is False
    # The temperature-driven Vth shift pushes gm/Id outside the default 3%
    # tolerance at the same fixed width -- a real, reported spread.
    assert other_result["status"] == "fail"
    assert other_result["operating_point"] is not None
    assert other_result["margins"]["gm_id_rel_error"] != pytest.approx(0.0, abs=0.03)

    # A non-sizing corner's "fail" does not fail the aggregate by default.
    assert report["status"] == "pass"


def test_run_size_hold_across_corners_fails_aggregate_on_spread(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
            },
            targets={"hold_across_corners": True},
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    assert report["corners"]["hold_across_corners"] is True
    assert report["status"] == "fail"
    # The sizing corner itself still reports its own true status.
    results_by_corner = {r["corner_id"]: r for r in report["corners"]["results"]}
    assert results_by_corner["tt/27C"]["status"] == "pass"


def test_run_size_corner_set_errored_corner_forces_aggregate_error(
    tmp_path, monkeypatch
):
    """An evaluator error at ANY declared corner makes the whole run
    `error`, even when the sizing corner itself solved cleanly (issue #729's
    error > fail > pass precedence, matching `klt sim`)."""
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch, error_temps={125.0})

    report = size.run_size(str(request))

    assert report["status"] == "error"
    results_by_corner = {r["corner_id"]: r for r in report["corners"]["results"]}
    assert results_by_corner["tt/27C"]["status"] == "pass"
    assert results_by_corner["tt/125C"]["status"] == "error"
    assert results_by_corner["tt/125C"]["diagnostic"] is not None
    # The sizing corner's own solved operating point is still reported --
    # the aggregate `status` signals reduced trust in the *spread*, not that
    # the sizing search itself failed.
    assert report["operating_point"] is not None
    assert report["operating_point"]["gm_id"] == pytest.approx(8.0, rel=0.02)


def test_run_size_corner_set_sizing_selects_declared_point(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
                "sizing": {"temperature_c": 125},
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    assert report["corners"]["sizing"]["corner_id"] == "tt/125C"
    results_by_corner = {r["corner_id"]: r for r in report["corners"]["results"]}
    assert results_by_corner["tt/125C"]["is_sizing"] is True
    assert results_by_corner["tt/27C"]["is_sizing"] is False


def test_cli_exit_code_error_status_from_non_sizing_corner(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch, error_temps={125.0})

    assert main(["size", str(request), "--format", "json"]) == 4


# --------------------------------------------------------------------------- #
# worst_case_margin objective (issue #769)
# --------------------------------------------------------------------------- #


def test_interp_gm_id_at_width_interpolates_and_clamps():
    points = [
        {"w_um": 1.0, "gm_id": 4.0},
        {"w_um": 4.0, "gm_id": 10.0},
    ]
    # Exact grid points.
    assert size._interp_gm_id_at_width(points, 1.0) == pytest.approx(4.0)
    assert size._interp_gm_id_at_width(points, 4.0) == pytest.approx(10.0)
    # Interior point: linear in ln(w). w=2 is the geometric mean of 1 and 4
    # (ln(2) is exactly halfway between ln(1) and ln(4)), so gm_id should be
    # exactly halfway between 4.0 and 10.0.
    assert size._interp_gm_id_at_width(points, 2.0) == pytest.approx(7.0, rel=1e-6)
    # Out of range clamps to the nearest endpoint rather than extrapolating.
    assert size._interp_gm_id_at_width(points, 0.1) == pytest.approx(4.0)
    assert size._interp_gm_id_at_width(points, 100.0) == pytest.approx(10.0)


def test_parse_objective_defaults_to_sizing_corner():
    assert size._parse_objective({}) == "sizing_corner"
    assert size._parse_objective({"corner": {}}) == "sizing_corner"
    assert size._parse_objective({"corners": {}}) == "sizing_corner"


def test_parse_objective_reads_corners_block():
    request = {"corners": {"objective": "worst_case_margin"}}
    assert size._parse_objective(request) == "worst_case_margin"


def test_parse_objective_invalid_raises():
    request = {"corners": {"objective": "bogus"}}
    with pytest.raises(size.SizeError, match="corners.objective"):
        size._parse_objective(request)


def test_run_size_worst_case_margin_invalid_objective_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
                "objective": "bogus",
            },
        ),
    )

    with pytest.raises(size.SizeError, match="corners.objective"):
        size.run_size(str(request))


def test_run_size_worst_case_margin_reports_corners_block(tmp_path, monkeypatch):
    """The `worst_case_margin` objective echoes itself, populates
    `corners.worst_case`, and reports a full-shaped result (operating point
    + margins) per declared corner -- issue #769's "validated in ngspice
    across the full corner set with per-corner margins reported"."""
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [-40, 27, 125],
                "vdd_v": 1.8,
                "objective": "worst_case_margin",
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    corners = report["corners"]
    assert corners["objective"] == "worst_case_margin"
    assert len(corners["declared"]) == 3
    assert len(corners["results"]) == 3
    assert corners["worst_case"] in {r["corner_id"] for r in corners["results"]}

    for result in corners["results"]:
        assert result["status"] in {"pass", "fail"}
        assert result["operating_point"] is not None
        assert result["margins"] is not None
        assert isinstance(result["margins"]["gm_id_rel_error"], float)

    # Top-level fields mirror the worst-margin corner's own confirmed result
    # under this objective (not the declared sizing corner, unlike the
    # default "sizing_corner" objective).
    worst_result = next(
        r for r in corners["results"] if r["corner_id"] == corners["worst_case"]
    )
    assert report["operating_point"] == worst_result["operating_point"]
    assert report["margins"] == worst_result["margins"]
    assert report["corner"]["corner_id"] == corners["worst_case"]

    assert report["method"]["name"].startswith("worst-corner margin")
    assert report["method"]["bracket_w_um"] is None
    assert report["method"]["interpolated_w_um"] is not None


def test_run_size_worst_case_margin_reduces_worst_case_error_vs_sizing_corner(
    tmp_path, monkeypatch
):
    """The whole point of the objective: across a wide PVT spread, the width
    it converges to should do at least as well on the *worst* per-corner
    margin as the default `sizing_corner` objective, which only ever
    targets the nominal corner and lets the rest fall where they may."""
    _write_models_lib(tmp_path)
    corners_spec = {
        "process": ["tt"],
        "temperature_c": [-40, 27, 125],
        "vdd_v": 1.8,
    }

    nominal_request = _write_request(
        tmp_path,
        _base_request(corner=None, corners=corners_spec),
        name="nominal.json",
    )
    _install_synthetic_ngspice_stub(monkeypatch)
    nominal_report = size.run_size(str(nominal_request))
    nominal_worst = max(
        abs(r["margins"]["gm_id_rel_error"])
        for r in nominal_report["corners"]["results"]
    )

    worst_case_request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={**corners_spec, "objective": "worst_case_margin"},
        ),
        name="worst_case.json",
    )
    worst_case_report = size.run_size(str(worst_case_request))
    worst_case_worst = max(
        abs(r["margins"]["gm_id_rel_error"])
        for r in worst_case_report["corners"]["results"]
    )

    assert worst_case_worst <= nominal_worst + 1e-9


def test_run_size_worst_case_margin_single_corner_matches_sizing_corner(
    tmp_path, monkeypatch
):
    """A single declared corner makes the two objectives mathematically
    equivalent (this module's own documented claim) -- verify the solved
    gm/Id actually agrees closely, even though the search algorithms
    differ."""
    _write_models_lib(tmp_path)
    corners_spec = {"process": ["tt"], "temperature_c": [27], "vdd_v": 1.8}

    sizing_request = _write_request(
        tmp_path,
        _base_request(corner=None, corners=corners_spec),
        name="sizing.json",
    )
    _install_synthetic_ngspice_stub(monkeypatch)
    sizing_report = size.run_size(str(sizing_request))

    worst_case_request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={**corners_spec, "objective": "worst_case_margin"},
        ),
        name="worst_case.json",
    )
    worst_case_report = size.run_size(str(worst_case_request))

    assert worst_case_report["operating_point"]["gm_id"] == pytest.approx(
        sizing_report["operating_point"]["gm_id"], rel=1e-3
    )
    assert worst_case_report["operating_point"]["w_um"] == pytest.approx(
        sizing_report["operating_point"]["w_um"], rel=1e-3
    )


def test_run_size_legacy_corner_unaffected_by_objective_feature(tmp_path, monkeypatch):
    """Regression (issue #769 acceptance criterion): a request using the
    original single-`corner` shape has no `objective` field to set at all,
    so it is bit-for-bit identical to a `corners` request with exactly one
    declared point and the (default) `sizing_corner` objective."""
    _write_models_lib(tmp_path)
    _install_synthetic_ngspice_stub(monkeypatch)

    legacy_request = _write_request(tmp_path, _base_request(), name="legacy.json")
    legacy_report = size.run_size(str(legacy_request))

    corners_request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={"process": ["tt"], "temperature_c": [27], "vdd_v": 1.8},
        ),
        name="corners.json",
    )
    corners_report = size.run_size(str(corners_request))

    assert legacy_report["operating_point"] == corners_report["operating_point"]
    assert legacy_report["margins"] == corners_report["margins"]
    assert legacy_report["status"] == corners_report["status"]
    assert corners_report["corners"]["objective"] == "sizing_corner"


def test_run_size_worst_case_margin_all_corners_sweep_error_reports_error(
    tmp_path, monkeypatch
):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
                "objective": "worst_case_margin",
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch, error_temps={27.0, 125.0})

    report = size.run_size(str(request))

    assert report["status"] == "error"
    assert report["operating_point"] is None
    assert report["margins"] is None
    assert report["corners"]["results"] is None


def test_run_size_worst_case_margin_partial_corner_error_forces_aggregate_error(
    tmp_path, monkeypatch
):
    """One declared corner's sweep failing under `worst_case_margin` still
    lets the search converge against the surviving corners' curves -- but
    the aggregate `status` is still forced to `error`, mirroring the
    `sizing_corner` objective's own error > fail > pass precedence."""
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [-40, 27, 125],
                "vdd_v": 1.8,
                "objective": "worst_case_margin",
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch, error_temps={125.0})

    report = size.run_size(str(request))

    assert report["status"] == "error"
    results_by_corner = {r["corner_id"]: r for r in report["corners"]["results"]}
    assert results_by_corner["tt/125C"]["status"] == "error"
    assert results_by_corner["tt/-40C"]["status"] in {"pass", "fail"}
    assert results_by_corner["tt/27C"]["status"] in {"pass", "fail"}
    # A trustworthy worst-case result is still reported from the surviving
    # corners, exactly like the sizing_corner objective still reports the
    # sizing corner's own result on a non-sizing corner's error.
    assert report["operating_point"] is not None
    assert report["margins"] is not None


def test_run_size_worst_case_margin_infeasible_reports_boundary(tmp_path, monkeypatch):
    """An unreachable target across every declared corner (even at the
    widest allowed width) reports the boundary width instead of
    extrapolating, mirroring the sizing_corner objective's own
    infeasible-target handling."""
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
                "objective": "worst_case_margin",
            },
            target={"id_a": 2e-05, "gm_id": 1000.0},
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    report = size.run_size(str(request))

    assert report["method"]["feasible"] is False
    assert report["operating_point"]["w_um"] == pytest.approx(20.0, rel=1e-6)


def test_cli_worst_case_margin_text_output_includes_objective(
    tmp_path, monkeypatch, capsys
):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_request(
            corner=None,
            corners={
                "process": ["tt"],
                "temperature_c": [27, 125],
                "vdd_v": 1.8,
                "objective": "worst_case_margin",
            },
        ),
    )
    _install_synthetic_ngspice_stub(monkeypatch)

    main(["size", str(request), "--format", "text"])
    captured = capsys.readouterr()
    assert "objective: worst_case_margin" in captured.out
    assert "corners:" in captured.out


# --------------------------------------------------------------------------- #
# Coupled multi-device topology sizing (issue #768, Phase 1a of epic #705)
# --------------------------------------------------------------------------- #
#
# Three tiers, mirroring the single-device tests above:
#
# - Pure-function/validation tests for the new `request.topology` shape and
#   the joint solve's own helpers (no ngspice).
# - Integration tests against the tiny synthetic `nmos_demo`/`pmos_demo`
#   library, which exercise the real assembled-topology deck, the coupled
#   iteration, and the per-instance read-back.
# - A canary reproduction (`@_SKIP_NO_SKY130_NGSPICE`) that hands the joint
#   solver the *hand-sized* 5T OTA canary's own measured per-device gm/Id as
#   the spec and asserts it re-derives the hand-sized widths.


def _base_topology_request(models_lib_name: str = "models.lib", **overrides) -> dict:
    """A `diff_pair_mirror_tail` request on the same synthetic
    `nmos_demo`/`pmos_demo` library `_write_models_lib` writes -- tuned
    (`bias.vcm_v: 1.3`, these gm/Id targets) so the joint solve converges
    with every device reporting `status: "pass"` against the real `ngspice`
    binary. See `examples/size/generate.py`'s `write_topology_request`
    docstring for why the default mid-supply common-mode bias would not
    leave the tail device enough headroom at these targets.
    """
    request = {
        "topology": "diff_pair_mirror_tail",
        "devices": {
            "input_pair": {
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
            "mirror": {
                "kind": "pmos",
                "model": "pmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
            "tail": {
                "kind": "nmos",
                "model": "nmos_demo",
                "l_um": 0.5,
                "w_min_um": 0.5,
                "w_max_um": 60,
            },
        },
        "models": {"lib": models_lib_name},
        "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        "budget": {"tail_current_a": 2e-05, "tail_mirror_ratio": 1},
        "target": {
            "input_pair": {"gm_id": 20.0},
            "mirror": {"gm_id": 12.0},
            "tail": {"gm_id": 20.0},
        },
        "bias": {"vcm_v": 1.3},
        "tolerance": {"gm_id_rel": 0.05},
        "options": {"sweep_points": 25},
    }
    request.update(overrides)
    return request


# --- request validation (raised before ngspice ever runs) ------------------ #


def test_load_request_device_and_topology_both_present_raises(tmp_path):
    request = _base_request()
    request["topology"] = "diff_pair_mirror_tail"
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="not both"):
        size.load_request(str(path))


def test_load_request_neither_device_nor_topology_raises(tmp_path):
    request = _base_request()
    del request["device"]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="device.*topology"):
        size.load_request(str(path))


def test_run_size_unsupported_topology_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    request["topology"] = "two_stage_miller"
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="unsupported topology"):
        size.run_size(str(path))


def test_run_size_topology_corners_not_supported_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    del request["corner"]
    request["corners"] = {"process": ["tt", "ss"], "vdd_v": 1.8}
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="does not yet support"):
        size.run_size(str(path))


@pytest.mark.parametrize("missing_role", ["input_pair", "mirror", "tail"])
def test_run_size_topology_missing_role_device_raises(tmp_path, missing_role):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    del request["devices"][missing_role]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match=f"devices.{missing_role}"):
        size.run_size(str(path))


def test_run_size_topology_wrong_device_kind_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    request["devices"]["input_pair"]["kind"] = "pmos"
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="input_pair.kind must be 'nmos'"):
        size.run_size(str(path))


def test_run_size_topology_missing_budget_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    del request["budget"]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="budget"):
        size.run_size(str(path))


def test_run_size_topology_non_positive_tail_current_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request(budget={"tail_current_a": -1e-5})
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="tail_current_a"):
        size.run_size(str(path))


def test_run_size_topology_non_positive_mirror_ratio_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request(
        budget={"tail_current_a": 2e-5, "tail_mirror_ratio": 0}
    )
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="tail_mirror_ratio"):
        size.run_size(str(path))


@pytest.mark.parametrize("missing_role", ["input_pair", "mirror", "tail"])
def test_run_size_topology_missing_target_role_raises(tmp_path, missing_role):
    _write_models_lib(tmp_path)
    request = _base_topology_request()
    del request["target"][missing_role]
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match=f"target.{missing_role}"):
        size.run_size(str(path))


def test_run_size_topology_invalid_vcm_v_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request(bias={"vcm_v": "mid"})
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="bias.vcm_v"):
        size.run_size(str(path))


def test_run_size_topology_invalid_max_joint_iterations_raises(tmp_path):
    _write_models_lib(tmp_path)
    request = _base_topology_request(
        options={"sweep_points": 10, "max_joint_iterations": 0}
    )
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="max_joint_iterations"):
        size.run_size(str(path))


# --- coupled-budget / joint-solve helpers (pure functions) ----------------- #


def test_parse_topology_budget_defaults_to_unity_mirror_ratio():
    budget = size._parse_topology_budget({"tail_current_a": 2e-5})

    assert budget["tail_mirror_ratio"] == 1.0
    assert budget["reference_current_a"] == pytest.approx(2e-5)
    assert budget["branch_current_a"] == pytest.approx(1e-5)


def test_parse_topology_budget_mirror_ratio_scales_reference_current():
    """`tail_mirror_ratio` is a coupled degree of freedom, not a per-device
    knob: a 4:1 tail mirror sources the same tail current from a quarter of
    the reference current, while each input-pair leg still carries half the
    tail current."""
    budget = size._parse_topology_budget(
        {"tail_current_a": 2e-5, "tail_mirror_ratio": 4}
    )

    assert budget["reference_current_a"] == pytest.approx(5e-6)
    assert budget["branch_current_a"] == pytest.approx(1e-5)


def test_topology_instances_cover_every_role_with_one_control_each():
    roles = {role for _key, _inst, role, _ctl in size.TOPOLOGY_INSTANCES}
    assert roles == set(size.TOPOLOGY_ROLES)
    for role in size.TOPOLOGY_ROLES:
        controls = [
            key
            for key, _inst, entry_role, is_control in size.TOPOLOGY_INSTANCES
            if entry_role == role and is_control
        ]
        assert controls == [size._ROLE_CONTROL_INSTANCE[role]]


def test_invert_curve_interpolates_within_the_swept_range():
    curve = [
        {"w_um": 1.0, "gm_id": 5.0},
        {"w_um": 4.0, "gm_id": 15.0},
    ]
    w_um, feasible, note = size._invert_curve(curve, 10.0)

    assert feasible is True
    assert note == ""
    assert w_um == pytest.approx(2.0)


def test_invert_curve_clamps_to_the_closest_boundary_when_unreachable():
    curve = [
        {"w_um": 1.0, "gm_id": 5.0},
        {"w_um": 4.0, "gm_id": 15.0},
    ]
    w_um, feasible, note = size._invert_curve(curve, 40.0)

    assert feasible is False
    assert w_um == 4.0
    assert "highest achievable" in note


def test_op_point_from_confirmed_normalizes_pmos_overdrive_polarity():
    """Two PDK conventions report a PMOS operating point differently: a bare
    SPICE `level=1` device returns negative Vgs/Vth, sky130's
    `__pfet_01v8` subcircuit returns positive magnitudes. Both describe the
    same strongly-inverted device, and both must classify as `strong` --
    keying the polarity off `sign(Vth)` is what makes that true (issue #768:
    the coupled topology's mirror load is a PMOS)."""
    device = {"kind": "pmos", "l_um": 0.5, "nf": 1, "mult": 1}
    negative_convention = {
        "w_um": 4.0,
        "gm_s": -1e-4,
        "id_a": -1e-5,
        "vgs_v": -0.9,
        "vth_v": -0.5,
        "vdsat_v": None,
    }
    positive_convention = dict(negative_convention, vgs_v=1.2, vth_v=1.0)

    negative_op, _gm_id, negative_vov, _approx = size._op_point_from_confirmed(
        negative_convention, device
    )
    positive_op, _gm_id, positive_vov, _approx = size._op_point_from_confirmed(
        positive_convention, device
    )

    assert negative_vov == pytest.approx(0.4)
    assert negative_op["inversion_level"] == "strong"
    assert positive_vov == pytest.approx(0.2)
    assert positive_op["inversion_level"] == "strong"


def test_parse_topology_log_groups_instances_and_node_voltages():
    log_text = "\n".join(
        [
            "** ngspice-99",
            "KLT_SIZE_INSTANCE tail",
            "@m.xtail.mnmos_demo[gm] = 1.5e-04",
            "@m.xtail.mnmos_demo[id] = 2.0e-05",
            "KLT_SIZE_INSTANCE input_a",
            "@m.xinputa.mnmos_demo[gm] = 1.0e-04",
            "@m.xinputa.mnmos_demo[id] = 1.0e-05",
            "KLT_SIZE_INSTANCE __nodes__",
            "v(out) = 8.9e-01",
            "v(tail) = 2.0e-01",
        ]
    )

    points, nodes = size._parse_topology_log(log_text)

    assert points["tail"]["gm_s"] == pytest.approx(1.5e-4)
    assert points["input_a"]["id_a"] == pytest.approx(1.0e-5)
    # Instances the deck never printed are present-but-None, so a caller can
    # tell "ran but incomplete" from "never ran" (same convention as
    # `_parse_sweep_log`).
    assert points["mirror_b"] is None
    assert nodes == {"out": pytest.approx(0.89), "tail": pytest.approx(0.2)}


def test_write_topology_deck_assembles_the_real_coupled_circuit(tmp_path):
    """The joint deck must be the *actual* topology -- six instances, a
    diode-connected tail replica, the DC-only feedback network that holds
    the output at the common-mode point -- not a per-device surrogate."""
    devices = size._parse_topology_devices(
        _base_topology_request()["devices"], "diff_pair_mirror_tail"
    )
    budget = size._parse_topology_budget(
        {"tail_current_a": 2e-5, "tail_mirror_ratio": 4}
    )
    deck_path = tmp_path / "joint.cir"

    size._write_topology_deck(
        deck_path=str(deck_path),
        devices=devices,
        widths_um={"tail": 3.0, "input_pair": 4.0, "mirror": 5.0},
        corner={"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
        budget=budget,
        vcm_v=0.9,
        models_lib="models.lib",
    )
    deck = deck_path.read_text()

    # Diode-connected tail replica fed by the (ratio-scaled) reference
    # current, tail device mirroring it up by `tail_mirror_ratio`.
    assert "Iref vdd nbias DC 5e-06" in deck
    assert "Xtailref nbias nbias 0 0 nmos_demo" in deck
    assert "Xtail tail nbias 0 0 nmos_demo" in deck
    assert "mult=4.0" in deck
    # Input pair sources tied to the tail node; mirror diode leg on n1.
    assert "Xinputa n1 inp tail 0 nmos_demo" in deck
    assert "Xinputb out inn tail 0 nmos_demo" in deck
    assert "Xmirrora n1 n1 vdd vdd pmos_demo" in deck
    assert "Xmirrorb out n1 vdd vdd pmos_demo" in deck
    # DC-only feedback network (short at DC, open at AC).
    assert "Lfb out inn" in deck
    assert "Cfb inn cm" in deck
    # Every instance instrumented, plus the coupled node voltages.
    for key, _instance, _role, _control in size.TOPOLOGY_INSTANCES:
        assert f"KLT_SIZE_INSTANCE {key}" in deck
    assert "print v(out)" in deck


# --- evaluator-error path, stubbed (no real ngspice needed) ---------------- #


def test_run_size_topology_stubbed_missing_binary_is_evaluator_error(
    tmp_path, monkeypatch
):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    report = size.run_size(str(request))

    assert report["status"] == "error"
    assert report["devices"] is None
    assert report["budget"]["measured"] is None
    # `method` is populated on every status, including this one.
    assert "Evaluator error" in report["method"]["rationale"]
    assert report["roles"]["tail"]["target_gm_id"] == 20.0


def test_cli_topology_exit_code_evaluator_errored(tmp_path, monkeypatch):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())
    _stub_subprocess_run(monkeypatch, side_effect=FileNotFoundError("no ngspice"))

    assert main(["size", str(request), "--format", "json"]) == 4


# --- real ngspice against the tiny synthetic library ----------------------- #


@_SKIP_NO_NGSPICE
def test_run_size_topology_solves_every_device_jointly(tmp_path):
    """Issue #768's first acceptance criterion: one request spanning a
    diff-pair + mirror + tail returns a sized operating point for *all*
    devices jointly, validated in ngspice."""
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())

    report = size.run_size(str(request))

    assert report["status"] == "pass", report["method"]["rationale"]
    assert report["topology"] == "diff_pair_mirror_tail"

    # Three solved widths shared by six reported instances.
    assert set(report["roles"]) == set(size.TOPOLOGY_ROLES)
    assert set(report["devices"]) == {
        key for key, _i, _r, _c in size.TOPOLOGY_INSTANCES
    }
    for role in size.TOPOLOGY_ROLES:
        assert report["roles"][role]["w_um"] > 0
    for key, _instance, role, _control in size.TOPOLOGY_INSTANCES:
        entry = report["devices"][key]
        assert entry["role"] == role
        assert entry["operating_point"]["w_um"] == report["roles"][role]["w_um"]

    # The joint solve actually ran against the assembled circuit.
    solve = report["joint_solve"]
    assert solve["converged"] is True
    assert solve["iterations_run"] >= 1
    for step in solve["iterations"]:
        assert set(step["widths_um"]) == set(size.TOPOLOGY_ROLES)

    # Every control device hit its target *in circuit*, not in a
    # diode-connected surrogate.
    tol = report["tolerance"]["gm_id_rel"]
    for role in size.TOPOLOGY_ROLES:
        control = report["devices"][size._ROLE_CONTROL_INSTANCE[role]]
        assert abs(control["margins"]["gm_id_rel_error"]) <= tol
        assert control["operating_point"]["gm_id"] == pytest.approx(
            report["roles"][role]["target_gm_id"], rel=tol
        )


@_SKIP_NO_NGSPICE
def test_run_size_topology_reports_measured_coupled_quantities(tmp_path):
    """The tail current split and the realized mirror ratio are *outputs*
    of the coupled solve -- the balanced topology should deliver ~50/50 and
    ~1:1, and the response has to say so rather than echo the request."""
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())

    report = size.run_size(str(request))
    measured = report["budget"]["measured"]

    assert measured["tail_split"][0] == pytest.approx(0.5, abs=0.02)
    assert measured["tail_split"][1] == pytest.approx(0.5, abs=0.02)
    assert measured["mirror_ratio"] == pytest.approx(1.0, rel=0.05)
    assert measured["tail_current_a"] == pytest.approx(
        report["budget"]["tail_current_a"], rel=0.25
    )
    # The output node is held at the common-mode point by the DC feedback
    # network, not floating to a rail.
    assert report["bias"]["vout_v"] == pytest.approx(report["bias"]["vcm_v"], abs=0.05)


@_SKIP_NO_NGSPICE
def test_run_size_topology_every_device_states_gm_id_and_inversion(tmp_path):
    """Issue #768's third acceptance criterion: every device's result states
    its gm/Id and its inversion-level rationale."""
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())

    report = size.run_size(str(request))

    for key, entry in report["devices"].items():
        op = entry["operating_point"]
        assert op["gm_id"] > 0
        assert op["inversion_level"] in {"weak", "moderate", "strong"}
        rationale = entry["rationale"]
        assert "gm/Id" in rationale
        assert f"Inversion level '{op['inversion_level']}'" in rationale
        assert entry["placement"], key
        # Both PMOS mirror legs must classify from a positive overdrive --
        # the polarity normalization is what keeps them off "weak".
        if entry["device"]["kind"] == "pmos":
            assert op["vov_v"] > 0


@_SKIP_NO_NGSPICE
def test_run_size_topology_joint_iteration_improves_on_the_seeded_widths(tmp_path):
    """The point of the joint solve: the seeded widths (iteration 0) come
    from per-role, diode-connected sizing -- exactly what "three independent
    single-device solves" would produce -- and the coupled iteration must
    measurably improve on them when the assembled circuit disagrees.

    The mid-supply common-mode bias below puts the tail device at a Vds far
    from the Vgs its diode-connected characterization assumed, so the seeded
    widths land visibly off target in the assembled circuit; a tight
    tolerance then forces at least one correction step.
    """
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_topology_request(
            target={
                "input_pair": {"gm_id": 12.0},
                "mirror": {"gm_id": 10.0},
                "tail": {"gm_id": 10.0},
            },
            bias={"vcm_v": 0.9},
            tolerance={"gm_id_rel": 0.005},
            options={"sweep_points": 25, "max_joint_iterations": 5},
        ),
    )

    report = size.run_size(str(request))
    steps = [s for s in report["joint_solve"]["iterations"] if s["status"] == "ok"]

    assert len(steps) >= 2, "tight tolerance should have forced a correction step"
    assert steps[-1]["worst_abs_rel_error"] < steps[0]["worst_abs_rel_error"]
    # ...and the widths genuinely moved between candidates.
    assert steps[-1]["widths_um"] != steps[0]["widths_um"]


@_SKIP_NO_NGSPICE
def test_run_size_topology_infeasible_target_reports_fail(tmp_path):
    _write_models_lib(tmp_path)
    request = _write_request(
        tmp_path,
        _base_topology_request(
            target={
                "input_pair": {"gm_id": 500.0},
                "mirror": {"gm_id": 12.0},
                "tail": {"gm_id": 20.0},
            }
        ),
    )

    report = size.run_size(str(request))

    assert report["status"] == "fail"
    assert report["roles"]["input_pair"]["feasible"] is False
    assert report["method"]["feasible"] is False
    assert "not reachable" in report["devices"]["input_a"]["rationale"]


@_SKIP_NO_NGSPICE
def test_run_size_topology_keep_artifacts_writes_decks(tmp_path):
    _write_models_lib(tmp_path)
    outdir = tmp_path / "artifacts"
    request = _write_request(
        tmp_path,
        _base_topology_request(
            options={"sweep_points": 10, "keep_artifacts": True},
        ),
    )

    report = size.run_size(str(request), artifacts_dir=str(outdir))

    assert (outdir / "tail_sweep.cir").is_file()
    assert (outdir / "input_pair_sweep.cir").is_file()
    assert (outdir / "mirror_sweep.cir").is_file()
    assert (outdir / "joint_0.cir").is_file()
    assert report["environment"]["artifacts_dir"] == str(outdir)


@_SKIP_NO_NGSPICE
def test_cli_topology_text_output(tmp_path, capsys):
    _write_models_lib(tmp_path)
    request = _write_request(tmp_path, _base_topology_request())

    assert main(["size", str(request), "--format", "text"]) == 0

    captured = capsys.readouterr()
    assert "topology: diff_pair_mirror_tail" in captured.out
    assert "solved widths:" in captured.out
    assert "[input_a] xinputa -- input_pair [control]" in captured.out
    assert "joint solve:" in captured.out


@_SKIP_NO_NGSPICE
def test_examples_size_topology_worked_example_passes():
    exit_code = main(
        ["size", str(EXAMPLES_DIR / "topology_request.json"), "--format", "json"]
    )
    assert exit_code == 0


# --- canary reproduction against the real sky130A models ------------------- #


_CANARY_INSTANCE_TO_ROLE = {"xm5": "tail", "xm1": "input_pair", "xm3": "mirror"}
_CANARY_OP_ELEMENT = {
    "xm5": "msky130_fd_pr__nfet_01v8",
    "xm1": "msky130_fd_pr__nfet_01v8",
    "xm3": "msky130_fd_pr__pfet_01v8",
}


def _measure_canary_gm_id(tmp_path: Path) -> dict[str, float]:
    """Measure the *hand-sized* 5T OTA canary's own per-role in-circuit
    gm/Id, by running its committed netlist (`examples/design-pipeline/
    ota_5t.spice`, W/L recorded in `05-sizing.json`) through ngspice at the
    `tt`/27C/1.8V corner.

    This is the spec the joint solver is then handed: it never sees the
    canary's widths, only the gm/Id the hand-sized design achieves and the
    shared tail-current budget. Reproducing the widths from that is the
    non-trivial part.
    """
    deck = tmp_path / "canary_ref.cir"
    log = tmp_path / "canary_ref.log"
    lines = [
        "* klt size test -- hand-sized 5T OTA canary reference operating point",
        f".lib {SKY130_NGSPICE_LIB} tt",
        f".include {CANARY_DIR / 'ota_5t.spice'}",
        ".temp 27",
        ".control",
        "op",
    ]
    for instance, role in _CANARY_INSTANCE_TO_ROLE.items():
        lines.append(f"echo KLT_SIZE_INSTANCE {role}")
        lines.append(f"print @m.{instance}.{_CANARY_OP_ELEMENT[instance]}[gm]")
        lines.append(f"print @m.{instance}.{_CANARY_OP_ELEMENT[instance]}[id]")
    lines += ["quit", ".endc", ".end"]
    deck.write_text("\n".join(lines) + "\n")

    subprocess.run(
        ["ngspice", "-b", str(deck), "-o", str(log)],
        capture_output=True,
        text=True,
        timeout=900,
    )
    values: dict[str, dict[str, float]] = {}
    current: str | None = None
    for raw in log.read_text(errors="replace").splitlines():
        line = raw.strip()
        marker = size._INSTANCE_MARKER_RE.match(line)
        if marker:
            current = marker.group(1)
            values[current] = {}
            continue
        match = size._OP_VALUE_RE.match(line)
        if match and current is not None:
            values[current][match.group(1)] = float(match.group(2))
    return {
        role: abs(entry["gm"]) / abs(entry["id"])
        for role, entry in values.items()
        if entry.get("gm") is not None and entry.get("id")
    }


@_SKIP_NO_SKY130_NGSPICE
def test_reproduces_canary_5t_ota_topology_jointly(tmp_path):
    """Issue #768's second acceptance criterion: the jointly-sized result is
    validated in ngspice and reproduces the hand-sized reference on the same
    spec.

    The reference is this repo's own hand-sized, corner-verified 5T OTA
    canary (`examples/design-pipeline/`, `05-sizing.json`): an NMOS input
    pair (M1/M2, 8/0.5 um), a PMOS mirror load (M3/M4, 6/1 um) and an NMOS
    tail with its bias replica (M5/M5b, 10/1 um), biased at 20 uA with a 1:1
    tail mirror, whose open-loop AC response passed all 20 declared corners
    (`sim-ac.result.json`). The curator's own reconciliation on #768
    recommends this canary over the LDO error amp named in the epic phase
    description, which only models its error amp behaviorally.

    The *spec* handed to `klt size` is the canary's own measured per-role
    in-circuit gm/Id (`_measure_canary_gm_id`) plus its tail-current budget
    and channel lengths -- never its widths. The solver has to invert the
    coupled circuit to get those back.

    Two things are asserted, and deliberately with different strictness:

    1. **Reproduces the spec** (tight): every role's control device lands
       within the requested gm/Id tolerance *in the assembled circuit*.
       This is the "validated in ngspice" half, and it is a real bar --
       the seeded, per-role diode-connected widths do not meet it.
    2. **Reproduces the reference widths** (a factor-of-two band, matching
       `test_reproduces_canary_5t_ota_input_pair`'s own precedent): the
       hand-sized design is one point on a shallow gm/Id-vs-W curve, and
       the canary's committed evidence spans a wide corner range while this
       command sizes at one nominal corner. A tighter band would encode
       PDK-release-specific numbers as a regression bar.
    """
    sizing = json.loads((CANARY_DIR / "05-sizing.json").read_text())
    ac_result = json.loads((CANARY_DIR / "sim-ac.result.json").read_text())
    devices = sizing["devices"]
    tail_current_a = sizing["bias"]["tail_current_ua"] * 1e-6

    reference_gm_id = _measure_canary_gm_id(tmp_path)
    assert set(reference_gm_id) == set(size.TOPOLOGY_ROLES), (
        "could not measure the hand-sized canary's own operating point: "
        f"{reference_gm_id}"
    )

    # Cross-check the measured input-pair gm/Id against the same committed
    # AC evidence `test_reproduces_canary_5t_ota_input_pair` derives its
    # target from (`gm1 = 2*pi*CL*ugf`, averaged over the four tt corners),
    # so this test is anchored to simulation evidence, not only to a fresh
    # measurement of the netlist.
    tt_ugf = [
        measurement["value"]
        for corner in ac_result["corners"]
        if corner["process"] == "tt"
        for measurement in corner["measurements"]
        if measurement["name"] == "ugf"
    ]
    ac_derived_gm_id = (
        2 * math.pi * (sizing["load_cap_pf"] * 1e-12) * (sum(tt_ugf) / len(tt_ugf))
    ) / (tail_current_a / 2)
    assert reference_gm_id["input_pair"] == pytest.approx(ac_derived_gm_id, rel=0.5)

    request = _write_request(
        tmp_path,
        {
            "topology": "diff_pair_mirror_tail",
            "devices": {
                "input_pair": {
                    "kind": "nmos",
                    "model": devices["M1"]["model"],
                    "l_um": devices["M1"]["l_um"],
                    "w_min_um": 1.0,
                    "w_max_um": 40.0,
                },
                "mirror": {
                    "kind": "pmos",
                    "model": devices["M3"]["model"],
                    "l_um": devices["M3"]["l_um"],
                    "w_min_um": 1.0,
                    "w_max_um": 40.0,
                },
                "tail": {
                    "kind": "nmos",
                    "model": devices["M5"]["model"],
                    "l_um": devices["M5"]["l_um"],
                    "w_min_um": 1.0,
                    "w_max_um": 40.0,
                },
            },
            "models": {
                "pdk": "sky130A",
                "lib": "libs.tech/ngspice/sky130.lib.spice",
            },
            "corner": {"process": "tt", "vdd_v": 1.8, "temperature_c": 27},
            "budget": {"tail_current_a": tail_current_a, "tail_mirror_ratio": 1},
            "target": {
                role: {"gm_id": reference_gm_id[role]} for role in size.TOPOLOGY_ROLES
            },
            # The canary's own common-mode operating point.
            "bias": {"vcm_v": 0.9},
            "tolerance": {"gm_id_rel": 0.05},
            "options": {
                # Same reasoning as `test_reproduces_canary_5t_ota_input_pair`
                # (#730): the real sky130A deck's wall-clock cost is dominated
                # by host contention, so give each invocation a ceiling well
                # above `size.DEFAULT_TIMEOUT_S`.
                "sweep_points": 12,
                "max_joint_iterations": 4,
                "timeout_s": 900,
            },
        },
        name="canary_topology.json",
    )

    report = size.run_size(str(request))

    assert report["status"] == "pass", (
        f"status={report['status']!r}, rationale="
        f"{report.get('method', {}).get('rationale')!r}"
    )
    assert report["joint_solve"]["converged"] is True

    # (1) Reproduces the spec, confirmed by ngspice on the assembled circuit.
    tol = report["tolerance"]["gm_id_rel"]
    for role in size.TOPOLOGY_ROLES:
        control = report["devices"][size._ROLE_CONTROL_INSTANCE[role]]
        assert control["operating_point"]["gm_id"] == pytest.approx(
            reference_gm_id[role], rel=tol
        ), f"{role}: {control['rationale']}"

    # (2) Reproduces the hand-sized widths within the documented band.
    hand_sized = {
        "input_pair": devices["M1"]["w_um"],
        "mirror": devices["M3"]["w_um"],
        "tail": devices["M5"]["w_um"],
    }
    for role, reference_w_um in hand_sized.items():
        sized_w_um = report["roles"][role]["w_um"]
        assert reference_w_um / 2 <= sized_w_um <= reference_w_um * 2, (
            f"{role}: sized W={sized_w_um:.3g}um is not within a factor of "
            f"two of the canary's hand-sized W={reference_w_um}um"
        )

    # The coupled quantities the assembled circuit actually delivered.
    measured = report["budget"]["measured"]
    assert measured["tail_split"][0] == pytest.approx(0.5, abs=0.02)
    assert measured["mirror_ratio"] == pytest.approx(1.0, rel=0.05)

    # Every device -- all six instances, not just the three solved roles --
    # states its own gm/Id and inversion-level rationale.
    assert len(report["devices"]) == 6
    for entry in report["devices"].values():
        assert entry["operating_point"]["inversion_level"] in {
            "weak",
            "moderate",
            "strong",
        }
        assert "gm/Id" in entry["rationale"]
        assert "Inversion level" in entry["rationale"]
