#!/usr/bin/env python3
"""Correlation study driver for the FLUTE/RUDY-family congestion pre-check
(issue #785, Epic #700 Phase 1 Section 3.6 of
`docs/design/place-and-route-improvements-survey.md`).

This is the measurement harness the issue's own acceptance criteria require
before any caller wires `klayout_tools.congestion` into `digital_fleet.py`'s
candidate-ranking gate: for a corpus of `klt place-and-route` trials, it
correlates each trial's post-placement congestion pre-check score against
that same trial's actual post-route outcome (its detailed-route DRC-
violation count, or a documented non-convergence sentinel when the route
stage did not finish -- see ``BADNESS_SENTINEL_DID_NOT_CONVERGE`` below),
and reports the DSE-sweep wall-clock the pre-check would have saved.

Usage::

    python3 scripts/research/flute_congestion_correlation.py \\
        --manifest docs/design/flute-congestion-precheck-corpus.json

The manifest is a JSON list of trial objects (see :class:`Trial` /
:func:`load_manifest`). Each trial names a **placement-stage** DEF (as
written by `place_and_route.py`'s `place` stage -- see that module's
`_stage_script_lines`, `place_def_path`) and records that same candidate's
actual post-route outcome, obtained by running the real `klt
place-and-route` CLI (typically against a real `openroad/orfs` Docker image
and a real volare-fetched PDK install, mirroring
`tests/corpus/place_and_route/regenerate.sh`'s own worked example -- this
script does not itself invoke OpenROAD, matching `nets_from_def`'s own
"no native extension/OpenROAD needed here" split).

See `docs/design/flute-congestion-precheck-results.md` for this study's own
real run against the `gcd` corpus fixture and its go/no-go verdict.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any

from klayout_tools.congestion import CongestionError, estimate_congestion, nets_from_def

#: Badness value assigned to a trial whose route stage never converged
#: (killed / exceeded a wall-clock budget) or otherwise failed outright
#: (e.g. `openroad`'s own `DPL-0038` "no room to place" error) -- larger
#: than any real finite DRC-violation count could plausibly be, since a
#: non-convergent route is a *worse* DSE-loop outcome than any design that
#: at least finished routing, however many DRC violations it left behind.
#: Deliberately a named constant (not a magic number in the correlation
#: math below) so a manifest's own JSON can reference it unambiguously and
#: a reader of a correlation report understands why one trial's "badness"
#: dwarfs the others.
BADNESS_SENTINEL_DID_NOT_CONVERGE = 1_000_000.0

#: Default RUDY-grid resolution for the congestion estimate (see
#: `native/congestion/src/contract.rs`'s own `grid_cols`/`grid_rows`
#: docs for the resolution/cost tradeoff this controls).
DEFAULT_GRID_COLS = 20
DEFAULT_GRID_ROWS = 20


@dataclass
class Trial:
    """One `klt place-and-route` candidate's measurement inputs.

    ``place_def``: path to that candidate's placement-stage DEF (see this
    module's own docstring for where `place_and_route.py` writes it).

    ``status``: ``"routed"`` (the route stage completed -- `drc_violation_count`
    is a real, finite count, 0 for a clean route) or ``"did_not_converge"``
    (the route stage was killed/timed out/failed outright before producing a
    DRC report -- `drc_violation_count` is `None`, `BADNESS_SENTINEL_DID_NOT_CONVERGE`
    is used instead).

    ``drc_violation_count``: required when ``status == "routed"``, ignored
    (must be ``None``) otherwise.

    ``place_wallclock_s`` / ``route_wallclock_s``: wall-clock seconds this
    trial's `place` stage / `route`-onward stages actually took (real,
    measured values from the run that produced this trial -- not modeled),
    used for the AC2 wall-clock-saved accounting. ``route_wallclock_s`` may
    be a lower bound (e.g. "still running after N seconds when killed") for
    a ``did_not_converge`` trial -- callers should treat it as such.
    """

    name: str
    place_def: str
    status: str
    drc_violation_count: int | None = None
    place_wallclock_s: float | None = None
    route_wallclock_s: float | None = None

    def badness(self) -> float:
        if self.status == "routed":
            if self.drc_violation_count is None:
                raise ValueError(
                    f"trial '{self.name}': status 'routed' requires drc_violation_count"
                )
            return float(self.drc_violation_count)
        if self.drc_violation_count is not None:
            raise ValueError(
                f"trial '{self.name}': status '{self.status}' must not set "
                "drc_violation_count"
            )
        return BADNESS_SENTINEL_DID_NOT_CONVERGE


def load_manifest(path: str) -> list[Trial]:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return [Trial(**entry) for entry in raw]


@dataclass
class TrialResult:
    trial: Trial
    congestion_score: float
    net_count: int
    overflow_tile_fraction: float
    max_density_um_per_um2: float
    pre_check_wallclock_s: float


def run_pre_check(trial: Trial, *, grid_cols: int, grid_rows: int) -> TrialResult:
    start = time.perf_counter()
    nets, die_box_um = nets_from_def(trial.place_def)
    response = estimate_congestion(
        die_box_um=die_box_um, grid_cols=grid_cols, grid_rows=grid_rows, nets=nets
    )
    elapsed = time.perf_counter() - start
    return TrialResult(
        trial=trial,
        congestion_score=response["congestion_score"],
        net_count=response["net_count"],
        overflow_tile_fraction=response["overflow_tile_fraction"],
        max_density_um_per_um2=response["max_density_um_per_um2"],
        pre_check_wallclock_s=elapsed,
    )


def pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Pearson correlation coefficient, or `None` when it is undefined
    (fewer than 2 points, or one series has zero variance -- both make the
    coefficient mathematically undefined, not "zero correlation")."""
    if len(xs) < 2:
        return None
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return statistics.correlation(xs, ys)


def summarize(results: list[TrialResult], *, reject_threshold: float) -> dict[str, Any]:
    congestion_scores = [r.congestion_score for r in results]
    badness = [r.trial.badness() for r in results]
    correlation = pearson_correlation(congestion_scores, badness)

    rejected = [r for r in results if r.congestion_score >= reject_threshold]
    kept = [r for r in results if r.congestion_score < reject_threshold]

    # A "false reject" is a candidate the threshold would have skipped
    # (never sent to `route`) despite it actually routing DRC-clean -- the
    # exact failure mode AC3 warns against ("without false-rejecting good
    # candidates"). A "false keep" is the mirror case: a candidate the
    # threshold would have sent to `route` that then failed to converge --
    # not itself a correctness bug (the pre-check is a *pre*-check, not a
    # guarantee), but worth surfacing as a miss.
    false_rejects = [
        r.trial.name
        for r in rejected
        if r.trial.status == "routed" and r.trial.badness() == 0.0
    ]
    false_keeps = [r.trial.name for r in kept if r.trial.status == "did_not_converge"]

    route_wallclock_saved_s = sum(
        r.trial.route_wallclock_s
        for r in rejected
        if r.trial.route_wallclock_s is not None
    )
    total_pre_check_wallclock_s = sum(r.pre_check_wallclock_s for r in results)

    return {
        "trial_count": len(results),
        "correlation": {
            "pearson_r": correlation,
            "note": (
                "undefined (need >= 2 trials with nonzero variance in both series)"
                if correlation is None
                else None
            ),
        },
        "reject_threshold": reject_threshold,
        "rejected_candidates": [r.trial.name for r in rejected],
        "kept_candidates": [r.trial.name for r in kept],
        "false_rejects": false_rejects,
        "false_keeps": false_keeps,
        "wallclock": {
            "route_wallclock_saved_s": route_wallclock_saved_s,
            "total_pre_check_wallclock_s": total_pre_check_wallclock_s,
        },
        "per_trial": [
            {
                "name": r.trial.name,
                "status": r.trial.status,
                "congestion_score": r.congestion_score,
                "net_count": r.net_count,
                "overflow_tile_fraction": r.overflow_tile_fraction,
                "max_density_um_per_um2": r.max_density_um_per_um2,
                "drc_violation_count": r.trial.drc_violation_count,
                "badness": r.trial.badness(),
                "pre_check_wallclock_s": r.pre_check_wallclock_s,
                "route_wallclock_s": r.trial.route_wallclock_s,
            }
            for r in results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", required=True, help="path to a trial manifest JSON file"
    )
    parser.add_argument("--grid-cols", type=int, default=DEFAULT_GRID_COLS)
    parser.add_argument("--grid-rows", type=int, default=DEFAULT_GRID_ROWS)
    parser.add_argument(
        "--reject-threshold",
        type=float,
        default=0.8,
        help="congestion_score at/above which a candidate is treated as rejected",
    )
    args = parser.parse_args(argv)

    try:
        trials = load_manifest(args.manifest)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        print(
            f"error: could not load manifest '{args.manifest}': {exc}", file=sys.stderr
        )
        return 1

    results = []
    for trial in trials:
        try:
            results.append(
                run_pre_check(trial, grid_cols=args.grid_cols, grid_rows=args.grid_rows)
            )
        except CongestionError as exc:
            print(f"error: trial '{trial.name}': {exc}", file=sys.stderr)
            return 1

    summary = summarize(results, reject_threshold=args.reject_threshold)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
