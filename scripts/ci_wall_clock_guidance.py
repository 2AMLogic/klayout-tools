"""Reader guidance printed under a CI wall-clock budget breach.

Split out of `check_ci_wall_clock.py` (size ratchet, #4751). `breaches` are the
checker's `Breach` rows; only `.kind` and `.confirmed` are read.
"""

from __future__ import annotations


def breach_guidance(breaches: list, *, cache_missed: bool) -> list[str]:
    """What the reader should do about `breaches` (a non-empty list)."""
    kinds = {b.kind for b in breaches}
    failing = [b for b in breaches if b.kind == "compute" and b.confirmed]
    guidance: list[str] = []
    if kinds == {"queue"}:
        guidance += [
            "",
            "Compute is WITHIN budget -- this is a scheduling/queue breach, not a",
            "code regression. Optimising these jobs would achieve nothing (issue",
            "#1971): look at runner-pool contention instead. Reported, not gated:",
            "a queue-only breach exits 0 and does not fail this build.",
        ]
        if cache_missed:
            guidance.append(
                "The cache miss above also inflates wall clock, which -- unlike "
                "compute -- is measured whole."
            )
    elif "compute" in kinds and not failing:
        guidance += [
            "",
            "Every compute breach above is UNCONFIRMED: another run of this same",
            "commit measured the same job inside a quiet-pool threshold, so this",
            "run measured the pool, not the code (issue #2615). Reported, not",
            "gated: this exits 0.",
        ]
    elif "compute" in kinds:
        guidance += [
            "",
            "This is a COMPUTE breach: the work took longer than the rolling",
            "baseline of recent runs allows, and the pool was not measurably busy",
            "at the time. What to do, in order:",
            "  1. Read the `basis` column. `rolling` compares against the median",
            "     of recent baseline-branch runs; `fixed ceiling` means that job",
            "     has too little history yet and is on the committed fallback in",
            "     .github/ci-wall-clock-budget.json.",
            "  2. If the slowdown is real, shrink the job -- or, if the new cost",
            "     is justified, say so in the PR. A rolling baseline absorbs a",
            "     justified increase on its own within a few baseline-branch runs;",
            "     no number has to be hand-edited for that.",
            "  3. If you believe the pool was busy and this check could not see it",
            "     (contention from workloads outside this repo's own Actions",
            "     history is invisible here), re-run the WHOLE run (or push a",
            "     fresh head). A clean full re-run measures every job again and",
            "     passes; re-running only this job re-reads the same timings.",
            "  4. Only edit the committed ceilings when a job is on the `fixed",
            "     ceiling` basis; the rolling rows are derived, not declared.",
        ]
        if cache_missed:
            guidance.append(
                "Cache-miss rebuild time is ALREADY excluded above, so this "
                "breach is not the cache service."
            )
    return guidance
