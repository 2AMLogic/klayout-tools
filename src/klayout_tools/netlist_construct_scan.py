"""Scanner primitives for a written structural-Verilog netlist: the
comment-aware blanker and the unsupported-construct regexes.

Shared verbatim by ``klt place-and-route``'s netlist pre-flight
(``place_and_route_request._reject_unsupported_netlist_constructs``,
issues #1973 and #5792) and ``klt synthesize``'s post-run scan
(``synthesize_x_scan``, issues #5074 and #5792), so the synthesize-time
warnings and the P&R rejection cannot drift apart. The module lives in the
place_and_route group, next to the only code that rejects on these
constructs; ``synthesize_x_scan`` imports it across groups under exact
``shared_private_permissions`` grants (import-boundaries.json).
"""

from __future__ import annotations

import re

_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_verilog_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _blank_verilog_comments(text: str) -> list[str]:
    """``text``'s lines with every comment blanked out but **line numbering
    preserved** -- one output entry per input line, comment characters
    replaced by spaces rather than deleted.

    :func:`_strip_verilog_comments` deletes comments outright, which
    collapses the line structure a multi-line ``/* ... */`` spans and makes
    the result useless for reporting a line number.
    ``place_and_route_request._reject_unsupported_netlist_constructs`` and
    ``synthesize_x_scan`` need to name the exact 1-based netlist line a
    diagnostic points at (issue #1973 -- the whole point is to beat
    OpenSTA's own ``line N, syntax error`` to the punch), so they need this
    shape instead.
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        chars = list(line)
        i = 0
        while i < len(chars):
            if in_block:
                if line.startswith("*/", i):
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    in_block = False
                    continue
                chars[i] = " "
                i += 1
            elif line.startswith("/*", i):
                chars[i] = chars[i + 1] = " "
                i += 2
                in_block = True
            elif line.startswith("//", i):
                for j in range(i, len(chars)):
                    chars[j] = " "
                break
            else:
                i += 1
        out.append("".join(chars))
    return out


#: A ``signed`` qualifier on a port/wire declaration in a structural-Verilog
#: netlist -- ``output signed [15:0] sample;``, ``wire signed [7:0] mid;``,
#: ``input wire signed [3:0] a;`` (issue #1973). Anchored to a preceding
#: declaration keyword on purpose: an expression-level ``$signed(...)`` cast
#: is accepted by every downstream reader (and is the RTL-side replacement
#: this repo's own ``docs/guides/digital-review/rtl-style-guide.md``
#: recommends for a ``signed`` port), so it must never be flagged -- and
#: ``$signed(`` is never immediately preceded by one of these keywords plus
#: whitespace.
_NETLIST_SIGNED_DECL_RE = re.compile(
    r"\b(?:input|output|inout|wire|reg|logic)\s+signed\b"
)

#: A bare ``x``-valued Verilog constant literal -- ``5'hxx``, ``2'bxx``,
#: ``1'bx``, ``8'shX0`` (issue #1973). Requires at least one ``x``/``X``
#: digit, so an ordinary resolved constant (``1'b0``, ``16'd100``,
#: ``8'hbe``) never matches -- only the unresolved bits OpenSTA turns into
#: an unroutable ``GROUND``-typed net.
_NETLIST_X_CONSTANT_RE = re.compile(
    r"\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]*[xX][0-9a-fA-FxX_]*"
)

#: A behavioural Verilog statement at a line's start -- a ``reg`` declaration
#: or an ``always`` block (issue #5792). Anchored to the line start on
#: purpose: Yosys names flop instances ``_reg_0_`` and nets ``regbank_out``
#: routinely, so a bare ``\breg\b`` would flag structural netlists that route
#: fine today, while ``write_verilog``'s behavioural rendering of a surviving
#: unmapped latch always puts the keyword at the start of its own indented
#: line (verified live, Yosys 0.67: ``  reg q;`` / ``  always @*``). A
#: structural netlist -- module/port/wire declarations and cell
#: instantiations only -- never has a line beginning with either keyword.
_NETLIST_BEHAVIOURAL_LATCH_RE = re.compile(r"^\s*(reg|always)\b")
