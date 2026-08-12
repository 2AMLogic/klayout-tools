"""``klt yield`` command: serialise the statistical yield report as text or
JSON.

Output goes through the shared envelope helpers in :mod:`.output`, as with
every other ``klt`` subcommand -- see ``docs/json-contract.md``.

Exit codes (see ``docs/cli/yield.md`` for the full table):
    0 - analysis ran; every measurement with a declared target_yield met it
        (or none declared one)
    1 - failed to run (bad/missing input, no spec limits, a sample set too
        small to carry a confidence interval, native extension not built) --
        returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
    3 - analysis ran; at least one measurement's yield claim was not met at
        the stated confidence
(2 is reserved for argparse usage errors, as with every other ``klt``
subcommand.)

Exit code 3 matches ``klt sim``'s "ran successfully, a limit was missed"
precedent rather than inventing a new number.
"""

import argparse

from ..yield_analysis import YieldError, run_yield
from .output import emit_error, emit_success

EXIT_PASS = 0
EXIT_TARGET_MISSED = 3


def run(args: argparse.Namespace) -> int:
    measurements = None
    if args.measurement:
        measurements = [name for spec in args.measurement for name in spec.split(",")]
        measurements = [name.strip() for name in measurements if name.strip()]
        if not measurements:
            return emit_error("yield", "--measurement given but empty", args.format)

    try:
        report = run_yield(
            args.samples,
            limits_path=args.limits,
            confidence=args.confidence,
            target_ci_halfwidth=args.target_ci_halfwidth,
            min_samples=args.min_samples,
            measurements=measurements,
        )
    except YieldError as exc:
        return emit_error("yield", str(exc), args.format)

    emit_success(report, args.format, _print_text)

    if report["status"] == "fail":
        return EXIT_TARGET_MISSED
    return EXIT_PASS


def _fmt(value, spec: str = ".6g") -> str:
    return "-" if value is None else format(value, spec)


def _print_text(report: dict) -> None:
    print(f"samples: {report['samples']}")
    if report.get("limits"):
        print(f"limits: {report['limits']}")
    source = report["source"]
    print(f"source: {source['kind']}  samples_analyzed: {source['sample_count']}")
    print(
        f"confidence: {report['confidence']}  "
        f"target_ci_halfwidth: {report['target_ci_halfwidth']}"
    )
    print(f"status: {report['status']}  measurements: {report['measurement_count']}")

    for m in report["measurements"]:
        unit = f" [{m['unit']}]" if m.get("unit") else ""
        print()
        print(f"{m['name']}{unit}  status: {m['status']}  N: {m['n']}")
        limits = m["limits"]
        print(
            f"  limits: min={_fmt(limits.get('min'))} max={_fmt(limits.get('max'))} "
            f"target_yield={_fmt(limits.get('target_yield'))}"
        )
        dist = m["distribution"]
        print(
            f"  fit ({dist['model']}): mean={_fmt(dist['mean'])} "
            f"stddev={_fmt(dist['stddev'])} "
            f"normality={dist['normality']['verdict']}"
        )
        emp = m["yield"]["empirical"]
        print(
            f"  yield (empirical, {emp['method']}): {emp['estimate']:.6f} "
            f"[{emp['confidence_interval']['low']:.6f}, "
            f"{emp['confidence_interval']['high']:.6f}] "
            f"at {emp['confidence']:.0%}, N={emp['n']}"
        )
        norm = m["yield"]["normal"]
        if norm is None:
            print("  yield (normal fit): - (degenerate fit)")
        else:
            print(
                f"  yield (normal, {norm['method']}): {norm['estimate']:.6f} "
                f"[{norm['confidence_interval']['low']:.6f}, "
                f"{norm['confidence_interval']['high']:.6f}] "
                f"at {norm['confidence']:.0%}, N={norm['n']}"
            )
        vr = m["yield"]["variance_reduced"]
        if vr is not None:
            sampling = m["sampling"]
            extra = (
                f" replicates={sampling['replicates']}"
                if sampling.get("replicates") is not None
                else (
                    f" effective_n={sampling['effective_sample_size']:.1f}"
                    if sampling.get("effective_sample_size") is not None
                    else ""
                )
            )
            print(
                f"  yield ({sampling['strategy']}, {vr['method']}): "
                f"{vr['estimate']:.6f} "
                f"[{vr['confidence_interval']['low']:.6f}, "
                f"{vr['confidence_interval']['high']:.6f}] "
                f"at {vr['confidence']:.0%}, N={vr['n']}{extra}"
            )
        cap = m["capability"]
        cpk_ci = cap["cpk_confidence_interval"]
        ci_text = (
            "" if cpk_ci is None else f" [{cpk_ci['low']:.4f}, {cpk_ci['high']:.4f}]"
        )
        print(
            f"  capability: cp={_fmt(cap['cp'], '.4f')} "
            f"cpk={_fmt(cap['cpk'], '.4f')}{ci_text} "
            f"sigma_to_spec={_fmt(cap['sigma_to_spec'], '.4f')} "
            f"limiting={cap['limiting_side'] or '-'}"
        )
        ss = m["sample_size"]
        print(
            f"  sample size: {ss['verdict']} (N={ss['n']}, "
            f"required>={ss['required_n']}, "
            f"observed +/-{ss['observed_ci_halfwidth']:.6f} vs target "
            f"+/-{ss['target_ci_halfwidth']})"
        )
        if limits.get("target_yield") is not None:
            needed = ss["required_n_for_target"]
            print(
                "    to claim target_yield="
                f"{limits['target_yield']}: "
                + (
                    f"N>={needed}"
                    if needed is not None
                    else "unreachable at the observed pass rate"
                )
            )
        vr_ss = ss.get("variance_reduced")
        if vr_ss is not None:
            print(
                f"    variance-reduced ({m['sampling']['strategy']}): "
                f"{vr_ss['verdict']} "
                f"(observed +/-{vr_ss['observed_ci_halfwidth']:.6f} vs target "
                f"+/-{vr_ss['target_ci_halfwidth']})"
            )
        nc = m.get("negative_control")
        if nc is None:
            print("  negative control: none declared")
        else:
            nc_emp = nc["yield"]["empirical"]
            nc_ci = nc_emp["confidence_interval"]
            desc = f" ({nc['description']})" if nc.get("description") else ""
            print(
                f"  negative control{desc}: {nc['verdict']} -- yield "
                f"{nc_emp['estimate']:.6f} [{nc_ci['low']:.6f}, {nc_ci['high']:.6f}] "
                f"vs nominal {nc['nominal_empirical_estimate']:.6f}, N={nc['n']}"
            )
        check = m.get("analytic_cross_check")
        if check is not None:
            print(
                f"  analytic cross-check ({check['kind']}): {check['verdict']} -- "
                f"analytic stddev={check['analytic_stddev']:.6g} "
                f"empirical stddev={check['empirical_stddev']:.6g} "
                f"[{check['empirical_stddev_confidence_interval']['low']:.6g}, "
                f"{check['empirical_stddev_confidence_interval']['high']:.6g}] "
                f"(delta {check['stddev_relative_delta']:+.2%})"
            )
        for warning in m["warnings"]:
            print(f"  warning: {warning}")

    warnings = report.get("warnings") or []
    if warnings:
        print()
        print("warnings:")
        for warning in warnings:
            print(f"  {warning}")
