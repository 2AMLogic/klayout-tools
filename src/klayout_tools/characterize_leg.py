"""One ``klt characterize`` simulation leg with ``--resume`` support (issue
#5350), split out of :mod:`characterize` (size ratchet)."""

from __future__ import annotations

import json
import os
from typing import Any

from . import sim


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def resolve_resume(request: dict[str, Any], resume: bool | None) -> bool:
    """The explicit ``resume`` argument wins; else ``request.options.resume``."""
    if resume is not None:
        return resume
    return bool((request.get("options") or {}).get("resume", False))


def report_collected(report: dict[str, Any]) -> bool:
    """True when every corner actually ran (a pass, or only a `.meas` that
    captured nothing -- a constraint probe's answer): a report holding an
    un-run/errored corner is re-simulated, not reused."""
    corners = report.get("corners")
    if not isinstance(corners, list) or not corners:
        return False
    return all(
        c.get("status") == "pass"
        or all(d.get("code") == "measurement" for d in c.get("diagnostics") or [])
        for c in corners
        if isinstance(c, dict)
    )


def run_sim_leg(
    *,
    testbench_path: str,
    testbench_text: str,
    sim_request: dict[str, Any],
    sim_request_path: str,
    sim_report_path: str,
    sim_dir: str,
    backend: str | None,
    resume: bool,
    **run_sim_kwargs: Any,
) -> dict[str, Any]:
    """Write one leg's testbench + sim request and run it.

    Under ``resume``, a leg whose testbench and request are byte-identical to
    the ones already on disk and whose ``sim-report.json`` survives is not
    re-simulated -- its report is reloaded. A changed input drops the stale
    report; the leg then runs through ``run_sim(resume=True)``, whose own
    fingerprint guard decides what a leftover checkpoint / batch receipt may
    be reused (a mismatch resubmits, never attaches stale results).
    ``run_sim_kwargs`` (e.g. the ``--wait-for-capacity`` shard, #5348) pass
    straight through to ``sim.run_sim``.
    """
    request_text = json.dumps(sim_request, indent=2) + "\n"
    unchanged = (
        _read_text(testbench_path) == testbench_text
        and _read_text(sim_request_path) == request_text
    )
    if resume and unchanged:
        cached = _read_text(sim_report_path)
        if cached is not None:
            try:
                report = json.loads(cached)
            except ValueError:
                report = None
            if isinstance(report, dict) and report_collected(report):
                return report
    if os.path.exists(sim_report_path):
        os.remove(sim_report_path)
    os.makedirs(os.path.dirname(testbench_path), exist_ok=True)
    with open(testbench_path, "w", encoding="utf-8") as handle:
        handle.write(testbench_text)
    with open(sim_request_path, "w", encoding="utf-8") as handle:
        handle.write(request_text)
    report = sim.run_sim(
        sim_request_path,
        artifacts_dir=sim_dir,
        backend=backend,
        resume=True if resume else None,
        **run_sim_kwargs,
    )
    with open(sim_report_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return report
