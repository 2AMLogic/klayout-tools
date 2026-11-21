"""The shared verdict gate for pipeline stages that consume a ``klt equiv``
report (issue #6852).

``synthesize``'s and ``techmap``'s ``verify_equivalence`` gates each hand a
just-returned :func:`klayout_tools.equiv.run_equiv` report to
:func:`require_equivalence`: every status other than ``"equivalent"`` is a
hard failure in the caller's own error class, and an ``"equivalent"``
verdict is projected down to the same six-field summary both stages attach
as their response's ``equivalence`` field. Each caller supplies its own
message prefixes, so the wording that names the pair of netlists compared
stays per-command (synthesis checks a netlist against its source RTL,
technology mapping against the pre-mapping generic netlist) while the
counterexample detail, diagnostic fallback, and proof-log pointer are
formatted identically.

Defined here (not in ``equiv.py``) for the same reason ``equiv_errors.py``
hosts :class:`~klayout_tools.equiv_errors.EquivError` (#4032): sibling
modules keep ``equiv.py`` -- pinned at its ``file-size-baseline.json``
ceiling -- from growing, and this module needs nothing from ``equiv``
itself (it consumes a report already returned).
"""

from __future__ import annotations

from typing import Any, TypeVar

#: The caller-supplied exception class :func:`require_equivalence` raises.
E = TypeVar("E", bound=Exception)


def require_equivalence(
    equiv_report: dict[str, Any],
    *,
    error_cls: type[E],
    counterexample_prefix: str,
    no_verdict_prefix: str,
) -> dict[str, Any]:
    """Gate an already-returned ``klt equiv`` report: raise ``error_cls``
    for every status other than ``"equivalent"``, else project the report
    down to the six-field summary (``status``, ``engine``,
    ``engine_version``, ``timeout_s``, ``elapsed_s``, ``artifacts``) both
    pipeline stages report as their ``equivalence`` field.

    ``counterexample_prefix`` starts the message for a proven
    ``"counterexample"`` (synthesize: ``"synthesized netlist is NOT
    equivalent to its source RTL"``); ``no_verdict_prefix`` starts it for
    every other non-``"equivalent"`` verdict -- an ``"inconclusive"``
    solver/process timeout first among them (synthesize: ``"equivalence
    check against source RTL"``), which is never treated as a pass.
    :func:`_verdict_failure_message` formats the rest identically for every
    caller. The report itself is never re-run or re-derived here: request
    construction, execution, and artifact placement stay with each caller.
    """
    status = equiv_report["status"]
    if status != "equivalent":
        raise error_cls(
            _verdict_failure_message(
                status,
                equiv_report,
                counterexample_prefix=counterexample_prefix,
                no_verdict_prefix=no_verdict_prefix,
            )
        )
    return {
        "status": status,
        "engine": equiv_report["engine"],
        "engine_version": equiv_report["engine_version"],
        "timeout_s": equiv_report["timeout_s"],
        "elapsed_s": equiv_report["elapsed_s"],
        "artifacts": equiv_report["artifacts"],
    }


def _verdict_failure_message(
    status: str,
    equiv_report: dict[str, Any],
    *,
    counterexample_prefix: str,
    no_verdict_prefix: str,
) -> str:
    """An actionable ``error_cls`` message for a non-``"equivalent``
    verdict -- names the diverging outputs for a proven ``"counterexample"``
    (or the fallback when none are reported), or the first diagnostic for
    every other verdict (or its fallback), and points at the proof log when
    one exists."""
    log_path = equiv_report.get("artifacts", {}).get("log_path")
    if status == "counterexample":
        counterexample = equiv_report.get("counterexample") or {}
        diverging = counterexample.get("diverging_outputs") or []
        confirmed = counterexample.get("confirmed_by_simulation")
        detail = (
            f"diverging outputs: {', '.join(diverging)}"
            if diverging
            else "no diverging outputs reported"
        )
        message = (
            f"{counterexample_prefix} "
            f"(klt equiv reported 'counterexample'; {detail}; "
            f"confirmed_by_simulation={confirmed})"
        )
    else:
        diagnostics = equiv_report.get("diagnostics") or []
        detail = diagnostics[0]["message"] if diagnostics else "no diagnostic detail"
        message = (
            f"{no_verdict_prefix} did not reach a verdict "
            f"(klt equiv reported '{status}'): {detail}"
        )
    if log_path:
        message += f" -- see {log_path} for the full proof"
    return message
