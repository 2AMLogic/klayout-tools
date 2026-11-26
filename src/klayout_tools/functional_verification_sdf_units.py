"""Issue #7983: an SDF whose ``TIMESCALE`` is not the time unit of the module
its delays land in is refused before the build; a matching run reports both.

Icarus Verilog 13.0 (stable) ignores an SDF file's ``(TIMESCALE ...)`` and
applies every delay number in the **annotated module's own** time unit. A
Liberty ``time_unit`` of ``1ps`` makes OpenSTA's ``write_sdf`` emit
``(TIMESCALE 1ps)``, and over cell models that declare `` `timescale 1ns ``
that annotates every delay 1000x too large while the transcript stays clean
and the report says ``annotated: true``. Measured live on raw ``iverilog
-g2012 -gspecify`` + ``vvp``: a 5 ns input edge through a 100 ps ``IOPATH``
on a ``1ns/1ps`` leaf rose at **105.000 ns** from ``100`` in a ``1ps`` file,
and at **5.100 ns** from ``0.100`` in a ``1ns`` file.

The unit each delay lands in, as Icarus elaborates it:

* An entry in a ``CELL`` is applied in the ``CELL``'s own module, the one
  its ``CELLTYPE`` names -- an ``IOPATH`` in the cell itself, an
  ``INTERCONNECT`` in the cell that contains the two instances (measured: a
  ``0.100`` entry in the root ``CELL`` took the root's ``1ns``, not the
  ``1ps`` leaf's unit). A ``DELAY`` clause at the file's top level lands in
  the annotation root.
* A module's unit is the last `` `timescale `` before its declaration, read
  across the sources in compile order: the directive persists from one file
  into the next and `` `resetall `` restores the compile-time default.
* That default is the request's ``options.timescale`` unit on the cocotb
  path (cocotb's Icarus runner passes it as ``+timescale+<unit>/<precision>``)
  and nothing on the Verilog-bench path, which rejects ``options.timescale``.
  A module's own directive always wins over it (measured: a ``1ps/1ps`` leaf
  under ``+timescale+1ns/1ps`` keeps its ps unit), so a matching request
  default can never mask a conflicting model declaration.

Only what Icarus applies is compared: a ``CELL`` that still carries a
``DELAY`` entry once the normalization passes are done. ``TIMINGCHECK``
entries apply nothing, and neither does an entry a pass removed. A target
whose unit cannot be established is refused rather than guessed: a model
that is not itself a ``request.sources`` entry, or one with no directive
where nothing supplies a default. Conditional-compilation directives are not
evaluated and `` `include ``d files are not followed.

:func:`_sdf_annotation_report` is the one ``environment.sdf`` constructor
both report builders (cocotb and Verilog bench) share; the two unit fields
it carries are what :func:`_check_sdf_time_units` records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import chain
from typing import Any

from ._artifact_envelope import hashed_path_envelope as _hashed_path_envelope
from ._text import read_text
from .functional_verification_base import FunctionalVerificationError
from .functional_verification_sdf import (
    _VERILOG_COMMENT_RE,
    _read_verilog_source_texts,
    _resolve_sdf_scope_module,
)
from .functional_verification_sdf_output_concat import _sdf_output_concat_report
from .functional_verification_sdf_rename import _sdf_renamed_report

#: A time unit with its ``1``/``10``/``100`` multiplier, in any spacing or
#: case, optionally followed by a precision: a Verilog `` `timescale ``
#: argument, or an SDF header written ``(TIMESCALE 1ns/1ps)``.
_TIME_UNIT_RE = re.compile(
    r"(\d+)\s*(ms|us|ns|ps|fs|s)(?:\s*/\s*\d+\s*(?:ms|us|ns|ps|fs|s))?", re.IGNORECASE
)

#: Each unit's length in femtoseconds, the finest unit either format knows.
_UNIT_FS = {"s": 10**15, "ms": 10**12, "us": 10**9, "ns": 10**6, "ps": 10**3, "fs": 1}

_SDF_TIMESCALE_RE = re.compile(r"\(TIMESCALE\s+([^)]*)\)", re.IGNORECASE)
_SDF_DELAYFILE_RE = re.compile(r"\(DELAYFILE\b")
_SDF_CELL_RE = re.compile(r"\(CELL\b")
_SDF_DELAY_RE = re.compile(r"\(DELAY\b")
_SDF_CELLTYPE_RE = re.compile(r'\(CELLTYPE\s+"([^"]*)"')
_SDF_INSTANCE_RE = re.compile(r"\(INSTANCE\s*([^)]*)\)")

#: In compile order, the three things that decide a module's time unit: a
#: well-formed `` `timescale `` directive, a `` `resetall `` directive, and a
#: module declaration. A malformed directive matches nothing here and is the
#: compiler's own error to report.
_VERILOG_UNIT_TOKEN_RE = re.compile(
    r"`timescale\s+(?P<amount>\d+)\s*(?P<unit>ms|us|ns|ps|fs|s)\s*/\s*"
    r"\d+\s*(?:ms|us|ns|ps|fs|s)"
    r"|`resetall\b"
    r"|^[ \t]*module\s+(?:automatic\s+)?(?P<module>[A-Za-z_][\w$]*|\\[^\s]+)",
    re.MULTILINE,
)

#: One delay target of an SDF: the ``CELLTYPE`` and ``INSTANCE`` of a ``CELL``
#: that carries a delay entry, or ``(None, None)`` for a ``DELAY`` clause
#: outside any ``CELL``.
_Target = tuple[str | None, str | None]


def _parse_time_unit(unit: str) -> tuple[str, int] | None:
    """``("<multiplier><unit>", length in femtoseconds)``, or ``None`` when
    ``unit`` does not start with a time unit."""
    match = _TIME_UNIT_RE.match(unit.strip())
    if match is None:
        return None
    name = f"{int(match.group(1))}{match.group(2).lower()}"
    return name, int(match.group(1)) * _UNIT_FS[match.group(2).lower()]


def _unit_fs(unit: str) -> int:
    """A canonical unit's length in femtoseconds."""
    parsed = _parse_time_unit(unit)
    assert parsed is not None, unit
    return parsed[1]


def _same_time_unit(left: str, right: str) -> bool:
    """Whether two time units are the same length of time. ``1ns`` and
    ``1000ps`` are; ``10ps`` and ``1ns`` are not -- the multiplier is part of
    the unit."""
    parsed = (_parse_time_unit(left), _parse_time_unit(right))
    return (
        parsed[0] is not None and parsed[1] is not None and parsed[0][1] == parsed[1][1]
    )


def _sdf_timescale(text: str) -> str:
    """The SDF file's ``TIMESCALE`` unit as ``"<multiplier><unit>"``, or
    ``"1ns"`` when it has none (the SDF standard's default). Refuses a
    ``TIMESCALE`` that names no time unit: the unit of every delay in the
    file would be unknown."""
    match = _SDF_TIMESCALE_RE.search(text)
    if match is None:
        return "1ns"
    written = match.group(1).strip()
    parsed = _parse_time_unit(written)
    if parsed is None:
        raise FunctionalVerificationError(
            "options.sdf: cannot read the SDF's TIMESCALE "
            f"{written!r} as a time unit such as 1ns, 10ps or 100ps, so "
            "the unit of every delay in the file is unknown"
        )
    return parsed[0]


def _verilog_time_units(
    source_texts: list[str], default_unit: str | None
) -> dict[str, str | None]:
    """Module name -> the time unit Icarus applies that module's delays in,
    as elaborated from ``source_texts`` in compile order: the last
    `` `timescale `` before the declaration, else ``default_unit`` (``None``:
    nothing supplies one)."""
    units: dict[str, str | None] = {}
    current = default_unit
    for text in source_texts:
        stripped = _VERILOG_COMMENT_RE.sub(" ", text)
        for token in _VERILOG_UNIT_TOKEN_RE.finditer(stripped):
            unit = token.group("unit")
            if unit is not None:
                current = f"{int(token.group('amount'))}{unit}"
            elif token.group(0) == "`resetall":
                current = default_unit
            else:
                units[token.group("module")] = current
    return units


def _sdf_clause_end(text: str, open_index: int) -> int | None:
    """Index just past the clause opening at ``text[open_index]``, or ``None``
    when its parentheses never balance (the simulator reports that).

    The same answer as :func:`._sdf_matching_paren`, visiting each ``)`` in
    turn instead of each character: the nesting depth at a ``)`` is the depth
    left by the previous one plus the ``(`` in between, which ``str.count``
    takes in one call. It matters because a post-route SDF has a clause per
    ``CELL`` and this preflight walks three of them per cell: on the shared
    loop a synthetic 70 MB, 400k-cell file took 16 s, against 2 s this way,
    where one existing pass over it takes 7 s. A running total per window
    cannot replace it: the depth can return to zero and rise again inside one
    window.
    """
    depth, previous = 1, open_index
    while True:
        close = text.find(")", previous + 1)
        if close < 0:
            return None
        depth += text.count("(", previous + 1, close)
        if depth == 1:
            return close + 1
        depth, previous = depth - 1, close


def _sdf_has_delay_entry(text: str, start: int, end: int) -> bool:
    """Whether the clause ``text[start:end]`` carries an applied delay: a
    ``(DELAY ...)`` child holding a type clause ``(ABSOLUTE|INCREMENT ...)``
    with anything nested in it. A ``TIMINGCHECK``, like every other clause,
    applies nothing."""
    delay = _SDF_DELAY_RE.search(text, start, end)
    if delay is None:
        return False
    delay_end = _sdf_clause_end(text, delay.start())
    if delay_end is None:
        return False
    type_open = text.find("(", delay.start() + 1, delay_end - 1)
    while type_open >= 0:
        type_end = _sdf_clause_end(text, type_open)
        if type_end is None:
            return False
        if text.find("(", type_open + 1, type_end - 1) >= 0:
            return True
        type_open = text.find("(", type_end, delay_end - 1)
    return False


def _sdf_cell_target(body: str) -> _Target:
    """``(CELLTYPE, INSTANCE)`` of the ``CELL`` whose text is ``body``. A
    ``CELL`` with no quoted ``CELLTYPE`` yields ``None``; an empty
    ``(INSTANCE)`` yields ``""``."""
    celltype = _SDF_CELLTYPE_RE.search(body)
    instance = _SDF_INSTANCE_RE.search(body)
    declared = celltype.group(1) if celltype else None
    return declared, instance.group(1).strip() if instance else ""


def _sdf_root_delay_targets(text: str) -> list[_Target]:
    """One ``(None, None)`` per ``DELAY`` clause at the file's top level."""
    delayfile = _SDF_DELAYFILE_RE.search(text)
    if delayfile is None:
        return []
    file_end = _sdf_clause_end(text, delayfile.start())
    if file_end is None:
        return []
    targets: list[_Target] = []
    clause = text.find("(", delayfile.start() + 1, file_end - 1)
    while clause >= 0:
        clause_end = _sdf_clause_end(text, clause)
        if clause_end is None:
            break
        is_delay = _SDF_DELAY_RE.match(text, clause) is not None
        if is_delay and _sdf_has_delay_entry(text, clause, clause_end):
            targets.append((None, None))
        clause = text.find("(", clause_end, file_end - 1)
    return targets


def _sdf_delay_targets(text: str) -> list[_Target]:
    """Every delay target of ``text`` in file order: ``(CELLTYPE, INSTANCE)``
    per ``CELL`` that still carries a delay entry, then one ``(None, None)``
    per ``DELAY`` clause outside any ``CELL``."""
    targets: list[_Target] = []
    for cell in _SDF_CELL_RE.finditer(text):
        cell_end = _sdf_clause_end(text, cell.start())
        if cell_end is not None and _sdf_has_delay_entry(text, cell.start(), cell_end):
            targets.append(_sdf_cell_target(text[cell.start() : cell_end]))
    targets.extend(_sdf_root_delay_targets(text))
    return targets


def _resolve_targets(
    targets: Iterable[_Target], units: dict[str, str | None], root: str | None
) -> dict[str | None, tuple[str | None, str | None]]:
    """Module -> ``(INSTANCE, declared unit)``: the first target naming each
    module, in file order, however many delays land in it. A ``DELAY`` clause
    outside any ``CELL`` is attributed to ``root``, the annotation scope's
    module."""
    resolved: dict[str | None, tuple[str | None, str | None]] = {}
    for celltype, instance in targets:
        # A DELAY clause outside any CELL lands in the annotation root; a
        # CELL names its own module by CELLTYPE, or names none.
        module = root if celltype is None and instance is None else celltype
        if module not in resolved:
            declared = units.get(module) if module is not None else None
            resolved[module] = (instance, declared)
    return resolved


def _target_parts(module: str | None, instance: str | None) -> tuple[str, str]:
    """``(where the SDF puts the target, how it names the module)``."""
    if instance is None:
        root = (
            "the annotation root" if module is None else f"annotation root '{module}'"
        )
        return "the DELAY clauses outside any CELL", root
    where = "the root CELL" if not instance else f"'{instance}'"
    if module is None:
        return where, "no CELLTYPE"
    return where, f'CELLTYPE "{module}"'


def _unresolved_reason(
    module: str | None, units: dict[str, str | None], instance: str | None
) -> str:
    """Why no time unit could be established for the module a target names."""
    if module is None:
        if instance is None:
            return (
                "options.sdf.scope does not resolve to a module declared in "
                "request.sources, so its own unit is unknown"
            )
        return "the CELL names no module to compare"
    if module not in units:
        return (
            f"no module '{module}' is declared in request.sources (a model "
            "reached only through `include cannot be checked -- list it in "
            "request.sources)"
        )
    return (
        f"module '{module}' declares no `timescale` and no usable default "
        "unit covers it (testbench.verilog_top rejects options.timescale, so "
        "there every annotated module must declare one)"
    )


def _unit_problems(
    targets: Iterable[_Target],
    units: dict[str, str | None],
    root: str | None,
    timescale: str,
) -> tuple[list[tuple[str, str]], list[str], str | None]:
    """``(mismatched, unresolved, applied_unit)`` for ``targets``, each
    target's declared unit against the SDF's ``timescale``. ``mismatched``
    pairs each target's description with the unit its module declares;
    ``applied_unit`` is that of the first target that matched -- every other
    one is the same length of time -- or ``None`` when nothing is retained.
    """
    wrong: list[tuple[str, str]] = []
    unknown: list[str] = []
    applied: str | None = None
    resolved = _resolve_targets(targets, units, root)
    for module, (instance, declared) in resolved.items():
        where, identity = _target_parts(module, instance)
        if declared is None:
            reason = _unresolved_reason(module, units, instance)
            unknown.append(f"{where} ({identity}): {reason}")
        elif not _same_time_unit(declared, timescale):
            wrong.append((f"{where} ({identity}, `timescale {declared})", declared))
        elif applied is None:
            applied = declared
    return wrong, unknown, applied


def _listing(entries: list[str]) -> str:
    """Up to three entries, then how many more there are."""
    more = len(entries) - 3
    return "; ".join(entries[:3]) + (f"; ... and {more} more" if more > 0 else "")


def _mismatch_refusal(timescale: str, wrong: list[tuple[str, str]]) -> str:
    """The message for delays whose module states another unit than the SDF:
    how far off each would be, from the first target's unit."""
    declared_fs, sdf_fs = _unit_fs(wrong[0][1]), _unit_fs(timescale)
    ratio = max(declared_fs, sdf_fs) // min(declared_fs, sdf_fs)
    direction = "large" if declared_fs > sdf_fs else "small"
    return (
        f"options.sdf: the SDF file is in {timescale} but Icarus Verilog "
        "ignores an SDF's TIMESCALE and applies every delay in the annotated "
        "module's own time unit. Delay targets declared in another unit: "
        f"{_listing([where for where, _declared in wrong])}. Every delay "
        f"annotated onto them would be {ratio}x too {direction} while the "
        "transcript stays clean and the report says annotated: true. Write "
        "the SDF in the models' unit, or the models in the SDF's unit, so "
        "both state the same time unit"
    )


def _unresolved_refusal(timescale: str, unknown: list[str]) -> str:
    """The message for delays whose module's unit could not be established."""
    return (
        "options.sdf: the time unit of these delay targets in a "
        f"{timescale} SDF cannot be established: {_listing(unknown)}. Icarus "
        "Verilog applies every SDF delay in the annotated module's own time "
        "unit, so an unverified unit could annotate every delay 1000x off "
        "while the transcript stays clean and the report says annotated: true"
    )


def _default_unit(
    verilog_tb: dict[str, Any] | None, timescale: tuple[str, str]
) -> str | None:
    """The unit a module without a `` `timescale `` takes: none on the
    Verilog-bench path, which rejects ``options.timescale``, else the
    request's own unit."""
    if verilog_tb is not None:
        return None
    parsed = _parse_time_unit(timescale[0])
    return parsed[0] if parsed else None


def _check_sdf_time_units(
    sdf: dict[str, Any],
    sdf_paths: list[str],
    sources: list[str],
    hdl_toplevel: str,
    verilog_tb: dict[str, Any] | None,
    timescale: tuple[str, str],
) -> None:
    """Refuse, before anything is built, when ``sdf_paths`` -- what
    ``$sdf_annotate`` will read -- carries a delay whose module states another
    time unit than the SDF's ``TIMESCALE``, or none that can be established.
    Otherwise record ``timescale`` and ``time_unit`` on ``sdf`` for the
    report.

    ``verilog_tb`` is set on the Verilog-bench path, where nothing supplies a
    default unit; there ``timescale`` is unused. Elsewhere it is the request's
    ``options.timescale``, the compile-time default. An unreadable SDF is left
    to the simulator's own diagnostic.
    """
    texts = [text for text in map(read_text, sdf_paths) if text is not None]
    if not texts:
        return
    timescale_unit = _sdf_timescale(texts[0])
    source_texts = _read_verilog_source_texts(sources)
    units = _verilog_time_units(source_texts, _default_unit(verilog_tb, timescale))
    root: str | None = hdl_toplevel
    if "scope" in sdf:
        root = _resolve_sdf_scope_module(source_texts, hdl_toplevel, sdf["scope"])
    targets = chain.from_iterable(map(_sdf_delay_targets, texts))
    wrong, unknown, applied = _unit_problems(targets, units, root, timescale_unit)
    if wrong:
        raise FunctionalVerificationError(_mismatch_refusal(timescale_unit, wrong))
    if unknown:
        raise FunctionalVerificationError(_unresolved_refusal(timescale_unit, unknown))
    sdf["timescale"] = timescale_unit
    sdf["time_unit"] = applied


def _sdf_annotation_report(
    sdf: dict[str, Any],
    repo_root: str | None,
    dropped_counts: dict[str, int],
    dropped_arcs: dict[str, list[dict[str, str]]],
    renamed: list[dict[str, str]],
    output_concat: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``environment.sdf`` object of a successful annotation, shared by
    the cocotb and the Verilog-bench report builder.

    ``file`` is the hashed envelope (issue #5784) of the caller's own SDF,
    never of a normalized copy. ``entries`` (issue #2364) echoes the request's
    entry population, ``"all"`` or ``"interconnect"``; ``scope`` is present
    only when ``options.sdf.scope`` was set; ``renamed`` (issue #2897) is one
    ``{from, to}`` per escaped-divider instance rename, and
    ``output_assign_rewritten`` (issue #7534) one entry per top-level output
    port whose per-bit assigns were folded -- both present only when non-empty.
    ``output_concat`` is the cocotb path's list for the latter; the
    Verilog-bench path has none.

    ``timescale`` is the SDF file's ``TIMESCALE`` unit and ``time_unit`` the
    unit :func:`_check_sdf_time_units` established for the retained delays --
    the same length of time, which is what the run verified -- or ``None``
    when the SDF carries no delay entry (issue #7983). Both are ``None`` when
    the preflight could not read the SDF, which cannot produce this report.
    """
    return {
        "file": _hashed_path_envelope(sdf["file"], repo_root=repo_root),
        "corner": sdf["corner"],
        "entries": sdf["entries"],
        **({"scope": sdf["scope"]} if "scope" in sdf else {}),
        "annotated": True,
        "partial": bool(dropped_counts),
        "dropped": _fv._sdf_dropped_report(dropped_counts, dropped_arcs),
        **_sdf_renamed_report(renamed),
        **_sdf_output_concat_report(output_concat or [], repo_root),
        "timescale": sdf.get("timescale"),
        "time_unit": sdf.get("time_unit"),
    }


# Imported last so that whichever of the two modules is imported first, the
# other finds every name it needs already defined (no load-time cycle).
from . import functional_verification as _fv  # noqa: E402
