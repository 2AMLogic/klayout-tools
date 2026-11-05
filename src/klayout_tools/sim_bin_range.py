"""Wording and diagnostic builders for the MOS width bin-range heuristic.

The culprit search (``sim._find_width_ceiling_culprit``) stays in
:mod:`klayout_tools.sim`; this module only renders its result, shared by the
post-hoc ``model_bin_range`` message and the non-fatal pre-run
``model_bin_range_preflight`` warning (issue #4976). The selection keys on
``w`` as written: ``nf`` is reported but never multiplied in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

#: ``(instance, model, w_um, nf)`` as returned by the culprit search.
Culprit = tuple[str, str, float, float]


def _cause(culprit: Culprit) -> str:
    _, _, w_um, nf_count = culprit
    return f"w={w_um:g}u" + (f" nf={nf_count:g}" if nf_count > 1 else "")


def message(culprit: Culprit, ceiling_um: float) -> str:
    """Post-hoc wording for ngspice's ``could not find a valid modelname``."""
    inst_name, model_name, _, _ = culprit
    return (
        f'ngspice reported "could not find a valid modelname" -- likely '
        f"cause: instance '{inst_name}' ({model_name}) with {_cause(culprit)} "
        f"exceeds the model card's per-instance width bin range "
        f"(undocumented, ~{ceiling_um:g} um on gf180mcu, keyed on w as "
        "written; nf= does not widen it); use m= to parallel multiple "
        "smaller-width instances instead of a single large w= (see issues "
        "#1214, #4976)"
    )


def preflight_diagnostic(culprit: Culprit, ceiling_um: float) -> dict[str, Any]:
    """Non-fatal pre-run ``model_bin_range_preflight`` warning entry."""
    inst_name, model_name, w_um, nf_count = culprit
    return {
        "severity": "warning",
        "code": "model_bin_range_preflight",
        "message": (
            f"preflight: instance '{inst_name}' ({model_name}) has "
            f"{_cause(culprit)}, at/above the ~{ceiling_um:g} um per-instance "
            "width bin range some PDKs enforce (keyed on w as written; nf= "
            'does not widen it); ngspice may abort with "could not find a '
            'valid modelname". Use m= to parallel smaller-width instances '
            "(see issue #4976)"
        ),
        "instance": inst_name,
        "model": model_name,
        "w_um": w_um,
        "nf": nf_count,
    }


def preflight_for_path(
    netlist_path: str,
    find_culprit: Callable[[str], Culprit | None],
    ceiling_um: float,
) -> dict[str, Any] | None:
    """Scan ``netlist_path``; the diagnostic, or ``None`` (never raises)."""
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            culprit = find_culprit(handle.read())
    except OSError:
        return None
    return None if culprit is None else preflight_diagnostic(culprit, ceiling_um)
