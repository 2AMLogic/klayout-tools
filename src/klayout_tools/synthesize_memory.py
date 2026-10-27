"""Memory-mapping evidence for ``klt synthesize`` (issue #2745, part of #2281).

``klt synthesize``'s mapped metrics (``area_um2``, ``instance_count``, ...)
are read from the *final* ``stat`` after ``synth``/``dfflibmap``/``abc``. By
then Yosys's ``synth`` has already run ``memory_map``, so final counts alone
cannot say whether the RTL ever contained an inferred memory, whether that
memory was lowered to flip-flops and muxes, or whether optimization removed
it before lowering. This module answers that question -- only as far as
Yosys's own statistics support it -- with two *extra* observation points
captured in a **separate, side Yosys invocation** that never touches the
production script, netlist or stats:

``elaborated``
    After ``hierarchy -check -top <top>`` and ``proc``: every
    ``RTLIL::Memory`` object (``stat``'s ``num_memories``/
    ``num_memory_bits``) plus any already-consolidated ``$mem``/``$mem_v2``
    cell. This is the earliest point where an inferred memory is visible.
``pre_lowering``
    After ``synth -top <top> -run :fine`` -- exactly ``synth``'s own
    ``begin`` and ``coarse`` labels, i.e. every pass the production
    ``synth -top <top>`` runs *before* ``fine``'s ``memory_map``. A memory
    still present here as a ``$mem``/``$mem_v2`` cell is what ``memory_map``
    then lowers (or would leave behind).

The third point, ``final``, is read from the **production** run's own stats
JSON (already written; no extra work): any ``$mem``/``$mem_v2`` cell or
unconsolidated memory left after mapping.

Classification is **per module definition** and bounded to those counts --
it never names a memory as a ROM or RAM and never attributes mapped cells to
a particular memory. Whenever the counts do not support one of the definite
outcomes (more memories at a later stage than an earlier one, a stage that
could not be read, the side invocation failing) the result is
``"indeterminate"`` with a ``reason``; it is never guessed.

Like ``synthesize_arithmetic.py``, this module imports ``synthesize.py``'s
helpers back at **call** time only, so ``synthesize.py`` is the only module
that depends on this one at import time.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._verilog_params import chparam_lines, read_verilog_line

#: The memory outcome vocabulary, per module and overall. Fixed (a caller may
#: branch on it); see ``docs/cli/synthesize.md`` for each value's meaning.
MEMORY_STATUSES = (
    "none_detected",
    "lowered_to_logic",
    "optimized_away",
    "surviving",
    "mixed",
    "indeterminate",
)

#: Cell types each of which *is* one consolidated memory (``memory_collect``'s
#: output). ``$memrd``/``$memwr``/``$meminit`` port cells are deliberately not
#: counted: they belong to an ``RTLIL::Memory`` already counted via
#: ``num_memories``, and counting both would double-count.
_MEMORY_CELL_TYPES = ("$mem", "$mem_v2")

#: File names (inside the run's ``.klt/synthesize/<run_id>/`` directory) of
#: the side invocation's own artifacts.
EVIDENCE_SCRIPT_SUFFIX = "_memory_evidence.ys"
_BLACKBOX_LIST_SUFFIX = "_blackboxes.txt"
_LIBRARY_CELLS_SUFFIX = "_library_cells.txt"
_ELABORATED_STATS_SUFFIX = "_memory_elaborated.json"
_PRE_LOWERING_STATS_SUFFIX = "_memory_pre_lowering.json"


def memory_evidence_script_text(
    *,
    sources: list[str],
    hdl_toplevel: str,
    blackbox_list_path: str,
    elaborated_stats_path: str,
    pre_lowering_stats_path: str,
    adder_sources: tuple[str, ...] = (),
    adder_techmap_path: str | None = None,
    parameters: dict[str, int | str] | None = None,
    liberty_path: str | None = None,
    library_cells_path: str | None = None,
) -> str:
    """The side invocation's ``.ys`` text.

    Mirrors the production script (:func:`klayout_tools.synthesize.
    _write_script`) up to -- and stopping before -- ``synth``'s ``fine``
    label: the same ``read_verilog`` order, the same ``hierarchy -check
    -top``, the same arithmetic ``techmap`` insertion when the request's
    ``arithmetic`` substitution is active, then ``synth -top <top> -run
    :fine``. The extra ``proc`` before the first ``stat`` is the pass
    ``synth``'s own ``coarse`` label (and the arithmetic insertion) would
    run first anyway; ``proc`` is idempotent, and ``stat``/``select -list``
    never modify the design. No liberty is read -- nothing here is
    technology-mapped. The one exception (issue #4205): when ``liberty_path``
    is given it is loaded ``-lib`` first, exactly as the production script
    does, so RTL instantiating a library cell elaborates; the cells that load
    declares are listed to ``library_cells_path`` so they are not reported as
    surviving blackboxes (they carry area).
    """
    lines = []
    if liberty_path is not None:
        lines.append(f"read_liberty -lib {liberty_path}")
        if library_cells_path is not None:
            lines.append(f"tee -q -o {library_cells_path} select -list =A:blackbox")
    lines += [read_verilog_line(path) for path in sources]
    lines += chparam_lines(parameters or {}, hdl_toplevel)
    lines.append(f"hierarchy -check -top {hdl_toplevel}")
    lines.append(f"tee -q -o {blackbox_list_path} select -list =A:blackbox =A:whitebox")
    lines.append("proc")
    lines.append(f"tee -q -o {elaborated_stats_path} stat -json")
    if adder_techmap_path is not None:
        lines += ["opt_expr", "opt_clean"]
        lines += [f"read_verilog {path}" for path in adder_sources]
        lines.append(f"techmap -map {adder_techmap_path}")
    lines.append(f"synth -top {hdl_toplevel} -run :fine")
    lines.append(f"tee -q -o {pre_lowering_stats_path} stat -json")
    return "\n".join(lines) + "\n"


def _load_modules(stats_path: str) -> dict[str, Any] | None:
    """``stat -json``'s ``modules`` dict from ``stats_path``, or ``None``
    when the file is missing, unreadable, not JSON, or has no ``modules``
    object. Never raises -- a missing stage is evidence of nothing."""
    try:
        with open(stats_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    modules = data.get("modules") if isinstance(data, dict) else None
    return modules if isinstance(modules, dict) else None


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def memory_counts(modules: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Per module definition: ``{"memories": n, "memory_bits": b}``.

    ``memories`` is ``num_memories`` (unconsolidated ``RTLIL::Memory``
    objects) plus the count of :data:`_MEMORY_CELL_TYPES` cells
    (consolidated memories). ``memory_bits`` is ``num_memory_bits`` only --
    Yosys does not report a bit count for a consolidated ``$mem_v2`` cell in
    ``stat``, so it is meaningful at the ``elaborated`` stage only. Module
    keys are reported unescaped (``"\\\\ram"`` -> ``"ram"``).
    """
    counts: dict[str, dict[str, int]] = {}
    for key, stats in modules.items():
        if not isinstance(stats, dict):
            continue
        by_type = stats.get("num_cells_by_type")
        cells = (
            sum(_int(by_type.get(t)) for t in _MEMORY_CELL_TYPES)
            if isinstance(by_type, dict)
            else 0
        )
        name = key[1:] if key.startswith("\\") else key
        counts[name] = {
            "memories": _int(stats.get("num_memories")) + cells,
            "memory_bits": _int(stats.get("num_memory_bits")),
        }
    return counts


def classify_module(
    elaborated: int, pre_lowering: int, final: int
) -> tuple[str, str | None]:
    """One module definition's outcome from its three memory counts, as
    ``(status, reason)`` -- ``reason`` is non-``None`` only for
    ``"indeterminate"``.

    The rules use only what the counts establish:

    - nothing at any stage -> ``"none_detected"``;
    - a later stage holding *more* memories than an earlier one (a memory
      inferred by a pass after elaboration, or a consolidation this
      bookkeeping does not model) -> ``"indeterminate"``: the extra memory
      was never observed at the earlier stage, so its fate cannot be told
      apart from the others';
    - every detected memory gone before lowering -> ``"optimized_away"``;
    - every detected memory present before lowering and gone after mapping
      -> ``"lowered_to_logic"``;
    - every detected memory still present after mapping -> ``"surviving"``;
    - any other combination (some removed, some lowered or surviving) ->
      ``"mixed"``.
    """
    if elaborated == 0 and pre_lowering == 0 and final == 0:
        return "none_detected", None
    if pre_lowering > elaborated or final > pre_lowering:
        return (
            "indeterminate",
            "a later stage reports more memories than an earlier stage "
            f"(elaborated={elaborated}, pre_lowering={pre_lowering}, "
            f"final={final}); the extra memories were not observed earlier",
        )
    if final == 0:
        if pre_lowering == 0:
            return "optimized_away", None
        if pre_lowering == elaborated:
            return "lowered_to_logic", None
        return "mixed", None
    if final == pre_lowering == elaborated:
        return "surviving", None
    return "mixed", None


def overall_status(module_statuses: list[str]) -> str:
    """Fold per-module statuses into one: ``"indeterminate"`` if any module
    is, else the single shared non-``"none_detected"`` status, else
    ``"mixed"`` for differing outcomes, else ``"none_detected"``."""
    relevant = {s for s in module_statuses if s != "none_detected"}
    if not relevant:
        return "none_detected"
    if "indeterminate" in relevant:
        return "indeterminate"
    if len(relevant) == 1:
        return next(iter(relevant))
    return "mixed"


def _scrub_paths(text: str, repo_root: str | None) -> str:
    """``text`` with the repo root prefix removed (paths become
    repo-relative) and the home directory shown as ``~``. A Yosys error or
    timeout message names absolute script/source paths, and ``reason`` lands
    in a report that may be committed, like every other response path
    (issue #1844)."""
    if repo_root:
        text = text.replace(repo_root.rstrip(os.sep) + os.sep, "")
    home = os.path.expanduser("~")
    if home and home != os.sep:
        text = text.replace(home.rstrip(os.sep) + os.sep, "~" + os.sep)
    return text


def _indeterminate(reason: str, script_path: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "status": "indeterminate",
        "reason": reason,
        "modules": [],
        "script_path": script_path,
    }


def build_memory_mapping(
    *,
    elaborated_modules: dict[str, Any] | None,
    pre_lowering_modules: dict[str, Any] | None,
    final_modules: dict[str, Any] | None,
    script_path: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the response's ``memory_mapping`` object from the three
    stages' ``stat -json`` ``modules`` dicts. Any stage being ``None``
    (unavailable) yields ``status: "indeterminate"`` with no module rows --
    an incomplete evidence chain never produces a classification."""
    missing = [
        name
        for name, value in (
            ("elaborated", elaborated_modules),
            ("pre_lowering", pre_lowering_modules),
            ("final", final_modules),
        )
        if value is None
    ]
    if missing:
        return _indeterminate(
            f"memory evidence stage(s) unavailable: {', '.join(missing)}",
            script_path,
        )
    assert elaborated_modules is not None
    assert pre_lowering_modules is not None
    assert final_modules is not None
    elab = memory_counts(elaborated_modules)
    pre = memory_counts(pre_lowering_modules)
    final = memory_counts(final_modules)

    rows: list[dict[str, Any]] = []
    reasons: list[str] = []
    for name in sorted(set(elab) | set(pre) | set(final)):
        e = elab.get(name, {}).get("memories", 0)
        p = pre.get(name, {}).get("memories", 0)
        f = final.get(name, {}).get("memories", 0)
        status, reason = classify_module(e, p, f)
        if status == "none_detected":
            continue
        if reason is not None:
            reasons.append(f"{name}: {reason}")
        rows.append(
            {
                "module": name,
                "status": status,
                "elaborated_memories": e,
                "elaborated_memory_bits": elab.get(name, {}).get("memory_bits", 0),
                "pre_lowering_memories": p,
                "final_memories": f,
            }
        )
    return {
        "status": overall_status([row["status"] for row in rows]),
        "reason": "; ".join(reasons) or None,
        "modules": rows,
        "script_path": script_path,
    }


def parse_declared_blackboxes(text: str) -> set[str]:
    """Module names from ``select -list =A:blackbox =A:whitebox`` output.

    A fully selected module is listed as its bare name, followed by one
    ``<module>/<member>`` line per port/wire; only the bare-name lines are
    kept."""
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if line and "/" not in line:
            names.add(line[1:] if line.startswith("\\") else line)
    return names


def blackbox_modules(
    declared: set[str] | None, final_modules: dict[str, Any] | None
) -> list[str] | None:
    """Sorted names of the ``declared`` blackbox/whitebox modules (from the
    side invocation, read after ``hierarchy``) that still have at least one
    instance in the production run's **final** stats -- i.e. a blackbox that
    survived mapping, whose area ``stat -liberty`` cannot include.

    Membership comes from the module's own ``blackbox``/``whitebox``
    attribute, never from its name, so nothing here is classified as a
    memory and a memory-like name means nothing. ``None`` when either input
    is unavailable: an empty list would falsely claim there are none."""
    if declared is None or final_modules is None:
        return None
    surviving: set[str] = set()
    for stats in final_modules.values():
        by_type = stats.get("num_cells_by_type") if isinstance(stats, dict) else None
        if not isinstance(by_type, dict):
            continue
        for cell_type, count in by_type.items():
            bare = cell_type[1:] if cell_type.startswith("\\") else cell_type
            if bare in declared and _int(count) > 0:
                surviving.add(bare)
    return sorted(surviving)


def collect_memory_evidence(
    *,
    output_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    final_stats_path: str,
    adder_sources: tuple[str, ...] = (),
    adder_techmap_path: str | None = None,
    repo_root: str | None = None,
    parameters: dict[str, int | str] | None = None,
    liberty_path: str | None = None,
) -> tuple[dict[str, Any], list[str] | None]:
    """Run the side evidence invocation and return ``(memory_mapping,
    blackbox_modules)`` for the response.

    Called only after the production run has succeeded, so it can neither
    change nor pre-empt production errors. **Never raises**: a Yosys
    failure, timeout, or unreadable stage degrades ``memory_mapping`` to
    ``status: "indeterminate"`` with the failure in ``reason`` (and
    ``blackbox_modules`` to ``None``). The evidence script embeds absolute
    paths only, but the RTL may not (``$readmemh "reference/x.hex"``), so
    the run uses ``cwd=repo_root`` -- the same cwd as the main Yosys run.
    """
    from .synthesize import (
        SynthesizeError,
        _report_path,
        _run_yosys,
        _write_script_text,
    )

    final_modules = _load_modules(final_stats_path)

    script_path = os.path.join(output_dir, f"{hdl_toplevel}{EVIDENCE_SCRIPT_SUFFIX}")
    blackbox_list_path = os.path.join(
        output_dir, f"{hdl_toplevel}{_BLACKBOX_LIST_SUFFIX}"
    )
    library_cells_path = os.path.join(
        output_dir, f"{hdl_toplevel}{_LIBRARY_CELLS_SUFFIX}"
    )
    elaborated_path = os.path.join(
        output_dir, f"{hdl_toplevel}{_ELABORATED_STATS_SUFFIX}"
    )
    pre_lowering_path = os.path.join(
        output_dir, f"{hdl_toplevel}{_PRE_LOWERING_STATS_SUFFIX}"
    )
    reported_script = _report_path(script_path, repo_root=repo_root)
    try:
        _write_script_text(
            script_path,
            memory_evidence_script_text(
                sources=resolved_sources,
                hdl_toplevel=hdl_toplevel,
                blackbox_list_path=blackbox_list_path,
                elaborated_stats_path=elaborated_path,
                pre_lowering_stats_path=pre_lowering_path,
                adder_sources=adder_sources,
                adder_techmap_path=adder_techmap_path,
                parameters=parameters,
                liberty_path=liberty_path,
                library_cells_path=library_cells_path,
            ),
        )
        _run_yosys(script_path, cwd=repo_root)
    except (SynthesizeError, OSError) as exc:
        reason = _scrub_paths(f"memory evidence invocation failed: {exc}", repo_root)
        return _indeterminate(reason, reported_script), None

    try:
        with open(blackbox_list_path, encoding="utf-8") as handle:
            declared: set[str] | None = parse_declared_blackboxes(handle.read())
    except (OSError, UnicodeDecodeError):
        declared = None
    if declared is not None and liberty_path is not None:
        try:
            with open(library_cells_path, encoding="utf-8") as handle:
                declared -= parse_declared_blackboxes(handle.read())
        except (OSError, UnicodeDecodeError):
            declared = None

    mapping = build_memory_mapping(
        elaborated_modules=_load_modules(elaborated_path),
        pre_lowering_modules=_load_modules(pre_lowering_path),
        final_modules=final_modules,
        script_path=reported_script,
    )
    return mapping, blackbox_modules(declared, final_modules)
