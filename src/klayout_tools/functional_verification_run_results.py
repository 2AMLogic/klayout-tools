"""Per-run result collection of the cocotb functional-verification flow.

Split out of ``functional_verification.py`` (the #1909 SDF-split precedent):
that module sits at its per-file size ceiling, and threading the request's
repo root into the per-seed ``results_xml`` envelope (issue #6762) would have
grown it. Names that stay in ``functional_verification.py`` are reached as
``_fv.<name>`` at call time, so monkeypatching them on
``klayout_tools.functional_verification`` still takes effect.
"""

from __future__ import annotations

from typing import Any

from ._artifact_envelope import RepoRoot, hashed_path_envelope


def _collect_run_results(
    run_specs: list[dict[str, Any]],
    *,
    engine: str,
    module: str,
    build_log: str,
    repo_root: RepoRoot,
) -> tuple[list[dict[str, Any]], tuple[int, int, int], list[dict[str, Any]]]:
    """Parse every run's results: ``(tests, (passed, failed, skipped),
    per-seed run reports)``. A seeded run's tests carry ``initial_state_seed``
    (issue #2998). ``repo_root`` is the repo the per-seed ``results_xml``
    envelope is relative to -- the request's, never the cwd's (issue #6762);
    required so no caller can fall back to the cwd by omission."""
    tests: list[dict[str, Any]] = []
    totals = [0, 0, 0]
    run_reports: list[dict[str, Any]] = []
    for spec in run_specs:
        spec_tests = _fv._parse_run_results(spec, engine=engine, build_log=build_log)
        counts = _fv._executed_result_counts(spec_tests, module)
        totals = [total + count for total, count in zip(totals, counts, strict=True)]
        if spec["seed"] is not None:
            for test in spec_tests:
                test["initial_state_seed"] = spec["seed"]
            run_reports.append(
                {
                    "seed": spec["seed"],
                    "status": "fail" if counts[1] else "pass",
                    "results_xml": hashed_path_envelope(
                        spec["results_xml"], repo_root=repo_root
                    ),
                }
            )
        tests.extend(spec_tests)
    return tests, (totals[0], totals[1], totals[2]), run_reports


from . import functional_verification as _fv  # noqa: E402
