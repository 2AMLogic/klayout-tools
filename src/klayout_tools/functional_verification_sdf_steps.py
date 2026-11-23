"""SDF-normalization pipeline steps for :func:`run_functional_verification`
using shared settings and diagnostics from ``functional_verification_base``.

Split out of ``functional_verification.py`` / ``functional_verification_sdf.py``
because the per-file size ratchet (``scripts/check_file_size_baseline.py``)
lets those files only shrink; a new step lands here instead (issue #5296).
"""

from __future__ import annotations

import os
import re
from typing import Any

from .functional_verification_base import (
    SDF_ALIAS_PORT_DROPPED_CLASS,
    FunctionalVerificationError,
)
from .functional_verification_sdf import (
    _SDF_BUS_PORT_TOKEN_RE,
    _SDF_INTERCONNECT_HEAD_RE,
    _collect_sdf_alias_port_bits,
    _cut_sdf_spans,
    _drop_sdf_zero_delay_alias_port_interconnects,
    _drop_sdf_zero_delay_bus_port_source_interconnects,
    _normalize_sdf_header_triples,
    _read_sdf_text,
    _sdf_cell_celltype,
    _sdf_cell_entry_removals,
    _sdf_interconnect_is_zero_delay,
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


def _apply_sdf_alias_port_drop(
    annotate_source_path: str,
    output_dir: str,
    sources: list[str],
    hdl_toplevel: str,
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #2285's zero-delay alias-port ``INTERCONNECT`` drop, one step
    of :func:`run_functional_verification`'s SDF-normalization pipeline --
    the code of that function's own inline alias block, moved out to this
    module unchanged (its two sibling drops already live as helpers there
    for the same reason: no new branch in the already-baselined function,
    which the per-file size ratchet also caps at its current line count).

    An ``INTERCONNECT`` onto a top-level port bit the netlist drives
    through a Verilog ``assign`` alias cannot be annotated by Icarus at
    all and cannot be made annotatable by rewriting the endpoint either;
    when such an entry is zero-delay at every corner, dropping it cannot
    change simulated timing, so it is removed here and counted under
    :data:`SDF_ALIAS_PORT_DROPPED_CLASS`. A non-zero entry is left in
    place and still fails the diagnostic gate loudly. Returns the path
    the next step should read -- ``annotate_source_path`` unchanged when
    nothing was dropped.
    """
    alias_drop = _drop_sdf_zero_delay_alias_port_interconnects(
        annotate_source_path,
        _collect_sdf_alias_port_bits(sources, hdl_toplevel),
    )
    if alias_drop is None:
        return annotate_source_path
    alias_text, alias_dropped_count = alias_drop
    new_path = os.path.join(output_dir, "klt_sdf_alias_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(alias_text)
    sdf_pre_dropped_counts[SDF_ALIAS_PORT_DROPPED_CLASS] = alias_dropped_count
    return new_path


# --------------------------------------------------------------------------- #
# Issue #2984's pipeline step. Moved verbatim out of functional_verification.py
# (whose per-file size ratchet lets it only shrink -- issue #6075's step below
# it in the pipeline needed the headroom).
# --------------------------------------------------------------------------- #

#: ``INTERCONNECT`` entries removed before annotation because they run from a
#: bit-selected top-level input port (any destination not already claimed by
#: :data:`SDF_PHYSICAL_ONLY_DROPPED_CLASS`) and carry zero delay at every
#: corner. Icarus can fail to insert an intermodpath for such a source
#: (``Could not find intermodpath!``); an all-zero entry models no delay, so
#: dropping it cannot change simulated timing. Issue #2984's counted
#: exemption; a non-zero entry of the same shape is left in place and still
#: fails loudly. Pre-dropped from the SDF text, not derived from a
#: diagnostic substring.
SDF_BUS_PORT_SOURCE_DROPPED_CLASS = "zero_delay_bus_port_source_interconnect"


def _apply_sdf_bus_port_source_drop(
    annotate_source_path: str,
    output_dir: str,
    ports: list[tuple[str, str, str]],
    sdf_pre_dropped_counts: dict[str, int],
) -> str:
    """Issue #2984's zero-delay bit-selected-input-source SDF-text drop, the
    sibling of :func:`_apply_sdf_physical_only_drop` and kept as its own
    function for the same reason (no new branch in
    :func:`run_functional_verification`). Runs after the physical-only pass,
    so entries that pass claimed keep their class and are not double counted.
    Counts under :data:`SDF_BUS_PORT_SOURCE_DROPPED_CLASS`.

    Returns ``annotate_source_path`` unchanged when nothing was dropped, or
    a new normalized-copy path otherwise.
    """
    drop = _drop_sdf_zero_delay_bus_port_source_interconnects(
        annotate_source_path,
        {name for direction, width, name in ports if width and direction == "input"},
    )
    if drop is None:
        return annotate_source_path
    text, count = drop
    new_path = os.path.join(output_dir, "klt_sdf_bus_port_source_dropped.sdf")
    with open(new_path, "w", encoding="utf-8") as handle:
        handle.write(text)
    sdf_pre_dropped_counts[SDF_BUS_PORT_SOURCE_DROPPED_CLASS] = count
    return new_path


def _sdf_refusal_attribution(
    annotate_paths: list[str], ports: list[tuple[str, str, str]]
) -> dict[str, Any]:
    """The annotation context :func:`run_functional_verification` stashes
    into its resolved ``sdf`` option dict so a later refusal can attribute
    intermodpath rejections (issue #5769): the files actually handed to
    ``$sdf_annotate``, in call order (normalized/split copies, never the
    request's own path once any pipeline step rewrote it), and the bus
    *input* port names -- the one source shape the #5769 hint is bounded
    to. Internal keys of that dict only: the report's ``environment.sdf``
    is built field-by-field, so these never reach the JSON contract.
    """
    return {
        "annotate_paths": list(annotate_paths),
        "bus_input_ports": {
            name for direction, width, name in ports if width and direction == "input"
        },
    }


# --------------------------------------------------------------------------- #
# Issue #5769: attributing the `Could not find intermodpath!` refusal.
#
# A NON-zero INTERCONNECT entry whose source is a bit-selected top-level
# input port on a fanout-above-1 net is rejected by Icarus 13.0 (shape (b)
# of the #1619/#5705 spike: `$sdf_annotate` inserts at most one interconnect
# modpath per such port net, so only the last-declared instance's
# destination annotates). The recorded decision (issue #5769, attended
# `/fleet:decide` round 20261116-r15) keeps that a hard failure -- every
# `dropped` class is bounded to zero-delay entries, and these carry real
# delay -- so this section only *explains* the refusal: it names the
# rejected arcs and the netlist-buffer workaround #5705 measured.
#
# Attribution resolves the transcript's own `SDF ERROR: <file>:<line>:`
# locators against the annotation files of :func:`_sdf_refusal_attribution`.
# The mapping (measured live on Icarus 13.0): the named file is the file of
# the `$sdf_annotate` call that failed, but the line counter is CUMULATIVE
# across calls -- with #1619's split, an entry on line 10 of the deferred
# file is reported as line 26 when the safe file carries 16 lines. A
# locator resolves by subtracting the earlier calls' total line counts, and
# anything that does not land cleanly on a one-line, paren-balanced,
# non-zero bit-selected-input INTERCONNECT entry is left unnamed: the
# refusal then keeps its exact pre-#5769 text, because attribution is
# best-effort, never a guess. The survivor arc (the last-declared
# instance's destination, which annotates and emits no diagnostic) can
# never be named -- attribution keys on the observed rejection, not on the
# SDF's entry list.
# --------------------------------------------------------------------------- #

#: The actionable diagnostic this section attributes: the SDF ERROR marker,
#: the ``<file>:<line>:`` locator, and Icarus's fixed message for a
#: rejected interconnect modpath insertion. The companion ``VPI error:
#: Could not insert intermodpath!`` line carries no locator and is
#: ignored -- the SDF ERROR line already names the same rejection.
_SDF_INTERMODPATH_ERROR_RE = re.compile(
    r"^SDF ERROR: (.+?):(\d+): Could not find intermodpath!$"
)


def _sdf_annotation_lines(
    annotate_paths: list[str],
) -> list[tuple[str, list[str]]]:
    """``(path, lines)`` per readable file of ``annotate_paths``, in
    ``$sdf_annotate`` call order. An unreadable path is skipped: whatever
    the simulator read from it, this refusal cannot resolve it now, and
    skipping keeps every later file's cumulative offset honest."""
    annotated: list[tuple[str, list[str]]] = []
    for path in annotate_paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                annotated.append((path, handle.read().splitlines()))
        except OSError:
            continue
    return annotated


def _sdf_located_entry_line(
    locator_path: str,
    locator_line: int,
    annotated: list[tuple[str, list[str]]],
) -> str | None:
    """The stripped text of the entry line the diagnostic locator names, or
    ``None`` when it does not resolve onto one of ``annotated``. Each
    file's own line number is the reported one minus the total lines of
    every earlier call's file (Icarus's counter is cumulative, never reset
    per file), and a line out of that file's range resolves nothing."""
    offset = 0
    for path, lines in annotated:
        if os.path.basename(locator_path) == os.path.basename(path) or (
            os.path.normpath(locator_path) == os.path.normpath(path)
        ):
            own_line = locator_line - offset
            if 1 <= own_line <= len(lines):
                return lines[own_line - 1].strip()
            return None
        offset += len(lines)
    return None


def _sdf_intermodpath_rejected_arcs(
    diagnostics: list[str],
    annotate_paths: list[str],
    bus_input_ports: set[str],
) -> list[str]:
    """Every distinct ``<source> → <destination>`` arc an intermodpath
    diagnostic confirms as rejected, in transcript order -- each resolved
    locator must land on a one-line ``INTERCONNECT`` entry (paren-balanced,
    so a multi-line entry's truncated head is never half-read) whose source
    is a bit-selected top-level input port in ``bus_input_ports`` and whose
    delay is non-zero somewhere. Zero-delay entries are never named:
    #2984's drop class owns those. See the comment block above for why an
    unresolved or ambiguous locator contributes nothing."""
    annotated = _sdf_annotation_lines(annotate_paths)
    arcs: list[str] = []
    for diagnostic in diagnostics:
        match = _SDF_INTERMODPATH_ERROR_RE.match(diagnostic)
        if match is None:
            continue
        entry = _sdf_located_entry_line(match.group(1), int(match.group(2)), annotated)
        head = _SDF_INTERCONNECT_HEAD_RE.match(entry) if entry else None
        port = _SDF_BUS_PORT_TOKEN_RE.match(head.group(1)) if head else None
        if (
            entry is None
            or head is None
            or port is None
            or port.group(1) not in bus_input_ports
            or entry.count("(") != entry.count(")")
            or _sdf_interconnect_is_zero_delay(entry)
        ):
            continue
        arc = f"{head.group(1)} → {head.group(2)}"
        if arc not in arcs:
            arcs.append(arc)
    return arcs


def _sdf_intermodpath_hint(sdf: dict[str, Any], diagnostics: list[str]) -> str:
    """The refusal-message suffix naming the confirmed-rejected non-zero
    bit-selected-input arcs (issue #5769), or ``""`` when the resolved
    ``sdf`` option carries no annotation context (a run that never reached
    the annotate stage, or a hand-built dict) or no diagnostic resolved
    onto such an arc -- the message then keeps its exact pre-#5769 text,
    which is also why the suffix is appended, never spliced in: the
    message prefix and the JSON ``error`` shape stay byte-identical for
    every unrelated or unresolved refusal."""
    annotate_paths = sdf.get("annotate_paths")
    bus_input_ports = sdf.get("bus_input_ports")
    if not isinstance(annotate_paths, list) or not isinstance(bus_input_ports, set):
        return ""
    arcs = _sdf_intermodpath_rejected_arcs(diagnostics, annotate_paths, bus_input_ports)
    if not arcs:
        return ""
    named = ", ".join(arcs[:5])
    more = f" (+{len(arcs) - 5} more)" if len(arcs) > 5 else ""
    return (
        f" Confirmed rejected non-zero INTERCONNECT arc(s) from a "
        f"bit-selected top-level input port: {named}{more}. Icarus's "
        "$sdf_annotate inserts at most one interconnect modpath per such "
        "port net -- on physical fanout above 1 it annotates only the "
        "last-declared instance's destination and rejects every other "
        "load, so these entries cannot annotate in any order (issue "
        "#5705). Workaround, measured in #5705: insert a buffer on that "
        "net in the netlist so the regenerated SDF names the buffer's "
        "output pin as the INTERCONNECT source instead of the port bit "
        "-- every entry on the net then applies. These entries carry "
        "real delay, so they stay a hard failure rather than a dropped "
        "class: a drop must never change simulated timing."
    )


def _sdf_refusal_error(
    sdf: dict[str, Any],
    engine: str,
    diagnostics: list[str],
    dropped_report: dict[str, dict[str, Any]],
) -> FunctionalVerificationError:
    """The exception both SDF-diagnostic refusal sites raise (issue #5854).

    The message is the one both sites used to build inline, unchanged
    before the #5769 attribution suffix :func:`_sdf_intermodpath_hint`
    appends when a diagnostic resolves onto a rejected non-zero
    bit-selected-input arc. The caller passes the *resolved* ``sdf``
    option dict, not just its ``file`` member: attribution reads the
    annotation context :func:`_sdf_refusal_attribution` stashed into that
    dict (internal keys; the report's ``environment.sdf`` is built
    field-by-field and never sees them). The caller also passes the
    already-built ``environment.sdf.dropped``-shaped report -- built by
    ``functional_verification._sdf_dropped_report``, which this module
    cannot import at module level (that module imports this one) -- and it
    is attached as the exception's ``details`` attribute only when at
    least one class was dropped: a refusal with an empty accounting keeps
    the exact attribute set every other refusal path's error has. The CLI
    merges ``details`` flat into ``error``: the path is
    ``error.sdf.dropped``.
    """
    shown = " | ".join(diagnostics[:5])
    more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
    error = FunctionalVerificationError(
        f"SDF back-annotation of '{sdf['file']}' did not fully apply: "
        f"{len(diagnostics)} diagnostic(s) in the {engine} transcript "
        f"-- {shown}{more}. The simulator exits 0 after skipping an "
        "annotation it cannot apply, so this run's verdict would have "
        "been a zero-delay one reported as if it were annotated."
        + _sdf_intermodpath_hint(sdf, diagnostics)
    )
    if dropped_report:
        error.details = {"sdf": {"dropped": dropped_report}}
    return error
