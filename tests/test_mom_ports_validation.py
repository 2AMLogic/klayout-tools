"""Closed-form validation for `klt mom`'s port definition + de-embedding
(issue #894, Phase 2b of the Method-of-Moments epic #701).

`tests/test_mom.py` covers the ports/de-embedding path's *contract* (the new
`ports`/`s_parameters` response fields, the port-count/position/frequency
error paths). This module covers its *numerics*, mirroring
`tests/test_mom_fullwave_validation.py`'s precedent for the full-wave
solve's own characteristic-impedance/propagation-constant fields: every
assertion cites the oracle it is checking against, along with the tolerance
and the measured number behind it.

## The oracles

- **Matched transmission-line segment** -- a lossless (or near-lossless)
  uniform line of length `L`, terminated at both ports in its own
  characteristic impedance `Z0`, has the closed-form S-parameters `S11 ==
  S22 == 0` (no reflection) and `S21 == S12 == exp(-gamma*L)` (pure
  propagation delay/attenuation) -- the standard matched two-port
  transmission-line result (Pozar, *Microwave Engineering*, section 2.2).
  `Z0` is taken from the classical two-wire-line closed form
  `tests/test_mom_fullwave_validation.py` already validates
  (`two_wire_line_z0_ohm`), and `gamma`'s phase (`beta`) from the TEM
  identity `beta = omega*sqrt(er)/c0`.
- **De-embedding removes the feed's own contribution** -- for a *uniform*
  line (the same homogeneous medium throughout, this MVP's own modeling
  assumption), the de-embedded S-parameters of a DUT segment bounded by two
  inward ports must match the S-parameters of modeling that DUT segment
  *alone* (no feed stub at all) -- **not** the S-parameters of the full
  (feed + DUT + feed) modeled length. This is the acceptance criterion in
  issue #894 ("de-embedding removes the port-feed's own parasitic
  contribution from the reported S-parameters"), checked directly against
  the DUT-alone closed form's propagation phase `beta * L_dut` (not
  `beta * L_total`).

Requires the `klt_mom_native` Rust extension (see
`docs/cli/mom.md#building-the-native-extension`); like its full-wave
sibling, the whole module skips with a clear reason when it is not built.
"""

from __future__ import annotations

import cmath
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
    (same closed form `tests/test_mom_fullwave_validation.py` uses)."""
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
    ports: list[dict],
    panel_size_um: float,
    segment_size_um: float = 5.0,
) -> None:
    entry = {"z0_um": 0.0, "z1_um": height_um}
    path.write_text(
        json.dumps(
            {
                "background_permittivity": 1.0,
                "panel_size_um": panel_size_um,
                "frequencies_hz": frequencies_hz,
                "segment_size_um": segment_size_um,
                "ports": ports,
                "stackup": [
                    {"layer": "1/0", "conductor": "go", **entry},
                    {"layer": "2/0", "conductor": "return", **entry},
                ],
            }
        )
    )


def _run_loop(
    tmp_path_factory,
    name,
    *,
    length_um,
    width_um,
    height_um,
    separation_um,
    **spec_kwargs,
):
    root = tmp_path_factory.mktemp("mom-ports-validation")
    gds = root / f"{name}.gds"
    _write_loop(gds, length_um, width_um, separation_um)
    spec = root / f"{name}.mom.json"
    _write_loop_spec(spec, height_um=height_um, **spec_kwargs)
    return run_mom(str(gds), str(spec))


# --- matched line vs closed form ---------------------------------------------


def test_matched_line_s_parameters_match_closed_form(tmp_path_factory):
    # 500um long, 2x2um bars, 40um separation -- same fixture
    # test_mom_fullwave_validation.py's characteristic-impedance test uses.
    # Ports at both physical ends, reference impedance set to the two-wire
    # closed-form Z0 -- the classical "matched line" case.
    length_um, width_um, height_um, separation_um = 500.0, 2.0, 2.0, 40.0
    frequency_hz = 1.0e9
    z0_oracle_ohm = two_wire_line_z0_ohm(width_um, height_um, separation_um)

    report = _run_loop(
        tmp_path_factory,
        "matched",
        length_um=length_um,
        width_um=width_um,
        height_um=height_um,
        separation_um=separation_um,
        frequencies_hz=[frequency_hz],
        ports=[
            {"position_um": 0.0, "reference_impedance_ohm": z0_oracle_ohm},
            {"position_um": length_um, "reference_impedance_ohm": z0_oracle_ohm},
        ],
        panel_size_um=2.0,
    )

    s = report["full_wave_sweep"][0]["s_parameters"]
    s11 = complex(s["s11_real"], s["s11_imag"])
    s22 = complex(s["s22_real"], s["s22_imag"])
    s21 = complex(s["s21_real"], s["s21_imag"])

    omega = 2.0 * math.pi * frequency_hz
    beta_oracle = omega / C0_M_PER_S  # TEM identity, background_permittivity=1
    phase_oracle = -beta_oracle * (length_um * 1e-6)

    print(
        f"\nmatched line: S11={s11:.4e} S22={s22:.4e} S21={s21:.6f} "
        f"|S21|={abs(s21):.6f} phase(S21)={cmath.phase(s21):.6f} rad "
        f"oracle phase={phase_oracle:.6f} rad"
    )
    # Reflection: driven by the (small) mismatch between the model's own
    # derived Z0 and the closed-form oracle Z0 used as the reference
    # impedance -- same ~10% Z0 discretisation budget
    # test_mom_fullwave_validation.py's own Z0 test measures, so a generous
    # bound here.
    assert abs(s11) < 0.15, f"expected S11 near 0 for a matched line, got {s11}"
    assert abs(s22) < 0.15, f"expected S22 near 0 for a matched line, got {s22}"
    # Near-lossless (vacuum, subwavelength cross-section): |S21| ~= 1.
    assert abs(s21) > 0.9, f"expected |S21| near 1 (near-lossless), got {abs(s21)}"
    # Propagation phase matches the TEM identity to the same 10% budget
    # test_mom_fullwave_validation.py's own beta test uses.
    phase_rel_err = abs(cmath.phase(s21) - phase_oracle) / abs(phase_oracle)
    assert phase_rel_err < 0.10, (
        f"S21 phase {cmath.phase(s21):.6f} rad vs TEM oracle {phase_oracle:.6f} rad, "
        f"rel.err {phase_rel_err * 100:.4f}% exceeds 10%"
    )


# --- de-embedding removes the feed's own contribution -------------------------


def test_de_embedding_matches_the_dut_alone_closed_form(tmp_path_factory):
    """The direct acceptance-criterion check: de-embedded S-parameters for a
    DUT segment bounded by inward ports must match the DUT-alone closed
    form (propagation phase over the DUT length only) -- not the full
    (feed + DUT + feed) modeled length's phase, which is what a broken (or
    absent) de-embedding step would instead return."""
    width_um, height_um, separation_um = 2.0, 2.0, 40.0
    feed1_um, dut_um, feed2_um = 100.0, 500.0, 150.0
    total_um = feed1_um + dut_um + feed2_um
    frequency_hz = 1.0e9
    reference_impedance_ohm = two_wire_line_z0_ohm(width_um, height_um, separation_um)

    report = _run_loop(
        tmp_path_factory,
        "deembed",
        length_um=total_um,
        width_um=width_um,
        height_um=height_um,
        separation_um=separation_um,
        frequencies_hz=[frequency_hz],
        ports=[
            {
                "position_um": feed1_um,
                "reference_impedance_ohm": reference_impedance_ohm,
            },
            {
                "position_um": feed1_um + dut_um,
                "reference_impedance_ohm": reference_impedance_ohm,
            },
        ],
        panel_size_um=2.0,
    )
    s = report["full_wave_sweep"][0]["s_parameters"]
    s21 = complex(s["s21_real"], s["s21_imag"])
    measured_phase = cmath.phase(s21)

    omega = 2.0 * math.pi * frequency_hz
    beta_oracle = omega / C0_M_PER_S
    dut_phase_oracle = -beta_oracle * (dut_um * 1e-6)
    total_phase_oracle = -beta_oracle * (total_um * 1e-6)

    print(
        f"\nde-embedding: measured S21 phase={measured_phase:.6f} rad  "
        f"DUT-alone oracle={dut_phase_oracle:.6f} rad  "
        f"full-length oracle (would-be-if-not-de-embedded)={total_phase_oracle:.6f} rad"
    )

    dut_rel_err = abs(measured_phase - dut_phase_oracle) / abs(dut_phase_oracle)
    total_rel_err = abs(measured_phase - total_phase_oracle) / abs(total_phase_oracle)

    # The measured phase must land near the DUT-alone oracle -- same 10%
    # TEM-identity budget the matched-line test above uses.
    assert dut_rel_err < 0.10, (
        f"de-embedded S21 phase {measured_phase:.6f} rad should match the DUT-alone "
        f"({dut_um}um) oracle {dut_phase_oracle:.6f} rad (rel.err "
        f"{dut_rel_err * 100:.4f}% exceeds 10%) -- de-embedding did not correctly "
        "strip the feed stubs' contribution"
    )
    # And it must clearly NOT land near the full-modeled-length oracle --
    # otherwise de-embedding would be silently a no-op and the assertion
    # above would have no power to catch that regression.
    assert total_rel_err > dut_rel_err, (
        f"sanity check failed: the de-embedded phase {measured_phase:.6f} rad is at "
        f"least as close to the full-length oracle {total_phase_oracle:.6f} rad as to "
        f"the DUT-alone oracle {dut_phase_oracle:.6f} rad -- de-embedding appears to "
        "be a no-op"
    )
