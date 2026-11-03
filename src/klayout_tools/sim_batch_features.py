"""Name the request feature(s) a batch runner predates (issue #4863).

Split out of :mod:`klayout_tools.sim_batch` (size ratchet, #2766). Everything
here is additive: it only adds ``missing_features`` (and the two klt versions)
to a per-corner diagnostic, and only when the runner-identity report names a
non-empty ``missing`` list.
"""

from __future__ import annotations

from typing import Any


def missing_features(identity: dict[str, Any]) -> list[str]:
    """The request-feature keys the runner predates (``[]`` when none, or
    when the runner version is unknown and ``missing`` is ``null``)."""
    report = identity.get("runner_request_features")
    missing = report.get("missing") if isinstance(report, dict) else None
    return list(missing) if missing else []


def gap_note(identity: dict[str, Any]) -> str:
    """Note suffix naming the missing feature(s); ``""`` when there are none."""
    missing = missing_features(identity)
    if not missing:
        return ""
    return (
        f"; the runner (klt {identity.get('runner_klt_version')}) predates "
        f"the request feature(s) {', '.join(missing)} -- update the "
        "runner image"
    )


def annotate_gap(
    diagnostic: dict[str, Any], identity: dict[str, Any], *, versions: bool = False
) -> None:
    """Add ``missing_features`` when the runner predates any; add the runner
    and client klt versions when it does, or when ``versions`` is set."""
    missing = missing_features(identity)
    if missing:
        diagnostic["missing_features"] = missing
    if missing or versions:
        diagnostic["runner_klt_version"] = identity.get("runner_klt_version")
        diagnostic["client_klt_version"] = identity.get("client_klt_version")
