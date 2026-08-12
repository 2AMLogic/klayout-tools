"""Closed-form validation for `klt mom`'s PEEC inductance/resistance path
(issue #797, Phase 1 of the Method-of-Moments epic #701).

This is the inductance-side sibling of `tests/test_mom_validation.py`, and it
follows the same convention: every number asserted here cites the oracle it
is checked against, and the tolerance is the *measured* agreement plus stated
headroom, never a round number picked for comfort.

The oracles:

- **DC resistance** -- `R = l / (sigma * A)` for a straight bar. No
  approximation is involved, so this is gated at round-off (`1e-12`), not at
  a percent.
- **Partial self inductance** -- the classical mean-distance asymptote for a
  straight bar of length `l`:

      Lp = (mu0 l / 2 pi) [ ln(2 l / GMD) - 1 + AMD/l + O((a/l)^2) ]

  with `GMD` / `AMD` the cross-section's geometric / arithmetic self mean
  distances. For a square of side `a` these are the classical `0.44705 a`
  (Rosa) and `0.521405433 a`. **Both constants are re-derived from their
  integral definitions by quadrature in `native/mom/src/peec.rs`'s test
  module** (`self_gmd_of_a_square_matches_its_published_value`,
  `self_mean_distance_of_a_square_matches_its_published_value`) -- they are
  quoted here rather than recomputed so this module stays a thin end-to-end
  check, not a second numerics suite.

  Note this is *not* Ruehli's `ln(2l/(w+t)) + 0.5 + 0.2235 (w+t)/l`: that
  form reuses the rounded GMD in the slot where the AMD belongs, which
  leaves a first-order residue that bottoms out around 2e-4. See
  `docs/design/mom-validation.md` for the measurement that established this.
- **Partial mutual inductance** -- Grover's exact thin-filament formula
  `M = (mu0/4pi) 2 [l asinh(l/d) - sqrt(l^2 + d^2) + d]` (F. W. Grover,
  *Inductance Calculations*, 1946), valid in the limit of zero
  cross-section, so it is checked at a tolerance reflecting the fixture's
  finite `a/d`.

Convergence under refinement is reported here too, but note what it means on
this path: the partial-element integrals are evaluated in *closed form*, so
refining `filament_subdivisions` is a **self-consistency** axis -- the answer
must not move -- rather than an accuracy axis. See
`docs/design/mom-validation.md` for the genuine mesh-convergence statement
(the thin-filament approximation converging onto the closed form).

Requires the `klt_mom_native` Rust extension (see
`docs/cli/mom.md#building-the-native-extension`); the whole module skips with
a clear reason when it is not built rather than silently passing.
"""

from __future__ import annotations

import json
import math
from typing import Any

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.mom import MomError, run_mom

pytest.importorskip(
    "klt_mom_native",
    reason=(
        "klt_mom_native is not built -- run `maturin develop --release` in "
        "native/mom/ (see docs/cli/mom.md#building-the-native-extension)"
    ),
)

# --- constants and analytic oracles ------------------------------------------

#: `mu0 / 4 pi` in H/m (CODATA 2018 `mu0 = 1.25663706212e-6 H/m`), then
#: converted to "nanohenries per micrometre of geometric factor" -- the same
#: conversion `native/mom/src/peec.rs` applies, so a mismatch here can only
#: come from the geometry, never from a units disagreement.
GEOM_UM_TO_NH = 1.00000000055e-7 * 1e3

#: Copper's bulk DC conductivity, S/m.
COPPER_S_PER_M = 5.8e7

#: Self geometric mean distance of a square of side `a`, in units of `a`
#: (Rosa's classical result; re-derived by quadrature in peec.rs's tests).
SQUARE_GMD = 0.44705

#: Self arithmetic mean distance of a square of side `a`, in units of `a` --
#: the classical mean distance between two uniformly random points in a unit
#: square (likewise re-derived by quadrature in peec.rs's tests).
SQUARE_AMD = 0.521405433

#: Fixture geometry: a 100 x 1 x 1 um bar, and a second identical bar with
#: its axis 10 um away in y.
BAR_LENGTH_UM = 100.0
BAR_SIDE_UM = 1.0
BAR_PITCH_UM = 10.0


def dc_resistance_ohm(length_um: float, area_um2: float, sigma_s_per_m: float) -> float:
    """`R = l / (sigma A)`, exactly, with um -> m conversions applied."""
    return (length_um * 1e-6) / (sigma_s_per_m * area_um2 * 1e-12)


def mean_distance_self_nh(length_um: float, side_um: float) -> float:
    """Partial self inductance (nH) of a square-section straight bar, from
    the mean-distance asymptote quoted in this module's docstring."""
    gmd = SQUARE_GMD * side_um
    amd = SQUARE_AMD * side_um
    geom = 2.0 * length_um * (math.log(2.0 * length_um / gmd) - 1.0 + amd / length_um)
    return GEOM_UM_TO_NH * geom


def grover_mutual_nh(length_um: float, separation_um: float) -> float:
    """Grover's exact mutual inductance (nH) of two parallel *thin* filaments
    of equal length, separated transversely by `separation_um`."""
    length, gap = length_um, separation_um
    geom = 2.0 * (length * math.asinh(length / gap) - math.hypot(length, gap) + gap)
    return GEOM_UM_TO_NH * geom


# --- fixtures ----------------------------------------------------------------


def _um(value: float) -> int:
    return int(round(value / 0.001))


def _write_parallel_bars(path) -> None:
    """Two 100 x 1 um bars on layers 1/0 and 2/0, axes 10 um apart in y.
    Extruded to 1 um thick by the spec, giving a square cross-section."""
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(
        kdb.Box.new(_um(0), _um(0), _um(BAR_LENGTH_UM), _um(BAR_SIDE_UM))
    )
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(
            _um(0),
            _um(BAR_PITCH_UM),
            _um(BAR_LENGTH_UM),
            _um(BAR_PITCH_UM + BAR_SIDE_UM),
        )
    )
    layout.write(str(path))


def _write_bar_spec(
    path,
    *,
    conductivity: float | None = COPPER_S_PER_M,
    filament_subdivisions: int | None = None,
    second_conductivity: float | None = ...,  # type: ignore[assignment]
    thickness_um: float = BAR_SIDE_UM,
) -> None:
    """Spec for `_write_parallel_bars`. `conductivity=None` omits the field
    entirely (the capacitance-only path); `second_conductivity` defaults to
    matching the first and can be set to `None` to omit it on `gnd` only."""
    if second_conductivity is ...:
        second_conductivity = conductivity

    def entry(layer: str, name: str, sigma: float | None) -> dict[str, Any]:
        e: dict[str, Any] = {
            "layer": layer,
            "conductor": name,
            "z0_um": 0.0,
            "z1_um": thickness_um,
        }
        if sigma is not None:
            e["conductivity_S_per_m"] = sigma
        return e

    spec: dict[str, Any] = {
        "background_permittivity": 1.0,
        # 1 um panels over a 100 um bar keeps the capacitance solve inside
        # the 8000-panel guard; the PEEC path does not use this knob.
        "panel_size_um": 1.0,
        "stackup": [
            entry("1/0", "sig", conductivity),
            entry("2/0", "gnd", second_conductivity),
        ],
    }
    if filament_subdivisions is not None:
        spec["filament_subdivisions"] = filament_subdivisions
    path.write_text(json.dumps(spec))


@pytest.fixture(scope="module")
def bars(tmp_path_factory):
    """The two-bar layout, written once for the whole module (the
    capacitance half of each solve is the slow part)."""
    path = tmp_path_factory.mktemp("mom_peec") / "bars.gds"
    _write_parallel_bars(path)
    return path


def _solve(bars, tmp_path, **spec_kwargs) -> dict[str, Any]:
    spec = tmp_path / "bars.mom.json"
    _write_bar_spec(spec, **spec_kwargs)
    return run_mom(str(bars), str(spec))


# --- resistance: exact, gated at round-off -----------------------------------


def test_dc_resistance_matches_the_exact_closed_form(bars, tmp_path):
    report = _solve(bars, tmp_path)
    exact = dc_resistance_ohm(BAR_LENGTH_UM, BAR_SIDE_UM**2, COPPER_S_PER_M)
    for name, measured in zip(
        report["conductors"], report["resistance_ohm"], strict=True
    ):
        error = abs(measured - exact) / exact
        print(
            f"R[{name}] = {measured:.9f} ohm, exact {exact:.9f} ohm, "
            f"rel.err {error:.3e}"
        )
        # No approximation is involved in l/(sigma A), so this is round-off.
        assert error < 1e-12


def test_dc_resistance_is_independent_of_filament_subdivisions(bars, tmp_path):
    reference = _solve(bars, tmp_path)["resistance_ohm"][0]
    for n in (2, 3, 4):
        measured = _solve(bars, tmp_path, filament_subdivisions=n)["resistance_ohm"][0]
        drift = abs(measured - reference) / reference
        print(
            f"filament_subdivisions = {n}: R = {measured:.12f} ohm (drift {drift:.3e})"
        )
        assert drift < 1e-12


# --- partial self inductance against the mean-distance asymptote -------------


def test_partial_self_inductance_matches_the_mean_distance_asymptote(bars, tmp_path):
    report = _solve(bars, tmp_path)
    oracle = mean_distance_self_nh(BAR_LENGTH_UM, BAR_SIDE_UM)
    for index, name in enumerate(report["conductors"]):
        measured = report["inductance_matrix_nh"][index][index]
        error = abs(measured - oracle) / oracle
        print(
            f"Lp[{name},{name}] = {measured:.9f} nH, asymptote {oracle:.9f} nH, "
            f"rel.err {error:.3e}"
        )
        # l/(w+t) = 50, where the asymptote's own O((a/l)^2) remainder
        # dominates; measured ~2e-6 (see docs/design/mom-validation.md).
        assert error < 1e-5


def test_partial_mutual_inductance_matches_grovers_thin_filament_formula(
    bars, tmp_path
):
    report = _solve(bars, tmp_path)
    measured = report["inductance_matrix_nh"][0][1]
    oracle = grover_mutual_nh(BAR_LENGTH_UM, BAR_PITCH_UM)
    error = abs(measured - oracle) / oracle
    print(
        f"Lp[sig,gnd] = {measured:.9f} nH, Grover thin filament {oracle:.9f} nH, "
        f"rel.err {error:.3e}"
    )
    # The oracle assumes zero cross-section; the fixture's is 1 um across at
    # a 10 um pitch, so a finite-section correction is expected rather than
    # exact agreement. Measured: 3.5e-5, gated at 1e-4 (~3x headroom).
    assert error < 1e-4


def test_partial_inductance_matrix_is_symmetric(bars, tmp_path):
    matrix = _solve(bars, tmp_path)["inductance_matrix_nh"]
    assert matrix[0][1] == pytest.approx(matrix[1][0], rel=1e-12)
    assert matrix[0][0] == pytest.approx(matrix[1][1], rel=1e-12)
    # Identical parallel bars: self dominates mutual, both positive.
    assert matrix[0][1] > 0.0
    assert matrix[0][0] > matrix[0][1]


# --- convergence under refinement --------------------------------------------


def test_inductance_is_invariant_under_filament_refinement(bars, tmp_path):
    """The partial-element integrals are closed-form, so refining the
    filament mesh must reproduce the same inductance rather than a better
    one. This is the CLI-visible half of the convergence statement; the
    genuine mesh-convergence study (the thin-filament approximation closing
    onto the closed form) lives in `native/mom/src/peec.rs`'s tests and is
    tabulated in `docs/design/mom-validation.md`.
    """
    reference = _solve(bars, tmp_path)
    l_ref = reference["inductance_matrix_nh"][0][0]
    m_ref = reference["inductance_matrix_nh"][0][1]
    assert reference["filament_count"] == 2
    assert reference["filament_subdivisions"] == 1

    for n in (2, 3, 4):
        report = _solve(bars, tmp_path, filament_subdivisions=n)
        assert report["filament_count"] == 2 * n**3
        assert report["filament_subdivisions"] == n
        self_drift = abs(report["inductance_matrix_nh"][0][0] - l_ref) / l_ref
        mutual_drift = abs(report["inductance_matrix_nh"][0][1] - m_ref) / m_ref
        print(
            f"filament_subdivisions = {n} ({report['filament_count']} filaments): "
            f"self drift {self_drift:.3e}, mutual drift {mutual_drift:.3e}"
        )
        assert self_drift < 1e-9
        assert mutual_drift < 1e-9


# --- contract ----------------------------------------------------------------


def test_capacitance_only_solve_leaves_the_peec_fields_null(bars, tmp_path):
    report = _solve(bars, tmp_path, conductivity=None)
    assert report["inductance_matrix_nh"] is None
    assert report["resistance_ohm"] is None
    assert report["filament_subdivisions"] is None
    assert report["filament_count"] == 0
    # ...and the capacitance half is unaffected.
    assert len(report["capacitance_matrix_ff"]) == 2


def test_cli_json_contract_in_peec_mode(bars, tmp_path, capsys):
    spec = tmp_path / "bars.mom.json"
    _write_bar_spec(spec)
    assert main(["mom", str(bars), str(spec), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)

    assert data["schema_version"] == 2
    assert set(data) == {
        "schema_version",
        "file",
        "spec",
        "background_permittivity",
        "panel_size_um",
        "filament_subdivisions",
        "conductors",
        "capacitance_matrix_ff",
        "inductance_matrix_nh",
        "resistance_ohm",
        "panel_count",
        "filament_count",
        "warnings",
    }
    assert len(data["inductance_matrix_nh"]) == 2
    assert len(data["resistance_ohm"]) == 2


def test_cli_text_output_renders_inductance_and_resistance(bars, tmp_path, capsys):
    spec = tmp_path / "bars.mom.json"
    _write_bar_spec(spec)
    assert main(["mom", str(bars), str(spec)]) == 0
    out = capsys.readouterr().out
    assert "(nanohenries, partial)" in out
    assert "DC resistance (ohms):" in out
    assert "filament_count: 2" in out


# --- error paths --------------------------------------------------------------


def test_conductivity_on_only_some_conductors_is_rejected(bars, tmp_path):
    with pytest.raises(MomError) as excinfo:
        _solve(bars, tmp_path, second_conductivity=None)
    message = str(excinfo.value)
    assert "gnd" in message
    # A partial declaration must not be silently read as "perfect conductor".
    assert "every conductor" in message


def test_zero_thickness_conductor_cannot_carry_current(bars, tmp_path):
    with pytest.raises(MomError) as excinfo:
        _solve(bars, tmp_path, thickness_um=0.0)
    message = str(excinfo.value)
    assert "zero extent along z" in message
    assert "capacitance-only" in message


def test_conflicting_conductivity_for_one_conductor_is_rejected(bars, tmp_path):
    spec = tmp_path / "bars.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 1.0,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "sig",
                        "z0_um": 0.0,
                        "z1_um": 1.0,
                        "conductivity_S_per_m": COPPER_S_PER_M,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "sig",
                        "z0_um": 0.0,
                        "z1_um": 1.0,
                        "conductivity_S_per_m": 3.77e7,
                    },
                ],
            }
        )
    )
    with pytest.raises(MomError, match="two different"):
        run_mom(str(bars), str(spec))


def test_non_positive_conductivity_is_rejected(bars, tmp_path):
    with pytest.raises(MomError, match="non-positive"):
        _solve(bars, tmp_path, conductivity=-1.0)


def test_bad_filament_subdivisions_is_rejected(bars, tmp_path):
    with pytest.raises(MomError, match="at least 1"):
        _solve(bars, tmp_path, filament_subdivisions=0)


def test_excessive_filament_subdivisions_is_rejected_cleanly(bars, tmp_path):
    # 2 boxes x 8^3 = 1024 filaments, past the solver's 512-filament cap.
    with pytest.raises(MomError, match="filaments"):
        _solve(bars, tmp_path, filament_subdivisions=8)


def test_non_bar_shaped_conductor_warns_about_the_current_direction(tmp_path):
    """A square pad has no "long way", so the longest-axis current-direction
    inference is not defensible for it -- the solver must say so rather than
    silently returning a number (mirroring the capacitance path's physicality
    warnings)."""
    gds = tmp_path / "pads.gds"
    layout = kdb.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(kdb.Box.new(_um(0), _um(0), _um(10), _um(10)))
    top.shapes(layout.layer(2, 0)).insert(
        kdb.Box.new(_um(0), _um(20), _um(10), _um(30))
    )
    layout.write(str(gds))

    spec = tmp_path / "pads.mom.json"
    spec.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": 2.0,
                "stackup": [
                    {
                        "layer": "1/0",
                        "conductor": "pad_a",
                        "z0_um": 0.0,
                        "z1_um": 0.5,
                        "conductivity_S_per_m": COPPER_S_PER_M,
                    },
                    {
                        "layer": "2/0",
                        "conductor": "pad_b",
                        "z0_um": 0.0,
                        "z1_um": 0.5,
                        "conductivity_S_per_m": COPPER_S_PER_M,
                    },
                ],
            }
        )
    )

    report = run_mom(str(gds), str(spec))
    # Numbers still come back -- a warning is a diagnostic, not a failure.
    assert report["inductance_matrix_nh"] is not None
    shape_warnings = [w for w in report["warnings"] if "not bar-shaped" in w]
    assert len(shape_warnings) == 2, report["warnings"]
    assert any("pad_a" in w for w in shape_warnings)
