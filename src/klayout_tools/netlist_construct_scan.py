"""Scanner primitives for a written structural-Verilog netlist: the
comment-aware blanker, the unsupported-construct regexes, and the
behavioural-statement rejection message composed from them.

Shared verbatim by ``klt place-and-route``'s netlist pre-flight
(``place_and_route_request._reject_unsupported_netlist_constructs``,
issues #1973 and #5792) and ``klt synthesize``'s post-run scan
(``synthesize_x_scan``, issues #5074 and #5792), so the synthesize-time
warnings and the P&R rejection cannot drift apart. The one deliberate
asymmetry (issue #6274): the pre-flight refuses a wider set of behavioural
statements (:data:`_NETLIST_BEHAVIOURAL_RE`, a safety net for netlists that
did not come from ``klt synthesize``) than ``klt synthesize``'s latch warning
scans for (:data:`_NETLIST_BEHAVIOURAL_LATCH_RE`) -- always as a *superset*,
never the reverse. The module lives in the place_and_route group, next to the
only code that rejects on these constructs; ``synthesize_x_scan`` imports it
across groups under exact ``shared_private_permissions`` grants
(import-boundaries.json).
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
#:
#: This is the **latch**-shaped pattern ``klt synthesize``'s
#: ``netlist_behavioural_latch`` warning scans with (its narrative is only
#: true for what ``write_verilog -noattr`` emits), so it is deliberately left
#: narrow; the P&R pre-flight's own, wider net is
#: :data:`_NETLIST_BEHAVIOURAL_RE`, which must stay a superset of this one.
_NETLIST_BEHAVIOURAL_LATCH_RE = re.compile(r"^\s*(reg|always)\b")

#: Every behavioural statement the P&R pre-flight refuses (issues #5792,
#: #6274): :data:`_NETLIST_BEHAVIOURAL_LATCH_RE`'s ``reg``/``always`` plus the
#: SystemVerilog ``always_ff``/``always_comb``/``always_latch`` and ``initial``
#: forms, which OpenSTA's structural reader rejects with the same bare
#: ``[ERROR STA-0171] ... syntax error`` and which a netlist that did not come
#: from ``klt synthesize`` can carry. Same line-start anchor and the same
#: trailing ``\b`` -- so ``always_on_buf`` (a module), ``initialize_cell`` and
#: a continuation line starting ``initial_value`` are not flagged. A
#: false-positive sweep over the repo's tracked ``.v`` files (32; 17 flagged by
#: the narrow pattern, all RTL/testbench sources) found this pattern flags no
#: file the narrow one does not and no ``klt synthesize``-written netlist.
#: ``function``/``task`` are deliberately not included: nothing in the issue
#: shows a netlist carrying them, and nothing here verifies OpenSTA's reaction.
_NETLIST_BEHAVIOURAL_RE = re.compile(r"^\s*(reg|always(?:_ff|_comb|_latch)?|initial)\b")

#: A level-sensitive ``always @*`` / ``always @(*)`` header on its own line
#: (comment-blanked, so trailing blanks are all that may follow).
_NETLIST_LEVEL_ALWAYS_RE = re.compile(r"^\s*always\s*@\s*(?:\*|\(\s*\*\s*\))\s*$")

#: The first line of an ``always`` body that is a bare ``if (...)``.
_NETLIST_IF_BODY_RE = re.compile(r"^\s*if\s*\(")


def _unmapped_latch_rendering_line(lines: list[str]) -> int | None:
    """The 1-based line of the first ``always @*`` block in ``lines``
    (comment-blanked, as :func:`_blank_verilog_comments` returns them) that is
    Yosys's rendering of a gate-level latch that survived synthesis unmapped,
    else ``None`` (issue #6274).

    ``write_verilog -noattr`` writes a latch as a level-sensitive ``always
    @*`` header followed directly -- on the next non-blank line -- by a bare
    ``if (...)`` body (``if (en) q = d;``, ``if (!en) q = d;``, ``if (rst) q
    = 1'h0;`` / ``else if (en) q = d;``; verified live, Yosys 0.67, ``proc``
    and ``synth`` flows alike). It puts each latch's ``reg`` declaration in
    the declaration block, **not** next to the ``always`` -- so this is the
    evidence that the netlist holds an unmapped latch, and a flagged ``reg``
    line need not be adjacent to it. A flop renders as ``always
    @(posedge clk)`` and combinational RTL as ``always @(*)`` + ``begin``, so
    neither qualifies -- which is what keeps an RTL source passed as a netlist
    from being narrated as a latch.
    """
    for index, line in enumerate(lines):
        if _NETLIST_LEVEL_ALWAYS_RE.match(line) is None:
            continue
        for body in lines[index + 1 :]:
            if body.strip():
                if _NETLIST_IF_BODY_RE.match(body) is not None:
                    return index + 1
                break
    return None


def _behavioural_statement_message(
    netlist_path: str,
    lineno: int,
    line: str,
    statement: str,
    lines: list[str],
) -> str:
    """The composed rejection message for the behavioural statement at
    ``lines[lineno - 1]``, a ``'reg'``/``'always*'``/``'initial'`` keyword
    named by ``statement`` (issue #6274) -- shared verbatim by the P&R
    pre-flight's rejection so the construct and cause clauses cannot drift
    from the patterns that detect them.

    The message always names the construct, line and offending text; its
    *cause* clause is the latch narrative only when the netlist carries
    Yosys's unmapped-latch rendering (:func:`_unmapped_latch_rendering_line`),
    and otherwise the generic "behavioural source, not a structural netlist"
    clause -- a flop ``reg`` in an RTL file handed over as the netlist is not
    a latch, and none of the latch fixes apply to it.
    """
    construct = (
        f"netlist '{netlist_path}' line {lineno} is behavioural "
        f"Verilog (a '{statement}' statement): "
        f"{line.strip()} -- OpenSTA's Verilog reader (used by the "
        "'floorplan' stage) rejects behavioural statements with a "
        "syntax error. "
    )
    latch_lineno = _unmapped_latch_rendering_line(lines)
    if latch_lineno is None:
        cause = (
            "A structural netlist has only module/port/wire "
            "declarations and cell instances, and this file is "
            "behavioural Verilog, not a structural netlist -- did "
            "you pass RTL source instead of the gate-level netlist "
            "`klt synthesize` wrote (its report's `netlist_path`)? "
            "Synthesize the RTL with `klt synthesize` and pass that "
            "netlist, or replace the behavioural statements with "
            "cell instances."
        )
    else:
        cause = (
            f"The `always @*` block at line {latch_lineno} is how "
            "`write_verilog` renders a gate-level latch that "
            "survived synthesis unmapped -- the synthesis library "
            "had no latch cell to map it onto (see the `klt "
            "synthesize` response's `structural.unexpected_latches` "
            "and `instance_counts_by_type` `$_DLATCH_*_` counts; its "
            "`warnings.netlist_behavioural_latch` names these "
            "lines). Use a `cell_library` that has a latch cell, "
            "give `klt synthesize`'s `latch_map` a target, or "
            "remove the inferred latch from the RTL."
        )
    return construct + cause
