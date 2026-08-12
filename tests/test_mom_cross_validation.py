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
