"""Off-host ``expr`` runner-staleness hint (issue #4097).

Split out of ``sim.py`` to keep its file-size ratchet honest.
"""

from __future__ import annotations

from typing import Any

from .sim_adapt import OFFHOST_BACKENDS
from .sim_batch_contract import REQUEST_FEATURE_MIN_RUNNER_VERSION
from .sim_batch_contract_spec import _missing_runner_features

EXPR_RUNNER_MAYBE_OLD_CODE = "expr_runner_may_predate"


_EXPR = "measurements[].expr"


def _supporting_versions(report: dict[str, Any]) -> tuple[str, str] | None:
    """Identify support only when every participating worker has a known version."""
    remote = (report.get("environment") or {}).get("remote")
    if not isinstance(remote, dict):
        return None
    workers = remote.get("fleet", [remote])
    if not isinstance(workers, list) or not workers:
        return None
    clients: set[str] = set()
    runners: set[str] = set()
    for worker in workers:
        if not isinstance(worker, dict):
            return None
        runner = worker.get("runner_klt_version")
        if (
            not isinstance(runner, str)
            or _missing_runner_features([_EXPR], runner) != []
        ):
            return None
        # A development build of the introducing release can precede the feature.
        if runner.startswith(f"{REQUEST_FEATURE_MIN_RUNNER_VERSION[_EXPR]}.dev"):
            return None
        runners.add(runner)
        client = worker.get("client_klt_version") or (
            report.get("provenance") or {}
        ).get("klt_version")
        clients.add(client if isinstance(client, str) else "unknown")
    return ", ".join(sorted(runners)), ", ".join(sorted(clients))


def _first_failure(corners: list[dict[str, Any]], silent: list[str]) -> str:
    prefixes = {f"measurement {name!r}" for name in silent}
    for corner in corners:
        for diagnostic in corner.get("diagnostics") or []:
            message = diagnostic.get("message", "")
            if (
                diagnostic.get("code") == "measurement"
                and message.partition(" produced no value")[0] in prefixes
            ):
                corner_id = corner.get("corner_id", "unknown corner")
                return f" First failure in {corner_id}: {message}"
    return ""


def attach_expr_runner_hint(
    report: dict[str, Any],
    measurements_spec: list[dict[str, Any]],
    corners: list[dict[str, Any]],
    *,
    backend: str | None,
) -> None:
    """Explain missing off-host expressions using runner and corner evidence."""
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
    versions = _supporting_versions(report)
    if versions is not None:
        runner, client = versions
        report.setdefault("diagnostics", []).append(
            {
                "code": "expr_no_value",
                "severity": "warning",
                "message": (
                    f"Off-host runner klt {runner} supports {_EXPR} "
                    f"(client klt {client}). "
                    f"{len(silent)} expression measurement(s) produced no value."
                    + _first_failure(corners, silent)
                ),
                "measurements": silent,
            }
        )
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
