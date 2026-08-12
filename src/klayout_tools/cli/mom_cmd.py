"""``klt mom`` command: serialise the Method-of-Moments capacitance report as
text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/mom.md`` for the full table):
    0 - solve succeeded, capacitance matrix returned
    1 - failed to run (bad file/spec, native extension not installed, a
        stackup entry matching no shapes, or a solver-level failure such as
        a singular potential-coefficient matrix) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)
"""

import argparse

from ..mom import MomError, run_mom
from .output import emit_error, emit_success


def run(args: argparse.Namespace) -> int:
    try:
        report = run_mom(args.file, args.spec, top=args.top)
    except MomError as exc:
        return emit_error("mom", str(exc), args.format)

    emit_success(report, args.format, _print_text)
    return 0


def _print_matrix(conductors: list, matrix: list, name_width: int, unit: str) -> None:
    header = " " * name_width + "  " + "  ".join(name.rjust(12) for name in conductors)
    print()
    print(header)
    for name, row in zip(conductors, matrix, strict=True):
        cells = "  ".join(f"{value:12.6g}" for value in row)
        print(f"{name.ljust(name_width)}  {cells}")
    print(f"({unit})")


def _print_text(report: dict) -> None:
    print(f"file: {report['file']}")
    print(f"spec: {report['spec']}")
    print(f"background_permittivity: {report['background_permittivity']}")
    print(f"panel_size_um: {report['panel_size_um']}")
    print(f"panel_count: {report['panel_count']}")

    conductors = report["conductors"]
    matrix = report["capacitance_matrix_ff"]
    print(f"conductors: {len(conductors)}")
    if not conductors:
        return

    name_width = max(len("conductor"), max(len(name) for name in conductors))
    _print_matrix(conductors, matrix, name_width, "femtofarads")

    if "inductance_matrix_nh" in report:
        print()
        print(f"filament_size_um: {report['filament_size_um']}")
        print(f"filament_count: {report['filament_count']}")
        _print_matrix(
            conductors, report["inductance_matrix_nh"], name_width, "nanohenries"
        )
        print()
        print("resistance (ohms):")
        for name, r in zip(conductors, report["resistance_ohm"], strict=True):
            print(f"  {name.ljust(name_width)}  {r:.6g}")

    if "full_wave_sweep" in report:
        print()
        print(f"segment_size_um: {report['segment_size_um']}")
        print(f"full_wave_segment_count: {report['full_wave_segment_count']}")
        print("full-wave sweep:")
        has_line = bool(report["full_wave_sweep"]) and (
            report["full_wave_sweep"][0].get("characteristic_impedance_real_ohm")
            is not None
        )
        for point in report["full_wave_sweep"]:
            line = f"  {point['frequency_hz']:.6g} Hz"
            if has_line:
                z0 = complex(
                    point["characteristic_impedance_real_ohm"],
                    point["characteristic_impedance_imag_ohm"],
                )
                line += (
                    f"  Z0={z0.real:.6g}{z0.imag:+.6g}j ohm"
                    f"  alpha={point['attenuation_np_per_m']:.6g} Np/m"
                    f"  beta={point['phase_rad_per_m']:.6g} rad/m"
                )
            print(line)

    warnings = report["warnings"]
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
