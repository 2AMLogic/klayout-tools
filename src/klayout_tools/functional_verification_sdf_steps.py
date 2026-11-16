"""SDF-normalization pipeline steps for :func:`run_functional_verification`
that need nothing from ``functional_verification`` itself.

Split out of ``functional_verification.py`` / ``functional_verification_sdf.py``
because the per-file size ratchet (``scripts/check_file_size_baseline.py``)
lets those files only shrink; a new step lands here instead (issue #5296).
"""

from __future__ import annotations

import os
import re

from .functional_verification_sdf import (
    _cut_sdf_spans,
    _normalize_sdf_header_triples,
    _read_sdf_text,
    _sdf_cell_celltype,
    _sdf_cell_entry_removals,
    _sdf_iopath_arc,
)

#: The ``environment.sdf.dropped`` class counting ``COND``-wrapped ``IOPATH``
#: entries removed before annotation because their condition is an operator
#: expression (``~D``, ``A & ~B``) that Icarus's SDF parser rejects as
#: ``Invalid/malformed COND``, failing the whole annotation (issue #5296).
#: Carries an ``arcs`` list like :data:`SDF_BUS_BIT_MODPATH_DROPPED_CLASS`.
SDF_COND_EXPRESSION_DROPPED_CLASS = "iopath_cond_expression"

#: ``SDF_BENIGN_DIAGNOSTIC_REASONS`` entry for the class above, merged into
#: that dict by ``functional_verification``.
SDF_COND_EXPRESSION_REASONS: dict[str, str] = {
    SDF_COND_EXPRESSION_DROPPED_CLASS: (
        "Icarus Verilog's SDF parser accepts only a bare signal or a simple "
        "equality as a conditional-path condition; an operator expression "
        "(e.g. '(COND ~CLK&~D&SETN (IOPATH ...))', OpenSTA's write_sdf "
        "shape for state-dependent arcs) is 'Invalid/malformed COND' and "
        "aborts the whole annotation. Those COND-wrapped IOPATH entries were "
        "removed before annotation and counted here, and 'arcs' names every "
        "affected cell arc. The cell's unconditional arcs are kept, but the "
        "state-dependent delay detail of the dropped arcs is NOT represented "
        "in this run. Simple 'COND D' / 'COND D==1'b1' entries are left in "
        "place"
    ),
}


def _apply_sdf_header_normalization(sdf_path: str, output_dir: str) -> str:
    """Issue #1880's always-on first step of
    :func:`run_functional_verification`'s SDF-normalization pipeline: fill
    the empty member of any ``VOLTAGE``/``TEMPERATURE``/``PROCESS`` header
    triple (OpenSTA's ``write_sdf`` writes them as ``min::max``) so
    ``iverilog -T typ`` does not reject the header with ``Chosen value not
    defined``. See :func:`_normalize_sdf_header_triples`.

    Returns ``sdf_path`` unchanged when the header needed nothing (a run on
    such an SDF is byte-identical to before), else the normalized-copy path.
    Nothing is dropped, so no ``environment.sdf.dropped`` class is recorded:
    Icarus selects no delay from these fields. A separate function for the
    same cyclomatic-complexity reason ``_apply_sdf_physical_only_drop``'s
    docstring records.
    """
    normalized = _normalize_sdf_header_triples(sdf_path)
    if normalized is None:
        return sdf_path
    new_path = os.path.join(output_dir, "klt_sdf_header_normalized.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(normalized)
    return new_path


# --------------------------------------------------------------------------- #
# Issue #5296: a `COND`-wrapped `IOPATH` whose condition is an expression.
#
# Icarus 13.0's SDF parser accepts only a bare signal (`COND D`) or a simple
# equality (`COND D==1'b1`) as a conditional-path condition. Any operator form
# -- `~D`, `!D`, `D&SETN`, `(~D & SETN)`, OpenSTA's `~CLK&~D&SETN` -- is
# `Invalid/malformed COND`, which then cascades into `Invalid/malformed delay
# type` and `Invalid DELAYFILE format`, failing the entire annotation
# (reproduced live against Icarus 13.0). The conditional arc is dropped and
# counted; the cell's unconditional arcs are kept. Rewriting the condition is
# rejected: Icarus binds a conditional arc by textual match with the Verilog
# `specify` `if`, so a rewrite risks silently binding the wrong arc.
# --------------------------------------------------------------------------- #

#: Characters that make an SDF ``COND`` condition an expression Icarus rejects.
_SDF_COND_EXPRESSION_CHARS_RE = re.compile(r"[~!&|^()]")

#: ``(COND <condition> (IOPATH ...))``: the condition text is group 1.
_SDF_COND_IOPATH_RE = re.compile(r"\(\s*COND\b(.*?)\(\s*IOPATH\b", re.DOTALL)


def _sdf_cond_iopath_arc(entry_text: str) -> tuple[str, str] | None:
    """``(input port, output port)`` of a ``COND``-wrapped ``IOPATH`` entry
    whose condition is an operator expression Icarus rejects (issue #5296),
    or ``None`` for any other entry (bare, simple-condition, non-IOPATH)."""
    head = _SDF_COND_IOPATH_RE.match(entry_text.lstrip())
    if head is None or not _SDF_COND_EXPRESSION_CHARS_RE.search(head.group(1)):
        return None
    inner = entry_text.lstrip()[head.end() - len("(IOPATH") :]
    return _sdf_iopath_arc(inner)


def _drop_sdf_cond_expression_iopaths(
    sdf_path: str,
) -> tuple[str, int, list[dict[str, str]]] | None:
    """Remove every ``COND``-wrapped ``IOPATH`` entry in ``sdf_path`` whose
    condition is an operator expression Icarus rejects (issue #5296),
    returning ``(text with those entries removed, how many, the distinct
    dropped arcs)`` -- or ``None`` when there is nothing to remove.

    Decided from the SDF text alone (the failure is a parse error, not a
    model mismatch). Simple ``COND D`` / ``COND D==1'b1`` entries, which
    annotate today, and all unconditional arcs are left in place.
    """
    text = _read_sdf_text(sdf_path)
    if text is None or "COND" not in text:
        return None

    arcs: set[tuple[str, str, str]] = set()
    removed_spans: list[tuple[int, int]] = []
    dropped = 0
    for cell_match in re.finditer(r"\(CELL\b", text):
        celltype = _sdf_cell_celltype(text, cell_match.start()) or ""

        def _droppable(entry_text: str, _cell=celltype) -> bool:
            arc = _sdf_cond_iopath_arc(entry_text)
            if arc is None:
                return False
            arcs.add((_cell, *arc))
            return True

        spans, count = _sdf_cell_entry_removals(text, cell_match.start(), _droppable)
        removed_spans.extend(spans)
        dropped += count

    if not dropped:
        return None
    return (
        _cut_sdf_spans(text, removed_spans),
        dropped,
        [{"celltype": c, "from": s, "to": d} for c, s, d in sorted(arcs)],
    )


def _apply_sdf_cond_expression_drop(
    annotate_source_path: str,
    output_dir: str,
    sdf_pre_dropped_counts: dict[str, int],
    sdf_pre_dropped_arcs: dict[str, list[dict[str, str]]],
) -> str:
    """Issue #5296's ``COND``-expression ``IOPATH`` drop, applied as one step
    of the SDF-normalization pipeline. Returns the path the next step should
    read -- ``annotate_source_path`` unchanged when nothing was dropped.
    Count and arcs are recorded into the two dicts in place, as in
    :func:`_apply_sdf_bus_bit_modpath_drop`."""
    cond_drop = _drop_sdf_cond_expression_iopaths(annotate_source_path)
    if cond_drop is None:
        return annotate_source_path
    cond_text, cond_count, cond_arcs = cond_drop
    new_path = os.path.join(output_dir, "klt_sdf_cond_expression_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(cond_text)
    sdf_pre_dropped_counts[SDF_COND_EXPRESSION_DROPPED_CLASS] = cond_count
    sdf_pre_dropped_arcs[SDF_COND_EXPRESSION_DROPPED_CLASS] = cond_arcs
    return new_path
