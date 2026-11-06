"""Post-run scan of a written synthesize netlist for surviving bare
``x``-valued constants (issue #5074)."""

from __future__ import annotations

from .place_and_route_request import _NETLIST_X_CONSTANT_RE, _blank_verilog_comments

#: Bound on how many offending netlist line numbers
#: :func:`netlist_x_constant_warning` names (issue #5074).
X_CONSTANT_WARNING_MAX_LINES = 5


def netlist_x_constant_warning(netlist_text: str) -> dict[str, str]:
    """A ``warnings`` entry (issue #5074) when the written netlist still
    carries a bare ``x``-valued constant (``assign w = 24'hxxxxxx;``).

    ``klt place-and-route`` refuses such a netlist in its pre-flight (same
    regex, shared from :mod:`place_and_route_request`), and re-running
    ``klt synthesize`` cannot clear it once ``setundef -zero``/``hilomap``
    have already run -- so the user is told here, before P&R. Names the
    first few 1-based line numbers and the first offending line. Empty when
    the netlist is clean.
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
    return {
        "netlist_x_constants": (
            f"netlist still has {len(hits)} bare x-valued constant assign(s) at "
            f"line(s) {shown}{suffix}, e.g. '{hits[0][1]}'; `klt place-and-route` "
            "will refuse this netlist. Remove the undriven/unused source signal "
            "in the RTL or drive it explicitly."
        )
    }
