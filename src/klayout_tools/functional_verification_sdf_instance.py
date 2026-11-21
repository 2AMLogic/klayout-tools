"""Issue #5002: reject an SDF ``CELL`` whose ``(INSTANCE ...)`` names an
instance the netlist does not have, before ``vvp`` starts.

Measured live on cocotb 2.1.0 + Icarus 13.0 through ``klt
functional-verification`` with a flat netlist, ``(DIVIDER /)`` and a top
``CELL`` spelled ``(CELLTYPE "top") (INSTANCE top)`` (the design name used
as the root instance) carrying ordinary ``INTERCONNECT`` entries: the run
aborts at the start of the first test with ``ERROR: NULL handle passed to
vpi_scan.`` / ``Assertion `0' failed`` (rc -6). The same SDF with the
design root spelled ``(INSTANCE)`` annotates and passes. The entries'
endpoints are irrelevant -- they resolve fine -- so the existing endpoint
guard (#3740) and the empty-``INSTANCE`` mis-scope guard (#4521) never see
it.
"""

from __future__ import annotations

import re


def _sdf_unresolvable_cell_instances(
    text: str, names: set[str], divider: str
) -> list[tuple[int, str]]:
    """``(line, instance path)`` for each ``CELL`` that carries
    ``INTERCONNECT`` entries but whose ``INSTANCE`` path starts at no
    instance in ``names``."""
    from .functional_verification_sdf import (
        _sdf_interconnect_entries,
        _sdf_split_unescaped,
    )

    offenders: dict[str, int] = {}
    for cell_instance, entry_start, _entry in _sdf_interconnect_entries(text):
        if cell_instance in ("", "*") or cell_instance in offenders:
            continue
        parts = _sdf_split_unescaped(cell_instance, divider)
        if parts and parts[0] not in names:
            offenders[cell_instance] = text.count("\n", 0, entry_start) + 1
    return [(line, instance) for instance, line in offenders.items()]


def _reject_sdf_unresolvable_cell_instance(
    sdf_path: str,
    source_paths: list[str],
    hdl_toplevel: str,
    scope: str | None = None,
) -> None:
    """Raise ``FunctionalVerificationError`` when a ``CELL`` with
    ``INTERCONNECT`` entries is scoped to an instance that is not in the
    annotation scope's module (issue #5002).

    Conservative like its #3740 sibling: a no-op when the scope module's
    instance list cannot be derived, when the ``DIVIDER`` is not ``.``/``/``,
    and for the design's own root cell (empty or ``*`` ``INSTANCE``).
    """
    from .functional_verification import FunctionalVerificationError
    from .functional_verification_sdf import (
        _find_gate_level_instances,
        _read_verilog_source_texts,
        _resolve_sdf_scope_module,
    )

    try:
        with open(sdf_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return
    source_texts = _read_verilog_source_texts(source_paths)
    scope_module = _resolve_sdf_scope_module(source_texts, hdl_toplevel, scope)
    if scope_module is None:
        return
    names = {
        str(instance["name"])
        for instance in _find_gate_level_instances(source_texts, scope_module)
    }
    if not names:
        return
    divider_match = re.search(r"\(DIVIDER\s+(\S+?)\)", text)
    divider = divider_match.group(1) if divider_match else "."
    if divider not in (".", "/"):
        return

    offenders = _sdf_unresolvable_cell_instances(text, names, divider)
    if not offenders:
        return
    shown = "; ".join(f"line {line}: (INSTANCE {path})" for line, path in offenders[:3])
    raise FunctionalVerificationError(
        "options.sdf: this SDF has a CELL with INTERCONNECT entries whose "
        f"INSTANCE names an instance that is not in '{scope_module}' "
        f"({shown}{'; ...' if len(offenders) > 3 else ''}; "
        f"{len(offenders)} unresolvable CELL scope(s)). Icarus Verilog "
        "cannot report such a scope: its $sdf_annotate resolver aborts vvp "
        "outright ('ERROR: NULL handle passed to vpi_scan.', SIGABRT). "
        "Write the design's own root cell with an empty (INSTANCE) -- not "
        "the design name -- or regenerate the SDF from the same netlist the "
        "simulation reads"
    )


def _reject_sdf_unresolvable_scopes(
    sdf_path: str,
    source_paths: list[str],
    hdl_toplevel: str,
    scope: str | None = None,
) -> None:
    """Run both pre-launch unresolvable-instance guards: the INTERCONNECT
    endpoint check (#3740) and the CELL ``INSTANCE`` check (#5002)."""
    from .functional_verification_sdf import (
        _reject_sdf_unresolvable_interconnect_instances,
    )

    _reject_sdf_unresolvable_interconnect_instances(
        sdf_path, source_paths, hdl_toplevel, scope
    )
    _reject_sdf_unresolvable_cell_instance(sdf_path, source_paths, hdl_toplevel, scope)
