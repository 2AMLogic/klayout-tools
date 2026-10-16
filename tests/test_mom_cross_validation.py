"""Cross-validation of `klt mom`'s full-wave S-parameters (issues #893/#894,
Phases 2a/2b of the Method-of-Moments epic #701) against an **external**
Method-of-Moments solver, on a shared benchmark geometry -- issue #895,
Phase 2c of the same epic.

`tests/test_mom_fullwave_validation.py` and `tests/test_mom_ports_validation.py`
already check `klt mom` against **analytic closed forms** (the two-wire-line
`Z0` formula, the TEM propagation identity). This module is a different,
complementary check: the epic's own Phase 0 reality-grounding section asks
for a cross-check against "an external MoM/FEM ... on a shared benchmark" --
i.e. a second, *independently implemented* numerical solver, not just a
second evaluation of the same textbook formula. See
`docs/design/mom-cross-validation.md` for the full methodology (why NEC2++,
the benchmark, the tolerance/metric definitions, and the measured results).

## The external oracle: NEC2++ (`PyNEC`)

[NEC2++](https://github.com/tmolteno/nec2pp) is a C++ port of the classical
Numerical Electromagnetics Code (NEC2, originally Lawrence Livermore National
Laboratory) -- a mature, independently-developed thin-wire Method-of-Moments
antenna/transmission-line solver with a decades-long track record, unrelated
to `klt_mom_native`'s own Rust implementation (different language, different
codebase, different discretisation -- NEC2's full surface-current MoM vs.
this repo's point-collocation Riemann-sum retarded-kernel integral). Its
Python bindings (`PyNEC`, GPL-3.0-only) are never imported by this test
module directly -- only by `scripts/mom_nec_reference.py`, always invoked as
its own subprocess (see that script's module docs for why, and
`docs/design/mom-cross-validation.md` for the license discussion this
mirrors from `docs/design/em-field-sim-spike.md`'s openEMS survey).

Requires **both** `klt_mom_native` (this repo's own solver -- see
`docs/cli/mom.md#building-the-native-extension`) and `PyNEC` (the external
oracle -- `uv sync --extra mom-cross-validation`, Python >=3.11 only) to be
installed; the whole module skips with a clear reason when either is
missing, exactly like its closed-form-validation siblings.
"""

from __future__ import annotations

import cmath
import json
import math
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.mom import run_mom

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)

_NEC_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "mom_nec_reference.py"
)

_PYNEC_PROBE = subprocess.run(
    [sys.executable, "-c", "import PyNEC"],
    capture_output=True,
    check=False,
)
if _PYNEC_PROBE.returncode != 0:
    pytest.skip(
        "PyNEC (the external NEC2++ MoM oracle) is not installed -- run "
        "`uv sync --extra mom-cross-validation` (Python >=3.11 only; see "
        "docs/design/mom-cross-validation.md)",
        allow_module_level=True,
    )

MU0_H_PER_M = 4.0 * math.pi * 1e-7
EPS0_F_PER_M = 8.854_187_812_8e-12
C0_M_PER_S = 299_792_458.0

# The shared benchmark: the same 500um-long, 2x2um-bar, 40um-separation
# two-wire loop `tests/test_mom_fullwave_validation.py`/
# `tests/test_mom_ports_validation.py` already use for their own closed-form
# checks -- reusing it here (rather than a new fixture) is deliberate: it is
# the geometry both this repo's own analytic validation *and* this external
# cross-check agree to treat as canonical, per
# `docs/design/mom-cross-validation.md`'s "The shared benchmark" section.
LENGTH_UM = 500.0
WIDTH_UM = 2.0
HEIGHT_UM = 2.0
SEPARATION_UM = 40.0
FREQUENCY_HZ = 1.0e9
SEGMENT_SIZE_UM = 5.0  # -> 100 klt-mom segments per conductor
NEC_SEGMENTS_PER_WIRE = 101  # matches SEGMENT_SIZE_UM's ~100-segment mesh
REFERENCE_IMPEDANCE_OHM = 50.0


def _dbu(value_um: float) -> int:
    return int(round(value_um / 0.001))


def _write_loop(path: Path) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(0, 0, _dbu(LENGTH_UM), _dbu(WIDTH_UM))
    )
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(
            0,
            _dbu(SEPARATION_UM),
            _dbu(LENGTH_UM),
            _dbu(SEPARATION_UM + WIDTH_UM),
        )
    )
    layout.write(str(path))


def _write_spec(path: Path) -> None:
    entry = {"z0_um": 0.0, "z1_um": HEIGHT_UM}
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 2.0,
                "frequencies_hz": [FREQUENCY_HZ],
                "segment_size_um": SEGMENT_SIZE_UM,
                "ports": [
                    {
                        "position_um": 0.0,
                        "reference_impedance_ohm": REFERENCE_IMPEDANCE_OHM,
                    },
                    {
                        "position_um": LENGTH_UM,
                        "reference_impedance_ohm": REFERENCE_IMPEDANCE_OHM,
                    },
                ],
                "stackup": [
                    {"layer": "1/0", "conductor": "go", **entry},
                    {"layer": "2/0", "conductor": "return", **entry},
                ],
            }
        )
    )


@pytest.fixture(scope="module")
def klt_mom_point(tmp_path_factory) -> dict:
    """`klt mom`'s own full-wave + S-parameter solve of the shared benchmark."""
    root = tmp_path_factory.mktemp("mom-cross-validation")
    gds = root / "loop.gds"
    spec = root / "loop.mom.json"
    _write_loop(gds)
    _write_spec(spec)
    report = run_mom(str(gds), str(spec))
    return report["full_wave_sweep"][0]


@pytest.fixture(scope="module")
def nec_point() -> dict:
    """The external NEC2++ oracle's solve of the same benchmark -- see
    `scripts/mom_nec_reference.py` for the method (open/short-circuit
    two-measurement technique)."""
    area_m2 = (WIDTH_UM * 1e-6) * (HEIGHT_UM * 1e-6)
    radius_um = math.sqrt(area_m2 / math.pi) * 1e6
    request = {
        "length_um": LENGTH_UM,
        "separation_um": SEPARATION_UM,
        "radius_um": radius_um,
        "frequency_hz": FREQUENCY_HZ,
        "segments_per_wire": NEC_SEGMENTS_PER_WIRE,
        "reference_impedance_ohm": REFERENCE_IMPEDANCE_OHM,
    }
    result = subprocess.run(
        [sys.executable, str(_NEC_SCRIPT)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# --- characteristic impedance: klt mom vs the external NEC2++ oracle --------


def test_characteristic_impedance_matches_nec_reference(klt_mom_point, nec_point):
    z0_klt = complex(
        klt_mom_point["characteristic_impedance_real_ohm"],
        klt_mom_point["characteristic_impedance_imag_ohm"],
    )
    z0_nec = complex(nec_point["z0_real_ohm"], nec_point["z0_imag_ohm"])
    rel_err = abs(z0_klt - z0_nec) / abs(z0_nec)

    print(
        f"\ncharacteristic impedance: klt mom Z0={z0_klt.real:.4f}"
        f"{z0_klt.imag:+.4f}j ohm  NEC2++ (external oracle) Z0="
        f"{z0_nec.real:.4f}{z0_nec.imag:+.4f}j ohm  rel.err={rel_err * 100:.4f}%"
    )
    # See docs/design/mom-cross-validation.md's "Methodology" section for
    # this tolerance's derivation: each solver independently carries up to
    # ~10% discretisation error against the analytic closed form (this
    # module's own siblings' own stated tolerance), so a 20% budget on their
    # pairwise difference has ample margin over the ~1.4% measured here while
    # still being tight enough to catch a genuinely broken solve.
    assert rel_err < 0.20, (
        f"klt mom's Z0 {z0_klt} should agree with the external NEC2++ "
        f"oracle's Z0 {z0_nec} (rel.err {rel_err * 100:.4f}% exceeds 20%)"
    )


# --- S-parameters: klt mom vs the external NEC2++ oracle ---------------------


def test_s_parameters_match_nec_reference(klt_mom_point, nec_point):
    s = klt_mom_point["s_parameters"]
    s11_klt = complex(s["s11_real"], s["s11_imag"])
    s21_klt = complex(s["s21_real"], s["s21_imag"])

    s11_nec = complex(nec_point["s11_real"], nec_point["s11_imag"])
    s21_nec = complex(nec_point["s21_real"], nec_point["s21_imag"])

    mag_rel_err = abs(abs(s21_klt) - abs(s21_nec)) / abs(s21_nec)
    phase_abs_err = abs(cmath.phase(s21_klt) - cmath.phase(s21_nec))

    print(
        f"\nS-parameters (50 ohm reference): klt mom S11={s11_klt:.4e} "
        f"S21={s21_klt:.6f} (|S21|={abs(s21_klt):.6f})  NEC2++ (external "
        f"oracle) S11={s11_nec:.4e} S21={s21_nec:.6f} (|S21|="
        f"{abs(s21_nec):.6f})  |S21| rel.err={mag_rel_err * 100:.4f}%  "
        f"phase(S21) abs.err={phase_abs_err:.6f} rad"
    )
    # |S21|: a near-lossless matched two-wire line has |S21| close to 1 for
    # both solvers -- a tight relative-error budget is appropriate (measured
    # ~0.03%; see docs/design/mom-cross-validation.md).
    assert mag_rel_err < 0.05, (
        f"|S21| rel.err {mag_rel_err * 100:.4f}% exceeds the 5% tolerance "
        f"(klt mom |S21|={abs(s21_klt):.6f} vs NEC2++ |S21|={abs(s21_nec):.6f})"
    )
    # phase(S21): compared in absolute radians, not relative error -- both
    # solvers' phase is small at this frequency/length (near-zero relative
    # error is ill-defined/misleading for a near-zero quantity), and an
    # absolute-radian budget is the metric docs/design/mom-cross-validation.md
    # documents (measured ~0.006 rad).
    assert phase_abs_err < 0.05, (
        f"phase(S21) abs.err {phase_abs_err:.6f} rad exceeds the 0.05 rad tolerance"
    )
    # S11: both solvers should report a near-matched line (S11 close to 0) --
    # compared as an absolute magnitude bound, the same convention
    # tests/test_mom_ports_validation.py's own matched-line test uses.
    assert abs(s11_klt) < 0.10, f"expected klt mom S11 near 0, got {s11_klt}"
    assert abs(s11_nec) < 0.10, f"expected NEC2++ S11 near 0, got {s11_nec}"


# --- bent-path driving-point mode (issue #3037) ------------------------------


def _run_nec_request(request: dict) -> dict:
    result = subprocess.run(
        [sys.executable, str(_NEC_SCRIPT)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _bent_request(signal: list, ret: list) -> dict:
    area_m2 = (WIDTH_UM * 1e-6) * (HEIGHT_UM * 1e-6)
    return {
        "mode": "bent_path_driving_point",
        "signal_path_um": signal,
        "return_path_um": ret,
        "radius_um": math.sqrt(area_m2 / math.pi) * 1e6,
        "frequency_hz": FREQUENCY_HZ,
        "segment_size_um": SEGMENT_SIZE_UM,
    }


# Measured orientation sensitivity of NEC2 on this geometry is 1.4-1.8%, and
# changing the legacy stub's own segment count (1 -> 3) moves Z_sc by ~2.9%,
# so 3% bounds NEC's discretisation noise while a wrong stub/connector
# (missing wire, wrong endpoint) lands far outside it.
STRAIGHT_CONTROL_TOLERANCE = 0.03


def test_straight_control_matches_legacy_short_circuit(nec_point):
    """The straightened control is the legacy short-circuit geometry, so the
    driving-point mode must reproduce the legacy `Z_sc` to within NEC2's own
    discretisation noise (`STRAIGHT_CONTROL_TOLERANCE`).

    The two are not bit-identical because the bent contract traverses the
    loop (return wire runs far -> near, antiparallel to the signal wire),
    while the legacy script directs both long wires 0 -> L. Both
    single-segment 40 um connectors sit next to 5 um segments, and NEC2's
    junction treatment then makes `Im(Z_sc)` orientation-dependent: giving
    the legacy geometry the same return direction moves it by ~1.8% and
    reproduces this mode's value; giving it the same direction as legacy
    matches to ~1e-9. See docs/design/mom-cross-validation.md."""
    result = _run_nec_request(
        _bent_request(
            [[0, 0, 0], [LENGTH_UM, 0, 0]],
            [[LENGTH_UM, SEPARATION_UM, 0], [0, SEPARATION_UM, 0]],
        )
    )
    z_in = complex(result["z_in_real_ohm"], result["z_in_imag_ohm"])
    z_sc = complex(nec_point["z_sc_real_ohm"], nec_point["z_sc_imag_ohm"])

    # the oracle actually executed: tool identity, frequency and geometry
    assert result["tool"]["name"] == "nec2++" and result["tool"]["binding"] == "PyNEC"
    assert result["frequency_hz"] == FREQUENCY_HZ
    assert [w["segments"] for w in result["wires"]] == [
        NEC_SEGMENTS_PER_WIRE,
        1,
        NEC_SEGMENTS_PER_WIRE,
        1,
    ]
    assert math.isfinite(z_in.real) and math.isfinite(z_in.imag)
    assert abs(z_in - z_sc) / abs(z_sc) < STRAIGHT_CONTROL_TOLERANCE, (
        f"control {z_in} vs legacy Z_sc {z_sc}"
    )
    print(f"\nstraight control Z_in={z_in:.4f} ohm  legacy Z_sc={z_sc:.4f} ohm")


def test_bent_fixture_driving_point_is_finite_and_repeatable():
    request = _bent_request(
        [[0, 0, 0], [200, 0, 0], [200, 100, 0]],
        [[200, 140, 0], [160, 140, 0], [160, 40, 0], [0, 40, 0]],
    )
    first = _run_nec_request(request)
    second = _run_nec_request(request)

    z_in = complex(first["z_in_real_ohm"], first["z_in_imag_ohm"])
    assert math.isfinite(z_in.real) and math.isfinite(z_in.imag)
    assert abs(z_in) > 0.0
    assert z_in == complex(second["z_in_real_ohm"], second["z_in_imag_ohm"])
    assert first["tool"]["name"] == "nec2++"
    assert first["frequency_hz"] == FREQUENCY_HZ
    assert len(first["wires"]) == 7 and first["source"]["tag"] == 7
    for key in ("z0_real_ohm", "s11_real"):
        assert key not in first
    print(
        f"\nbent fixture Z_in={z_in:.4f} ohm  tool={first['tool']}  "
        f"f={first['frequency_hz']:.3e} Hz  wires={len(first['wires'])}"
    )


# --- bent-path winding: klt mom vs NEC2++ driving-point impedance (#2730) -----
#
# The signal winding is an L ((0,0) -> (200,0) -> (200,100)), the return a U
# ((200,120) -> (180,120) -> (180,20) -> (0,20)); 2x2 um sections on z in
# [0, 2] um. NEC2++'s bent-path driving-point mode (#3037) closes the loop with
# a far connector ((200,100) -> (200,120)) and a near feed stub ((0,20) ->
# (0,0)); klt mom models the same four bars as four conductors, with the
# connector/stub shortened by 1.5 um at each end so no two conductors touch
# (the electrical lengths are 17 um instead of 20 um). The loop impedance is
# the signed sum Z_in = s^T Z s over the full impedance matrix, `s` giving each
# conductor's current direction against its canonical traversal (S +1, R -1:
# the return runs from the lexicographically larger terminal; connector +1;
# stub -1, i.e. the polarity the NEC source stub carries).

BENT_FREQUENCY_HZ = 1.0e9
BENT_KLT_SEGMENT_UM = 1.0
# NEC2's thin-wire kernel needs segment length >~ 8 wire radii (radius 1.128
# um -> ~9 um); finer segmentation makes the single-segment source stub's
# neighbours ill-conditioned (Im(Z_in) swings through zero below 3 um on this
# fixture). The oracle is therefore run at its own valid segmentations, and its
# spread between them (2.5% .. 9.9% against klt mom) sets the tolerance.
BENT_NEC_SEGMENTS_UM = (10.0, 20.0)
BENT_TOLERANCE = 0.15

_BENT_SIGNAL = [[0, 0, 0], [200, 0, 0], [200, 100, 0]]
_BENT_RETURN = [[200, 120, 0], [180, 120, 0], [180, 20, 0], [0, 20, 0]]
_BENT_BOXES_UM = (
    # signal L (corner square owned by the horizontal leg)
    [(0, -1, 201, 1), (199, 1, 201, 100)],
    # return U (corner squares owned by the leg arriving at each)
    [(179, 119, 200, 121), (179, 21, 181, 119), (0, 19, 181, 21)],
    # far connector, near feed stub
    [(199, 101.5, 201, 118.5)],
    [(-1, 1.5, 1, 18.5)],
)
_BENT_SIGNS = (1.0, -1.0, 1.0, -1.0)


@pytest.fixture(scope="module")
def bent_klt_matrix(tmp_path_factory) -> list[list[complex]]:
    root = tmp_path_factory.mktemp("mom-bent-winding")
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    for i, boxes in enumerate(_BENT_BOXES_UM, start=1):
        for x0, y0, x1, y1 in boxes:
            top.shapes(layout.layer(i, 0)).insert(
                kdb.Box.new(_dbu(x0), _dbu(y0), _dbu(x1), _dbu(y1))
            )
    layout.write(str(root / "bent.gds"))
    (root / "bent.mom.json").write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 2.0,
                "frequencies_hz": [BENT_FREQUENCY_HZ],
                "segment_size_um": BENT_KLT_SEGMENT_UM,
                "stackup": [
                    {"layer": f"{i}/0", "conductor": name, "z0_um": 0.0, "z1_um": 2.0}
                    for i, name in enumerate(("signal", "return", "far", "near"), 1)
                ],
            }
        )
    )
    report = run_mom(str(root / "bent.gds"), str(root / "bent.mom.json"))
    point = report["full_wave_sweep"][0]
    z = [
        [complex(re, im) for re, im in zip(row_re, row_im, strict=True)]
        for row_re, row_im in zip(
            point["impedance_matrix_real_ohm"],
            point["impedance_matrix_imag_ohm"],
            strict=True,
        )
    ]
    return z


def _loop_impedance(z, signs) -> complex:
    return sum(signs[i] * z[i][j] * signs[j] for i in range(4) for j in range(4))


@pytest.mark.parametrize("nec_segment_um", BENT_NEC_SEGMENTS_UM)
def test_bent_winding_driving_point_matches_nec_reference(
    bent_klt_matrix, nec_segment_um
):
    request = _bent_request(_BENT_SIGNAL, _BENT_RETURN)
    request["segment_size_um"] = nec_segment_um
    result = _run_nec_request(request)
    z_nec = complex(result["z_in_real_ohm"], result["z_in_imag_ohm"])
    z_klt = _loop_impedance(bent_klt_matrix, _BENT_SIGNS)
    rel = abs(z_klt - z_nec) / abs(z_nec)

    # the oracle actually executed, on the stated geometry and frequency
    assert result["tool"]["name"] == "nec2++" and result["tool"]["binding"] == "PyNEC"
    assert result["tool"]["version"]
    assert result["frequency_hz"] == BENT_FREQUENCY_HZ
    assert len(result["wires"]) == 7 and result["source"]["tag"] == 7
    assert math.isfinite(z_nec.real) and math.isfinite(z_nec.imag)
    # lossless conductors: the real part is radiation only (<< the reactance)
    assert abs(z_klt.real) < 1e-6 * abs(z_klt.imag)
    assert abs(z_nec.real) < 1e-6 * abs(z_nec.imag)

    print(
        f"\nbent winding driving point @ {BENT_FREQUENCY_HZ:.3e} Hz "
        f"(NEC2++ {result['tool']['version']}, NEC segment {nec_segment_um} um, "
        f"klt segment {BENT_KLT_SEGMENT_UM} um): klt mom Z_in="
        f"{z_klt.real:.4e}{z_klt.imag:+.4f}j ohm  NEC2++ Z_in="
        f"{z_nec.real:.4e}{z_nec.imag:+.4f}j ohm  rel.err={rel * 100:.3f}%"
    )
    assert rel < BENT_TOLERANCE, (
        f"klt mom Z_in {z_klt} vs NEC2++ {z_nec}: rel.err {rel * 100:.3f}% "
        f"exceeds {BENT_TOLERANCE * 100:.0f}%"
    )


def test_bent_winding_control_wrong_return_polarity_is_far_outside_tolerance(
    bent_klt_matrix,
):
    """Falsifiability control: treating the return path as co-directed with the
    signal (the sign a solver that ignored the traversal direction would use)
    lands far outside the tolerance, so agreement above is not vacuous."""
    request = _bent_request(_BENT_SIGNAL, _BENT_RETURN)
    request["segment_size_um"] = BENT_NEC_SEGMENTS_UM[0]
    result = _run_nec_request(request)
    z_nec = complex(result["z_in_real_ohm"], result["z_in_imag_ohm"])
    z_wrong = _loop_impedance(bent_klt_matrix, (1.0, 1.0, 1.0, 1.0))
    rel = abs(z_wrong - z_nec) / abs(z_nec)
    print(
        f"\ncontrol (wrong polarity): Z={z_wrong:.4f} vs NEC2++ {z_nec:.4f}, "
        f"rel={rel:.2f}"
    )
    assert rel > 3 * BENT_TOLERANCE
