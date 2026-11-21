"""Off-host ``expr`` runner-staleness hint (issue #4097).

Split out of ``sim.py`` to keep its file-size ratchet honest.
"""

from __future__ import annotations

from typing import Any

from .sim_adapt import OFFHOST_BACKENDS

EXPR_RUNNER_MAYBE_OLD_CODE = "expr_runner_may_predate"


def attach_expr_runner_hint(
    report: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    backend: str | None,
) -> None:
    """Additive/optional (issue #4097): on an off-host run, a
    ``measurements[].expr`` measurement that came back with no value may mean
    the runner image predates ``expr`` (its version compare is major.minor
    only, so the pre-launch check cannot prove it). Name that suspect beside
    the per-measurement "produced no value"."""
    if backend not in OFFHOST_BACKENDS:
        return
    expr_names = {
        spec["name"]
        for spec in measurements_spec
        if isinstance(spec, dict) and spec.get("expr") is not None and "name" in spec
    }
    if not expr_names:
        return
    silent = sorted(
        {
            m["name"]
            for corner in corners
            for m in corner.get("measurements") or []
            if m.get("name") in expr_names and m.get("value") is None
        }
    )
    if not silent:
        return
    report.setdefault("diagnostics", []).append(
        {
            "code": EXPR_RUNNER_MAYBE_OLD_CODE,
            "severity": "warning",
            "message": (
                f"measurements[].expr measurement(s) {', '.join(silent)} "
                "produced no value on the off-host runner, which may predate "
                "`expr` (runner may predate expr; compare "
                "environment.remote.runner_klt_version with the client "
                "version and rebuild the runner image if older)"
            ),
            "measurements": silent,
        }
    )
