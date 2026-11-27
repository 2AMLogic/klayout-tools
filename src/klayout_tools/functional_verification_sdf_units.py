"""Issue #7983: an SDF whose ``TIMESCALE`` is not the time unit of the module
its delays land in is refused before the build; a matching run reports both.

Icarus Verilog 13.0 (stable) ignores an SDF file's ``(TIMESCALE ...)`` and
applies cell path delays in the annotated module's time unit. A
Liberty ``time_unit`` of ``1ps`` makes OpenSTA's ``write_sdf`` emit
``(TIMESCALE 1ps)``, and over cell models that declare `` `timescale 1ns ``
that annotates every delay 1000x too large while the transcript stays clean
and the report says ``annotated: true``. Measured live on raw ``iverilog
-g2012 -gspecify`` + ``vvp``: a 5 ns input edge through a 100 ps ``IOPATH``
on a ``1ns/1ps`` leaf rose at **105.000 ns** from ``100`` in a ``1ps`` file,
and at **5.100 ns** from ``0.100`` in a ``1ns`` file.

The unit each delay lands in, as Icarus elaborates it:

* An ``IOPATH`` uses the unit of its ``CELLTYPE`` at the annotated instance.
  An ``INTERCONNECT`` uses the unit of the module calling ``$sdf_annotate``,
  even when the interconnect is in a child cell. The generated annotation
  module is included in the same source-order scan as the design.
* A module-local ``timeunit`` overrides the active directive for that
  module. Otherwise the unit is the last `` `timescale `` before its declaration, read
  across the sources in compile order: the directive persists from one file
  into the next and `` `resetall `` restores the compilation-unit ``timeunit``,
  falling back to the compile-time default when none was declared.
* That default is the request's ``options.timescale`` unit on the cocotb
  path (cocotb's Icarus runner passes it as ``+timescale+<unit>/<precision>``)
  and nothing on the Verilog-bench path, which rejects ``options.timescale``.
  A module's own directive always wins over it (measured: a ``1ps/1ps`` leaf
  under ``+timescale+1ns/1ps`` keeps its ps unit), so a matching request
  default can never mask a conflicting model declaration.

Only what Icarus applies is compared: a ``CELL`` that still carries a
``DELAY`` entry once the normalization passes are done. ``TIMINGCHECK``
entries apply nothing, and neither does an entry a pass removed. A target
whose unit cannot be established is refused. The scan uses the active HDL
after preprocessing, including per-source define wrappers.

:func:`_sdf_annotation_report` is the one ``environment.sdf`` constructor
both report builders (cocotb and Verilog bench) share; the two unit fields
it carries are what :func:`_check_sdf_time_units` records.
"""

from __future__ import annotations

from collections.abc import Iterable
from fractions import Fraction
from typing import Any

from ._artifact_envelope import hashed_path_envelope as _hashed_path_envelope
from ._text import read_text
from .functional_verification_base import (
    SDF_ANNOTATE_MODULE,
    FunctionalVerificationError,
)
from .functional_verification_hdl_units import (
    _active_sources,
    _Module,
    _ModuleTimeUnits,
    _verilog_time_units,
)
from .functional_verification_sdf import _sdf_split_unescaped, _split_sdf_scope
from .functional_verification_sdf_output_concat import _sdf_output_concat_report
from .functional_verification_sdf_rename import _sdf_renamed_report
from .functional_verification_sdf_unit_syntax import (
    _parse_time_unit,
    _same_time_unit,
    _sdf_delay_targets,
    _sdf_syntax,
    _sdf_timescale,
    _Target,
    _unit_fs,
)


def _target_declaration(
    units: _ModuleTimeUnits, root: _Module | None, target: _Target
) -> _Module | None:
    celltype, instance, divider, _ = target
    if instance is None:
        return root
    if celltype is None:
        return None
    if celltype not in units.nested:
        return units.top.get(celltype)
    if instance == "*":
        nested = units.nested[celltype]
        first = units.top.get(celltype, nested[0])
        return first if all(m.unit == first.unit for m in nested) else None
    path = _sdf_split_unescaped(instance, divider) if instance else []
    if path and path[-1] != celltype:
        # Icarus gives an implicit nested instance the declaration's own
        # name. An explicitly named instance uses the top-level definition.
        return units.top.get(celltype)
    module = units.follow(root, path)
    return module if module and module.name == celltype else None


def _target_parts(target: _Target, root: _Module | None = None) -> tuple[str, str]:
    """``(where the SDF puts the target, how it names the module)``."""
    if target.interconnect:
        return (
            "the retained INTERCONNECT entries",
            f"annotation module '{SDF_ANNOTATE_MODULE}'",
        )
    module, instance = target.celltype, target.instance
    if instance is None:
        identity = f"annotation root '{root.name}'" if root else "the annotation root"
        return "the DELAY clauses outside any CELL", identity
    where = "the root CELL" if not instance else f"'{instance}'"
    if module is None:
        return where, "no CELLTYPE"
    return where, f'CELLTYPE "{module}"'


def _unresolved_reason(
    module: str | None, units: _ModuleTimeUnits, instance: str | None
) -> str:
    """Why no time unit could be established for the module a target names."""
    if module is None:
        if instance is None:
            return (
                "options.sdf.scope does not resolve to a module declared in "
                "request.sources, so its own unit is unknown"
            )
        return "the CELL names no module to compare"
    if module not in units.top and module not in units.nested:
        return (
            f"no module '{module}' is declared in request.sources after preprocessing"
        )
    return (
        f"the effective time unit of module '{module}' could not be resolved "
        "from its timeunit, active `timescale or compiler default "
        "(testbench.verilog_top rejects options.timescale)"
    )


def _unit_problems(
    targets: Iterable[_Target],
    units: _ModuleTimeUnits,
    root: _Module | None,
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
    seen: set[tuple[str | None, str | None]] = set()
    for target in targets:
        module: str | None
        if target.interconnect:
            definition = units.top.get(SDF_ANNOTATE_MODULE)
            module = SDF_ANNOTATE_MODULE
        else:
            definition = _target_declaration(units, root, target)
            module = root.name if target.instance is None and root else target.celltype
        declared = definition.unit if definition else None
        if (module, declared) in seen:
            continue
        seen.add((module, declared))
        where, identity = _target_parts(target, root)
        if declared is None:
            reason = _unresolved_reason(module, units, target.instance)
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
    """Report the scale error for each target in the diagnostic."""
    sdf_fs = _unit_fs(timescale)
    details = []
    for where, declared in wrong[:3]:
        declared_fs = _unit_fs(declared)
        ratio = Fraction(max(declared_fs, sdf_fs), min(declared_fs, sdf_fs))
        direction = "large" if declared_fs > sdf_fs else "small"
        details.append(f"{where}: {ratio}x too {direction}")
    if len(wrong) > 3:
        details.append(f"... and {len(wrong) - 3} more")
    return (
        f"options.sdf: the SDF file is in {timescale} but Icarus Verilog "
        "ignores an SDF's TIMESCALE. Delays applied in another HDL time unit: "
        f"{'; '.join(details)}. Use the same time unit for the SDF and "
        "the HDL modules that supply these delays' units"
    )


def _unresolved_refusal(timescale: str, unknown: list[str]) -> str:
    """The message for delays whose module's unit could not be established."""
    return (
        "options.sdf: the time unit of these delay targets in a "
        f"{timescale} SDF cannot be established: {_listing(unknown)}. Icarus "
        "Verilog ignores the SDF's TIMESCALE, so an unverified unit could "
        "annotate delays 1000x off "
        "while the transcript stays clean and the report says annotated: true"
    )


def _check_sdf_time_units(
    sdf: dict[str, Any],
    sdf_paths: list[str],
    sources: list[str],
    dut_path: str,
    default_unit: str | None,
    includes: list[str],
    defines: dict[str, str | None],
    user_build_args: list[str],
    cocotb_build_dir: str | None,
) -> None:
    """Refuse, before anything is built, when ``sdf_paths`` -- what
    ``$sdf_annotate`` will read -- carries a delay whose module states another
    time unit than the SDF's ``TIMESCALE``, or none that can be established.
    Otherwise record ``timescale`` and ``time_unit`` on ``sdf`` for the
    report.

    Cocotb supplies its build directory and compiler default unit. The
    Verilog-bench path runs in the caller's cwd with no declared default.
    An unreadable SDF is left to the simulator's own diagnostic.
    """
    targets: list[_Target] = []
    timescale_unit = None
    for path in sdf_paths:
        text = read_text(path)
        if text is None:
            return
        syntax = _sdf_syntax(text)
        unit = _sdf_timescale(syntax)
        if timescale_unit is not None and unit != timescale_unit:
            raise FunctionalVerificationError(
                "options.sdf: split files have different TIMESCALE units"
            )
        timescale_unit = unit
        targets.extend(_sdf_delay_targets(text, syntax))
    if timescale_unit is None:
        return
    sdf["timescale"] = timescale_unit
    sdf["time_unit"] = None
    if not targets:
        return
    try:
        source_texts = _active_sources(
            sources,
            includes,
            defines,
            user_build_args,
            cocotb_build_dir,
        )
    except FunctionalVerificationError as exc:
        target = _target_parts(targets[0])
        raise FunctionalVerificationError(
            f"options.sdf: cannot establish the HDL time unit for {target[0]} "
            f"({target[1]}) in a {timescale_unit} SDF: {exc}"
        ) from exc
    parsed_default = _parse_time_unit(default_unit) if default_unit else None
    units = _verilog_time_units(
        source_texts, parsed_default[0] if parsed_default else None
    )
    scope = sdf.get("scope")
    components = iter(_split_sdf_scope(f"{dut_path}.{scope}" if scope else dut_path))
    root = units.follow(
        units.top.get(next(components, "").removeprefix("\\")), components
    )
    wrong, unknown, applied = _unit_problems(targets, units, root, timescale_unit)
    if wrong:
        raise FunctionalVerificationError(_mismatch_refusal(timescale_unit, wrong))
    if unknown:
        raise FunctionalVerificationError(_unresolved_refusal(timescale_unit, unknown))
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
