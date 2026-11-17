"""Text rendering of ``klt sim --op-lint`` (split out of sim_cmd.py for the
file-size ratchet, issue #5751)."""

from ..env_provenance import render_path_field
from .sim_pdk_text import _print_pdk


def _print_op_lint_text(report: dict) -> None:
    print(f"netlist: {render_path_field(report['netlist'])}")
    _print_pdk(report)
    print(f"status: {report['status']}")
    print(f"corner: {report['corner']['corner_id']}")
    print(
        f"devices: {report['device_count']}  "
        f"findings: {report['finding_count']} "
        f"(errors: {report['error_count']}, warnings: {report['warning_count']})"
    )

    devices = report["devices"]
    if devices:
        print()
        print("devices:")
        for device in devices:
            values = device["values"]
            detail = "  ".join(
                f"{key}={values[key]!r}"
                for key in sorted(values)
                if values[key] is not None
            )
            print(
                f"  {device['name']} [{device['kind']}] {device['region']}"
                f"{'  ' + detail if detail else ''}"
            )

    corner_margins = report.get("corner_margins")
    if corner_margins:
        print()
        print("corner margins (|Vds| - |Vdsat|, worst first):")
        for entry in corner_margins:
            margin = entry["worst_margin_v"]
            shown = "n/a" if margin is None else f"{margin * 1000:.1f} mV"
            print(
                f"  {entry['corner_id']}  {shown}  "
                f"device={entry['worst_device'] or '-'}  "
                f"triode={entry['triode_count']}"
            )
            for diagnostic in entry["diagnostics"]:
                print(f"      {diagnostic['code']}: {diagnostic['message']}")

    findings = report["findings"]
    if findings:
        print()
        print("findings:")
        for finding in findings:
            print(f"  [{finding['severity']}] {finding['check']}: {finding['message']}")
            print(f"      -> {finding['suggestion']}")

    diagnostics = report["diagnostics"]
    if diagnostics:
        print()
        for diagnostic in diagnostics:
            print(
                f"diagnostic: {diagnostic['severity']} {diagnostic['code']} - "
                f"{diagnostic['message']}"
            )
