#!/usr/bin/env python3
"""Standalone NEC2++ (`PyNEC`) reference solve for `klt mom`'s full-wave
S-parameter cross-validation (2AMLogic/klayout-tools issue #895, Phase 2c of
the Method-of-Moments epic #701).

This script is deliberately **not** importable Python library code, and is
never imported directly by `tests/test_mom_cross_validation.py` -- it is
always invoked as a **subprocess** (`subprocess.run([sys.executable,
__file__, ...])`), reading a geometry spec as JSON on stdin and printing a
JSON result to stdout. This mirrors how every other GPL-licensed engine this
repo touches is integrated (ngspice, Yosys, Icarus Verilog, Verilator -- all
invoked as external processes, never `import`ed into `klt`'s own code) and
the explicit precedent `docs/design/em-field-sim-spike.md` sets for openEMS
("fine to invoke as a subprocess, forecloses in-process/library embedding
inside this repo's MIT surface"): `PyNEC` (the Python bindings for
nec2++/NEC2, the classical Numerical Electromagnetics Code) is GPL-3.0-only
(verified via its PyPI metadata), the same license category. Keeping it
behind a subprocess boundary, in its own optional
`klayout-tools[mom-cross-validation]` extra (never installed by default, and
never part of the published wheel's *runtime* surface), means no GPL code is
ever linked into the same process as this MIT-licensed repo's own code --
only two independent processes exchanging JSON over stdout, the same
"mere aggregation" pattern the other external-tool integrations above use.

## Method: the classical open-circuit/short-circuit two-measurement technique

NEC(2++) is a thin-wire Method-of-Moments antenna/scattering solver: it does
not report a two-port network's S-parameters directly, only a driven
structure's feed-point impedance for a given excitation and geometry. This
script derives the transmission line's own characteristic impedance `Z0` and
propagation constant `gamma` from **two** NEC solves of the *same* geometry,
differing only in how the far end is terminated -- the standard textbook
technique for characterising a uniform line (the NEC-modeling equivalent of
open/short-terminated VNA calibration measurements):

- **Open-circuit**: the two conductors are left unconnected at the far end.
  NEC reports `Z_oc`, the driving-point impedance looking into the near end.
- **Short-circuit**: a connecting wire segment is added between the two
  conductors at the far end. NEC reports `Z_sc`.

For a uniform transmission line of length `l`, characteristic impedance
`Z0`, and propagation constant `gamma`:

```text
Z_oc = Z0 / tanh(gamma * l)
Z_sc = Z0 * tanh(gamma * l)
```

so:

```text
Z0          = sqrt(Z_oc * Z_sc)
tanh(gamma*l) = sqrt(Z_sc / Z_oc)
gamma       = atanh(sqrt(Z_sc / Z_oc)) / l
```

`Z0`/`gamma` are then converted to S-parameters at a chosen real reference
impedance via the same standard ABCD (chain-parameter) cascade
`native/mom/src/fullwave.rs`'s `line_abcd`/`abcd_to_s` use (Pozar,
*Microwave Engineering*, Table 4.2) -- see `_line_abcd`/`_abcd_to_s` below,
reimplemented independently here (not imported from the Rust crate or its
Python wrapper) so this script has no dependency on `klt_mom_native` at all,
keeping the two solvers' code paths genuinely disjoint end to end.

## The near-end feed and the two conductors' own geometry

The near end (`x=0`) always carries a short connecting "feed stub" wire
between the two conductors (NEC requires an excited segment, and a voltage
source between the two separate wire structures needs a physical connecting
segment to source across) -- this exactly matches the "gap source" modeling
convention standard NEC transmission-line tutorials use. Both conductors are
modeled as round wires of the request's `radius_um` -- the same
equal-area-circle effective radius `native/mom/src/fullwave.rs` substitutes
for a rectangular bar cross-section (see that module's own docs), so the two
solvers model the *same* effective geometry, not merely the same nominal
box dimensions.

## Usage

```bash
python3 scripts/mom_nec_reference.py <<'EOF'
{
  "length_um": 500.0,
  "separation_um": 40.0,
  "radius_um": 1.1283791670955126,
  "frequency_hz": 1.0e9,
  "segments_per_wire": 101,
  "reference_impedance_ohm": 50.0
}
EOF
```

prints a JSON object with `z_oc_real_ohm`/`z_oc_imag_ohm`,
`z_sc_real_ohm`/`z_sc_imag_ohm`, `z0_real_ohm`/`z0_imag_ohm`,
`attenuation_np_per_m`/`phase_rad_per_m`, and
`s11_real`/`s11_imag`/`s12_real`/`s12_imag`/`s21_real`/`s21_imag`/
`s22_real`/`s22_imag` (S-parameters at `reference_impedance_ohm`, both
ports).
"""

from __future__ import annotations

import cmath
import json
import sys


def _run_nec(
    length_m: float,
    separation_m: float,
    radius_m: float,
    frequency_hz: float,
    segments_per_wire: int,
    far_end_shorted: bool,
) -> complex:
    """One NEC solve: two parallel round wires along x, separated by
    `separation_m` along y, fed via a short connecting stub at x=0, with
    the far end (x=`length_m`) either left open or shorted by a second
    connecting stub. Returns the near-end driving-point impedance (ohms)."""
    from PyNEC import nec_context  # imported here -- see module docs

    ctx = nec_context()
    geo = ctx.get_geometry()
    # Near-end feed stub (tag 1, a single segment) -- the excitation source.
    geo.wire(1, 1, 0.0, 0.0, 0.0, 0.0, separation_m, 0.0, radius_m, 1.0, 1.0)
    # The two long conductors (tags 2/3).
    geo.wire(
        2,
        segments_per_wire,
        0.0,
        0.0,
        0.0,
        length_m,
        0.0,
        0.0,
        radius_m,
        1.0,
        1.0,
    )
    geo.wire(
        3,
        segments_per_wire,
        0.0,
        separation_m,
        0.0,
        length_m,
        separation_m,
        0.0,
        radius_m,
        1.0,
        1.0,
    )
    if far_end_shorted:
        # Far-end connecting stub (tag 4) -- the short-circuit termination.
        geo.wire(
            4,
            1,
            length_m,
            0.0,
            0.0,
            length_m,
            separation_m,
            0.0,
            radius_m,
            1.0,
            1.0,
        )
    ctx.geometry_complete(0)
    ctx.gn_card(-1, 0, 0, 0, 0, 0, 0, 0)  # free space, no ground plane
    ctx.fr_card(0, 1, frequency_hz / 1.0e6, 0)  # NEC's FR card wants MHz
    # Voltage-source excitation on the near-end feed stub (tag 1, segment 1).
    ctx.ex_card(0, 1, 1, 0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    ctx.xq_card(0)
    return complex(ctx.get_impedance_real(0), ctx.get_impedance_imag(0))


def _line_abcd(
    z0: complex, gamma: complex, length_m: float
) -> tuple[complex, complex, complex, complex]:
    """ABCD (chain-parameter) matrix of a uniform line -- same formula as
    `native/mom/src/fullwave.rs`'s `line_abcd`, reimplemented independently
    (see module docs)."""
    gl = gamma * length_m
    a = cmath.cosh(gl)
    b = z0 * cmath.sinh(gl)
    c = cmath.sinh(gl) / z0
    return a, b, c, a


def _abcd_to_s(
    a: complex, b: complex, c: complex, d: complex, z01: float, z02: float
) -> dict[str, float]:
    """ABCD -> S-parameter conversion at real reference impedances `z01`/
    `z02` -- same formula as `native/mom/src/fullwave.rs`'s `abcd_to_s`
    (Pozar, *Microwave Engineering*, Table 4.2), reimplemented independently
    (see module docs)."""
    z01c, z02c = complex(z01, 0.0), complex(z02, 0.0)
    sqrt_z01z02 = cmath.sqrt(z01c * z02c)
    delta = a * z02c + b + c * z01c * z02c + d * z01c

    s11 = (a * z02c + b - c * z01c * z02c - d * z01c) / delta
    s12 = (a * d - b * c) * 2.0 * sqrt_z01z02 / delta
    s21 = 2.0 * sqrt_z01z02 / delta
    s22 = (-a * z02c + b - c * z01c * z02c + d * z01c) / delta

    return {
        "s11_real": s11.real,
        "s11_imag": s11.imag,
        "s12_real": s12.real,
        "s12_imag": s12.imag,
        "s21_real": s21.real,
        "s21_imag": s21.imag,
        "s22_real": s22.real,
        "s22_imag": s22.imag,
    }


def solve(spec: dict) -> dict:
    length_m = spec["length_um"] * 1e-6
    separation_m = spec["separation_um"] * 1e-6
    radius_m = spec["radius_um"] * 1e-6
    frequency_hz = spec["frequency_hz"]
    segments_per_wire = spec["segments_per_wire"]
    reference_impedance_ohm = spec.get("reference_impedance_ohm", 50.0)

    z_oc = _run_nec(
        length_m, separation_m, radius_m, frequency_hz, segments_per_wire, False
    )
    z_sc = _run_nec(
        length_m, separation_m, radius_m, frequency_hz, segments_per_wire, True
    )

    z0 = cmath.sqrt(z_oc * z_sc)
    gamma = cmath.atanh(cmath.sqrt(z_sc / z_oc)) / length_m

    a, b, c, d = _line_abcd(z0, gamma, length_m)
    s_parameters = _abcd_to_s(
        a, b, c, d, reference_impedance_ohm, reference_impedance_ohm
    )

    return {
        "z_oc_real_ohm": z_oc.real,
        "z_oc_imag_ohm": z_oc.imag,
        "z_sc_real_ohm": z_sc.real,
        "z_sc_imag_ohm": z_sc.imag,
        "z0_real_ohm": z0.real,
        "z0_imag_ohm": z0.imag,
        "attenuation_np_per_m": gamma.real,
        "phase_rad_per_m": gamma.imag,
        **s_parameters,
    }


def main() -> int:
    spec = json.loads(sys.stdin.read())
    result = solve(spec)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
