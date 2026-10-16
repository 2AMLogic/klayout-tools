"""Closed-form validation and convergence-under-refinement for `klt mom`'s
full-wave (frequency-domain, retarded Green's function) partial-impedance
sweep (issue #893, Phase 2a of the Method-of-Moments epic #701).

`tests/test_mom.py` covers the full-wave path's *contract* (the new response
fields, the bar-shape/frequency-validation error paths). This module covers
its *numerics*, mirroring `tests/test_mom_peec_validation.py`'s precedent for
the PEEC solve: every assertion cites the oracle it is checking against,
along with the tolerance and the measured number behind it.

## The oracles

- **Two-wire transmission-line characteristic impedance** -- for two
  identical parallel round wires of radius `a` separated by `D` (`a << D`),
  `Z0 = (eta0 / pi) * acosh(D / (2*a))`, `eta0 = sqrt(mu0/eps0)` the
  free-space wave impedance. `a` is taken as the equal-area circle radius of
  the (square) bar cross section -- the same shape-substitution convention
  `tests/test_mom_peec_validation.py`'s own oracles use.
- **TEM propagation** -- in a homogeneous, lossless background medium, any
  uniform two-conductor transmission line's propagation constant is exactly
  `gamma = j*omega*sqrt(er)/c0` (`beta = omega*sqrt(er)/c0`, `alpha = 0`),
  independent of the line's cross-sectional geometry -- the identity
  `L' * C' = er / c0^2` any TEM line satisfies.

Requires the `klt_mom_native` Rust extension (see
`docs/cli/mom.md#building-the-native-extension`); like `tests/test_mom.py`,
the whole module skips with a clear reason when it is not built.
"""

from __future__ import annotations

import json
import math

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

MU0_H_PER_M = 4.0 * math.pi * 1e-7
EPS0_F_PER_M = 8.854_187_812_8e-12
ETA0_OHM = math.sqrt(MU0_H_PER_M / EPS0_F_PER_M)
C0_M_PER_S = 299_792_458.0


def two_wire_line_z0_ohm(
    width_um: float, height_um: float, separation_um: float
) -> float:
    """Classical two-wire transmission-line characteristic impedance, ohms
    (see module docs)."""
    area_m2 = width_um * 1e-6 * height_um * 1e-6
    a_m = math.sqrt(area_m2 / math.pi)
    sep_m = separation_um * 1e-6
    return ETA0_OHM / math.pi * math.acosh(sep_m / (2.0 * a_m))


def _dbu(value_um: float) -> int:
    return int(round(value_um / 0.001))


def _write_loop(path, length_um: float, width_um: float, separation_um: float) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(0, 0, _dbu(length_um), _dbu(width_um))
    )
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(
            0,
            _dbu(separation_um),
            _dbu(length_um),
            _dbu(separation_um + width_um),
        )
    )
    layout.write(str(path))


def _write_loop_spec(
    path,
    *,
    height_um: float,
    frequencies_hz: list[float],
    segment_size_um: float,
    panel_size_um: float,
) -> None:
    entry = {"z0_um": 0.0, "z1_um": height_um}
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": panel_size_um,
                "frequencies_hz": frequencies_hz,
                "segment_size_um": segment_size_um,
                "stackup": [
                    {"layer": "1/0", "conductor": "go", **entry},
                    {"layer": "2/0", "conductor": "return", **entry},
                ],
            }
        )
    )


@pytest.fixture(scope="module")
def solver(tmp_path_factory):
    root = tmp_path_factory.mktemp("mom-fullwave-validation")
    cache: dict[tuple, dict] = {}

    def loop(
        length_um: float,
        width_um: float,
        height_um: float,
        separation_um: float,
        frequencies_hz: tuple[float, ...],
        segment_size_um: float = 5.0,
        panel_size_um: float = 2.0,
    ):
        key = (
            length_um,
            width_um,
            height_um,
            separation_um,
            frequencies_hz,
            segment_size_um,
            panel_size_um,
        )
        if key not in cache:
            gds = root / f"loop-{length_um}-{separation_um}.gds"
            if not gds.exists():
                _write_loop(gds, length_um, width_um, separation_um)
            spec = root / f"loop-{key}.json".replace(" ", "")
            _write_loop_spec(
                spec,
                height_um=height_um,
                frequencies_hz=list(frequencies_hz),
                segment_size_um=segment_size_um,
                panel_size_um=panel_size_um,
            )
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    from types import SimpleNamespace

    return SimpleNamespace(loop=loop)


# --- characteristic impedance vs two-wire-line closed form -------------------


def test_characteristic_impedance_matches_two_wire_line_closed_form(solver):
    # 500um long, 2x2um bars, 40um separation, 1MHz (k*length ~= 1e-5,
    # deeply quasi-TEM -- see fullwave.rs's own low-frequency-limit test).
    length_um, width_um, height_um, separation_um = 500.0, 2.0, 2.0, 40.0
    report = solver.loop(length_um, width_um, height_um, separation_um, (1.0e6,))

    point = report["full_wave_sweep"][0]
    z0_re = point["characteristic_impedance_real_ohm"]
    z0_im = point["characteristic_impedance_imag_ohm"]
    oracle_ohm = two_wire_line_z0_ohm(width_um, height_um, separation_um)
    rel_err = abs(z0_re - oracle_ohm) / oracle_ohm

    print(
        f"\ntwo-wire line: measured Z0={z0_re:.4f}{z0_im:+.4f}j ohm  "
        f"closed-form oracle={oracle_ohm:.4f} ohm  rel.err={rel_err * 100:.4f}%"
    )
    # Stated tolerance: 10% -- compounds the capacitance solve's own
    # point-collocation discretisation error with the full-wave solve's
    # thin-equivalent-wire shape substitution (see native/mom/src/fullwave.rs
    # module docs) and the finite-length/end-effect correction to the
    # infinite-line closed form, the same three-way budget
    # native/mom/src/fullwave.rs's own Rust-level unit test uses.
    assert rel_err < 0.10, f"rel.err {rel_err * 100:.4f}% exceeds the 10% tolerance"
    # Effectively lossless (vacuum, subwavelength cross-section).
    assert abs(z0_im) < 0.1 * abs(z0_re)


def test_characteristic_impedance_converges_under_segment_refinement(solver):
    length_um, width_um, height_um, separation_um = 500.0, 2.0, 2.0, 40.0
    levels = [40.0, 20.0, 10.0, 5.0]
    values = [
        solver.loop(
            length_um, width_um, height_um, separation_um, (1.0e6,), segment_size_um=s
        )["full_wave_sweep"][0]["characteristic_impedance_real_ohm"]
        for s in levels
    ]
    diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]

    print("\ntwo-wire line Z0 convergence (axial segment refinement):")
    for s, v in zip(levels, values, strict=True):
        print(f"    segment_size_um={s:.2f}  Z0={v:.6f} ohm")
    print(f"    successive |differences|: {[f'{d:.6f}' for d in diffs]}")

    assert all(
        later < earlier for earlier, later in zip(diffs, diffs[1:], strict=False)
    ), f"successive refinements should move the answer strictly less each time: {diffs}"


# --- propagation constant vs the TEM identity ---------------------------------


def test_propagation_constant_matches_tem_identity(solver):
    # Same geometry, at a higher frequency (1 GHz) where beta is large
    # enough to measure precisely; beta = omega/c0 and alpha ~= 0 hold for
    # ANY lossless homogeneous-medium TEM line, independent of the specific
    # cross-section/separation -- a stronger, geometry-independent check
    # than the acosh formula above.
    length_um, width_um, height_um, separation_um = 500.0, 2.0, 2.0, 40.0
    frequency_hz = 1.0e9
    report = solver.loop(length_um, width_um, height_um, separation_um, (frequency_hz,))

    point = report["full_wave_sweep"][0]
    omega = 2.0 * math.pi * frequency_hz
    beta_oracle = omega / C0_M_PER_S
    beta_rel_err = abs(point["phase_rad_per_m"] - beta_oracle) / beta_oracle

    print(
        f"\nbeta={point['phase_rad_per_m']:.6e} rad/m  TEM oracle omega/c0="
        f"{beta_oracle:.6e} rad/m  rel.err={beta_rel_err * 100:.4f}%"
    )
    assert beta_rel_err < 0.10, f"beta rel.err {beta_rel_err * 100:.4f}% exceeds 10%"
    assert abs(point["attenuation_np_per_m"]) < 1e-3 * beta_oracle


# --- series-path winding (issue #2730) ----------------------------------------
#
# A single open U winding (centreline (0,100) -> (0,0) -> (120,0) -> (120,100),
# 2x2 um section) drawn as three tiled legs, each corner square owned by the
# leg arriving at it. The retarded sweep meshes it leg by leg (segment-local
# direction/length/radius) instead of collapsing the turns to one straight wire.

WINDING_LEGS_UM = (
    # (x0, y0, x1, y1) in um
    (-1.0, 1.0, 1.0, 100.0),
    (-1.0, -1.0, 121.0, 1.0),
    (119.0, 1.0, 121.0, 100.0),
)
WINDING_TOTAL_LENGTH_UM = 100.0 + 120.0 + 100.0
WINDING_WIDTH_UM = 2.0
# Square cross-section self-GMD / width -- the published 0.44705 value that
# native/mom/src/peec.rs's `self_gmd_of_a_square_matches_its_published_value`
# pins.
SQUARE_GMD_OVER_WIDTH = 0.447_049_16
MU0_OVER_2PI_H_PER_UM = 2.0e-13


def _write_winding(gds_path, spec_path, **extra) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    shapes = top.shapes(layout.layer(1, 0))
    for x0, y0, x1, y1 in WINDING_LEGS_UM:
        shapes.insert(kdb.Box.new(_dbu(x0), _dbu(y0), _dbu(x1), _dbu(y1)))
    layout.write(str(gds_path))
    spec_path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 2.0,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "winding",
                        "z0_um": 0.0,
                        "z1_um": 2.0,
                        "conductivity_S_per_m": 5.96e7,
                    }
                ],
                **extra,
            }
        )
    )


@pytest.fixture(scope="module")
def winding(tmp_path_factory):
    root = tmp_path_factory.mktemp("mom-winding-fullwave")
    cache: dict[tuple, dict] = {}

    def solve(frequencies_hz, segment_size_um, compute_inductance=False):
        key = (tuple(frequencies_hz), segment_size_um, compute_inductance)
        if key not in cache:
            gds, spec = root / "u.gds", root / f"u-{len(cache)}.json"
            extra = {"segment_size_um": segment_size_um}
            if frequencies_hz:
                extra["frequencies_hz"] = list(frequencies_hz)
            if compute_inductance:
                extra["compute_inductance"] = True
                extra["filament_size_um"] = 0.5
            _write_winding(gds, spec, **extra)
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    return solve


def test_winding_low_frequency_limit_matches_static_series_inductance(winding):
    """Im(Z)/omega at 1 kHz (k*length ~ 1e-8) reproduces the static series-path
    inductance of #2729 for the same request.

    Measured 5.48% raw difference. It is not discretisation error: the retarded
    fill's reduced thin-wire kernel is a surface-current wire of the equal-area
    radius `a` (self term ln(2l/a) - 1) while PEEC's filament bundle is the
    uniform-volume-current square bar (ln(2l/GMD) - 1), so per leg the static
    value is larger by mu0/(2*pi) * l * ln(a/GMD). Adding that analytic
    offset closes the gap to 5.8e-5. Bounds: 7% raw (the convention), 1%
    once the convention is accounted for."""
    static = winding((), 5.0, compute_inductance=True)
    l_static_h = static["inductance_matrix_nh"][0][0] * 1e-9

    point = winding((1.0e3,), 0.5)["full_wave_sweep"][0]
    omega = 2.0 * math.pi * 1.0e3
    l_dyn_h = point["impedance_matrix_imag_ohm"][0][0] / omega
    rel = abs(l_dyn_h - l_static_h) / l_static_h

    a_um = WINDING_WIDTH_UM / math.sqrt(math.pi)
    gmd_um = SQUARE_GMD_OVER_WIDTH * WINDING_WIDTH_UM
    offset_h = MU0_OVER_2PI_H_PER_UM * WINDING_TOTAL_LENGTH_UM * math.log(a_um / gmd_um)
    rel_corrected = abs(l_dyn_h + offset_h - l_static_h) / l_static_h

    print(
        f"\nU winding low-frequency limit: static PEEC L={l_static_h * 1e9:.6f} nH  "
        f"full-wave Im(Z)/omega={l_dyn_h * 1e9:.6f} nH  rel={rel * 100:.4f}%  "
        f"convention offset {offset_h * 1e9:.6f} nH -> corrected rel="
        f"{rel_corrected * 100:.5f}%"
    )
    assert rel < 0.07, f"raw rel.diff {rel * 100:.4f}% exceeds 7%"
    assert rel_corrected < 0.01, (
        f"convention-corrected rel.diff {rel_corrected * 100:.5f}% exceeds 1%"
    )


def test_winding_impedance_converges_under_segment_refinement(winding):
    """Cells straddling the 1.13 um equivalent radius: the point-collocation
    self term only converges once cells are a few radii or finer."""
    levels = [4.0, 2.0, 1.0, 0.5]
    values = [
        complex(
            p["impedance_matrix_real_ohm"][0][0],
            p["impedance_matrix_imag_ohm"][0][0],
        )
        for p in (winding((5.0e9,), s)["full_wave_sweep"][0] for s in levels)
    ]
    steps = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]
    print("\nU winding Z(5 GHz) convergence (axial segment refinement):")
    for s, v in zip(levels, values, strict=True):
        print(f"    segment_size_um={s:.2f}  Z={v.real:.6f}{v.imag:+.6f}j ohm")
    print(f"    successive |differences|: {[f'{d:.6f}' for d in steps]}")
    assert all(
        later < earlier for earlier, later in zip(steps, steps[1:], strict=False)
    )
    assert steps[-1] / abs(values[-1]) < 0.005


def test_winding_sweep_reports_series_impedance_without_line_quantities(winding):
    report = winding((1.0e9, 2.0e9), 2.0)
    assert report["conductors"] == ["winding"]
    first, second = report["full_wave_sweep"]
    # Inductive, ~linear in frequency below self-resonance.
    x1 = first["impedance_matrix_imag_ohm"][0][0]
    x2 = second["impedance_matrix_imag_ohm"][0][0]
    assert x1 > 0 and x2 / x1 == pytest.approx(2.0, rel=1e-3)
    # No uniform line, no ports.
    assert first.get("characteristic_impedance_real_ohm") is None
    assert first.get("s_parameters") is None
