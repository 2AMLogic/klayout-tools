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
            "options": {"sweep_points": 12},
        },
        name="canary.json",
    )

    report = size.run_size(str(request))

    # ngspice -- not the interpolator -- confirms the returned point hits
    # the canary-derived target.
    assert report["status"] == "pass"
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
