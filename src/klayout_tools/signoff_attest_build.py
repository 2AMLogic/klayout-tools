"""``klt signoff --attest N --check ENV [--require-build-match]`` (issue #5875).

``--check`` excludes ``provenance.klt_version`` from its diff, so an envelope
produced by another ``klt`` build passes silently. This attaches the same
report-only ``build_drift`` marker envelope aggregation carries (issue #4223)
and, opt-in, turns a ``stale`` marker into a drift. Split out of
:mod:`.signoff_attest` for the file-size ratchet.
"""

from __future__ import annotations

from typing import Any

from .signoff import _BUILD_DRIFT_STALE, _build_drift
from .signoff_attest import _load_committed_attestation

BUILD_DRIFT_FIELD = "provenance.klt_version"


def add_require_build_match_argument(parser: Any) -> None:
    """Register ``--require-build-match`` on the ``klt signoff`` parser."""
    parser.add_argument(
        "--require-build-match",
        action="store_true",
        help=(
            "with --attest and --check (issue #5875): also fail (exit 3, status "
            "'drifted') when the committed envelope's provenance.klt_version "
            "names a different klt build than the one running. A missing or "
            "+unknown version is 'unverifiable' and never fails. Without it "
            "build_drift is report-only."
        ),
    )


def flag_problem(args: Any) -> str | None:
    """The refusal message for ``--require-build-match`` without ``--attest``
    and ``--check`` (``--attest`` presence is checked by the caller)."""
    if (
        getattr(args, "require_build_match", False)
        and getattr(args, "check", None) is None
    ):
        return "--require-build-match only applies to --attest with --check"
    return None


def attach_build_drift(
    result: dict[str, Any], committed_path: str, require: bool
) -> None:
    """Add ``build_drift`` to a ``--check`` result; with ``require`` and a
    ``stale`` state, record a ``provenance.klt_version`` drift and set
    ``status`` to ``drifted``."""
    committed = _load_committed_attestation(committed_path)
    drift = _build_drift(committed.get("provenance"))
    result["build_drift"] = drift
    if require and drift["state"] == _BUILD_DRIFT_STALE:
        result["status"] = "drifted"
        result["drift"].append(
            {
                "field": BUILD_DRIFT_FIELD,
                "committed": drift["cited_klt_version"],
                "fresh": drift["running_klt_version"],
            }
        )


def build_drift_line(result: dict[str, Any]) -> str:
    return f"build_drift: {result['build_drift']['state']}"
