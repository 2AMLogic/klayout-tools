"""``klt size`` command: serialise a gm/Id sizing result -- single-device or
coupled multi-device topology (issue #768) -- as text or JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/size.md`` for the full table):
    0 - the confirmed operating point meets the gm/Id target within
        tolerance
    1 - the request could not even be attempted (bad request, unresolvable
        model library, unsupported engine/device kind) -- returned by
        ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - the search ran but the target could not be met (infeasible within
        the given width bounds, or outside the stated tolerance)
    4 - the ngspice evaluator itself errored (launch failure, timeout, or
        no usable operating-point data)

Reuses the exact exit-code trichotomy ``klt sim`` established (0/1/3/4,
skipping 2 which argparse itself owns) rather than inventing a fourth
scheme -- see ``docs/cli/sim.md``'s "Exit codes" section for the reasoning
this command shares.
"""

import argparse

from ..env_provenance import render_path_field
from ..size import SizeError, run_size
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TARGET_UNMET = 3
EXIT_EVALUATOR_ERRORED = 4


def run(args: argparse.Namespace) -> int:
    try:
        report = run_size(
            args.request,
            artifacts_dir=args.outdir,
            timeout_s=args.timeout_s,
        )
    except SizeError as exc:
        return emit_error("size", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "error":
        return EXIT_EVALUATOR_ERRORED
    if report["status"] == "fail":
        return EXIT_TARGET_UNMET
    return EXIT_PASS


def _print_text(report: dict) -> None:
    if "topology" in report:
        _print_topology_text(report)
        return

    device = report["device"]
    print(f"device: {device['kind']} {device['model']} (L={device['l_um']}um)")
    print(f"status: {report['status']}")

    target = report["target"]
    print(f"target: gm/Id={target['gm_id']:g}  Id={target['id_a']:g}A")

    op = report.get("operating_point")
    if op is not None:
        print(
            f"sized: W={op['w_um']:g}um  gm/Id={op['gm_id']:g}  "
            f"Id={op['id_a']:g}A  inversion={op['inversion_level']}"
        )
        if op.get("vgs_v") is not None:
            vds_str = f"  Vds={op['vds_v']:g}V" if op.get("vds_v") is not None else ""
            print(
                f"  Vgs={op['vgs_v']:g}V  Vth={op['vth_v']!r}  "
                f"Vov={op['vov_v']!r}{vds_str}"
            )

    margins = report.get("margins")
    if margins is not None:
        print(
            f"margins: gm_id_rel_error={margins['gm_id_rel_error']:+.4g}  "
            f"id_rel_error={margins['id_rel_error']:+.4g}"
        )

    corners = report.get("corners")
    if corners is not None and len(corners.get("declared") or []) > 1:
        print()
        print(f"objective: {corners.get('objective', 'sizing_corner')}")
        worst_case_id = corners.get("worst_case")
        print("corners:")
        for entry in corners.get("results") or corners.get("declared") or []:
            sizing_marker = "*" if entry.get("is_sizing") else " "
            worst_marker = "!" if entry.get("corner_id") == worst_case_id else " "
            entry_status = entry.get("status", "not evaluated")
            entry_margins = entry.get("margins")
            margin_str = (
                f"  gm_id_rel_error={entry_margins['gm_id_rel_error']:+.4g}"
                if entry_margins is not None
                else ""
            )
            print(
                f"  {sizing_marker}{worst_marker} {entry['corner_id']}: "
                f"{entry_status}{margin_str}"
            )

    method = report["method"]
    print()
    print(f"method: {method['name']}")
    print(f"rationale: {method['rationale']}")

    env = report["environment"]
    print()
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    # Issue #1274: `models_lib` is the `{path, scope}` shape
    # `env_provenance.repo_relative_path` defines -- rendered through the
    # same shared helper `klt sim`/`klt env-provenance` use, so a PDK
    # outside the repo prints `<outside repo>`, never an absolute home path
    # (and never a raw dict repr).
    print(f"models_lib: {render_path_field(env['models_lib'])}")


def _print_topology_text(report: dict) -> None:
    """Text rendering for a coupled multi-device topology result (issue
    #768) -- a different top-level shape from single-device mode's: no
    top-level ``device``/``operating_point``, but a ``roles`` map (the
    solved widths), a ``devices`` map keyed by instance (every device's own
    in-circuit operating point and rationale), and a ``joint_solve`` block
    (the coupled iteration trajectory)."""
    print(f"topology: {report['topology']}")
    print(f"status: {report['status']}")

    budget = report["budget"]
    print(
        f"budget: tail_current={budget['tail_current_a']:.6g}A  "
        f"branch_current={budget['branch_current_a']:.6g}A  "
        f"tail_mirror_ratio={budget['tail_mirror_ratio']:g}  "
        f"i_ref={budget['reference_current_a']:.6g}A"
    )
    measured = budget.get("measured")
    if measured is not None:
        split = measured.get("tail_split")
        split_str = f"{split[0] * 100:.1f}/{split[1] * 100:.1f}%" if split else "n/a"
        print(
            f"measured: tail_current={measured['tail_current_a']:.6g}A  "
            f"split={split_str}  "
            f"mirror_ratio={measured['mirror_ratio']:.4g}"
        )

    print()
    print("solved widths:")
    for role, entry in (report.get("roles") or {}).items():
        width = entry["w_um"]
        width_str = f"{width:g}um" if width is not None else "-"
        print(
            f"  {role}: W={width_str}  L={entry['l_um']:g}um  "
            f"target gm/Id={entry['target_gm_id']:g}  "
            f"(control: {entry['control_instance']})"
        )

    for key, entry in (report.get("devices") or {}).items():
        device = entry["device"]
        print()
        control = " [control]" if entry["is_control"] else ""
        print(
            f"[{key}] {entry['instance']} -- {entry['role']}{control}: "
            f"{device['kind']} {device['model']} (L={device['l_um']}um)"
        )
        op = entry.get("operating_point")
        if op is not None:
            print(
                f"  W={op['w_um']:g}um  gm/Id={op['gm_id']:g}  "
                f"Id={op['id_a']:g}A  inversion={op['inversion_level']}  "
                f"status={entry['status']}"
            )
        print(f"  rationale: {entry['rationale']}")

    solve = report.get("joint_solve")
    if solve is not None:
        print()
        print(
            f"joint solve: {solve['iterations_run']} coupled ngspice "
            f"iteration(s), converged={solve['converged']}"
        )
        for step in solve.get("iterations") or []:
            worst = step.get("worst_abs_rel_error")
            worst_str = f"{worst:.4g}" if worst is not None else "n/a"
            widths = "  ".join(
                f"{role}={w:g}um" for role, w in step["widths_um"].items()
            )
            print(f"  #{step['index']} {widths}  worst|gm/Id err|={worst_str}")

    method = report.get("method")
    if method is not None:
        print()
        print(f"method: {method['name']}")
        print(f"rationale: {method['rationale']}")

    env = report["environment"]
    print()
    print(f"engine: {env['engine']} {env['engine_version'] or '-'}")
    # Issue #1274: `models_lib` is the `{path, scope}` shape
    # `env_provenance.repo_relative_path` defines -- rendered through the
    # same shared helper `klt sim`/`klt env-provenance` use, so a PDK
    # outside the repo prints `<outside repo>`, never an absolute home path
    # (and never a raw dict repr).
    print(f"models_lib: {render_path_field(env['models_lib'])}")
