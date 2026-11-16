"""Post-run scan of a written synthesize netlist for surviving bare
``x``-valued constants (issue #5074)."""

from __future__ import annotations

from .place_and_route_request import _NETLIST_X_CONSTANT_RE, _blank_verilog_comments

#: Bound on how many offending netlist line numbers
#: :func:`netlist_x_constant_warning` names (issue #5074).
X_CONSTANT_WARNING_MAX_LINES = 5


def netlist_x_constant_warning(
    netlist_text: str,
    *,
    cell_library: str | None = None,
    has_tie_cells: bool = True,
) -> dict[str, str]:
    """A ``warnings`` entry (issue #5074) when the written netlist still
    carries a bare ``x``-valued constant (``assign w = 24'hxxxxxx;``).

    ``klt place-and-route`` refuses such a netlist in its pre-flight (same
    regex, shared from :mod:`place_and_route_request`), and re-running
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
