"""Closed-form validation and convergence-under-refinement for `klt mom`'s
PEEC inductance/resistance extraction (issue #797, Phase 1a of the
Method-of-Moments epic #701).

`tests/test_mom.py` covers the PEEC path's *contract* (the new response
fields, the bar-shape/conductivity error paths). This module covers its
*numerics*, mirroring `tests/test_mom_validation.py`'s precedent for the
capacitance solver: every assertion cites the oracle it is checking against,
along with the tolerance and the measured number behind it.

## The oracles

- **Straight-wire self-inductance** -- Rosa's classical DC (uniform current
  density) partial self-inductance of an isolated straight round wire,
  `L = (mu0*l / 2*pi) * (ln(2*l/a) - 3/4)`, `l` = length, `a` = radius. This
  is a *shape-substituted* oracle for `klt mom`'s rectangular bar (the same
  role `docs/design/mom-validation.md`'s Kirchhoff-disk oracle plays for the
  capacitance solver's square plates): the bar's radius is taken as the
  equal-area circle's, `a = sqrt(width*height / pi)`, and a square
  cross-section's *true* self geometric mean distance is measured (by direct
  Monte Carlo integration, in the PR description) to be ~1.7% larger than
  the equal-area circle's -- so the converged PEEC answer is expected to sit
  slightly *below* this oracle, not to converge onto it exactly.
- **Two-wire transmission-line loop inductance** -- for two identical
  parallel round wires of separation `D` (`a << D << l`),
  `L' = (mu0/pi) * (ln(D/a) + 1/4)` per unit length; `L_loop = 2*(L_self -
  M(l, D))` in the same limit. Independently re-derived and numerically
  cross-checked against `klt mom`'s own filament-bundle method in the PR
  description (not just cited).
- **DC resistance** -- `R = length / (conductivity * cross_sectional_area)`,
  Ohm's law for a uniform bar. Exact, not asymptotic; checked at `rel=1e-9`.

Requires the `klt_mom_native` Rust extension (see
`docs/cli/mom.md#building-the-native-extension`); like `tests/test_mom.py`,
the whole module skips with a clear reason when it is not built.
`docs/design/mom-validation.md`'s "Inductance/resistance" section records the
measured numbers this file prints.
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

#: Copper bulk conductivity, S/m -- a representative on-chip metal value used
#: throughout these fixtures (the exact value only affects the resistance
#: check, which is checked exactly regardless).
COPPER_S_PER_M = 5.96e7


def rosa_self_inductance_nh(
    length_um: float, width_um: float, height_um: float
) -> float:
    """Rosa's straight-wire self-inductance closed form, in nanohenries, for
    a bar reduced to the equal-area-circle radius (see module docs)."""
    length_m = length_um * 1e-6
    area_m2 = width_um * 1e-6 * height_um * 1e-6
    radius_m = math.sqrt(area_m2 / math.pi)
    henries = (
        MU0_H_PER_M
        / (2.0 * math.pi)
        * length_m
        * (math.log(2.0 * length_m / radius_m) - 0.75)
    )
    return henries * 1e9


def two_wire_line_loop_nh(
    length_um: float, width_um: float, height_um: float, separation_um: float
) -> float:
    """Classical two-wire transmission-line loop inductance, in nanohenries
    (see module docs)."""
    length_m = length_um * 1e-6
    area_m2 = width_um * 1e-6 * height_um * 1e-6
    radius_m = math.sqrt(area_m2 / math.pi)
    sep_m = separation_um * 1e-6
    henries = length_m * (MU0_H_PER_M / math.pi) * (math.log(sep_m / radius_m) + 0.25)
    return henries * 1e9


def _dbu(value_um: float) -> int:
    return int(round(value_um / 0.001))


def _write_bar(path, length_um: float, width_um: float) -> None:
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(0, 0, _dbu(length_um), _dbu(width_um))
    )
    layout.write(str(path))


def _write_bar_spec(
    path,
    *,
    height_um: float,
    filament_size_um: float,
    panel_size_um: float = 5.0,
) -> None:
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": panel_size_um,
                "compute_inductance": True,
                "filament_size_um": filament_size_um,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "wire",
                        "z0_um": 0.0,
                        "z1_um": height_um,
                        "conductivity_S_per_m": COPPER_S_PER_M,
                    }
                ],
            }
        )
    )


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
    filament_size_um: float,
    panel_size_um: float = 5.0,
) -> None:
    entry = {"z0_um": 0.0, "z1_um": height_um, "conductivity_S_per_m": COPPER_S_PER_M}
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": panel_size_um,
                "compute_inductance": True,
                "filament_size_um": filament_size_um,
                "stackup": [
                    {"layer": "1/0", "conductor": "go", **entry},
                    {"layer": "2/0", "conductor": "return", **entry},
                ],
            }
        )
    )


@pytest.fixture(scope="module")
def solver(tmp_path_factory):
    root = tmp_path_factory.mktemp("mom-peec-validation")
    cache: dict[tuple, dict] = {}

    def wire(length_um: float, width_um: float, height_um: float, filament_um: float):
        key = ("wire", length_um, width_um, height_um, filament_um)
        if key not in cache:
            gds = root / f"wire-{length_um}-{width_um}.gds"
            if not gds.exists():
                _write_bar(gds, length_um, width_um)
            spec = root / f"wire-{length_um}-{width_um}-{height_um}-{filament_um}.json"
            _write_bar_spec(spec, height_um=height_um, filament_size_um=filament_um)
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    def loop(
        length_um: float,
        width_um: float,
        height_um: float,
        separation_um: float,
        filament_um: float,
        panel_um: float = 5.0,
    ):
        key = (
            "loop",
            length_um,
            width_um,
            height_um,
            separation_um,
            filament_um,
            panel_um,
        )
        if key not in cache:
            gds = root / f"loop-{length_um}-{separation_um}.gds"
            if not gds.exists():
                _write_loop(gds, length_um, width_um, separation_um)
            spec = (
                root / f"loop-{length_um}-{width_um}-{height_um}-{separation_um}-"
                f"{filament_um}-{panel_um}.json"
            )
            _write_loop_spec(
                spec,
                height_um=height_um,
                filament_size_um=filament_um,
                panel_size_um=panel_um,
            )
            cache[key] = run_mom(str(gds), str(spec))
        return cache[key]

    from types import SimpleNamespace

    return SimpleNamespace(wire=wire, loop=loop)


# --- straight-wire self-inductance vs Rosa closed form -----------------------


def test_straight_wire_self_inductance_matches_rosa_closed_form(solver):
    # 2000um long, 4x4um square cross section (aspect ratio 500:1): deep in
    # the "thin wire" (l >> a) regime the closed form assumes.
    length_um, width_um, height_um = 2000.0, 4.0, 4.0
    report = solver.wire(length_um, width_um, height_um, 0.5)

    measured_nh = report["inductance_matrix_nh"][0][0]
    oracle_nh = rosa_self_inductance_nh(length_um, width_um, height_um)
    rel_err = abs(measured_nh - oracle_nh) / oracle_nh

    print(
        f"\nstraight wire: measured={measured_nh:.6f} nH  "
        f"Rosa oracle={oracle_nh:.6f} nH  rel.err={rel_err * 100:.4f}%  "
        f"filaments={report['filament_count']}"
    )
    # Stated tolerance: 2% (measured below; see the module doc's note on the
    # expected small negative shape-substitution offset for a square bar).
    assert rel_err < 0.02, f"rel.err {rel_err * 100:.4f}% exceeds the 2% tolerance"


def test_straight_wire_self_inductance_converges_under_filament_refinement(solver):
    length_um, width_um, height_um = 2000.0, 4.0, 4.0
    levels = [4.0, 2.0, 1.0, 0.5]  # 1x1, 2x2, 4x4, 8x8 filaments
    values = [
        solver.wire(length_um, width_um, height_um, f)["inductance_matrix_nh"][0][0]
        for f in levels
    ]
    diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]

    print("\nstraight wire convergence (filament refinement):")
    for f, v in zip(levels, values, strict=True):
        print(f"    filament_size_um={f:.2f}  L={v:.8f} nH")
    print(f"    successive |differences|: {[f'{d:.6f}' for d in diffs]}")

    assert all(
        later < earlier for earlier, later in zip(diffs, diffs[1:], strict=False)
    ), f"successive refinements should move the answer strictly less each time: {diffs}"


# --- loop inductance vs two-wire-line closed form -----------------------------


def test_loop_inductance_matches_two_wire_line_closed_form(solver):
    # 5000um long, 4x4um bars, 60um separation: l >> D >> a throughout.
    length_um, width_um, height_um, separation_um = 5000.0, 4.0, 4.0, 60.0
    report = solver.loop(
        length_um, width_um, height_um, separation_um, 0.5, panel_um=10.0
    )

    l_nh = report["inductance_matrix_nh"]
    assert l_nh[0][1] == pytest.approx(l_nh[1][0], rel=1e-9)
    loop_nh = l_nh[0][0] + l_nh[1][1] - 2.0 * l_nh[0][1]
    oracle_nh = two_wire_line_loop_nh(length_um, width_um, height_um, separation_um)
    rel_err = abs(loop_nh - oracle_nh) / oracle_nh

    print(
        f"\nloop: measured={loop_nh:.6f} nH  two-wire-line oracle={oracle_nh:.6f} nH  "
        f"rel.err={rel_err * 100:.4f}%"
    )
    # Stated tolerance: 5% (looser than the straight-wire check -- the loop
    # oracle compounds two asymptotic approximations, l>>a *and* l>>D).
    assert rel_err < 0.05, f"rel.err {rel_err * 100:.4f}% exceeds the 5% tolerance"


# --- DC resistance -------------------------------------------------------------


def test_dc_resistance_matches_ohms_law_exactly(solver):
    length_um, width_um, height_um = 200.0, 2.0, 2.0
    report = solver.wire(length_um, width_um, height_um, 0.5)

    length_m = length_um * 1e-6
    area_m2 = (width_um * 1e-6) * (height_um * 1e-6)
    expected_ohm = length_m / (COPPER_S_PER_M * area_m2)

    assert report["resistance_ohm"][0] == pytest.approx(expected_ohm, rel=1e-9)
