"""``klt yield-threshold`` command: probit threshold fit for binary-outcome
decision circuits (issue #2976).

Reads a records document of per-sample ``(stimulus, outcome)`` pairs (per
corner) and reports the threshold mean/sigma with profile-likelihood
confidence intervals, per-stimulus-level proportions with Clopper-Pearson
intervals, a goodness-of-fit check and, optionally, the stimulus at a target
error rate. Pure Python; needs no native extension. Output goes through the
shared envelope helpers in :mod:`.output` -- see ``docs/json-contract.md``.

Exit codes:
    0 - the records were read and every corner reported (a corner whose data
        cannot be fitted is reported with a ``fit.status``, not an error)
    1 - the records document is missing/unreadable/malformed or an option is
        invalid -- returned by ``emit_error`` as ``output.ERROR_EXIT_CODE``
(2 is reserved for argparse usage errors.)
"""

from __future__ import annotations

import argparse

from ..probit import ThresholdError, fit_document
from .output import emit_error, emit_success

COMMAND = "yield-threshold"


def run(args: argparse.Namespace) -> int:
    try:
        payload = fit_document(
            args.records,
            confidence=args.confidence,
            target_error_rate=args.target_error_rate,
            threshold=args.threshold,
            outcome_when=args.outcome_when,
        )
    except ThresholdError as exc:
        return emit_error(COMMAND, str(exc), args.format)
    emit_success(payload, args.format, _print_text)
    return 0


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _interval(ci: dict) -> str:
    return f"[{_fmt(ci['lower'])}, {_fmt(ci['upper'])}]"


def _print_text(payload: dict) -> None:
    pct = payload["confidence"] * 100
    for corner in payload["corners"]:
        fit = corner["fit"]
        excluded = corner["excluded"]
        print(
            f"corner {corner['name']}: N={corner['n_samples']} "
            f"errored={excluded['errored']} inconclusive={excluded['inconclusive']}"
        )
        for level in corner["levels"]:
            lo, hi = level["ci"]
            print(
                f"  stimulus {_fmt(level['stimulus'])}: "
                f"{level['high']}/{level['n']} high "
                f"({level['proportion']:.4f}, CP {pct:g}% [{lo:.4f}, {hi:.4f}])"
            )
        print(f"  fit: {fit['status']}")
        if fit["status"] != "ok":
            print(f"    {fit['reason']}")
            if "transition_interval" in fit:
                print(f"    transition interval: {fit['transition_interval']}")
        else:
            print(
                f"    mu    = {_fmt(fit['mu'])}  {pct:g}% CI {_interval(fit['mu_ci'])}"
            )
            print(
                f"    sigma = {_fmt(fit['sigma'])}  {pct:g}% CI "
                f"{_interval(fit['sigma_ci'])}"
            )
            gof = corner["goodness_of_fit"]
            print(
                f"    gof: chi2={_fmt(gof['pearson_chi2'])} dof={gof['dof']} "
                f"p={_fmt(gof['p_value'])}"
                + ("" if gof["reliable"] else f" ({gof['reason']})")
            )
        target = corner["target"]
        if target is not None:
            flag = (
                "EXTRAPOLATED" if target["extrapolated"] else "resolved by the sample"
            )
            print(
                f"  error rate {target['error_rate']:g}: stimulus "
                f"[{_fmt(target['stimulus_low'])}, {_fmt(target['stimulus_high'])}] "
                f"({flag}); "
                f"N resolves {target['resolvable_error_rate']:.3g}"
            )
