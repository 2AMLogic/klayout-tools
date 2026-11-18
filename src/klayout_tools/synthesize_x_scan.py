"""Post-run scan of a written synthesize netlist for constructs
`klt place-and-route`'s own pre-flight will refuse -- surviving bare
``x``-valued constants (issue #5074) and the behavioural ``reg``/``always``
rendering of a latch the synthesis library had no cell for (issue #5792) --
so the caller hears it from the stage that wrote the netlist, not as a raw
syntax error from a later stage."""

from __future__ import annotations

from .netlist_construct_scan import (
    _NETLIST_BEHAVIOURAL_LATCH_RE,
    _NETLIST_X_CONSTANT_RE,
    _blank_verilog_comments,
)

#: Bound on how many offending netlist line numbers
#: :func:`netlist_x_constant_warning` names (issue #5074).
X_CONSTANT_WARNING_MAX_LINES = 5

#: Bound on how many offending netlist line numbers
#: :func:`netlist_behavioural_latch_warning` names (issue #5792).
BEHAVIOURAL_LATCH_WARNING_MAX_LINES = 5


def netlist_x_constant_warning(
    netlist_text: str,
    *,
    cell_library: str | None = None,
    has_tie_cells: bool = True,
) -> dict[str, str]:
    """A ``warnings`` entry (issue #5074) when the written netlist still
    carries a bare ``x``-valued constant (``assign w = 24'hxxxxxx;``).

    ``klt place-and-route`` refuses such a netlist in its pre-flight (same
    regex, shared from :mod:`netlist_construct_scan`), and re-running
    ``klt synthesize`` cannot clear it once ``setundef -zero``/``hilomap``
    have already run -- so the user is told here, before P&R. Names the
    first few 1-based line numbers and the first offending line. Empty when
    the netlist is clean.

    ``has_tie_cells`` says whether this run's ``cell_library`` had a
    verified tie-cell mapping (``synthesize._TIE_CELLS``). It decides the
    **advice**, not the detection: with a mapping the x bits survived
    ``setundef -zero``/``hilomap``, so the RTL is at fault; without one that
    pass never ran at all, and telling the user to change their RTL sends
    them after the wrong cause. ``cell_library`` names the offending key in
    that second case.
    """
    hits = [
        (lineno, line.strip())
        for lineno, line in enumerate(_blank_verilog_comments(netlist_text), start=1)
        if _NETLIST_X_CONSTANT_RE.search(line)
    ]
    if not hits:
        return {}
    shown = ", ".join(str(n) for n, _ in hits[:X_CONSTANT_WARNING_MAX_LINES])
    more = len(hits) - X_CONSTANT_WARNING_MAX_LINES
    suffix = f" (+{more} more)" if more > 0 else ""
    if has_tie_cells:
        advice = (
            "Remove the undriven/unused source signal in the RTL or drive it "
            "explicitly."
        )
    else:
        named = f" '{cell_library}'" if cell_library else ""
        advice = (
            f"cell_library{named} has no verified tie-cell mapping, so the "
            "`setundef -zero`/`hilomap` pass that would have replaced these x "
            "bits never ran -- see the unsupported_cell_library warning for the "
            "cell_library keys that do have one. Correct the cell_library "
            "before changing the RTL."
        )
    return {
        "netlist_x_constants": (
            f"netlist still has {len(hits)} bare x-valued constant assign(s) at "
            f"line(s) {shown}{suffix}, e.g. '{hits[0][1]}'; `klt place-and-route` "
            f"will refuse this netlist. {advice}"
        )
    }


def netlist_behavioural_latch_warning(netlist_text: str) -> dict[str, str]:
    """A ``warnings`` entry (issue #5792) when the written netlist carries a
    behavioural Verilog statement -- a ``reg`` declaration or ``always``
    block -- instead of gate-level cell instances.

    ``write_verilog`` renders a gate-level latch that survived synthesis
    unmapped this way when the resolved library has no latch cell to map it
    onto: ``dfflibmap`` maps flip-flops only, and ``latch_map`` (issue
    #4458) has no target to give it. OpenSTA's structural reader rejects the
    shape outright, so ``klt place-and-route`` refuses this netlist in its
    pre-flight (same regex, shared from :mod:`netlist_construct_scan`) --
    the caller is told here, before P&R. Names the first few 1-based line
    numbers and the first offending line. Empty when the netlist is clean
    (which is also the ``latch_map``-mapped shape: the mapped latch is a
    cell instance).
    """
    hits = [
        (lineno, line.strip())
        for lineno, line in enumerate(_blank_verilog_comments(netlist_text), start=1)
        if _NETLIST_BEHAVIOURAL_LATCH_RE.search(line)
    ]
    if not hits:
        return {}
    shown = ", ".join(str(n) for n, _ in hits[:BEHAVIOURAL_LATCH_WARNING_MAX_LINES])
    more = len(hits) - BEHAVIOURAL_LATCH_WARNING_MAX_LINES
    suffix = f" (+{more} more)" if more > 0 else ""
    return {
        "netlist_behavioural_latch": (
            f"netlist still has {len(hits)} behavioural Verilog statement(s) "
            f"(a `reg` declaration or `always` block) at line(s) {shown}{suffix}, "
            f"e.g. '{hits[0][1]}': an inferred latch survived synthesis "
            "unmapped -- the cell_library has no latch cell to map it onto "
            "(or the `latch_map` entry did not cover it), so `write_verilog` "
            "rendered it behaviourally instead of as a cell instance; see "
            "`structural.unexpected_latches` and `instance_counts_by_type` "
            "`$_DLATCH_*_`. `klt place-and-route` will refuse this netlist. "
            "Use a cell_library that has a latch cell, give `latch_map` a "
            "target, or remove the latch from the RTL."
        )
    }


def netlist_warnings(
    netlist_text: str,
    *,
    cell_library: str | None = None,
    has_tie_cells: bool = True,
) -> dict[str, str]:
    """Both post-run netlist scans in one ``warnings`` splat (issues #5074,
    #5792): :func:`netlist_x_constant_warning` plus
    :func:`netlist_behavioural_latch_warning`, over one read of the written
    netlist. ``run_synthesize`` is the only caller; the two functions stay
    public for their own focused tests.
    """
    warnings = netlist_x_constant_warning(
        netlist_text,
        cell_library=cell_library,
        has_tie_cells=has_tie_cells,
    )
    warnings.update(netlist_behavioural_latch_warning(netlist_text))
    return warnings
