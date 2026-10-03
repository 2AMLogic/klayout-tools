"""Convert a `klt place-and-route` `verilog_path` gate-level Verilog netlist
into the plain-element-shaped SPICE `klt lvs` needs on its reference side
(issue #1336).

**The gap this closes.** `klt place-and-route` writes the as-built,
gate-level netlist of a routed digital design as Verilog (`verilog_path`,
issue #996) -- module instantiations of standard-cell library types,
port-connected by name, no expressions, no behavioral statements (OpenROAD's
own `write_verilog`, plain, no `-include_pwr_gnd`). `klt lvs` compares SPICE
netlists only; nothing in `klt` turned that Verilog into a comparable
reference, so `klt lvs` could not run at all against a `klt
place-and-route` result. See `docs/cli/lvs.md`'s "Netlist form: gate-level
Verilog (`reference.form = "gate-level-verilog"`)" section for the
caller-facing contract this module implements.

**Shape of the conversion.** Each standard-cell instance
(`<cell_type> <inst_name> ( .PORT(NET), ... );`) becomes a plain SPICE
subcircuit call, `X<inst> <nets in the cell's own declared pin order>
<cell_type>`, exactly the shape `klt extract --abstract-cells` (issue #620)
already writes for a black-box abstracted cell -- both sides describe a
standard cell as a pin-only black box, never its internal transistors, so
they compare structurally instead of a real-devices-vs-black-box mismatch
that could never be clean. Every distinct instantiated library cell type
gets one pin-only `.SUBCKT <cell_type> ... .ENDS` stub (no devices), and
every parsed Verilog `module` becomes its own `.SUBCKT`/`.ENDS` block -- so a
hierarchical input (unusual for `verilog_path`, which is always a single
flat module post-place-and-route, but not assumed away here) converts
correctly, with an inner module's own `.SUBCKT` boundary standing in for a
"library cell" lookup at any call site that names it.

**Pin order comes from the real PDK library, never hardcoded** (this
issue's own acceptance criterion): the caller resolves each library cell
type's real `.SUBCKT <cell> <pins...>` declaration from the PDK's own
`libs.ref/<library>/spice/<library>.spice` (or `.../cdl/<library>.cdl`) file
-- the same `libs_ref` asset `klt pdk`/`klt place-and-route` already resolve
-- via :func:`parse_subckt_pin_orders`, and hands this module a
`pin_order_lookup(cell_type) -> list[str] | None` callback
(:func:`convert_gate_level_verilog`). A cell type the lookup does not
resolve is a hard error naming the missing cell, never a silent skip (the
gluing/PDK-resolution side of this contract lives in `klt lvs`'s own
`lvs.py`, not here, so this module stays PDK-install-free and unit-testable
without a real PDK on disk).

**No power/ground pins carried, by design.** `docs/cli/place-and-route.md`'s
"As-built netlist" section documents that `verilog_path` is written without
`-include_pwr_gnd`, so it never carries `VPWR`/`VGND`/well-tie connections
with or without `request.power` -- there is nothing in the Verilog to
recover them from. Each stub's declared pin list is therefore the *signal*
subset of the real PDK pin order: whichever of that cell type's real pins
actually appear in at least one Verilog instance connection across the
whole netlist, in the PDK's own declared relative order.

A layout-side abstraction that *does* carry power pins (which `klt extract
--abstract-cells` normally will) still compares cleanly: KLayout's comparer
matches these black-box cell circuits on their common, name-matched signal
pins and tolerates the layout's extra pins and extra power nets (measured
on a real routed sky130 `gcd`: `status: "match"`, no `pin.unmatched`). The
real, disclosed cost is the other side of that coin -- **a power-net defect
is invisible to a Verilog-derived compare**, since the reference has no
power connectivity to contradict it. See `docs/cli/lvs.md`'s "No
power/ground pins" note; power-grid correctness belongs to `klt power`/`klt
drc`, not here.

**A port-to-port (or port-to-internal-net) `assign` alias is tracked, not
resolved, here** (issue #2021). `assign <net> = <net>;` is resolved
transparently for every *instance* connection (an aliased net used as
`.PORT(NET)` reads back as its ultimate target, above), but a module's own
declared *port list* is emitted exactly as written -- an aliased port
becomes its own `.SUBCKT` pin, with nothing inside the body ever
referencing it (every instance that would have used it was rewritten to the
alias's target instead). Left alone, that pin reads back from
`NetlistSpiceReader` as an isolated, disconnected net and `klt lvs` reports
it as an unmatched pin/net even when the layout is completely correct --
gate-level Verilog routinely carries a port-to-port alias like `assign
dbg_uart_byte[i] = rx_byte[i];`, where the layout has exactly one physical
net for both names. This module only *records* which ports are
alias-driven (:func:`collect_gate_level_port_aliases`,
:func:`parse_gate_level_verilog`'s own `port_aliases` field) -- there is no
SPICE-text way to
express "two distinct pin names, one net" (repeating a net name across two
`.SUBCKT` header positions would just drop one of the two declared names).
`klayout_tools.lvs._apply_gate_level_port_aliases` applies the actual fix,
joining the alias pin's net onto its canonical target's net in the real
`kdb.Netlist` object built from this module's SPICE text, after it is read
back -- see that function's docstring, and `docs/cli/lvs.md`'s
`topology.reference_port_alias_joined` finding.

**Deliberately narrow, deliberately loud** (mirrors
`klayout_tools.netlist_normalize`'s own discipline for the sibling
subckt-call conversion): only the structurally simple constructs a
gate-level, already-flattened netlist actually needs are supported --
`module`/`endmodule`, `input`/`output`/`inout` port declarations (with an
optional `[msb:lsb]` bus range), plain instance calls with **named**
(`.PORT(NET)`) connections, a bare identifier or single-index bit-select
(`net`/`bus[3]`) or `1'b0`/`1'b1` constant as a connection expression (or
one of the multi-bit connections described below), a
simple `assign <net> = <net>;` alias, and a plain concatenation `assign
<net> = { <net>, <net>, ... };` (issue #2372 -- Yosys's routine rendering of
a vector alias or tie-off padding), expanded MSB-first into one per-bit
alias using the module's own `wire`/`input`/`output`/`inout` declared
widths; the two sides' total widths must agree exactly. A concatenation
operand must be a plain net or single-index bit-select -- replication
(`{N{x}}`), literals (`1'b0`) and part-selects (`a[7:4]`) inside a
concatenation stay rejected. A Verilog escaped identifier
(`\\name`) is accepted wherever a name is, as the single atomic token
Verilog's own whitespace-terminated escape rule defines -- so a
place-and-route-flattened hierarchy path like `\\my_array[0].u_inst/_01_` is
the literal net name `my_array[0].u_inst/_01_`, never re-read as a
bit-select of a bus `my_array` (issue #1371).

**Multi-bit instance connections onto a vector port** (issue #2657) -- the
shapes Yosys and OpenROAD `write_verilog` emit when a black-box hard macro
(an SRAM, any IP with a bus pin) is instantiated -- are also accepted as a
`.PORT(<expr>)` connection: a concatenation `{a, b[3], c[7:4], 2'b01}`, a
multi-bit range slice `bus[7:0]` (also on an escaped base, `\\bus [7:0]`), a
sized constant wider than one bit (`8'h00`, `16'hffff`, `4'b1010`, each bit
tied to the module-scoped `__CONST0__`/`__CONST1__` net), and a whole
declared bus named plainly (`.PORT(bus)`) -- and, so a whole-bus
connection resolves through Yosys's routine `assign dout = _05_;`, a plain
`assign` between two declared buses aliases bit for bit (equal widths
required). Each resolves at parse time to the MSB-first list of per-bit
nets, from the module's own declared widths; a range slice must name a
declared bus in its declared direction, and a plain concatenation operand
must be declared. Replication (`{N{x}}`), a
nested concatenation, an unsized or `x`/`z` constant, a constant whose value
does not fit its width, and any other expression stay rejected. The per-bit
nets are bound to the cell's real per-bit pins only in
:func:`convert_gate_level_verilog`, once the PDK pin order is known: a
library cell's `<PORT>[<index>]` pins are ordered by **numeric bit index,
descending** (the highest index is the MSB -- the `[N-1:0]` convention
every real vector-ported macro checked declares its Verilog ports with:
sky130's OpenRAM `sky130_sram_*` and gf180mcu's `gf180mcu_fd_ip_sram__*`,
whose `.subckt`/`.cdl` pins are named `din0[31]`..`din0[0]` / `D[7]`..`D[0]`).
The index, not the pin's position in the `.subckt` header, decides the
binding, so a library that declares its bits in another header order still
binds correctly; a library whose bus pins are not named `<PORT>[<index>]`
(e.g. `D<7>`) has no per-bit pins to bind and fails loudly rather than
guessing. A width mismatch between the expression and the pin
count is an error naming the instance, port and both widths, never a
silent truncate or pad. (An `assign` right-hand side keeps its own,
narrower concatenation grammar above -- unchanged by this.)

Anything else -- positional instance connections, a non-constant
expression, `always`/`case`/other behavioral statements -- raises
:class:`VerilogNetlistError` naming the offending construct, never a silent
best-effort guess (the same "a wrong conversion in a sign-off tool must
never pass silently" rationale `netlist_normalize.py` documents for its own
scope).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Verilog block (`/* ... */`) and line (`// ...`) comments, stripped before
#: any other parsing. `re.S` so a block comment can span newlines.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")

#: Splits the (comment-stripped) source on `endmodule` -- the one keyword in
#: this grammar with no trailing `;`, so it cannot be handled by the
#: semicolon-based statement splitter below. Every chunk but the last
#: (trailing, must be blank) is one `module ... ... endmodule` body.
_ENDMODULE_RE = re.compile(r"\bendmodule\b")

#: An identifier: a plain Verilog identifier, or an escaped identifier
#: (`\name`, terminated by whitespace -- Verilog's own escape convention for
#: identifiers containing characters that would otherwise be delimiters).
#: The post-backslash character class here is deliberately the *plain*
#: identifier one: this pattern validates module/port/direction-statement
#: names, where the escape only ever guards against a keyword collision.
#: Connection expressions do **not** use it for an escaped name -- see
#: :data:`_ESCAPED_IDENT_RE`.
_IDENT_RE = re.compile(r"\\?[A-Za-z_$][A-Za-z0-9_$]*")

#: A Verilog escaped identifier in its full generality: a leading backslash
#: followed by a run of non-whitespace characters, terminated by whitespace
#: (or the end of the expression). Verilog's escape convention makes *every*
#: printable, non-whitespace character up to that terminator part of the
#: literal name -- including `[`, `]`, `.` and `/`, which synthesis/
#: place-and-route emits when it flattens a `generate`/`genvar` hierarchy
#: into one net name (`\my_array[0].u_inst/_01_`, issue #1371). Such a name
#: is one atomic token and must never be re-parsed for an embedded
#: bit-select: the `[0]` in it is part of the name, not a bus index.
_ESCAPED_IDENT_RE = re.compile(r"\\\S+")

#: A connection expression this module accepts as a plain net reference:
#: a *plain* identifier, optionally followed by a single-index bit-select
#: (`bus[3]`) -- never a range slice (`bus[7:0]`), which is rejected
#: explicitly (see :func:`_parse_connection_expr`). Escaped identifiers
#: never reach these patterns; :func:`_parse_escaped_connection_expr`
#: handles them first, using the select suffixes below for the one shape
#: that *is* a select on an escaped base (`\bus [3]`, whitespace-separated).
_BIT_SELECT_RE = re.compile(
    r"^(?P<base>[A-Za-z_$][A-Za-z0-9_$]*)\[(?P<index>[^\]:]+)\]$"
)
_RANGE_SELECT_RE = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*\[[^\]]*:[^\]]*\]$")
_INDEX_SUFFIX_RE = re.compile(r"^\[(?P<index>[^\]:]+)\]$")
_RANGE_SUFFIX_RE = re.compile(r"^\[[^\]]*:[^\]]*\]$")

#: A `<width>'b<bits>` constant, e.g. `1'b0`/`1'b1` -- the only constant
#: shape this module recognises (see the module docstring's "no power/
#: ground" note: constants are otherwise vanishingly rare in a routed,
#: technology-mapped netlist, since tie cells normally carry constants
#: instead).
_CONST_RE = re.compile(r"^\d*'[bB]([01])$")

#: A sized Verilog constant of any base -- `8'h00`, `16'hffff`, `4'b1010`,
#: `3'd5`, `1'h0` (issue #2657). Only an instance connection (and an operand
#: of a connection concatenation) reads this; a width above one expands to
#: one `__CONST0__`/`__CONST1__` net per bit, MSB-first. The digit class is
#: deliberately wide (`x`/`z`/`?` included) so an unknown/high-impedance bit
#: is matched here and rejected by name rather than falling through to a
#: vaguer "not a plain net reference" error.
_SIZED_CONST_RE = re.compile(
    r"^(?P<width>\d+)\s*'(?P<signed>[sS])?(?P<base>[bBoOdDhH])\s*"
    r"(?P<digits>[0-9a-fA-FxXzZ?_]+)$"
)
_CONST_BASES = {"b": 2, "o": 8, "d": 10, "h": 16}

#: A multi-bit range slice with integer bounds, on a plain base
#: (`bus[7:0]`) or, via :data:`_ESCAPED_RANGE_RE`, on an escaped base
#: written with Verilog's terminating whitespace (`\bus [7:0]`).
_RANGE_SLICE_RE = re.compile(
    r"^(?P<base>[A-Za-z_$][A-Za-z0-9_$]*)\s*"
    r"\[\s*(?P<msb>-?\d+)\s*:\s*(?P<lsb>-?\d+)\s*\]$"
)
_ESCAPED_RANGE_RE = re.compile(
    r"^(?P<base>\\\S+)\s+\[\s*(?P<msb>-?\d+)\s*:\s*(?P<lsb>-?\d+)\s*\]$"
)

#: Net names substituted for a `1'b0`/`1'b1` connection, module-scoped (every
#: constant reference within one module shares the same synthesized net, the
#: same "one tie net feeds many loads" shape a real tie-cell produces).
_CONST_NET_NAMES = {"0": "__CONST0__", "1": "__CONST1__"}

#: Declaration keywords that introduce a port direction (and therefore also
#: contribute to a module's per-port bit width via an optional `[msb:lsb]`
#: range) -- see :func:`_parse_direction_statement`.
_DIRECTION_KEYWORDS = ("input", "output", "inout")

#: Declaration keywords this module recognises but does not need to act on
#: (an internal wire has no SPICE-level counterpart to declare -- a node
#: exists in SPICE wherever it is referenced, never via a separate
#: declaration card).
_IGNORED_DECL_KEYWORDS = ("wire", "reg", "tri", "supply0", "supply1")

_DIRECTION_STMT_RE = re.compile(
    r"^(?:" + "|".join(_DIRECTION_KEYWORDS) + r")\b\s*"
    r"(?:reg\b\s*)?"
    r"(?:\[\s*(?P<msb>-?\d+)\s*:\s*(?P<lsb>-?\d+)\s*\]\s*)?"
    r"(?P<names>.*)$",
    re.S,
)


class VerilogNetlistError(Exception):
    """Raised when a gate-level Verilog netlist cannot be converted
    correctly and unambiguously to plain-element-shaped SPICE: an
    unsupported construct (positional instance ports, an expression this
    module does not model, a multi-bit connection whose width disagrees
    with the port it drives), a malformed declaration, or a library cell
    instantiated with no resolvable pin order. Always names the offending
    construct/instance -- never a silent best-effort guess that could
    degrade a sign-off comparison invisibly.
    """


@dataclass
class _Instance:
    cell: str
    name: str
    #: `{<port name>: <connection expression text>}`, in encounter order --
    #: resolved into :attr:`connections` by :func:`_resolve_connection` only
    #: once every declaration in the module is seen (issue #2657: a
    #: multi-bit expression needs the declared widths, which may follow the
    #: instance).
    expressions: dict[str, str] = field(default_factory=dict)
    #: `{<port name>: <net name> | [<net name>, ...]}`, in encounter order --
    #: one net for a scalar connection, or the MSB-first per-bit net list of
    #: a multi-bit one (issue #2657). A `.PORT()` empty connection or a
    #: missing (never mentioned) pin is not recorded here --
    #: :func:`convert_gate_level_verilog` synthesizes a fresh disconnected
    #: net for it at write time, once the stub's full declared pin list is
    #: known.
    connections: dict[str, str | list[str]] = field(default_factory=dict)


@dataclass
class _Module:
    name: str
    #: Fully bit-expanded boundary pin names, in declared order (e.g. a
    #: `[15:0] a_in` port becomes `a_in[15]`, `a_in[14]`, ..., `a_in[0]`).
    ports: list[str]
    instances: list[_Instance]
    #: `{<alias>: <target net>}` from a simple `assign <alias> = <target>;`
    #: statement -- resolved (transitively) before nets are written.
    aliases: dict[str, str]


def _strip_comments(text: str) -> str:
    text = _BLOCK_COMMENT_RE.sub(" ", text)
    return _LINE_COMMENT_RE.sub("", text)


def _split_top_level(text: str, seps: str = ",") -> list[str]:
    """Split ``text`` on any character in ``seps`` that is not nested inside
    ``()``/``[]``/`{}` -- used for both a module's port list and one
    instance's `.PORT(NET), .PORT(NET)` connection list, neither of which
    can contain a top-level comma any other way in this grammar."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
        if char in seps and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def _parse_header(statement: str) -> tuple[str, list[str]]:
    """Parse a `module <name> ( <port>, ... )` statement (the `;` already
    stripped by the caller) into `(name, port_list)`."""
    rest = statement.strip()
    if not rest.lower().startswith("module"):
        raise VerilogNetlistError(
            f"expected a 'module' declaration to open this block, found: "
            f"{statement.strip()[:80]!r}"
        )
    rest = rest[len("module") :].strip()
    open_paren = rest.find("(")
    if open_paren == -1:
        raise VerilogNetlistError(
            f"'module' declaration has no port list: {statement.strip()[:80]!r}"
        )
    name = rest[:open_paren].strip()
    if not _IDENT_RE.fullmatch(name):
        raise VerilogNetlistError(f"'module' declaration has no valid name: {rest!r}")
    close_paren = rest.rfind(")")
    if close_paren == -1 or close_paren < open_paren:
        raise VerilogNetlistError(
            f"'module {name}' declaration's port list is not closed: "
            f"{statement.strip()[:80]!r}"
        )
    port_names = _split_top_level(rest[open_paren + 1 : close_paren])
    trailer = rest[close_paren + 1 :].strip()
    if trailer:
        raise VerilogNetlistError(
            f"'module {name}' declaration has unexpected trailing content "
            f"{trailer!r} -- only a plain, non-ANSI port list "
            "('module name(a, b, c);') is supported"
        )
    for port in port_names:
        if not _IDENT_RE.fullmatch(port):
            raise VerilogNetlistError(
                f"'module {name}' port list entry {port!r} is not a plain "
                "port name -- ANSI-style inline port declarations "
                "('module name(input a, output b);') are not supported, "
                "only 'module name(a, b); input a; output b;'"
            )
    return _unescape(name), [_unescape(p) for p in port_names]


def _unescape(identifier: str) -> str:
    """Strip a Verilog escaped identifier's leading backslash. Verilog's own
    escape convention only exists to let a name (still terminated by
    whitespace/a delimiter) contain characters that would otherwise be
    parsed as syntax; the remaining text is used as-is, exactly like every
    real Verilog reader treats it."""
    return identifier[1:] if identifier.startswith("\\") else identifier


def _expand_range(base: str, msb: int, lsb: int) -> list[str]:
    step = -1 if msb >= lsb else 1
    return [f"{base}[{bit}]" for bit in range(msb, lsb + step, step)]


def _parse_direction_statement(
    statement: str, port_widths: dict[str, list[str]]
) -> None:
    match = _DIRECTION_STMT_RE.match(statement.strip())
    if match is None:
        raise VerilogNetlistError(
            f"could not parse port direction declaration: {statement.strip()[:80]!r}"
        )
    names_text = match.group("names")
    names = [_unescape(n) for n in _split_top_level(names_text)]
    if not names:
        raise VerilogNetlistError(
            f"port direction declaration names no signal: {statement.strip()[:80]!r}"
        )
    for name in names:
        if not _IDENT_RE.fullmatch(name):
            # Re-validate the already-unescaped name so a stray range/
            # expression in the name list fails loudly instead of silently
            # becoming a malformed net name.
            raise VerilogNetlistError(
                f"port direction declaration has a non-identifier entry "
                f"{name!r}: {statement.strip()[:80]!r}"
            )
        if match.group("msb") is not None:
            msb = int(match.group("msb"))
            lsb = int(match.group("lsb"))
            port_widths[name] = _expand_range(name, msb, lsb)
        else:
            port_widths.setdefault(name, [name])


def _parse_escaped_connection_expr(expr: str) -> str:
    """Resolve a connection expression that opens with a Verilog escaped
    identifier (`\\...`) to a net name.

    Verilog terminates an escaped identifier at the first whitespace, so the
    whole non-whitespace run after the backslash is one literal net name --
    `[`, `]`, `.` and `/` included (a place-and-route tool writes exactly
    this when it flattens a `generate`/`genvar` hierarchy path into a single
    name, `\\my_array[0].u_inst/_01_`; issue #1371). It is therefore never
    re-parsed against the plain-identifier bit-select grammar, which would
    misread that embedded `[0]` as a bus index and reject the connection.

    A select on an escaped name is written with the terminating whitespace
    made explicit (`\\bus [3]`), so anything left after the escaped token is
    handled here: a single-index select is folded into the same
    `<net>[<index>]` flat name every other net uses. This function resolves
    to *one* net, so a multi-bit range slice is rejected here exactly as it
    is for a plain base name -- which is what an `assign` operand still
    gets. An instance connection never reaches this with a range slice:
    :func:`_resolve_connection` expands `\\bus [7:0]` per bit first (issue
    #2657), the same as `bus[7:0]`, so the escaped and plain paths accept
    the same multi-bit shapes.
    """
    match = _ESCAPED_IDENT_RE.match(expr)
    if match is None:
        # A lone backslash: the escape opened a name and the terminating
        # whitespace arrived immediately, so there is no name at all.
        raise VerilogNetlistError(
            f"connection {expr!r} opens a Verilog escaped identifier that "
            "names nothing -- an escaped name must have at least one "
            "character between its backslash and the terminating whitespace"
        )
    name = _unescape(match.group(0))
    rest = expr[match.end() :].strip()
    if not rest:
        return name
    if _RANGE_SUFFIX_RE.match(rest):
        raise VerilogNetlistError(
            f"connection {expr!r} is a multi-bit range slice -- only a plain "
            "net name or a single-index bit-select ('bus[3]') is supported "
            "for an instance port connection"
        )
    index_match = _INDEX_SUFFIX_RE.match(rest)
    if index_match:
        return f"{name}[{index_match.group('index').strip()}]"
    raise VerilogNetlistError(
        f"connection {expr!r} is not a plain net reference, a single-index "
        "bit-select, or a '1'b0'/'1'b1' constant -- concatenation and "
        "general expressions are not supported"
    )


def _parse_connection_expr(expr: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve one single-net expression to a net name, or raise
    :class:`VerilogNetlistError` for anything this narrow grammar does not
    model (a range slice, concatenation, a non-constant expression).

    The scalar core of both an `assign` operand and an instance connection.
    An instance connection goes through :func:`_resolve_connection` first,
    which handles the multi-bit shapes (issue #2657) and falls back to this
    for everything else."""
    expr = expr.strip()
    const_match = _CONST_RE.match(expr)
    if const_match:
        return _CONST_NET_NAMES[const_match.group(1)]
    if expr.startswith("\\"):
        return _parse_escaped_connection_expr(expr)
    if _RANGE_SELECT_RE.match(expr):
        raise VerilogNetlistError(
            f"connection {expr!r} is a multi-bit range slice -- only a plain "
            "net name or a single-index bit-select ('bus[3]') is supported "
            "for an instance port connection"
        )
    if _IDENT_RE.fullmatch(expr) or _BIT_SELECT_RE.match(expr):
        return _unescape(expr)
    raise VerilogNetlistError(
        f"connection {expr!r} is not a plain net reference, a single-index "
        "bit-select, or a '1'b0'/'1'b1' constant -- concatenation and "
        "general expressions are not supported"
    )


def _parse_instance_statement(statement: str) -> _Instance:
    rest = statement.strip()
    open_paren = rest.find("(")
    if open_paren == -1:
        raise VerilogNetlistError(
            f"expected an instance declaration ('<cell> <inst> ( ... )'), "
            f"found: {rest[:80]!r}"
        )
    head = _split_top_level(rest[:open_paren], seps=" \t\n")
    if len(head) != 2:
        raise VerilogNetlistError(
            f"expected '<cell_type> <instance_name> (' before the "
            f"connection list, found: {rest[:80]!r}"
        )
    cell, inst_name = (_unescape(h) for h in head)
    close_paren = rest.rfind(")")
    if close_paren == -1 or close_paren < open_paren:
        raise VerilogNetlistError(
            f"instance '{inst_name}' ('{cell}') connection list is not closed"
        )
    trailer = rest[close_paren + 1 :].strip()
    if trailer:
        raise VerilogNetlistError(
            f"instance '{inst_name}' ('{cell}') has unexpected trailing "
            f"content {trailer!r} after its connection list"
        )
    body = rest[open_paren + 1 : close_paren].strip()
    expressions: dict[str, str] = {}
    if body:
        for item in _split_top_level(body):
            if not item.startswith("."):
                raise VerilogNetlistError(
                    f"instance '{inst_name}' ('{cell}') has a positional "
                    f"(non-named) connection {item!r} -- only named "
                    "'.PORT(NET)' connections are supported"
                )
            port_open = item.find("(")
            port_close = item.rfind(")")
            if port_open == -1 or port_close == -1 or port_close < port_open:
                raise VerilogNetlistError(
                    f"instance '{inst_name}' ('{cell}') connection {item!r} "
                    "is not a well-formed '.PORT(NET)' entry"
                )
            port_name = item[1:port_open].strip()
            if not _IDENT_RE.fullmatch(port_name):
                raise VerilogNetlistError(
                    f"instance '{inst_name}' ('{cell}') connection {item!r} "
                    "names an invalid port"
                )
            expr = item[port_open + 1 : port_close].strip()
            if not expr:
                # `.PORT()` -- an explicit no-connect. Leave it unrecorded;
                # `_convert_module` synthesizes a fresh disconnected net for
                # any declared pin with no recorded connection.
                continue
            expressions[_unescape(port_name)] = expr
    return _Instance(cell=cell, name=inst_name, expressions=expressions)


def _resolve_instance_connections(
    instance: _Instance, net_widths: dict[str, list[str]]
) -> None:
    """Fill ``instance.connections`` from its raw ``expressions``. Deferred
    like an `assign` until the whole module is read (issue #2657): a
    multi-bit connection expands against declared widths, and a declaration
    may follow the instance that uses it."""
    for port, expr in instance.expressions.items():
        try:
            instance.connections[port] = _resolve_connection(expr, net_widths)
        except VerilogNetlistError as exc:
            raise _instance_error(instance, port, str(exc)) from exc


def _instance_error(
    instance: _Instance, port: str, message: str
) -> VerilogNetlistError:
    return VerilogNetlistError(
        f"instance '{instance.name}' ('{instance.cell}') port '{port}': {message}"
    )


def _sized_constant_bits(expr: str) -> list[str] | None:
    """The MSB-first `__CONST0__`/`__CONST1__` net list of a sized Verilog
    constant (`8'h00`, `4'b1010`, `1'h0`; issue #2657), or ``None`` when
    ``expr`` is not one. An `x`/`z`/`?` digit, a zero width, or a value that
    does not fit its declared width is an error -- Verilog would silently
    truncate the last; a sign-off converter must not."""
    match = _SIZED_CONST_RE.match(expr)
    if match is None:
        return None
    width = int(match.group("width"))
    digits = match.group("digits").replace("_", "")
    if width == 0 or not digits:
        raise VerilogNetlistError(f"constant {expr!r} has no bits")
    if re.search(r"[xXzZ?]", digits):
        raise VerilogNetlistError(
            f"constant {expr!r} has an 'x'/'z' (unknown/high-impedance) bit, "
            "which has no net to tie to"
        )
    try:
        value = int(digits, _CONST_BASES[match.group("base").lower()])
    except ValueError as exc:
        raise VerilogNetlistError(
            f"constant {expr!r} has a digit that is not valid in its base"
        ) from exc
    if value >= 1 << width:
        raise VerilogNetlistError(
            f"constant {expr!r} does not fit in its declared {width} bit(s)"
        )
    return [_CONST_NET_NAMES[bit] for bit in format(value, f"0{width}b")]


def _range_slice_bits(expr: str, net_widths: dict[str, list[str]]) -> list[str] | None:
    """The MSB-first per-bit net list of a multi-bit range slice
    (`bus[7:0]`, `\\bus [7:0]`; issue #2657), or ``None`` when ``expr`` is
    not one.

    The base must be a declared bus and the slice must run in its declared
    direction over declared bits: Verilog itself rejects a reversed
    part-select, and an undeclared base has no width to check against, so
    either is an error rather than a guess at which bit is the MSB."""
    match = _RANGE_SLICE_RE.match(expr) or _ESCAPED_RANGE_RE.match(expr)
    if match is None:
        return None
    base = _unescape(match.group("base"))
    bits = _expand_range(base, int(match.group("msb")), int(match.group("lsb")))
    declared = net_widths.get(base)
    if declared is None or declared == [base]:
        raise VerilogNetlistError(
            f"range slice {expr!r} selects from {base!r}, which has no "
            "'wire'/'input'/'output'/'inout' bus declaration in this module"
        )
    positions = [declared.index(bit) if bit in declared else -1 for bit in bits]
    if -1 in positions or positions != list(
        range(positions[0], positions[0] + len(positions))
    ):
        raise VerilogNetlistError(
            f"range slice {expr!r} does not select declared bits of {base!r} "
            f"in their declared direction ({declared[0]} .. {declared[-1]})"
        )
    return bits


def _split_concatenation(expr: str) -> list[str] | None:
    """The top-level operands of ``expr`` when it is exactly one
    brace-delimited concatenation (`{a, b}`), else ``None`` -- so
    `{a} & {b}` is not mistaken for one."""
    if not expr.startswith("{"):
        return None
    depth = 0
    for position, char in enumerate(expr):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                if position != len(expr) - 1:
                    return None
                return _split_top_level(expr[1:-1])
    return None


def _is_bit_select(expr: str) -> bool:
    """``True`` for a single-index bit-select -- `bus[3]`, or `\\bus [3]` on
    an escaped base. An escaped name with no whitespace before its `[`
    (`\\a.b[3]`) is one atomic name, not a select (issue #1371)."""
    if expr.startswith("\\"):
        match = _ESCAPED_IDENT_RE.match(expr)
        rest = expr[match.end() :].strip() if match else ""
        return bool(_INDEX_SUFFIX_RE.match(rest))
    return bool(_BIT_SELECT_RE.match(expr))


def _concat_connection_operand_bits(
    operand: str, net_widths: dict[str, list[str]]
) -> list[str]:
    """One operand of an instance-connection concatenation (issue #2657) to
    its MSB-first per-bit net list: a declared net (its full declared
    width), a single-index bit-select, a range slice, or a sized constant.

    Wider than an `assign` concatenation operand
    (:func:`_concat_operand_bits`) on purpose: Yosys/OpenROAD routinely
    write a bus-pin connection as `{ 24'h000000, data[7:0] }`, so a
    part-select and a sized constant are exactly what this position needs.
    Replication and a nested concatenation stay rejected."""
    if "{" in operand or "}" in operand:
        raise VerilogNetlistError(
            f"concatenation operand {operand!r} is a replication or nested "
            "concatenation, which is not supported"
        )
    constant = _sized_constant_bits(operand)
    if constant is not None:
        return constant
    if "'" in operand:
        raise VerilogNetlistError(
            f"concatenation operand {operand!r} is an unsized constant -- its "
            "width (and so every later operand's bit position) is ambiguous"
        )
    sliced = _range_slice_bits(operand, net_widths)
    if sliced is not None:
        return sliced
    net = _parse_connection_expr(operand)
    if net in net_widths:
        return list(net_widths[net])
    if _is_bit_select(operand):
        # A single-index bit-select (`bus[3]`, `\bus [3]`): one bit by
        # construction, whatever the base's declared width.
        return [net]
    raise VerilogNetlistError(
        f"concatenation operand {operand!r} has no 'wire'/'input'/'output'/"
        "'inout' declaration in this module, so its bit width is unknown"
    )


def _resolve_connection(expr: str, net_widths: dict[str, list[str]]) -> str | list[str]:
    """Resolve one `.PORT(<expr>)` instance connection to a single net
    name, or -- for a multi-bit connection (issue #2657) -- its MSB-first
    per-bit net list: a concatenation, a range slice, a sized constant wider
    than one bit, or a plain name of a declared bus. Everything else falls
    through to :func:`_parse_connection_expr`'s scalar grammar unchanged."""
    operands = _split_concatenation(expr)
    if operands is not None:
        if not operands:
            raise VerilogNetlistError(f"concatenation {expr!r} is empty")
        bits: list[str] = []
        for operand in operands:
            bits.extend(_concat_connection_operand_bits(operand, net_widths))
        return bits
    constant = _sized_constant_bits(expr)
    if constant is not None:
        return constant[0] if len(constant) == 1 else constant
    sliced = _range_slice_bits(expr, net_widths)
    if sliced is not None:
        return sliced
    net = _parse_connection_expr(expr)
    declared = net_widths.get(net)
    if declared is not None and declared != [net]:
        # A whole declared bus named plainly -- `.D(data)`, Yosys's own
        # rendering of a full-width bus connection.
        return list(declared)
    return net


def _port_aliases(ports: list[str], aliases: dict[str, str]) -> dict[str, str]:
    """``{<port>: <canonical net>}`` for every ``port`` whose value comes
    purely from a (possibly chained) ``assign`` -- issue #2021.

    Mirrors :func:`_resolve_alias`'s own chain-following (so a multi-hop
    ``assign c = b; assign b = a;`` collapses ``c``/``b`` onto ``a`` exactly
    as instance connections already do), but is applied to the module's
    *declared port list* instead: :func:`convert_gate_level_verilog` (see
    its own docstring) never resolves a header port name the way it resolves
    an instance's connection expression, so a port whose only Verilog-level
    connection is an ``assign`` is emitted as a ``.SUBCKT`` pin with nothing
    inside the body ever referencing it -- an isolated, disconnected net
    once read back by ``NetlistSpiceReader``. That is invisible here (this
    function only inspects the parsed Verilog); it is
    ``klayout_tools.lvs._apply_gate_level_port_aliases`` that turns this
    mapping into an actual fix, joining the alias port's net onto its
    canonical target's net in the real ``kdb.Netlist`` object
    ``NetlistSpiceReader`` builds, *after* this module's own text-only
    conversion.

    Only a ``port`` whose canonical target differs from itself is included
    (the common case -- most ports have no ``assign`` at all -- returns an
    empty mapping). The canonical target may itself be another declared
    port (the issue's own headline case, e.g. ``assign dbg_uart_byte[i] =
    rx_byte[i];``, two port names for one electrical node) or a purely
    internal net an instance connects to (the identical mechanical bug, one
    layer simpler: a port renamed via ``assign`` rather than two ports tied
    together) -- both are handled identically, since the fix's job either
    way is "make this port's net the same object as whatever it was really
    wired to."
    """
    result: dict[str, str] = {}
    for port in ports:
        canonical = _resolve_alias(port, aliases)
        if canonical != port:
            result[port] = canonical
    return result


def _parse_module_chunk(chunk: str) -> _Module:
    statements = _split_top_level(chunk, seps=";")
    if not statements:
        raise VerilogNetlistError("empty module body (no 'module' declaration found)")
    name, header_ports = _parse_header(statements[0])

    port_widths: dict[str, list[str]] = {}
    wire_widths: dict[str, list[str]] = {}
    instances: list[_Instance] = []
    aliases: dict[str, str] = {}
    assign_statements: list[str] = []

    for statement in statements[1:]:
        stripped = statement.strip()
        if not stripped:
            continue
        first_word = re.match(r"^\S+", stripped)
        keyword = first_word.group(0) if first_word else ""
        if keyword in _DIRECTION_KEYWORDS:
            _parse_direction_statement(stripped, port_widths)
        elif keyword in _IGNORED_DECL_KEYWORDS:
            _record_net_declaration_widths(stripped, wire_widths)
        elif keyword == "assign":
            # Deferred until every declaration in the module is seen, so a
            # concatenation right-hand side (issue #2372) can be expanded
            # bit-by-bit against the operands' declared widths regardless
            # of statement order.
            assign_statements.append(stripped)
        else:
            instances.append(_parse_instance_statement(stripped))

    net_widths = {**wire_widths, **port_widths}
    for instance in instances:
        _resolve_instance_connections(instance, net_widths)
    for stripped in assign_statements:
        _parse_assign_statement(stripped, aliases, net_widths)

    ports: list[str] = []
    for port in header_ports:
        ports.extend(port_widths.get(port, [port]))

    return _Module(name=name, ports=ports, instances=instances, aliases=aliases)


_ASSIGN_RE = re.compile(r"^assign\s+(?P<lhs>\S+)\s*=\s*(?P<rhs>\S+)$")

#: An ``assign`` whose right-hand side is a brace-delimited expression --
#: routed to :func:`_expand_concat_assign` (issue #2372) instead of the
#: plain-alias path. The left-hand side is re-validated there.
_CONCAT_ASSIGN_RE = re.compile(
    r"^assign\s+(?P<lhs>[^=]+?)\s*=\s*(?P<rhs>\{.*\})$", re.S
)

#: A Verilog declaration of internal nets (``wire``/``reg``/...), with an
#: optional ``signed`` qualifier and ``[msb:lsb]`` range -- read only to
#: learn each net's bit list for concatenation expansion (issue #2372).
_NET_DECL_RE = re.compile(
    r"^(?:" + "|".join(_IGNORED_DECL_KEYWORDS) + r")\b\s*"
    r"(?:signed\b\s*)?"
    r"(?:\[\s*(?P<msb>-?\d+)\s*:\s*(?P<lsb>-?\d+)\s*\]\s*)?"
    r"(?P<names>.*)$",
    re.S,
)


def _record_net_declaration_widths(
    statement: str, net_widths: dict[str, list[str]]
) -> None:
    """Record the bit list of every net a ``wire``/``reg``/... declaration
    names (issue #2372). Deliberately permissive -- these declarations were
    ignored outright before, and still carry no SPICE-level meaning -- so an
    entry this does not understand is simply skipped rather than raised on;
    a concatenation that later needs an unrecorded net's width fails loudly
    in :func:`_expand_concat_assign` instead."""
    match = _NET_DECL_RE.match(statement.strip())
    if match is None:
        return
    for raw in _split_top_level(match.group("names")):
        if not _IDENT_RE.fullmatch(raw) and not _ESCAPED_IDENT_RE.fullmatch(raw):
            continue
        name = _unescape(raw)
        if match.group("msb") is not None:
            net_widths[name] = _expand_range(
                name, int(match.group("msb")), int(match.group("lsb"))
            )
        else:
            net_widths.setdefault(name, [name])


def _unsupported_assign(statement: str, reason: str = "") -> VerilogNetlistError:
    message = (
        "only a plain 'assign <net> = <net>;' alias or a plain concatenation "
        "'assign <net> = { <net>, ... };' is supported, found: "
        f"{statement.strip()[:80]!r}"
    )
    if reason:
        message += f" -- {reason}"
    return VerilogNetlistError(message)


def _concat_operand_bits(
    statement: str, operand: str, net_widths: dict[str, list[str]]
) -> list[str]:
    """Resolve one side of a concatenation assign (the LHS, or one RHS
    operand) to its MSB-first list of single-bit net names."""
    operand = operand.strip()
    if "{" in operand or "}" in operand:
        raise _unsupported_assign(
            statement,
            f"operand {operand!r} is a replication or nested concatenation, "
            "which is not supported",
        )
    if "'" in operand or re.fullmatch(r"\d+", operand):
        raise _unsupported_assign(
            statement,
            f"operand {operand!r} is a literal constant, which is not "
            "supported inside a concatenation",
        )
    if _RANGE_SELECT_RE.match(operand):
        raise _unsupported_assign(
            statement,
            f"operand {operand!r} is a multi-bit part-select, which is not "
            "supported inside a concatenation",
        )
    try:
        net = _parse_connection_expr(operand)
    except VerilogNetlistError as exc:
        raise _unsupported_assign(statement, str(exc)) from exc
    if net in net_widths:
        return list(net_widths[net])
    if net.endswith("]"):
        # A single-index bit-select (`bus[3]`, `\bus [3]`): one bit by
        # construction, whatever the base's declared width.
        return [net]
    raise _unsupported_assign(
        statement,
        f"operand {operand!r} has no 'wire'/'input'/'output'/'inout' "
        "declaration in this module, so its bit width is unknown",
    )


def _expand_concat_assign(
    statement: str,
    lhs: str,
    rhs: str,
    aliases: dict[str, str],
    net_widths: dict[str, list[str]],
) -> None:
    """Expand ``assign <lhs> = { <op>, <op>, ... };`` into one per-bit
    alias ``<lhs bit> -> <operand bit>`` (issue #2372) -- Yosys's routine
    rendering of a bit-for-bit vector alias or tie-off padding.

    Verilog concatenation is MSB-first: the first operand supplies the
    most-significant bits. Both sides are expanded to MSB-first bit lists
    from the module's own declarations and zipped. Operands are restricted
    to plain nets and single-index bit-selects; replication (``{N{x}}``),
    literals (``1'b0``) and part-selects (``a[7:4]``) are rejected with the
    same "only a plain ... is supported" error as any other unsupported
    ``assign``, as is a width mismatch between the two sides (Verilog would
    silently zero-extend or truncate; a sign-off converter must not).
    """
    if "{" in lhs or "}" in lhs:
        raise _unsupported_assign(
            statement,
            "a concatenation on the left-hand side is not supported",
        )
    inner = rhs.strip()[1:-1]
    operands = _split_top_level(inner)
    if not operands:
        raise _unsupported_assign(statement, "the concatenation is empty")
    lhs_bits = _concat_operand_bits(statement, lhs, net_widths)
    rhs_bits: list[str] = []
    for operand in operands:
        rhs_bits.extend(_concat_operand_bits(statement, operand, net_widths))
    if len(lhs_bits) != len(rhs_bits):
        raise _unsupported_assign(
            statement,
            f"left-hand side is {len(lhs_bits)} bit(s) wide but the "
            f"concatenation is {len(rhs_bits)} bit(s) wide",
        )
    for lhs_bit, rhs_bit in zip(lhs_bits, rhs_bits, strict=True):
        aliases[lhs_bit] = rhs_bit


def _parse_assign_statement(
    statement: str,
    aliases: dict[str, str],
    net_widths: dict[str, list[str]] | None = None,
) -> None:
    concat = _CONCAT_ASSIGN_RE.match(statement.strip())
    if concat is not None:
        _expand_concat_assign(
            statement,
            concat.group("lhs"),
            concat.group("rhs"),
            aliases,
            net_widths or {},
        )
        return
    match = _ASSIGN_RE.match(statement.strip())
    if match is None:
        raise _unsupported_assign(statement)
    lhs = _parse_connection_expr(match.group("lhs"))
    rhs = _parse_connection_expr(match.group("rhs"))
    lhs_bits = (net_widths or {}).get(lhs, [lhs])
    rhs_bits = (net_widths or {}).get(rhs, [rhs])
    if lhs_bits == [lhs] and rhs_bits == [rhs]:
        aliases[lhs] = rhs
        return
    # A whole-bus alias (`assign dout = _05_;`, Yosys's routine rendering of
    # a bus output): alias bit for bit, so a per-bit consumer -- a
    # bit-expanded port, or a whole-bus macro connection (issue #2657) --
    # resolves through it.
    if len(lhs_bits) != len(rhs_bits):
        raise _unsupported_assign(
            statement,
            f"left-hand side is {len(lhs_bits)} bit(s) wide but the "
            f"right-hand side is {len(rhs_bits)} bit(s) wide",
        )
    for lhs_bit, rhs_bit in zip(lhs_bits, rhs_bits, strict=True):
        aliases[lhs_bit] = rhs_bit


def _resolve_alias(net: str, aliases: dict[str, str]) -> str:
    seen: set[str] = set()
    while net in aliases and net not in seen:
        seen.add(net)
        net = aliases[net]
    return net


def parse_gate_level_verilog(text: str) -> list[dict[str, object]]:
    """Parse gate-level Verilog ``text`` into a list of module descriptions,
    one per ``module``/``endmodule`` block, in file order.

    Each entry is ``{"name": str, "ports": list[str], "instances":
    list[{"cell": str, "name": str, "connections": dict[str, str |
    list[str]]}], "port_aliases": dict[str, str]}`` -- plain
    JSON-serialisable primitives, mirroring every other pure-library
    function in this repo. ``ports`` is already bit-expanded (a ``[15:0]``
    bus port becomes 16 individual entries); ``connections`` values are
    already alias-resolved (a preceding ``assign`` is transparent to every
    consumer of this data). A ``connections`` value is one net name for a
    scalar connection, or -- for a multi-bit connection onto a vector port
    (a concatenation, a range slice, a sized constant wider than one bit, or
    a whole declared bus; issue #2657) -- the MSB-first list of per-bit net
    names, bound to the port's real per-bit pins only later, by
    :func:`convert_gate_level_verilog`. A plain ``assign`` between two
    declared buses aliases bit for bit, so ``port_aliases`` keys are then
    the bit-expanded port names.
    ``port_aliases`` (issue #2021, see :func:`_port_aliases`) is
    ``{<port>: <canonical net>}`` for every declared port whose only
    Verilog-level connection is an ``assign`` -- empty for every module that
    has none, which is most of them.

    Raises :class:`VerilogNetlistError` for anything this narrow grammar
    does not model -- see the module docstring's "Deliberately narrow"
    section for the exact supported subset.
    """
    cleaned = _strip_comments(text)
    chunks = _ENDMODULE_RE.split(cleaned)
    trailing = chunks.pop()
    if trailing.strip():
        raise VerilogNetlistError(
            f"unexpected content after the last 'endmodule': {trailing.strip()[:80]!r}"
        )
    if not chunks:
        raise VerilogNetlistError("no 'module' ... 'endmodule' block found")

    modules: list[dict[str, object]] = []
    for chunk in chunks:
        module = _parse_module_chunk(chunk)
        instances = [
            {
                "cell": inst.cell,
                "name": inst.name,
                "connections": {
                    port: (
                        [_resolve_alias(bit, module.aliases) for bit in net]
                        if isinstance(net, list)
                        else _resolve_alias(net, module.aliases)
                    )
                    for port, net in inst.connections.items()
                },
            }
            for inst in module.instances
        ]
        modules.append(
            {
                "name": module.name,
                "ports": list(module.ports),
                "instances": instances,
                "port_aliases": _port_aliases(module.ports, module.aliases),
            }
        )
    return modules


def collect_gate_level_port_aliases(text: str) -> dict[str, dict[str, str]]:
    """``{<module name>: {<port>: <canonical net>}}`` for every module in
    gate-level Verilog ``text`` that declares at least one port whose only
    Verilog-level connection is an ``assign`` (issue #2021) -- a module with
    none is omitted entirely, so the common case (no port aliasing at all)
    returns ``{}``.

    A thin wrapper around :func:`parse_gate_level_verilog`'s own
    ``port_aliases`` field, re-parsing the same ``text``
    :func:`convert_gate_level_verilog` already parsed once for the SPICE
    conversion -- kept as a separate entry point (rather than folded into
    that function's return value) so the SPICE-text conversion's own return
    type/signature stays exactly what every existing caller/test already
    depends on. The re-parse is cheap (a `klt place-and-route` reference is
    a single flat module, never large enough for this to matter) and pure
    (no side effects, safe to call any number of times against the same
    text).

    ``klayout_tools.lvs._read_reference_netlist`` calls this once, right
    after its own (already-successful) :func:`convert_gate_level_verilog`
    call, and hands the result to ``run_lvs`` as an *output* parameter
    (mirroring ``placeholder_value_classes``'s own pattern), which
    ``klayout_tools.lvs._apply_gate_level_port_aliases`` then applies to the
    real ``kdb.Netlist`` object built from that SPICE text -- the module
    docstring's "Deliberately narrow, deliberately loud" section explains why
    the fix cannot live in the SPICE text itself.
    """
    modules = parse_gate_level_verilog(text)
    return {
        module["name"]: dict(module["port_aliases"])  # type: ignore[arg-type]
        for module in modules
        if module["port_aliases"]
    }


#: Matches a `.subckt`/`.SUBCKT` header line (SPICE directives are
#: case-insensitive), capturing the cell name and its declared pin list --
#: used by :func:`parse_subckt_pin_orders` to read a real PDK library file's
#: own pin order, never a second, hardcoded convention.
_SUBCKT_HEADER_RE = re.compile(r"^\.subckt\s+(\S+)\s+(.*)$", re.IGNORECASE)


def parse_subckt_pin_orders(text: str) -> dict[str, list[str]]:
    """Parse every ``.subckt <name> <pin> ...`` header in a PDK library's
    ``.spice``/``.cdl`` file ``text`` into ``{<name>: [<pin>, ...]}``, in
    the file's own declared order.

    Ignores everything else in the file (the device-level body of each
    subcircuit, comments, ``.model`` cards) -- this reads pin order only,
    never a full SPICE parse (the file can be tens of thousands of lines for
    a full standard-cell library; a full parse would also require a real
    ``.model``/device library this module has no use for).

    A SPICE `+` continuation line is honored (a `.subckt` header long enough
    to wrap is real, e.g. a large hard macro's pin list), via the same
    join-continuations convention :mod:`klayout_tools.netlist_normalize`
    already uses.
    """
    from .netlist_normalize import _merge_continuations

    pin_orders: dict[str, list[str]] = {}
    for line in _merge_continuations(text.splitlines()):
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        match = _SUBCKT_HEADER_RE.match(stripped)
        if match is None:
            continue
        cell_name = match.group(1)
        pins = match.group(2).split()
        pin_orders[cell_name] = pins
    return pin_orders


def convert_gate_level_verilog(
    text: str,
    *,
    pin_order_lookup,
) -> str:
    """Convert gate-level Verilog ``text`` to plain-element-shaped SPICE
    ``klt lvs`` can read directly via ``NetlistSpiceReader`` (issue #1336).

    ``pin_order_lookup(cell_type: str) -> list[str] | None`` resolves a
    library cell type's real pin order (see :func:`parse_subckt_pin_orders`
    -- the caller in ``klt lvs`` builds this from the resolved PDK's own
    library file); ``None`` means the cell type is not a resolvable library
    cell.

    Every parsed ``module`` becomes a ``.SUBCKT <name> <ports...> .ENDS``
    block whose instances call either another parsed module (a genuine
    hierarchical reference -- resolved as such automatically, never
    confused with a library cell: a module wins over a lookup hit of the
    same name) or a library cell, resolved via ``pin_order_lookup`` and
    written as its own pin-only stub the first time it is encountered (see
    the module docstring's "Shape of the conversion" and "No power/ground
    pins carried" sections for exactly which pins that stub declares and
    why). A cell type that resolves as neither is :class:`VerilogNetlistError`,
    naming the instance and cell type -- never a silent skip.

    A multi-bit connection (issue #2657) is bound here, where the real pin
    names are first known, by :func:`_bind_connections`: a library cell's
    `<PORT>[<index>]` pins in descending numeric index order, a parsed
    sub-module's bit-expanded ports in their declared order.
    """
    modules = parse_gate_level_verilog(text)
    real_orders = _library_pin_orders(modules, pin_order_lookup)
    bound, stub_pin_order = _bind_all_instances(modules, real_orders)

    out: list[str] = []
    for module, module_bound in zip(modules, bound, strict=True):
        out.append(f".SUBCKT {module['name']} {' '.join(module['ports'])}".rstrip())
        for instance, connections in zip(
            module["instances"], module_bound, strict=True
        ):
            cell = instance["cell"]
            pins = (
                stub_pin_order[cell]
                if cell in stub_pin_order
                else _sub_module_by_name(modules, cell)["ports"]
            )
            nets = [
                connections.get(pin, f"__NC_{instance['name']}_{pin}__") for pin in pins
            ]
            out.append(f"X{_sanitize(instance['name'])} {' '.join(nets)} {cell}")
        out.append(f".ENDS {module['name']}")

    for cell, pins in stub_pin_order.items():
        out.append(f".SUBCKT {cell} {' '.join(pins)}")
        out.append(f".ENDS {cell}")

    return "\n".join(out) + "\n"


def _library_pin_orders(
    modules: list[dict[str, object]], pin_order_lookup
) -> dict[str, list[str]]:
    """``{<library cell>: <real PDK pin order>}`` for every instantiated cell
    type that is not a call to another parsed module, in encounter order --
    all resolved before any connection is bound, since a multi-bit
    connection (issue #2657) can only be bound against real pin names."""
    module_names = {module["name"] for module in modules}
    real_orders: dict[str, list[str]] = {}
    for module in modules:
        for instance in module["instances"]:  # type: ignore[attr-defined]
            cell = instance["cell"]
            if cell in module_names or cell in real_orders:
                continue
            real_order = pin_order_lookup(cell)
            if real_order is None:
                raise VerilogNetlistError(
                    f"library cell '{cell}' has no resolvable pin order (no "
                    "matching '.subckt' declaration in the resolved PDK "
                    "library) -- pass the correct 'reference.library' (and "
                    "'reference.pdk'/'reference.pdk_root' if needed), or "
                    "confirm this cell type ships in that library"
                )
            real_orders[cell] = list(real_order)
    return real_orders


def _bind_all_instances(
    modules: list[dict[str, object]], real_orders: dict[str, list[str]]
) -> tuple[list[list[dict[str, str]]], dict[str, list[str]]]:
    """``(bound, stub_pin_order)``: every instance's ``{<pin>: <net>}``
    binding (:func:`_bind_connections`), per module, in instance order; and
    each library cell's black-box stub pin list -- the union of the pins
    actually connected across every instance of that cell type, in the
    real PDK declared order. A connection to a pin the library cell does
    not declare is an error naming every such pin across the cell type."""
    bound: list[list[dict[str, str]]] = []
    used_pins: dict[str, set[str]] = {cell: set() for cell in real_orders}
    unknown_pins: dict[str, set[str]] = {cell: set() for cell in real_orders}
    for module in modules:
        module_bound: list[dict[str, str]] = []
        for instance in module["instances"]:  # type: ignore[attr-defined]
            cell = instance["cell"]
            library = cell in real_orders
            pins = (
                real_orders[cell]
                if library
                else _sub_module_by_name(modules, cell)["ports"]
            )
            pin_nets, unknown = _bind_connections(instance, pins, library=library)  # type: ignore[arg-type]
            if library:
                used_pins[cell].update(pin_nets)
                unknown_pins[cell].update(unknown)
            module_bound.append(pin_nets)
        bound.append(module_bound)

    stub_pin_order: dict[str, list[str]] = {}
    for cell, real_order in real_orders.items():
        if unknown_pins[cell]:
            raise VerilogNetlistError(
                f"library cell '{cell}' is instantiated with connection(s) to "
                f"pin(s) {sorted(unknown_pins[cell])!r} that are not in its "
                f"resolved PDK pin list {real_order!r}"
            )
        stub_pin_order[cell] = [pin for pin in real_order if pin in used_pins[cell]]
    return bound, stub_pin_order


def _port_bit_pins(port: str, pins: list[str], *, library: bool) -> list[str]:
    """``port``'s per-bit pins among ``pins`` (every `<port>[<index>]`
    entry), MSB-first (issue #2657).

    A parsed sub-module's ports were bit-expanded by :func:`_expand_range`
    from its own `[msb:lsb]` declaration, so their declared order already is
    MSB-first, ascending range included. A library cell's pins come from a
    `.subckt`/`.cdl` header, whose bit names carry the Verilog index but
    whose header position carries nothing Verilog defines, so they are
    sorted by that index, highest first: the `[N-1:0]` convention every real
    vector-ported macro checked uses (see the module docstring)."""
    pattern = re.compile(rf"^{re.escape(port)}\[(-?\d+)\]$")
    hits = [
        (int(match.group(1)), pin)
        for pin in pins
        if (match := pattern.match(pin)) is not None
    ]
    if library:
        hits.sort(key=lambda hit: hit[0], reverse=True)
    return [pin for _, pin in hits]


def _bind_connections(
    instance: dict[str, object], pins: list[str], *, library: bool
) -> tuple[dict[str, str], set[str]]:
    """``({<real pin>: <net>}, {<unknown port>, ...})`` for one instance
    calling a cell/module with declared ``pins``.

    A scalar connection binds to its pin by name, as it always has. A
    multi-bit one (issue #2657) is zipped MSB-first onto the port's per-bit
    pins (:func:`_port_bit_pins`) -- or onto a scalar pin of that name, when
    one bit wide. A bit-count mismatch either way is an error naming the
    instance, port and both widths, never a silent truncate or pad. A port
    with no pin at all is returned as unknown for the caller to report (a
    library cell's error aggregates every such port across every instance
    of the cell type).
    """
    pin_set = set(pins)
    pin_nets: dict[str, str] = {}
    unknown: set[str] = set()
    connections: dict[str, str | list[str]] = instance["connections"]  # type: ignore[assignment]
    for port, net in connections.items():
        if isinstance(net, str) and port in pin_set:
            bit_pins = [port]
        else:
            bit_pins = _port_bit_pins(port, pins, library=library)
            if not bit_pins and port in pin_set:
                bit_pins = [port]
        if not bit_pins:
            unknown.add(port)
            continue
        bits = net if isinstance(net, list) else [net]
        if len(bits) != len(bit_pins):
            raise VerilogNetlistError(
                f"instance '{instance['name']}' ('{instance['cell']}') port "
                f"'{port}' is {len(bit_pins)} bit(s) wide ({bit_pins[0]} .. "
                f"{bit_pins[-1]}) but its connection is {len(bits)} bit(s) wide"
            )
        for pin, bit in zip(bit_pins, bits, strict=True):
            if pin in pin_nets:
                raise VerilogNetlistError(
                    f"instance '{instance['name']}' ('{instance['cell']}') "
                    f"connects pin '{pin}' twice"
                )
            pin_nets[pin] = bit
    return pin_nets, unknown


def _sub_module_by_name(
    modules: list[dict[str, object]], name: str
) -> dict[str, object]:
    for module in modules:
        if module["name"] == name:
            return module
    raise VerilogNetlistError(f"module '{name}' is not defined")  # pragma: no cover


#: Mirrors `klayout_tools.extract_abstract._sanitize_instance_name`'s
#: convention -- a device/subcircuit instance name is a cosmetic handle, so
#: mapping every character outside `[A-Za-z0-9_]` to `_` (e.g. a Verilog
#: instance name inherited from RTL hierarchy, `mem/inst_0`) keeps the
#: written SPICE well-formed without inventing a second sanitisation rule.
_INSTANCE_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_]")


def _sanitize(name: str) -> str:
    return _INSTANCE_NAME_UNSAFE_RE.sub("_", name)
