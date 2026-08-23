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
whole netlist, in the PDK's own declared relative order. A caller comparing
against a layout-side abstraction that *does* carry power pins (e.g. an
in-cell-label or LEF-macro abstraction with `VPWR`/`VGND` pins) will see
those flagged as `pin.unmatched` on the reference side -- a real, disclosed
limitation of Verilog-derived references, not a defect in this converter
(see `docs/cli/lvs.md`).

**Deliberately narrow, deliberately loud** (mirrors
`klayout_tools.netlist_normalize`'s own discipline for the sibling
subckt-call conversion): only the structurally simple constructs a
gate-level, already-flattened netlist actually needs are supported --
`module`/`endmodule`, `input`/`output`/`inout` port declarations (with an
optional `[msb:lsb]` bus range), plain instance calls with **named**
(`.PORT(NET)`) connections, a bare identifier or single-index bit-select
(`net`/`bus[3]`) or `1'b0`/`1'b1` constant as a connection expression, and a
simple `assign <net> = <net>;` alias. Anything else -- positional instance
connections, concatenation, a multi-bit range slice, a non-constant
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
_IDENT_RE = re.compile(r"\\?[A-Za-z_$][A-Za-z0-9_$]*")

#: A connection expression this module accepts as a plain net reference:
#: an identifier, optionally followed by a single-index bit-select
#: (`bus[3]`) -- never a range slice (`bus[7:0]`), which is rejected
#: explicitly (see :func:`_parse_connection_expr`).
_BIT_SELECT_RE = re.compile(
    r"^(?P<base>\\?[A-Za-z_$][A-Za-z0-9_$]*)\[(?P<index>[^\]:]+)\]$"
)
_RANGE_SELECT_RE = re.compile(r"^\\?[A-Za-z_$][A-Za-z0-9_$]*\[[^\]]*:[^\]]*\]$")

#: A `<width>'b<bits>` constant, e.g. `1'b0`/`1'b1` -- the only constant
#: shape this module recognises (see the module docstring's "no power/
#: ground" note: constants are otherwise vanishingly rare in a routed,
#: technology-mapped netlist, since tie cells normally carry constants
#: instead).
_CONST_RE = re.compile(r"^\d*'[bB]([01])$")

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
    module does not model, a range-slice connection), a malformed
    declaration, or a library cell instantiated with no resolvable pin
    order. Always names the offending construct/instance -- never a silent
    best-effort guess that could degrade a sign-off comparison invisibly.
    """


@dataclass
class _Instance:
    cell: str
    name: str
    #: `{<port name>: <net name>}`, in encounter order. A `.PORT()` empty
    #: connection or a missing (never mentioned) pin is not recorded here --
    #: :func:`_convert_module` synthesizes a fresh disconnected net for it at
    #: write time, once the stub's full declared pin list is known.
    connections: dict[str, str] = field(default_factory=dict)


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


def _parse_connection_expr(expr: str, aliases: dict[str, str] | None = None) -> str:
    """Resolve one `.PORT(<expr>)` connection expression to a net name, or
    raise :class:`VerilogNetlistError` for anything this narrow grammar does
    not model (a range slice, concatenation, a non-constant expression)."""
    expr = expr.strip()
    const_match = _CONST_RE.match(expr)
    if const_match:
        return _CONST_NET_NAMES[const_match.group(1)]
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
    connections: dict[str, str] = {}
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
            connections[_unescape(port_name)] = _parse_connection_expr(expr)
    return _Instance(cell=cell, name=inst_name, connections=connections)


def _parse_module_chunk(chunk: str) -> _Module:
    statements = _split_top_level(chunk, seps=";")
    if not statements:
        raise VerilogNetlistError("empty module body (no 'module' declaration found)")
    name, header_ports = _parse_header(statements[0])

    port_widths: dict[str, list[str]] = {}
    instances: list[_Instance] = []
    aliases: dict[str, str] = {}

    for statement in statements[1:]:
        stripped = statement.strip()
        if not stripped:
            continue
        first_word = re.match(r"^\S+", stripped)
        keyword = first_word.group(0) if first_word else ""
        if keyword in _DIRECTION_KEYWORDS:
            _parse_direction_statement(stripped, port_widths)
        elif keyword in _IGNORED_DECL_KEYWORDS:
            continue
        elif keyword == "assign":
            _parse_assign_statement(stripped, aliases)
        else:
            instances.append(_parse_instance_statement(stripped))

    ports: list[str] = []
    for port in header_ports:
        ports.extend(port_widths.get(port, [port]))

    return _Module(name=name, ports=ports, instances=instances, aliases=aliases)


_ASSIGN_RE = re.compile(r"^assign\s+(?P<lhs>\S+)\s*=\s*(?P<rhs>\S+)$")


def _parse_assign_statement(statement: str, aliases: dict[str, str]) -> None:
    match = _ASSIGN_RE.match(statement.strip())
    if match is None:
        raise VerilogNetlistError(
            f"only a plain 'assign <net> = <net>;' alias is supported, found: "
            f"{statement.strip()[:80]!r}"
        )
    lhs = _parse_connection_expr(match.group("lhs"))
    rhs = _parse_connection_expr(match.group("rhs"))
    aliases[lhs] = rhs


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
    list[{"cell": str, "name": str, "connections": dict[str, str]}]}`` --
    plain JSON-serialisable primitives, mirroring every other pure-library
    function in this repo. ``ports`` is already bit-expanded (a ``[15:0]``
    bus port becomes 16 individual entries); ``connections`` values are
    already alias-resolved (a preceding ``assign`` is transparent to every
    consumer of this data).

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
                    port: _resolve_alias(net, module.aliases)
                    for port, net in inst.connections.items()
                },
            }
            for inst in module.instances
        ]
        modules.append(
            {"name": module.name, "ports": list(module.ports), "instances": instances}
        )
    return modules


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
    """
    modules = parse_gate_level_verilog(text)
    module_names = {module["name"] for module in modules}

    # First pass: for every library-cell instance (i.e. not a call to
    # another parsed module), record every pin actually connected across
    # every instance of that cell type -- the union that becomes the
    # black-box stub's own declared (signal-only) pin list, ordered per the
    # real PDK declaration.
    used_pins: dict[str, set[str]] = {}
    for module in modules:
        for instance in module["instances"]:
            cell = instance["cell"]
            if cell in module_names:
                continue
            used_pins.setdefault(cell, set()).update(instance["connections"])

    stub_pin_order: dict[str, list[str]] = {}
    for cell, connected in used_pins.items():
        real_order = pin_order_lookup(cell)
        if real_order is None:
            raise VerilogNetlistError(
                f"library cell '{cell}' has no resolvable pin order (no "
                "matching '.subckt' declaration in the resolved PDK "
                "library) -- pass the correct 'reference.library' (and "
                "'reference.pdk'/'reference.pdk_root' if needed), or "
                "confirm this cell type ships in that library"
            )
        unknown = connected - set(real_order)
        if unknown:
            raise VerilogNetlistError(
                f"library cell '{cell}' is instantiated with connection(s) to "
                f"pin(s) {sorted(unknown)!r} that are not in its resolved PDK "
                f"pin list {real_order!r}"
            )
        stub_pin_order[cell] = [pin for pin in real_order if pin in connected]

    out: list[str] = []
    for module in modules:
        out.append(f".SUBCKT {module['name']} {' '.join(module['ports'])}".rstrip())
        for instance in module["instances"]:
            cell = instance["cell"]
            connections = instance["connections"]
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
