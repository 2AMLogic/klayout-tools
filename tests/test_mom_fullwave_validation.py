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
