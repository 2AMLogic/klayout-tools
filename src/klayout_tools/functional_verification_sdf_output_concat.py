"""Whole-vector concat rewrite for separately assigned output buses
(issue #7534).

A P&R netlist writer drives a multi-bit top-level output through separate
per-bit assignments -- ``assign y[0] = b0; assign y[1] = b1;`` -- and the
post-route SDF then carries real ``INTERCONNECT`` arcs onto those bits.
Icarus cannot annotate an arc across that join (#6937: zero-delay arcs are
dropped and counted, nonzero ones refuse before the build), while the
equivalent whole-vector spelling ``assign y = {b1,b0};`` annotates fully
(verified live against Icarus 13.0 and proven equivalent with Yosys on a
real P&R netlist, #6937's corrections). This module rewrites the provably
equivalent per-bit spelling into the concat spelling in a build-dir copy
of the source -- the same normalization posture the SDF itself already
gets -- so those arcs annotate without touching the caller's netlist.

The rewrite is strictly gated: a port is rewritten only when every bit of
its declared ``[N:0]`` range is assigned exactly once, each right-hand
side is a bare scalar identifier -- plain or escaped, the ``\\sub.block_any``
spelling a flattening netlist writer emits for every hierarchical net
name; an escaped element keeps its terminating space in the emitted
concat, and its backslash-stripped spelling feeds the self-reference
check -- nothing else in the module references any bit (or the whole
vector) -- so no cell pin drives a bit and no other statement reads one
-- and the SDF carries at least one nonzero ``INTERCONNECT`` onto one of
its bits, keeping every all-zero run on today's
``zero_delay_alias_port_interconnect`` drop. Every other shape is
left exactly as it was, and the typed pre-build refusal still fires for
it (#6937's bound, unchanged).
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._artifact_envelope import hashed_path_envelope as _hashed_path_envelope
from .functional_verification_sdf import (
    _SDF_BUS_PORT_TOKEN_RE,
    _SDF_INTERCONNECT_HEAD_RE,
    _VERILOG_COMMENT_RE,
    _drop_sdf_interconnect_entries,
    _read_sdf_text,
    _sdf_interconnect_is_zero_delay,
)

#: An ``output``/``inout`` declaration of a bus with a plain ``[N:0]``
#: range -- the only width spelling whose concat equivalent this module
#: can build (a ``[0:3]``/``[3:1]`` style range is left to the refusal).
_VERILOG_OUTPUT_BUS_RANGE_RE = re.compile(
    r"\b(?:output|inout)\s+(?:(?:reg|wire|logic|signed)\s+)*"
    r"\[\s*(\d+)\s*:\s*0\s*\]\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*(?:\s*,\s*[A-Za-z_$][A-Za-z0-9_$]*)*)"
)

#: A bare scalar Verilog identifier -- the only right-hand side whose
#: per-bit assign is provably the concat of the same identifiers. An
#: escaped identifier (``\\foo``, whose terminating whitespace ``.strip()``
#: has already removed by the time it is judged) names the same scalar and
#: qualifies too.
_VERILOG_BARE_IDENT_RE = re.compile(r"(?:[A-Za-z_$][A-Za-z0-9_$]*|\\\S+)\Z")


def _canonical_ident(name: str) -> str:
    """The comparison spelling of a bare identifier: an escaped identifier
    (``\\foo``) names the same object as plain ``foo``, so the
    self-reference guard drops the leading backslash before comparing."""
    return name[1:] if name.startswith("\\") else name


def _concat_element(name: str) -> str:
    """The concat spelling of one right-hand-side identifier: an escaped
    identifier extends to the next whitespace, so it must carry a trailing
    space or the following ``,``/``}`` would join its name."""
    return name + " " if name.startswith("\\") else name


def _sdf_buses_with_nonzero_output_bit_arcs(text: str) -> set[str]:
    """Names of the top-scope vector ports that carry at least one nonzero
    ``INTERCONNECT`` destination bit-select (``y[1]`` -- never an instance
    pin, which always contains the divider). Decided on the SDF text alone
    via the shared counted-exemption walker in ``top_only`` mode, with a
    predicate that records and never drops."""
    buses: set[str] = set()

    def _record(entry_text: str) -> bool:
        head = _SDF_INTERCONNECT_HEAD_RE.match(entry_text)
        if head is not None:
            destination = _SDF_BUS_PORT_TOKEN_RE.fullmatch(head.group(2))
            if destination is not None and not _sdf_interconnect_is_zero_delay(
                entry_text
            ):
                buses.add(destination.group(1))
        return False

    _drop_sdf_interconnect_entries(text, _record, top_only=True)
    return buses


def _top_module_span(text: str, hdl_toplevel: str) -> tuple[int, int] | None:
    """``(start, end)`` of the ``hdl_toplevel`` module chunk -- the same
    chunk regex :func:`_collect_sdf_alias_port_bits` matches -- or ``None``
    when the file does not declare it."""
    match = re.search(
        r"\bmodule\s+" + re.escape(hdl_toplevel) + r"(?![\w$]).*?\bendmodule\b",
        text,
        re.DOTALL,
    )
    return (match.start(), match.end()) if match else None


def _bus_declared_top_bit(stripped: str, bus: str) -> int | None:
    """The ``N`` of ``bus``'s single ``[N:0]`` output declaration in the
    comment-stripped module chunk, or ``None`` when it is not declared
    exactly once with that range shape (or is a ``[0:0]`` scalar-width
    bus, which has no separate-bit spelling worth rewriting)."""
    tops = [
        int(match.group(1))
        for match in _VERILOG_OUTPUT_BUS_RANGE_RE.finditer(stripped)
        for name in re.split(r"\s*,\s*", match.group(2))
        if name == bus
    ]
    if len(tops) != 1 or tops[0] < 1:
        return None
    return tops[0]


def _per_bit_assigns(
    stripped: str, bus: str, top: int
) -> dict[int, tuple[str, int, int]] | None:
    """``{bit: (rhs identifier, start, end)}`` when every bit ``0..top`` of
    ``bus`` is assigned exactly once to a bare scalar identifier (and no
    assign targets an index outside the range) -- else ``None``."""
    assign_re = re.compile(
        r"\bassign\s+" + re.escape(bus) + r"\s*\[\s*(\d+)\s*\]\s*=\s*([^;]*?)\s*;"
    )
    found: dict[int, tuple[str, int, int]] = {}
    for match in assign_re.finditer(stripped):
        index = int(match.group(1))
        if index in found or index > top:
            return None
        rhs = match.group(2).strip()
        if not _VERILOG_BARE_IDENT_RE.fullmatch(rhs) or _canonical_ident(rhs) == bus:
            return None
        if rhs.startswith("\\") and not re.search(
            re.escape(rhs) + r"\s+;\Z", stripped[match.start() : match.end()]
        ):
            # An escaped identifier runs to the next whitespace, so one
            # butted against the ``;`` is the prefix of a longer name
            # (``assign y[0] = \\foo;bar ;``) -- unprovable, leave it.
            return None
        found[index] = (rhs, match.start(), match.end())
    if sorted(found) != list(range(top + 1)):
        return None
    return found


def _bus_is_exclusively_assign_driven(
    stripped: str,
    header_end: int,
    bus: str,
    decl_name_spans: list[tuple[int, int]],
    assign_spans: list[tuple[int, int]],
) -> bool:
    """``True`` when, outside the module header, the declarations and the
    recognized per-bit assigns, no reference to ``bus`` -- whole vector or
    any bit-select -- remains in the comment-stripped chunk: no instance
    port connection drives or reads a bit, no whole-vector ``assign``
    targets it, and no other statement mentions it."""
    blanked = list(stripped)
    for start, end in [(0, header_end + 1), *decl_name_spans, *assign_spans]:
        for index in range(start, min(end, len(blanked))):
            blanked[index] = " "
    body = "".join(blanked)
    return not (
        re.search(r"\b" + re.escape(bus) + r"\s*\[", body)
        or re.search(r"\b" + re.escape(bus) + r"\b", body)
    )


def _full_line_span(raw: str, start: int, end: int) -> tuple[int, int]:
    """``(start, end)`` widened to the whole line when the statement is the
    line's only content, so the rewritten copy drops the removed assigns'
    lines outright instead of leaving whitespace husks behind."""
    line_start = raw.rfind("\n", 0, start) + 1
    if end < len(raw) and raw[end] == "\n" and not raw[line_start:start].strip():
        return line_start, end + 1
    return start, end


def _bus_concat_edits(
    chunk: str,
    stripped: str,
    header_end: int,
    decl_name_spans: list[tuple[int, int]],
    bus: str,
    top: int,
) -> tuple[list[tuple[int, int, str | None]], dict[str, Any]] | None:
    """``(edits, record)`` for one qualifying bus -- the whole-line spans
    of its per-bit assigns (the first carrying the concat replacement, the
    rest cut) and its ``{port, bits}`` report entry -- or ``None`` when the
    bus does not qualify or its statements cannot be located uniquely in
    the raw chunk (a comment interleaved *inside* one of them)."""
    found = _per_bit_assigns(stripped, bus, top)
    if found is None:
        return None
    decl_spans = [
        (start, end)
        for start, end in decl_name_spans
        if bus in re.split(r"\s*,\s*", stripped[start:end])
    ]
    assign_spans = [(start, end) for _, start, end in found.values()]
    if not _bus_is_exclusively_assign_driven(
        stripped, header_end, bus, decl_spans, assign_spans
    ):
        return None
    raw_spans: list[tuple[int, tuple[int, int]]] = []
    for _, start, end in found.values():
        statement = stripped[start:end]
        if chunk.count(statement) != 1:
            return None
        stmt_start = chunk.index(statement)
        raw_spans.append(
            (
                stmt_start,
                _full_line_span(chunk, stmt_start, stmt_start + len(statement)),
            )
        )
    raw_spans.sort()
    first_stmt, (first_start, first_end) = raw_spans[0]
    indent = chunk[first_start:first_stmt]
    concat = (
        "{"
        + ",".join(_concat_element(found[index][0]) for index in range(top, -1, -1))
        + "}"
    )
    replacement = indent + f"assign {bus} = {concat};"
    if chunk[first_end - 1] == "\n":
        replacement += "\n"
    edits: list[tuple[int, int, str | None]] = [(first_start, first_end, replacement)]
    edits.extend((start, end, None) for _, (start, end) in raw_spans[1:])
    return edits, {"port": bus, "bits": top + 1}


def _rewrite_module_buses(
    raw: str, hdl_toplevel: str, gated: set[str]
) -> tuple[str, list[dict[str, Any]]] | None:
    """``(new text, [{port, bits}])`` for the one file declaring
    ``hdl_toplevel`` when at least one gated output bus qualifies for the
    concat rewrite -- else ``None`` (the file is used unchanged)."""
    span = _top_module_span(raw, hdl_toplevel)
    if span is None:
        return None
    chunk_start, chunk_end = span
    chunk = raw[chunk_start:chunk_end]
    stripped = _VERILOG_COMMENT_RE.sub(" ", chunk)
    header_end = stripped.find(";")
    if header_end == -1:
        return None
    decl_name_spans = [
        (match.start(2), match.end(2))
        for match in _VERILOG_OUTPUT_BUS_RANGE_RE.finditer(stripped)
    ]
    edits: list[tuple[int, int, str | None]] = []
    records: list[dict[str, Any]] = []
    for bus in sorted(gated):
        top = _bus_declared_top_bit(stripped, bus)
        if top is None:
            continue
        result = _bus_concat_edits(
            chunk, stripped, header_end, decl_name_spans, bus, top
        )
        if result is None:
            continue
        edits.extend(result[0])
        records.append(result[1])
    if not edits:
        return None
    parts: list[str] = []
    cursor = 0
    for start, end, text in sorted(edits, key=lambda edit: edit[0]):
        parts.append(chunk[cursor:start])
        if text is not None:
            parts.append(text)
        cursor = max(cursor, end)
    parts.append(chunk[cursor:])
    return raw[:chunk_start] + "".join(parts) + raw[chunk_end:], records


def _apply_sdf_output_assign_rewrite(
    sdf_path: str,
    sources: list[str],
    hdl_toplevel: str,
    output_dir: str,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Issue #7534's source-side step: ``sources`` with every file whose
    top module drives a gated output bus through separate per-bit assigns
    replaced by a build-dir copy holding the equivalent whole-vector
    ``assign y = {bN,…,b0};``, plus ``[{port, bits, file}]`` per rewritten
    port for ``environment.sdf.output_assign_rewritten``. A no-op returning
    the inputs unchanged when the SDF names no nonzero ``INTERCONNECT``
    onto a bit of such a bus (so an all-zero run keeps #2285's drop, and a
    direct/whole-vector wiring is never copied)."""
    text = _read_sdf_text(sdf_path)
    if text is None:
        return sources, []
    gated = _sdf_buses_with_nonzero_output_bit_arcs(text)
    if not gated:
        return sources, []
    new_sources: list[str] = []
    rewritten: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        try:
            with open(source, encoding="utf-8", errors="replace") as handle:
                raw = handle.read()
        except OSError:
            new_sources.append(source)
            continue
        result = _rewrite_module_buses(raw, hdl_toplevel, gated)
        if result is None:
            new_sources.append(source)
            continue
        copy_dir = os.path.join(output_dir, "klt_sdf_output_concat_sources", str(index))
        os.makedirs(copy_dir, exist_ok=True)
        copy_path = os.path.join(copy_dir, os.path.basename(source))
        with open(copy_path, "w", encoding="utf-8") as handle:
            handle.write(result[0])
        new_sources.append(copy_path)
        rewritten.extend({**record, "file": copy_path} for record in result[1])
    return new_sources, rewritten


def _sdf_output_concat_report(
    rewritten: list[dict[str, Any]], repo_root: str | None
) -> dict[str, list]:
    """``environment.sdf.output_assign_rewritten`` (issue #7534; additive,
    present only when a port's per-bit output assigns were rewritten into
    the whole-vector concat -- the same absent-otherwise discipline as
    ``environment.sdf.renamed``). It does not set ``partial`` and adds no
    ``dropped`` class: no timing is discarded, only respelled."""
    if not rewritten:
        return {}
    return {
        "output_assign_rewritten": [
            {
                "port": entry["port"],
                "bits": entry["bits"],
                "file": _hashed_path_envelope(entry["file"], repo_root=repo_root),
            }
            for entry in rewritten
        ]
    }
